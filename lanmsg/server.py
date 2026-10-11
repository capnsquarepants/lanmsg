import argparse
import hashlib
import hmac
import os
import secrets
import sqlite3
import time
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .appearance import Appearance

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


def create_app(database=None):
    path = Path(database or os.environ.get("LANMSG_DB", "data/lanmsg.db"))
    path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def db():
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            with conn:
                yield conn
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
        INSERT OR IGNORE INTO channels(id,name) VALUES(1,'general');
        """)

    peers = set()

    @asynccontextmanager
    async def lifespan(app):
        yield
        for peer in list(peers):
            await peer.close()

    app = FastAPI(title="LANmsg", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    def user(token):
        with db() as conn:
            row = conn.execute("SELECT users.id,username,owner FROM sessions JOIN users ON users.id=sessions.user_id WHERE token=? AND expires>?", (token or "", time.time())).fetchone()
        if not row:
            raise HTTPException(401, "Please sign in")
        return dict(row)

    def auth(request):
        # A same-origin header prevents cross-site form requests with session cookies.
        if request.method != "GET" and request.headers.get("x-lanmsg") != "1":
            raise HTTPException(403, "Invalid request origin")
        return user(request.cookies.get("session"))

    async def broadcast(event):
        for peer in list(peers):
            try:
                user(peer.cookies.get("session"))
                await peer.send_json(event)
            except HTTPException:
                peers.discard(peer)
                await peer.close(code=1008)
            except Exception:
                peers.discard(peer)

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/status")
    def status():
        with db() as conn:
            return {"setup": conn.execute("SELECT count(*) FROM users").fetchone()[0] == 0}

    @app.get("/api/appearance")
    def appearance():
        # Design tokens are public so the sign-in screen uses the same theme.
        with db() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key='appearance'").fetchone()
        return Appearance.model_validate_json(row["value"]) if row else Appearance()

    @app.put("/api/admin/appearance")
    async def save_appearance(body: Appearance, request: Request):
        if not auth(request)["owner"]:
            raise HTTPException(403)
        with db() as conn:
            conn.execute("INSERT INTO settings(key,value) VALUES('appearance',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (body.model_dump_json(),))
        result = body.model_dump()
        await broadcast({"type": "appearance", "appearance": result})
        return result

    @app.post("/api/{action}")
    def account(action: str, body: Credentials, request: Request, response: Response):
        if action not in ("register", "login"):
            raise HTTPException(404)
        if request.headers.get("x-lanmsg") != "1":
            raise HTTPException(403)
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if action == "register":
                owner = conn.execute("SELECT count(*) FROM users").fetchone()[0] == 0
                if not owner and not conn.execute("SELECT 1 FROM invites WHERE token=?", (body.invite,)).fetchone():
                    raise HTTPException(403, "A valid invite is required")
                salt = secrets.token_hex(16)
                password = hashlib.scrypt(body.password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()
                try:
                    conn.execute("INSERT INTO users(username,salt,password,owner) VALUES(?,?,?,?)", (body.username, salt, password, owner))
                except sqlite3.IntegrityError:
                    raise HTTPException(409, "That username is taken")
                conn.execute("DELETE FROM invites WHERE token=?", (body.invite,))
            row = conn.execute("SELECT * FROM users WHERE username=?", (body.username,)).fetchone()
            if not row or not hmac.compare_digest(row["password"], hashlib.scrypt(body.password.encode(), salt=bytes.fromhex(row["salt"]), n=16384, r=8, p=1).hex()):
                raise HTTPException(401, "Incorrect username or password")
            token = secrets.token_urlsafe(32)
            conn.execute("INSERT INTO sessions VALUES(?,?,?)", (token, row["id"], time.time() + 30 * 86400))
            response.set_cookie("session", token, httponly=True, samesite="strict", secure=request.url.scheme == "https", max_age=30 * 86400)
            return {"id": row["id"], "username": row["username"], "owner": row["owner"]}

    @app.post("/api/session/logout")
    def logout(request: Request, response: Response):
        auth(request)
        with db() as conn:
            conn.execute("DELETE FROM sessions WHERE token=?", (request.cookies.get("session"),))
        response.delete_cookie("session")
        return {"ok": True}

    @app.get("/api/me")
    def me(request: Request):
        return auth(request)

    @app.get("/api/channels")
    def channels(request: Request):
        auth(request)
        with db() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM channels ORDER BY id")]

    @app.post("/api/admin/channels")
    async def add_channel(body: Channel, request: Request):
        if not auth(request)["owner"]:
            raise HTTPException(403)
        with db() as conn:
            try:
                cursor = conn.execute("INSERT INTO channels(name) VALUES(?)", (body.name,))
            except sqlite3.IntegrityError:
                raise HTTPException(409, "Channel already exists")
            result = {"id": cursor.lastrowid, "name": body.name, "kind": "text"}
        await broadcast({"type": "channels"})
        return result

    @app.post("/api/admin/invites")
    def invite(request: Request):
        if not auth(request)["owner"]:
            raise HTTPException(403)
        token = secrets.token_urlsafe(18)
        with db() as conn:
            conn.execute("INSERT INTO invites VALUES(?)", (token,))
        return {"invite": token}

    @app.get("/api/channels/{channel_id}/messages")
    def history(channel_id: int, request: Request, before: int = 9223372036854775807):
        auth(request)
        with db() as conn:
            return [dict(r) for r in conn.execute("SELECT messages.*, users.username FROM messages JOIN users ON users.id=user_id WHERE channel_id=? AND messages.id<? ORDER BY messages.id DESC LIMIT 50", (channel_id, before))][::-1]

    @app.post("/api/channels/{channel_id}/messages")
    async def send(channel_id: int, body: Message, request: Request):
        member = auth(request)
        if not body.content.strip():
            raise HTTPException(422, "Message cannot be blank")
        with db() as conn:
            if not conn.execute("SELECT 1 FROM channels WHERE id=? AND kind='text'", (channel_id,)).fetchone():
                raise HTTPException(404, "Text channel not found")
            conn.execute("INSERT OR IGNORE INTO messages(channel_id,user_id,content,nonce,created) VALUES(?,?,?,?,?)", (channel_id, member["id"], body.content.strip(), body.nonce, time.time()))
            row = dict(conn.execute("SELECT messages.*,users.username FROM messages JOIN users ON users.id=user_id WHERE user_id=? AND nonce=?", (member["id"], body.nonce)).fetchone())
        await broadcast({"type": "message", "message": row})
        return row

    @app.websocket("/ws")
    async def websocket(socket: WebSocket):
        origin = socket.headers.get("origin")
        expected = f"{'https' if socket.url.scheme == 'wss' else 'http'}://{socket.headers.get('host')}"
        try:
            user(socket.cookies.get("session"))
        except HTTPException:
            await socket.close(code=1008)
            return
        if origin != expected:
            await socket.close(code=1008)
            return
        await socket.accept()
        peers.add(socket)
        try:
            while True:
                await socket.receive_text()
                user(socket.cookies.get("session"))
                await socket.send_json({"type": "pong"})
        except (WebSocketDisconnect, HTTPException):
            pass
        finally:
            peers.discard(socket)

    return app


def main():
    import uvicorn
    parser = argparse.ArgumentParser(description="LANmsg text chat server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    uvicorn.run(create_app(), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
