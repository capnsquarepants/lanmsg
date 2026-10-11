import argparse
import asyncio
import hashlib
import hmac
import os
import secrets
import shutil
import sqlite3
import time
import threading
import unicodedata
from contextlib import asynccontextmanager, contextmanager
from dataclasses import replace
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.concurrency import run_in_threadpool

from .appearance import Appearance
from .reliability import AdmissionMiddleware, Hub, Limits, RateLimiter
from .settings import AppSettings, LIMIT_FIELDS

STATIC = Path(__file__).parent / "static"


class Credentials(BaseModel):
    username: str = Field(min_length=2, max_length=32, pattern=r"^[a-zA-Z0-9_-]+$")
    password: str = Field(min_length=10, max_length=128)
    invite: str = ""


class Message(BaseModel):
    content: str = Field(min_length=1, max_length=4000)
    nonce: str = Field(min_length=1, max_length=64)


class Channel(BaseModel):
    name: str = Field(min_length=1, max_length=32, pattern=r"^[a-z0-9_-]+$")


class ProfileUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    display_name: str = Field(min_length=1, max_length=32)

    @field_validator("display_name")
    @classmethod
    def clean_display_name(cls, value):
        value = unicodedata.normalize("NFC", value).strip()
        if not value or any(unicodedata.category(char).startswith("C") for char in value):
            raise ValueError("Use a visible display name without control characters")
        return value


def create_app(database=None, limits=None):
    limits = limits or Limits()
    path = Path(database or os.environ.get("LANMSG_DB", "data/lanmsg.db"))
    path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def db():
        conn = sqlite3.connect(path, timeout=1)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            with conn:
                yield conn
        except sqlite3.OperationalError as error:
            if "full" in str(error).lower():
                raise HTTPException(507, "Server storage is full") from error
            raise HTTPException(503, "Database temporarily unavailable; retry shortly", headers={"Retry-After": "1"}) from error
        finally:
            conn.close()

    with db() as conn:
        conn.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, username TEXT UNIQUE COLLATE NOCASE, salt TEXT, password TEXT, owner INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, user_id INTEGER REFERENCES users(id), expires REAL);
        CREATE TABLE IF NOT EXISTS invites(token TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY, name TEXT UNIQUE, kind TEXT DEFAULT 'text');
        CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY, channel_id INTEGER REFERENCES channels(id), user_id INTEGER REFERENCES users(id), content TEXT, nonce TEXT, created REAL, UNIQUE(user_id, nonce));
        CREATE INDEX IF NOT EXISTS messages_channel_history ON messages(channel_id,id);
        CREATE INDEX IF NOT EXISTS sessions_expiry ON sessions(expires);
        INSERT OR IGNORE INTO channels(id,name) VALUES(1,'general');
        """)
        # Upgrade existing databases without changing account IDs or message authors.
        conn.execute("BEGIN IMMEDIATE")
        if "display_name" not in {row["name"] for row in conn.execute("PRAGMA table_info(users)")}:
            conn.execute("ALTER TABLE users ADD COLUMN display_name TEXT NOT NULL DEFAULT ''")
        conn.execute("UPDATE users SET display_name=username WHERE display_name=''")
        if "expires" not in {row["name"] for row in conn.execute("PRAGMA table_info(invites)")}:
            conn.execute("ALTER TABLE invites ADD COLUMN expires REAL NOT NULL DEFAULT 0")
            conn.execute("UPDATE invites SET expires=?", (time.time() + 7 * 86400,))
        stored = conn.execute("SELECT value FROM settings WHERE key='app'").fetchone()
    saved_settings = AppSettings.model_validate_json(stored["value"]) if stored else AppSettings(**{key: getattr(limits, key) for key in LIMIT_FIELDS})
    limits = replace(limits, **{key: getattr(saved_settings, key) for key in LIMIT_FIELDS})
    settings_lock = threading.Lock()

    limiter = RateLimiter(limits.rate_keys)

    def cleanup():
        with db() as conn:
            conn.execute("DELETE FROM sessions WHERE expires<=?", (time.time(),))
            conn.execute("DELETE FROM invites WHERE expires<=?", (time.time(),))
            if limits.retention_days:
                conn.execute("DELETE FROM messages WHERE created<?", (time.time() - limits.retention_days * 86400,))

    cleanup()

    @asynccontextmanager
    async def lifespan(app):
        hub.start()
        async def maintenance():
            while True:
                await asyncio.sleep(limits.cleanup_interval)
                try:
                    await run_in_threadpool(cleanup)
                except Exception:
                    import logging
                    logging.getLogger("lanmsg").exception("Session cleanup failed")
        task = asyncio.create_task(maintenance())
        try:
            yield
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await hub.close()

    app = FastAPI(title="LANmsg", lifespan=lifespan)
    app.add_middleware(AdmissionMiddleware, limits=limits, limiter=limiter)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    def user(token):
        with db() as conn:
            row = conn.execute("SELECT users.id,username,display_name,owner FROM sessions JOIN users ON users.id=sessions.user_id WHERE token=? AND expires>?", (token or "", time.time())).fetchone()
        if not row:
            raise HTTPException(401, "Please sign in")
        return dict(row)

    def auth(request):
        # A same-origin header prevents cross-site form requests with session cookies.
        if request.method != "GET" and request.headers.get("x-lanmsg") != "1":
            raise HTTPException(403, "Invalid request origin")
        member = user(request.cookies.get("session"))
        if request.method != "GET":
            message = request.url.path.endswith("/messages")
            rate, burst = (limits.message_rate, limits.message_burst) if message else (limits.write_rate, limits.write_burst)
            if not limiter.allow(("message" if message else "write", member["id"]), rate, burst):
                raise HTTPException(429, "Too many changes; retry shortly", headers={"Retry-After": str(max(1, int(1 / rate) + 1))})
            if shutil.disk_usage(path.parent).free < limits.minimum_free_bytes:
                raise HTTPException(507, "Server is low on disk space")
        return member

    hub = Hub(limits, user)

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/status")
    def status():
        with db() as conn:
            return {"setup": conn.execute("SELECT count(*) FROM users").fetchone()[0] == 0, **saved_settings.public()}

    def owner(request):
        member = auth(request)
        if not member["owner"]:
            raise HTTPException(403, "Server owner access required")
        return member

    def settings_response():
        settings = saved_settings
        effective = {key: getattr(limits, key) for key in LIMIT_FIELDS}
        return {"settings": settings.model_dump(), "effective_limits": effective,
                "restart_required": any(getattr(settings, key) != value for key, value in effective.items())}

    @app.get("/api/admin/console")
    def console(request: Request):
        owner(request)
        with db() as conn:
            stats = {table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in ("users", "channels", "messages")}
            channels = [dict(row) for row in conn.execute("SELECT * FROM channels ORDER BY id")]
            invites = [dict(row) for row in conn.execute("SELECT token,expires FROM invites WHERE expires>? ORDER BY expires DESC", (time.time(),))]
        stats["connections"] = len(hub.peers)
        stats["free_disk_bytes"] = shutil.disk_usage(path.parent).free
        stats["database_bytes"] = path.stat().st_size
        return {**settings_response(), "stats": stats, "channels": channels, "invites": invites}

    @app.put("/api/admin/settings")
    def update_settings(body: AppSettings, request: Request):
        nonlocal saved_settings
        owner(request)
        with settings_lock:
            with db() as conn:
                conn.execute("INSERT INTO settings(key,value) VALUES('app',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (body.model_dump_json(),))
            saved_settings = body
            result = settings_response()
            hub.publish({"type": "server_settings", "settings": body.public()})
        return result

    @app.delete("/api/admin/invites/{token}")
    def revoke_invite(token: str, request: Request):
        owner(request)
        with db() as conn:
            conn.execute("DELETE FROM invites WHERE token=?", (token,))
        return {"ok": True}

    @app.get("/api/appearance")
    def appearance():
        # Design tokens are public so the sign-in screen uses the same theme.
        with db() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key='appearance'").fetchone()
        return Appearance.model_validate_json(row["value"]) if row else Appearance()

    @app.put("/api/admin/appearance")
    def save_appearance(body: Appearance, request: Request):
        if not auth(request)["owner"]:
            raise HTTPException(403)
        with db() as conn:
            conn.execute("INSERT INTO settings(key,value) VALUES('appearance',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (body.model_dump_json(),))
        result = body.model_dump()
        hub.publish({"type": "appearance", "appearance": result})
        return result

    @app.post("/api/{action}")
    def account(action: str, body: Credentials, request: Request, response: Response):
        if action not in ("register", "login"):
            raise HTTPException(404)
        if request.headers.get("x-lanmsg") != "1":
            raise HTTPException(403)
        if shutil.disk_usage(path.parent).free < limits.minimum_free_bytes:
            raise HTTPException(507, "Server is low on disk space")
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if action == "register":
                owner = conn.execute("SELECT count(*) FROM users").fetchone()[0] == 0
                if not owner and not saved_settings.registration_enabled:
                    raise HTTPException(403, "New account registration is paused")
                if not owner and not conn.execute("SELECT 1 FROM invites WHERE token=? AND expires>?", (body.invite, time.time())).fetchone():
                    raise HTTPException(403, "A valid invite is required")
                salt = secrets.token_hex(16)
                password = hashlib.scrypt(body.password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()
                try:
                    conn.execute("INSERT INTO users(username,display_name,salt,password,owner) VALUES(?,?,?,?,?)", (body.username, body.username, salt, password, owner))
                except sqlite3.IntegrityError:
                    raise HTTPException(409, "That username is taken")
                conn.execute("DELETE FROM invites WHERE token=?", (body.invite,))
            row = conn.execute("SELECT * FROM users WHERE username=?", (body.username,)).fetchone()
            if not row or not hmac.compare_digest(row["password"], hashlib.scrypt(body.password.encode(), salt=bytes.fromhex(row["salt"]), n=16384, r=8, p=1).hex()):
                raise HTTPException(401, "Incorrect username or password")
            token = secrets.token_urlsafe(32)
            conn.execute("INSERT INTO sessions VALUES(?,?,?)", (token, row["id"], time.time() + 30 * 86400))
            conn.execute("DELETE FROM sessions WHERE user_id=? AND token NOT IN (SELECT token FROM sessions WHERE user_id=? ORDER BY expires DESC LIMIT 10)", (row["id"], row["id"]))
            response.set_cookie("session", token, httponly=True, samesite="strict", secure=request.url.scheme == "https", max_age=30 * 86400)
            return {"id": row["id"], "username": row["username"], "display_name": row["display_name"], "owner": row["owner"]}

    @app.post("/api/session/logout")
    def logout(request: Request, response: Response):
        auth(request)
        with db() as conn:
            conn.execute("DELETE FROM sessions WHERE token=?", (request.cookies.get("session"),))
        hub.revoke(request.cookies.get("session"))
        response.delete_cookie("session")
        return {"ok": True}

    @app.get("/api/me")
    def me(request: Request):
        return auth(request)

    @app.put("/api/me")
    def update_profile(body: ProfileUpdate, request: Request):
        member = auth(request)
        with db() as conn:
            conn.execute("UPDATE users SET display_name=? WHERE id=?", (body.display_name, member["id"]))
        profile = {"id": member["id"], "username": member["username"], "display_name": body.display_name}
        hub.publish({"type": "profile", "profile": profile})
        return {**profile, "owner": member["owner"]}

    @app.get("/api/users/{user_id}")
    def get_profile(user_id: int, request: Request):
        auth(request)
        with db() as conn:
            row = conn.execute("SELECT id,username,display_name FROM users WHERE id=?", (user_id,)).fetchone()
        if not row:
            raise HTTPException(404, "User not found")
        return dict(row)

    @app.get("/api/channels")
    def channels(request: Request):
        auth(request)
        with db() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM channels ORDER BY id")]

    @app.post("/api/admin/channels")
    def add_channel(body: Channel, request: Request):
        if not auth(request)["owner"]:
            raise HTTPException(403)
        with db() as conn:
            try:
                cursor = conn.execute("INSERT INTO channels(name) VALUES(?)", (body.name,))
            except sqlite3.IntegrityError:
                raise HTTPException(409, "Channel already exists")
            result = {"id": cursor.lastrowid, "name": body.name, "kind": "text"}
        hub.publish({"type": "channels"})
        return result

    @app.post("/api/admin/invites")
    def invite(request: Request):
        if not auth(request)["owner"]:
            raise HTTPException(403)
        token = secrets.token_urlsafe(18)
        with db() as conn:
            conn.execute("DELETE FROM invites WHERE expires<=?", (time.time(),))
            if conn.execute("SELECT count(*) FROM invites").fetchone()[0] >= 100:
                raise HTTPException(409, "There are already 100 unused invites")
            conn.execute("INSERT INTO invites(token,expires) VALUES(?,?)", (token, time.time() + 7 * 86400))
        return {"invite": token}

    @app.get("/api/channels/{channel_id}/messages")
    def history(channel_id: int, request: Request, before: int = 9223372036854775807):
        auth(request)
        with db() as conn:
            return [dict(r) for r in conn.execute("SELECT messages.*, users.username, users.display_name FROM messages JOIN users ON users.id=user_id WHERE channel_id=? AND messages.id<? ORDER BY messages.id DESC LIMIT 50", (channel_id, before))][::-1]

    @app.post("/api/channels/{channel_id}/messages")
    def send(channel_id: int, body: Message, request: Request):
        member = auth(request)
        if not body.content.strip():
            raise HTTPException(422, "Message cannot be blank")
        with db() as conn:
            if not conn.execute("SELECT 1 FROM channels WHERE id=? AND kind='text'", (channel_id,)).fetchone():
                raise HTTPException(404, "Text channel not found")
            conn.execute("INSERT OR IGNORE INTO messages(channel_id,user_id,content,nonce,created) VALUES(?,?,?,?,?)", (channel_id, member["id"], body.content.strip(), body.nonce, time.time()))
            row = dict(conn.execute("SELECT messages.*,users.username,users.display_name FROM messages JOIN users ON users.id=user_id WHERE user_id=? AND nonce=?", (member["id"], body.nonce)).fetchone())
        hub.publish({"type": "message", "message": row})
        return row

    @app.websocket("/ws")
    async def websocket(socket: WebSocket):
        origin = socket.headers.get("origin")
        expected = f"{'https' if socket.url.scheme == 'wss' else 'http'}://{socket.headers.get('host')}"
        ip = socket.client.host if socket.client else "unknown"
        if origin != expected or hub.pending >= limits.http_concurrency:
            await socket.close(code=1008)
            return
        if not limiter.allow(("ws", ip), 2, limits.connections_per_ip):
            await socket.close(code=1013)
            return
        hub.pending += 1
        try:
            await run_in_threadpool(user, socket.cookies.get("session"))
        except HTTPException:
            await socket.close(code=1008)
            return
        finally:
            hub.pending -= 1
        if not await hub.connect(socket, socket.cookies.get("session"), ip):
            return
        last_ping = 0
        try:
            while True:
                payload = await asyncio.wait_for(socket.receive_text(), limits.idle_timeout)
                if len(payload) > 64 or time.monotonic() - last_ping < 1:
                    break
                last_ping = time.monotonic()
                await run_in_threadpool(user, socket.cookies.get("session"))
                hub.reply(socket, {"type": "pong"})
        except (WebSocketDisconnect, HTTPException, asyncio.TimeoutError):
            pass
        finally:
            hub.drop(socket)

    return app


def main():
    import uvicorn
    parser = argparse.ArgumentParser(description="LANmsg text chat server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--max-connections", type=int, default=100)
    parser.add_argument("--max-http", type=int, default=32)
    parser.add_argument("--retention-days", type=int, default=0, help="Delete older messages during cleanup; 0 keeps all history")
    args = parser.parse_args()
    if args.max_connections < 1 or args.max_http < 1 or args.retention_days < 0:
        parser.error("Connection and request limits must be positive")
    limits = Limits(connections=args.max_connections, http_concurrency=args.max_http, retention_days=args.retention_days)
    uvicorn.run(create_app(limits=limits), host=args.host, port=args.port, ws_max_size=2048, ws_max_queue=8)


if __name__ == "__main__":
    main()
