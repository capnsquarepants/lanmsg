import argparse
import asyncio
import hashlib
import hmac
import os
import re
import secrets
import shutil
import sqlite3
import time
import threading
import unicodedata
from contextlib import asynccontextmanager, contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from starlette.concurrency import run_in_threadpool

from .appearance import Appearance
from .attachments import ID_PATTERN, attachment_directory, clean_filename, preview_type
from .previews import PreviewError, PreviewService
from .reliability import AdmissionMiddleware, Hub, Limits, RateLimiter
from .settings import AppSettings, LIMIT_FIELDS
from .roles import can_change_role, identity

STATIC = Path(__file__).parent / "static"


def mentioned_users(conn, content):
    text = re.sub(r'https?://[^\s<>"`]+', ' ', content, flags=re.I)
    handles = sorted(set(match.lower() for match in re.findall(r"(?<![\w@/])@([a-zA-Z0-9_-]{2,32})(?![\w-])", text)))
    if not handles:
        return []
    return [dict(user) for user in conn.execute("SELECT id,username FROM users WHERE lower(username) IN (" + ",".join("?" for _ in handles) + ") ORDER BY id", handles)]


def sync_mentions(conn, message_id, content):
    conn.execute("DELETE FROM message_mentions WHERE message_id=?", (message_id,))
    conn.executemany("INSERT INTO message_mentions(message_id,user_id) VALUES(?,?)", [(message_id, user["id"]) for user in mentioned_users(conn, content)])


class Credentials(BaseModel):
    username: str = Field(min_length=2, max_length=32, pattern=r"^[a-zA-Z0-9_-]+$")
    password: str = Field(min_length=10, max_length=128)
    invite: str = ""


class Message(BaseModel):
    content: str = Field(default="", max_length=4000)
    nonce: str = Field(min_length=1, max_length=64)
    attachments: list[str] = Field(default_factory=list, max_length=3)

    @field_validator("attachments")
    @classmethod
    def attachment_ids(cls, value):
        if len(set(value)) != len(value) or any(not ID_PATTERN.fullmatch(item) for item in value):
            raise ValueError("Use up to three unique attachment IDs")
        return value


class PreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(min_length=1, max_length=2048)


class MessageEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str = Field(max_length=4000)
    version: int = Field(ge=1)


class ReadPosition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message_id: int = Field(ge=0)


class AdminRole(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["admin", "moderator", "user", "banned"] | None = None
    admin: bool | None = None  # Compatibility with older clients.
    reason: str = Field(default="", max_length=300)

    @model_validator(mode="after")
    def one_role(self):
        if (self.role is None) == (self.admin is None):
            raise ValueError("Supply exactly one role")
        if any(unicodedata.category(char).startswith("C") for char in self.reason):
            raise ValueError("Use a reason without control characters")
        self.reason = self.reason.strip()
        return self


class OwnershipTransfer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: int = Field(ge=1)
    password: str = Field(min_length=10, max_length=128)


class BanUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    banned: bool
    reason: str = Field(default="", max_length=300)

    @field_validator("reason")
    @classmethod
    def clean_reason(cls, value):
        value = value.strip()
        if any(unicodedata.category(char).startswith("C") for char in value):
            raise ValueError("Use a reason without control characters")
        return value


class Channel(BaseModel):
    name: str = Field(min_length=1, max_length=32, pattern=r"^[a-z0-9_-]+$")

    @field_validator("name", mode="before")
    @classmethod
    def normalize_name(cls, value):
        return re.sub(r"\s+", "-", value.strip().lower()) if isinstance(value, str) else value


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


class ManagedProfile(ProfileUpdate):
    bio: str = Field(default="", max_length=160)
    nickname_color: Literal["default", "accent", "text"] = "default"
    media_previews: bool | None = None
    notifications: Literal["mentions", "all", "none"] = "mentions"
    desktop_notifications: bool = False

    @field_validator("bio")
    @classmethod
    def clean_bio(cls, value):
        value = unicodedata.normalize("NFC", value).strip()
        if any(unicodedata.category(char).startswith("C") and char != "\n" for char in value):
            raise ValueError("Use plain text without control characters")
        return value


def create_app(database=None, limits=None):
    limits = limits or Limits()
    path = Path(database or os.environ.get("LANMSG_DB", "data/lanmsg.db"))
    path.parent.mkdir(parents=True, exist_ok=True)
    files = attachment_directory(path)
    files.mkdir(exist_ok=True)
    storage_lock = threading.Lock()
    upload_slots = threading.BoundedSemaphore(2)
    upload_reserved = 0

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
        user_columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
        for column, declaration in (("admin", "INTEGER NOT NULL DEFAULT 0"), ("banned", "INTEGER NOT NULL DEFAULT 0"), ("ban_reason", "TEXT NOT NULL DEFAULT ''"), ("notifications", "TEXT NOT NULL DEFAULT 'mentions'"), ("desktop_notifications", "INTEGER NOT NULL DEFAULT 0"), ("bio", "TEXT NOT NULL DEFAULT ''"), ("nickname_color", "TEXT NOT NULL DEFAULT 'default'"), ("media_previews", "INTEGER NOT NULL DEFAULT 0")):
            if column not in user_columns:
                conn.execute(f"ALTER TABLE users ADD COLUMN {column} {declaration}")
        if "role" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'user' CHECK(role IN ('admin','moderator','user','banned'))")
            conn.execute("UPDATE users SET role=CASE WHEN owner=1 THEN 'admin' WHEN banned=1 THEN 'banned' WHEN admin=1 THEN 'admin' ELSE 'user' END")
            conn.execute("UPDATE users SET admin=(role='admin'),banned=(role='banned')")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS single_server_owner ON users(owner) WHERE owner=1")
        if "media_preview_override" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN media_preview_override INTEGER")
            # Preserve existing explicit choices. New accounts inherit the server default.
            conn.execute("UPDATE users SET media_preview_override=media_previews")
        if "expires" not in {row["name"] for row in conn.execute("PRAGMA table_info(invites)")}:
            conn.execute("ALTER TABLE invites ADD COLUMN expires REAL NOT NULL DEFAULT 0")
            conn.execute("UPDATE invites SET expires=?", (time.time() + 7 * 86400,))
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(messages)")}
        for column, declaration in (("version", "INTEGER NOT NULL DEFAULT 1"), ("edited", "REAL"), ("deleted", "REAL"), ("deleted_by", "INTEGER REFERENCES users(id)")):
            if column not in columns:
                conn.execute(f"ALTER TABLE messages ADD COLUMN {column} {declaration}")
        conn.execute("""CREATE TABLE IF NOT EXISTS message_versions(
            message_id INTEGER REFERENCES messages(id) ON DELETE CASCADE,
            version INTEGER NOT NULL, content TEXT NOT NULL, changed REAL NOT NULL,
            actor_id INTEGER REFERENCES users(id), action TEXT NOT NULL,
            PRIMARY KEY(message_id, version))""")
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS channel_reads(
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            channel_id INTEGER REFERENCES channels(id) ON DELETE CASCADE,
            message_id INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(user_id,channel_id));
        CREATE TABLE IF NOT EXISTS message_mentions(
            message_id INTEGER REFERENCES messages(id) ON DELETE CASCADE,
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            PRIMARY KEY(message_id,user_id));
        CREATE INDEX IF NOT EXISTS mentions_by_user ON message_mentions(user_id,message_id);
        CREATE TABLE IF NOT EXISTS message_sequence(id INTEGER PRIMARY KEY CHECK(id=1),value INTEGER NOT NULL);
        INSERT OR IGNORE INTO message_sequence VALUES(1,(SELECT coalesce(max(id),0) FROM messages));
        CREATE TABLE IF NOT EXISTS audit_log(
            id INTEGER PRIMARY KEY AUTOINCREMENT,created REAL NOT NULL,
            actor_id INTEGER REFERENCES users(id),action TEXT NOT NULL,
            target_id INTEGER,detail TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS attachments(
            id TEXT PRIMARY KEY,owner_id INTEGER REFERENCES users(id),
            message_id INTEGER REFERENCES messages(id) ON DELETE SET NULL,
            filename TEXT NOT NULL,size INTEGER NOT NULL,media_type TEXT NOT NULL,created REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS attachments_message ON attachments(message_id);
        CREATE INDEX IF NOT EXISTS attachments_owner_pending ON attachments(owner_id,message_id,created);
        """)
        conn.execute("BEGIN IMMEDIATE")
        if "read_start_id" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN read_start_id INTEGER NOT NULL DEFAULT 0")
            # Existing chat history is a baseline, rather than thousands of new alerts.
            conn.execute("UPDATE users SET read_start_id=(SELECT coalesce(max(id),0) FROM messages)")
        conn.execute("UPDATE message_sequence SET value=max(value,coalesce((SELECT max(id) FROM messages),0),coalesce((SELECT max(read_start_id) FROM users),0),coalesce((SELECT max(message_id) FROM channel_reads),0)) WHERE id=1")
        if not conn.execute("SELECT 1 FROM settings WHERE key='mention_index'").fetchone():
            for row in conn.execute("SELECT id,content FROM messages WHERE deleted IS NULL"):
                sync_mentions(conn, row["id"], row["content"])
            conn.execute("INSERT INTO settings(key,value) VALUES('mention_index','1')")
        stored = conn.execute("SELECT value FROM settings WHERE key='app'").fetchone()
    saved_settings = AppSettings.model_validate_json(stored["value"]) if stored else AppSettings(**{key: getattr(limits, key) for key in LIMIT_FIELDS})
    limits = replace(limits, **{key: getattr(saved_settings, key) for key in LIMIT_FIELDS})
    settings_lock = threading.Lock()
    message_lock = threading.Lock()

    limiter = RateLimiter(limits.rate_keys)
    preview_service = PreviewService()
    preview_waiters = threading.BoundedSemaphore(max(1, min(4, limits.http_concurrency // 4)))

    def cleanup():
        with db() as conn:
            conn.execute("DELETE FROM sessions WHERE expires<=?", (time.time(),))
            conn.execute("DELETE FROM invites WHERE expires<=?", (time.time(),))
            if limits.retention_days:
                conn.execute("DELETE FROM messages WHERE created<?", (time.time() - limits.retention_days * 86400,))
            stale = [row[0] for row in conn.execute("SELECT id FROM attachments WHERE message_id IS NULL AND created<?", (time.time() - 3600,))]
            conn.executemany("DELETE FROM attachments WHERE id=?", [(attachment_id,) for attachment_id in stale])
            known = {row[0] for row in conn.execute("SELECT id FROM attachments")}
        # Commit metadata first. An interruption can leave an orphan file, never a live row with a deleted file.
        for attachment_id in stale:
            if ID_PATTERN.fullmatch(attachment_id):
                try:
                    (files / attachment_id).unlink(missing_ok=True)
                except OSError:
                    pass  # A download may temporarily keep the file open on Windows; orphan cleanup retries.
        # Recover files left by an interrupted upload without touching unrelated files.
        for file in files.iterdir():
            if ID_PATTERN.fullmatch(file.name.removesuffix(".tmp")) and file.name not in known:
                try:
                    if file.is_file() and file.stat().st_mtime < time.time() - 3600:
                        file.unlink(missing_ok=True)
                except OSError:
                    continue

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
            preview_service.close()

    app = FastAPI(title="LANmsg", lifespan=lifespan)
    app.state.preview_service = preview_service
    app.add_middleware(AdmissionMiddleware, limits=limits, limiter=limiter)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    def user(token):
        with db() as conn:
            row = conn.execute("SELECT users.id,username,display_name,owner,role FROM sessions JOIN users ON users.id=sessions.user_id WHERE token=? AND expires>? AND users.role!='banned'", (token or "", time.time())).fetchone()
        if not row:
            raise HTTPException(401, "Please sign in")
        return identity(row)

    def auth(request):
        # A same-origin header prevents cross-site form requests with session cookies.
        if request.method != "GET" and request.headers.get("x-lanmsg") != "1":
            raise HTTPException(403, "Invalid request origin")
        member = user(request.cookies.get("session"))
        if request.method != "GET" and request.url.path != "/api/previews":
            message = request.url.path.endswith("/messages")
            read = request.url.path.endswith("/read") or request.url.path == "/api/read-all"
            rate, burst = (2, 10) if read else ((limits.message_rate, limits.message_burst) if message else (limits.write_rate, limits.write_burst))
            if not limiter.allow(("read" if read else "message" if message else "write", member["id"]), rate, burst):
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

    def admin(request):
        member = auth(request)
        if not (member["owner"] or member["admin"]):
            raise HTTPException(403, "Administrator access required")
        return member

    def staff(request):
        member = auth(request)
        if member["role"] not in ("admin", "moderator"):
            raise HTTPException(403, "Moderator access required")
        return member

    def require_admin(conn, member, owner_only=False):
        # Recheck inside the write transaction: a concurrent demotion cannot race a mutation.
        row = require_active(conn, member)
        if not (row["owner"] or (row["admin"] and not owner_only)):
            raise HTTPException(403, "Server owner access required" if owner_only else "Administrator access required")

    def require_active(conn, member):
        row = conn.execute("SELECT id,owner,role FROM users WHERE id=?", (member["id"],)).fetchone()
        if not row or row["role"] == "banned":
            raise HTTPException(401, "Please sign in")
        return {**dict(row), "admin": row["role"] == "admin", "moderator": row["role"] == "moderator"}

    def audit(conn, member, action, target_id=None, detail=""):
        conn.execute("INSERT INTO audit_log(created,actor_id,action,target_id,detail) VALUES(?,?,?,?,?)", (time.time(), member["id"], action, target_id, detail))

    def settings_response():
        settings = saved_settings
        effective = {key: getattr(limits, key) for key in LIMIT_FIELDS}
        return {"settings": settings.model_dump(), "effective_limits": effective,
                "restart_required": any(getattr(settings, key) != value for key, value in effective.items())}

    @app.get("/api/admin/console")
    def console(request: Request):
        member = staff(request)
        with db() as conn:
            stats = {table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in ("users", "channels", "messages")}
            channels = [dict(row) for row in conn.execute("SELECT * FROM channels ORDER BY id")]
            invites = [dict(row) for row in conn.execute("SELECT token,expires FROM invites WHERE expires>? ORDER BY expires DESC", (time.time(),))] if member["role"] == "admin" else []
        stats["connections"] = len(hub.peers)
        stats["free_disk_bytes"] = shutil.disk_usage(path.parent).free
        stats["database_bytes"] = path.stat().st_size
        return {**(settings_response() if member["role"] == "admin" else {}), "stats": stats, "channels": channels, "invites": invites}

    @app.put("/api/admin/settings")
    def update_settings(body: AppSettings, request: Request):
        nonlocal saved_settings
        member = admin(request)
        with settings_lock:
            with db() as conn:
                conn.execute("BEGIN IMMEDIATE")
                require_admin(conn, member)
                changed = sorted(key for key, value in body.model_dump().items() if value != saved_settings.model_dump()[key])
                conn.execute("INSERT INTO settings(key,value) VALUES('app',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (body.model_dump_json(),))
                if changed:
                    audit(conn, member, "settings.update", detail=", ".join(changed))
            saved_settings = body
            result = settings_response()
            hub.publish({"type": "server_settings", "settings": body.public()})
        return result

    @app.delete("/api/admin/invites/{token}")
    def revoke_invite(token: str, request: Request):
        member = admin(request)
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            require_admin(conn, member)
            if conn.execute("DELETE FROM invites WHERE token=?", (token,)).rowcount:
                audit(conn, member, "invite.revoke")
        return {"ok": True}

    @app.get("/api/admin/members")
    def moderation_members(request: Request):
        staff(request)
        with db() as conn:
            return [dict(row) for row in conn.execute("SELECT id,username,display_name,owner,role,admin,banned,ban_reason FROM users ORDER BY owner DESC,role,username COLLATE NOCASE")]

    def set_role(user_id, role, reason, request, legacy=False, banned=None):
        member = staff(request)
        tokens = []
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            actor = require_active(conn, member)
            target = conn.execute("SELECT id,owner,role FROM users WHERE id=?", (user_id,)).fetchone()
            if not target:
                raise HTTPException(404, "User not found")
            if banned is not None:
                role = "banned" if banned else ("user" if target["role"] == "banned" else target["role"])
            if not can_change_role(actor, target, role):
                raise HTTPException(403, "You cannot change this account's role")
            if target["role"] == "banned" and (role in ("admin", "moderator") or legacy):
                raise HTTPException(409, "Unban this account to User before granting staff access")
            if target["role"] != role:
                conn.execute("UPDATE users SET role=?,admin=?,banned=?,ban_reason=? WHERE id=?", (role, role == "admin", role == "banned", reason if role == "banned" else "", user_id))
                action = "member.ban" if role == "banned" else "member.unban" if target["role"] == "banned" else "role.update"
                audit(conn, member, action, user_id, (target["role"] + " -> " + role) + (": " + reason if reason else ""))
            if role == "banned":
                tokens = [row[0] for row in conn.execute("SELECT token FROM sessions WHERE user_id=?", (user_id,))]
                conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
        for token in tokens:
            hub.revoke(token)
        hub.publish({"type": "permissions", "user_id": user_id})
        return {"ok": True}

    @app.put("/api/admin/members/{user_id}/role")
    def change_role(user_id: int, body: AdminRole, request: Request):
        role = body.role if body.role is not None else "admin" if body.admin else "user"
        return set_role(user_id, role, body.reason, request, legacy=body.admin is not None)

    @app.put("/api/admin/members/{user_id}/ban")
    def change_ban(user_id: int, body: BanUpdate, request: Request):
        # Retain the old endpoint, using the same role hierarchy as the new selector.
        return set_role(user_id, None, body.reason, request, banned=body.banned)

    @app.post("/api/admin/ownership")
    def transfer_ownership(body: OwnershipTransfer, request: Request):
        member = owner(request)
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            require_admin(conn, member, owner_only=True)
            current = conn.execute("SELECT salt,password FROM users WHERE id=?", (member["id"],)).fetchone()
            candidate = hashlib.scrypt(body.password.encode(), salt=bytes.fromhex(current["salt"]), n=16384, r=8, p=1).hex()
            if not hmac.compare_digest(current["password"], candidate):
                raise HTTPException(403, "Incorrect password")
            target = conn.execute("SELECT id,owner,role FROM users WHERE id=?", (body.user_id,)).fetchone()
            if not target:
                raise HTTPException(404, "User not found")
            if target["owner"] or target["role"] != "admin":
                raise HTTPException(409, "Choose another active Admin to receive ownership")
            conn.execute("UPDATE users SET owner=0 WHERE id=?", (member["id"],))
            conn.execute("UPDATE users SET owner=1 WHERE id=?", (body.user_id,))
            audit(conn, member, "ownership.transfer", body.user_id)
        hub.publish({"type": "permissions", "user_id": member["id"]})
        hub.publish({"type": "permissions", "user_id": body.user_id})
        return {"ok": True}

    @app.get("/api/admin/audit")
    def audit_history(request: Request, before: int = 9223372036854775807):
        staff(request)
        with db() as conn:
            rows = [dict(row) for row in conn.execute("""SELECT a.*,u.username AS actor_username,t.username AS target_username
                FROM audit_log a LEFT JOIN users u ON u.id=a.actor_id
                LEFT JOIN users t ON t.id=a.target_id AND a.action IN ('admin.grant','admin.revoke','member.ban','member.unban','role.update','ownership.transfer')
                WHERE a.id<? ORDER BY a.id DESC LIMIT 51""", (before,))]
        return {"entries": rows[:50], "next_before": rows[49]["id"] if len(rows) > 50 else None}

    @app.get("/api/appearance")
    def appearance():
        # Design tokens are public so the sign-in screen uses the same theme.
        with db() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key='appearance'").fetchone()
        return Appearance.model_validate_json(row["value"]) if row else Appearance()

    @app.put("/api/admin/appearance")
    def save_appearance(body: Appearance, request: Request):
        member = admin(request)
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            require_admin(conn, member)
            conn.execute("INSERT INTO settings(key,value) VALUES('appearance',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (body.model_dump_json(),))
            audit(conn, member, "appearance.update")
        result = body.model_dump()
        hub.publish({"type": "appearance", "appearance": result})
        return result

    @app.post("/api/register")
    @app.post("/api/login")
    def account(body: Credentials, request: Request, response: Response):
        action = request.url.path.rsplit("/", 1)[-1]
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
                    conn.execute("INSERT INTO users(username,display_name,salt,password,owner,role,admin,read_start_id) VALUES(?,?,?,?,?,?,?,(SELECT value FROM message_sequence WHERE id=1))", (body.username, body.username, salt, password, owner, "admin" if owner else "user", int(owner)))
                except sqlite3.IntegrityError:
                    raise HTTPException(409, "That username is taken")
                conn.execute("DELETE FROM invites WHERE token=?", (body.invite,))
            row = conn.execute("SELECT * FROM users WHERE username=?", (body.username,)).fetchone()
            if not row or not hmac.compare_digest(row["password"], hashlib.scrypt(body.password.encode(), salt=bytes.fromhex(row["salt"]), n=16384, r=8, p=1).hex()):
                raise HTTPException(401, "Incorrect username or password")
            if row["role"] == "banned":
                raise HTTPException(403, "This account is banned")
            token = secrets.token_urlsafe(32)
            conn.execute("INSERT INTO sessions VALUES(?,?,?)", (token, row["id"], time.time() + 30 * 86400))
            conn.execute("DELETE FROM sessions WHERE user_id=? AND token NOT IN (SELECT token FROM sessions WHERE user_id=? ORDER BY expires DESC LIMIT 10)", (row["id"], row["id"]))
            response.set_cookie("session", token, httponly=True, samesite="strict", secure=request.url.scheme == "https", max_age=30 * 86400)
            return identity(row)

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
            conn.execute("BEGIN IMMEDIATE")
            require_active(conn, member)
            conn.execute("UPDATE users SET display_name=? WHERE id=?", (body.display_name, member["id"]))
        profile = {"id": member["id"], "username": member["username"], "display_name": body.display_name}
        hub.publish({"type": "profile", "profile": profile})
        return {**member, **profile}

    def profile_row(conn, user_id):
        row = conn.execute("SELECT id,username,display_name,bio,nickname_color,role,owner FROM users WHERE id=?", (user_id,)).fetchone()
        if not row:
            raise HTTPException(404, "User not found")
        return dict(row)

    @app.get("/api/me/profile")
    def my_profile(request: Request):
        member = auth(request)
        with db() as conn:
            profile = profile_row(conn, member["id"])
            enabled = conn.execute("SELECT media_preview_override,notifications,desktop_notifications FROM users WHERE id=?", (member["id"],)).fetchone()
        return {**profile, "media_previews": None if enabled[0] is None else bool(enabled[0]), "notifications": enabled[1], "desktop_notifications": bool(enabled[2])}

    @app.put("/api/me/profile")
    def save_profile(body: ManagedProfile, request: Request):
        member = auth(request)
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            require_active(conn, member)
            conn.execute("UPDATE users SET display_name=?,bio=?,nickname_color=?,media_preview_override=?,notifications=?,desktop_notifications=? WHERE id=?", (body.display_name, body.bio, body.nickname_color, body.media_previews, body.notifications, body.desktop_notifications, member["id"]))
            profile = profile_row(conn, member["id"])
        hub.publish({"type": "profile", "profile": profile})
        return {**profile, "media_previews": body.media_previews, "notifications": body.notifications, "desktop_notifications": body.desktop_notifications}

    @app.get("/api/users")
    def member_directory(request: Request):
        auth(request)
        with db() as conn:
            return [dict(row) for row in conn.execute("SELECT id,username,display_name,role,owner FROM users WHERE role!='banned' ORDER BY username COLLATE NOCASE")]

    def attachment_info(row):
        return {"id": row["id"], "filename": row["filename"], "size": row["size"], "media_type": row["media_type"], "url": "/api/attachments/" + row["id"]}

    @app.get("/api/attachments")
    def pending_attachments(request: Request):
        member = auth(request)
        with db() as conn:
            return [attachment_info(row) for row in conn.execute("SELECT * FROM attachments WHERE owner_id=? AND message_id IS NULL ORDER BY created", (member["id"],))]

    @app.post("/api/attachments")
    async def upload_attachment(request: Request, filename: str):
        nonlocal upload_reserved
        member = await run_in_threadpool(auth, request)
        if not saved_settings.uploads_enabled:
            raise HTTPException(403, "File uploads are disabled")
        try:
            filename = clean_filename(filename)
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        if not upload_slots.acquire(blocking=False):
            raise HTTPException(503, "Uploads are busy; retry shortly", headers={"Retry-After": "1"})
        attachment_id = secrets.token_hex(16)
        temporary = files / (attachment_id + ".tmp")
        destination = files / attachment_id
        budget = saved_settings.attachment_max_mb * 1024 * 1024
        reserved = False
        committed = False
        try:
            def reserve():
                nonlocal upload_reserved
                with storage_lock, db() as conn:
                    total, count = conn.execute("SELECT coalesce(sum(size),0),count(*) FROM attachments").fetchone()
                    if count >= 10000 or total + upload_reserved + budget > saved_settings.attachment_storage_mb * 1024 * 1024:
                        raise HTTPException(507, "Attachment storage budget is full")
                    if shutil.disk_usage(path.parent).free < limits.minimum_free_bytes + upload_reserved + budget:
                        raise HTTPException(507, "Server is low on disk space")
                    if conn.execute("SELECT count(*) FROM attachments WHERE owner_id=? AND message_id IS NULL", (member["id"],)).fetchone()[0] >= 3:
                        raise HTTPException(409, "Send or remove your pending attachments first")
                    upload_reserved += budget
            await run_in_threadpool(reserve)
            reserved = True
            size = 0
            header = b""
            async with asyncio.timeout(30):
                with temporary.open("xb") as output:
                    async for chunk in request.stream():
                        size += len(chunk)
                        if size > budget:
                            raise HTTPException(413, "File exceeds the server's upload limit")
                        if len(header) < 32:
                            header += chunk[:32-len(header)]
                        if chunk:
                            await run_in_threadpool(output.write, chunk)
            if not size:
                raise HTTPException(422, "Empty files cannot be uploaded")
            media_type = preview_type(header, filename)
            def finish():
                nonlocal committed
                with storage_lock, db() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    require_active(conn, member)
                    if not saved_settings.uploads_enabled or size > saved_settings.attachment_max_mb * 1024 * 1024:
                        raise HTTPException(403, "Upload settings changed; retry with the current limits")
                    total, count = conn.execute("SELECT coalesce(sum(size),0),count(*) FROM attachments").fetchone()
                    if count >= 10000 or total + size > saved_settings.attachment_storage_mb * 1024 * 1024 or shutil.disk_usage(path.parent).free < limits.minimum_free_bytes:
                        raise HTTPException(507, "Attachment storage budget is full or disk space is low")
                    if conn.execute("SELECT count(*) FROM attachments WHERE owner_id=? AND message_id IS NULL", (member["id"],)).fetchone()[0] >= 3:
                        raise HTTPException(409, "Send or remove your pending attachments first")
                    temporary.rename(destination)
                    conn.execute("INSERT INTO attachments VALUES(?,?,NULL,?,?,?,?)", (attachment_id, member["id"], filename, size, media_type, time.time()))
                committed = True
            task = asyncio.create_task(run_in_threadpool(finish))
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                await task
                raise
            return {"id": attachment_id, "filename": filename, "size": size, "media_type": media_type, "url": "/api/attachments/" + attachment_id}
        except TimeoutError:
            raise HTTPException(408, "Upload timed out") from None
        except OSError:
            raise HTTPException(507, "Could not store the attachment") from None
        finally:
            temporary.unlink(missing_ok=True)
            if not committed:
                destination.unlink(missing_ok=True)
            if reserved:
                with storage_lock:
                    upload_reserved -= budget
            upload_slots.release()

    @app.delete("/api/attachments/{attachment_id}")
    def discard_attachment(attachment_id: str, request: Request):
        member = auth(request)
        if not ID_PATTERN.fullmatch(attachment_id):
            raise HTTPException(404, "Attachment not found")
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            require_active(conn, member)
            row = conn.execute("SELECT * FROM attachments WHERE id=?", (attachment_id,)).fetchone()
            if not row or row["owner_id"] != member["id"]:
                raise HTTPException(404, "Attachment not found")
            if row["message_id"] is not None:
                raise HTTPException(409, "Delete the message to remove a sent attachment")
            (files / attachment_id).unlink(missing_ok=True)
            conn.execute("DELETE FROM attachments WHERE id=?", (attachment_id,))
        return {"ok": True}

    @app.get("/api/attachments/{attachment_id}")
    def download_attachment(attachment_id: str, request: Request, inline: bool = False):
        member = auth(request)
        if not ID_PATTERN.fullmatch(attachment_id):
            raise HTTPException(404, "Attachment not found")
        with db() as conn:
            row = conn.execute("SELECT a.*,m.deleted FROM attachments a LEFT JOIN messages m ON m.id=a.message_id WHERE a.id=?", (attachment_id,)).fetchone()
            if not row or (row["message_id"] is None and row["owner_id"] != member["id"]) or (row["deleted"] is not None and not (member["owner"] or member["admin"])):
                raise HTTPException(404, "Attachment not found")
            if inline:
                override = conn.execute("SELECT media_preview_override FROM users WHERE id=?", (member["id"],)).fetchone()[0]
                if not (saved_settings.media_previews if override is None else bool(override)):
                    raise HTTPException(403, "Previews are disabled for your account")
                if not row["media_type"].startswith(("image/", "video/")):
                    raise HTTPException(415, "This file cannot be previewed inline")
        file = files / attachment_id
        if not file.is_file():
            raise HTTPException(404, "Attachment file is unavailable")
        return FileResponse(file, filename=row["filename"], content_disposition_type="inline" if inline else "attachment",
                            media_type=row["media_type"] if inline else "application/octet-stream",
                            headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff", "Content-Security-Policy": "sandbox; default-src 'none'"})

    @app.get("/api/users/{user_id}/profile")
    def public_profile(user_id: int, request: Request):
        auth(request)
        with db() as conn:
            return profile_row(conn, user_id)

    @app.post("/api/previews")
    def link_preview(body: PreviewRequest, request: Request, response: Response):
        member = auth(request)
        with db() as conn:
            override = conn.execute("SELECT media_preview_override FROM users WHERE id=?", (member["id"],)).fetchone()[0]
        if not (saved_settings.media_previews if override is None else bool(override)):
            raise HTTPException(403, "Previews are disabled for your account")
        if not limiter.allow(("preview", member["id"]), 2, 6):
            raise HTTPException(429, "Too many preview requests; retry shortly", headers={"Retry-After":"1"})
        response.headers["Cache-Control"] = "private, no-store"
        if not preview_waiters.acquire(blocking=False):
            raise HTTPException(503, "Preview service is busy; retry later", headers={"Retry-After":"1"})
        try:
            return preview_service.get(body.url)
        except PreviewError as error:
            raise HTTPException(error.status, str(error)) from None
        finally:
            preview_waiters.release()

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
        member = admin(request)
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            require_admin(conn, member)
            try:
                cursor = conn.execute("INSERT INTO channels(name) VALUES(?)", (body.name,))
            except sqlite3.IntegrityError:
                raise HTTPException(409, "Channel already exists")
            result = {"id": cursor.lastrowid, "name": body.name, "kind": "text"}
            audit(conn, member, "channel.create", result["id"], body.name)
        hub.publish({"type": "channels"})
        return result

    @app.post("/api/admin/invites")
    def invite(request: Request):
        member = admin(request)
        token = secrets.token_urlsafe(18)
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            require_admin(conn, member)
            conn.execute("DELETE FROM invites WHERE expires<=?", (time.time(),))
            if conn.execute("SELECT count(*) FROM invites").fetchone()[0] >= 100:
                raise HTTPException(409, "There are already 100 unused invites")
            conn.execute("INSERT INTO invites(token,expires) VALUES(?,?)", (token, time.time() + 7 * 86400))
            audit(conn, member, "invite.create")
        return {"invite": token}

    def public_message(row, conn):
        result = {key: row[key] for key in ("id", "channel_id", "user_id", "created", "username", "display_name", "nickname_color", "author_role", "version", "edited", "deleted")}
        result["content"] = "" if row["deleted"] is not None else row["content"]
        result["mentions"] = []
        result["attachments"] = [] if row["deleted"] is not None else [attachment_info(file) for file in conn.execute("SELECT * FROM attachments WHERE message_id=? ORDER BY id", (row["id"],))]
        if result["content"]:
            result["mentions"] = [dict(user) for user in conn.execute("SELECT u.id,u.username FROM message_mentions mm JOIN users u ON u.id=mm.user_id WHERE mm.message_id=? ORDER BY u.id", (row["id"],))]
        return result

    def message_row(conn, message_id):
        row = conn.execute("SELECT messages.*,users.username,users.display_name,users.nickname_color,users.role AS author_role FROM messages JOIN users ON users.id=user_id WHERE messages.id=?", (message_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Message not found")
        return row

    def read_position(conn, user_id, channel_id):
        return conn.execute("SELECT coalesce((SELECT message_id FROM channel_reads WHERE user_id=u.id AND channel_id=?),u.read_start_id) FROM users u WHERE id=?", (channel_id, user_id)).fetchone()[0]

    def channel_read_state(conn, user_id):
        return [dict(row) for row in conn.execute("""
            WITH positions AS (
                SELECT c.id,coalesce(r.message_id,u.read_start_id) AS read_id
                FROM channels c CROSS JOIN users u LEFT JOIN channel_reads r ON r.channel_id=c.id AND r.user_id=u.id
                WHERE u.id=? AND c.kind='text')
            SELECT p.id,p.read_id,coalesce((SELECT max(id) FROM messages WHERE channel_id=p.id),0) AS latest_id,
                count(CASE WHEN m.id>p.read_id AND m.deleted IS NULL AND m.user_id!=? THEN 1 END) AS unread,
                count(CASE WHEN m.id>p.read_id AND m.deleted IS NULL AND m.user_id!=? AND mm.user_id IS NOT NULL THEN 1 END) AS mentions,
                min(CASE WHEN m.id>p.read_id AND m.deleted IS NULL AND m.user_id!=? THEN m.id END) AS first_unread
            FROM positions p LEFT JOIN messages m ON m.channel_id=p.id AND m.id>p.read_id
            LEFT JOIN message_mentions mm ON mm.message_id=m.id AND mm.user_id=? GROUP BY p.id
            """, (user_id, user_id, user_id, user_id, user_id))]

    @app.get("/api/read-state")
    def read_state(request: Request):
        member = auth(request)
        with db() as conn:
            return {"channels": channel_read_state(conn, member["id"])}

    @app.put("/api/channels/{channel_id}/read")
    def mark_read(channel_id: int, body: ReadPosition, request: Request):
        member = auth(request)
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            require_active(conn, member)
            if not conn.execute("SELECT 1 FROM channels WHERE id=? AND kind='text'", (channel_id,)).fetchone():
                raise HTTPException(404, "Text channel not found")
            if body.message_id and not conn.execute("SELECT 1 FROM messages WHERE id=? AND channel_id=?", (body.message_id, channel_id)).fetchone():
                raise HTTPException(422, "Read position must be a message in this channel")
            position = max(read_position(conn, member["id"], channel_id), body.message_id)
            conn.execute("INSERT INTO channel_reads VALUES(?,?,?) ON CONFLICT(user_id,channel_id) DO UPDATE SET message_id=max(message_id,excluded.message_id)", (member["id"], channel_id, position))
        hub.publish({"type": "reads", "user_id": member["id"]})
        return {"channel_id": channel_id, "read_id": position}

    @app.post("/api/read-all")
    def mark_all_read(request: Request):
        member = auth(request)
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            require_active(conn, member)
            latest = conn.execute("SELECT value FROM message_sequence WHERE id=1").fetchone()[0]
            conn.execute("UPDATE users SET read_start_id=max(read_start_id,?) WHERE id=?", (latest, member["id"]))
            conn.execute("UPDATE channel_reads SET message_id=max(message_id,?) WHERE user_id=?", (latest, member["id"]))
        hub.publish({"type": "reads", "user_id": member["id"]})
        return {"ok": True}

    @app.get("/api/mentions")
    def mentions_inbox(request: Request, before: int = 9223372036854775807, unread_only: bool = True):
        member = auth(request)
        with db() as conn:
            rows = list(conn.execute("""SELECT m.*,u.username,u.display_name,u.nickname_color,u.role AS author_role,c.name AS channel_name
                FROM message_mentions mm JOIN messages m ON m.id=mm.message_id
                JOIN users u ON u.id=m.user_id JOIN channels c ON c.id=m.channel_id
                JOIN users me ON me.id=mm.user_id LEFT JOIN channel_reads r ON r.user_id=me.id AND r.channel_id=m.channel_id
                WHERE mm.user_id=? AND m.id<? AND m.deleted IS NULL AND m.user_id!=?
                AND (?=0 OR m.id>coalesce(r.message_id,me.read_start_id)) ORDER BY m.id DESC LIMIT 51""",
                (member["id"], before, member["id"], int(unread_only))))
            items = [{**public_message(row, conn), "channel_name": row["channel_name"], "unread": row["id"] > read_position(conn, member["id"], row["channel_id"])} for row in rows[:50]]
        return {"messages": items, "next_before": items[-1]["id"] if len(rows) > 50 else None}

    @app.get("/api/channels/{channel_id}/view")
    def channel_view(channel_id: int, request: Request, around: int | None = None):
        member = auth(request)
        with db() as conn:
            if not conn.execute("SELECT 1 FROM channels WHERE id=? AND kind='text'", (channel_id,)).fetchone():
                raise HTTPException(404, "Text channel not found")
            position = read_position(conn, member["id"], channel_id)
            if around is not None and not conn.execute("SELECT 1 FROM messages WHERE id=? AND channel_id=?", (around, channel_id)).fetchone():
                raise HTTPException(404, "Message no longer exists in this channel")
            if around is None:
                around = conn.execute("SELECT min(id) FROM messages WHERE channel_id=? AND id>? AND deleted IS NULL AND user_id!=?", (channel_id, position, member["id"])).fetchone()[0]
            select = "SELECT m.*,u.username,u.display_name,u.nickname_color,u.role AS author_role FROM messages m JOIN users u ON u.id=m.user_id WHERE m.channel_id=?"
            if around is None:
                rows = list(conn.execute(select + " ORDER BY m.id DESC LIMIT 50", (channel_id,)))[::-1]
            else:
                rows = list(conn.execute(select + " AND m.id<? ORDER BY m.id DESC LIMIT 20", (channel_id, around)))[::-1]
                rows += list(conn.execute(select + " AND m.id>=? ORDER BY m.id LIMIT 30", (channel_id, around)))
            oldest = rows[0]["id"] if rows else 0
            newest = rows[-1]["id"] if rows else 0
            return {"messages": [public_message(row, conn) for row in rows], "read_id": position,
                    "has_older": bool(conn.execute("SELECT 1 FROM messages WHERE channel_id=? AND id<? LIMIT 1", (channel_id, oldest)).fetchone()),
                    "has_newer": bool(conn.execute("SELECT 1 FROM messages WHERE channel_id=? AND id>? LIMIT 1", (channel_id, newest)).fetchone())}

    @app.get("/api/channels/{channel_id}/messages")
    def history(channel_id: int, request: Request, before: int = 9223372036854775807, after: int | None = None):
        auth(request)
        with db() as conn:
            if after is not None:
                return [public_message(r, conn) for r in conn.execute("SELECT messages.*,users.username,users.display_name,users.nickname_color,users.role AS author_role FROM messages JOIN users ON users.id=user_id WHERE channel_id=? AND messages.id>? ORDER BY messages.id LIMIT 50", (channel_id, after))]
            return [public_message(r, conn) for r in conn.execute("SELECT messages.*, users.username, users.display_name, users.nickname_color,users.role AS author_role FROM messages JOIN users ON users.id=user_id WHERE channel_id=? AND messages.id<? ORDER BY messages.id DESC LIMIT 50", (channel_id, before))][::-1]

    @app.post("/api/channels/{channel_id}/messages")
    def send(channel_id: int, body: Message, request: Request):
        member = auth(request)
        if not body.content.strip() and not body.attachments:
            raise HTTPException(422, "Message cannot be blank")
        with message_lock:
            with db() as conn:
                if not conn.execute("SELECT 1 FROM channels WHERE id=? AND kind='text'", (channel_id,)).fetchone():
                    raise HTTPException(404, "Text channel not found")
                conn.execute("BEGIN IMMEDIATE")
                require_active(conn, member)
                inserted = not conn.execute("SELECT 1 FROM messages WHERE user_id=? AND nonce=?", (member["id"], body.nonce)).fetchone()
                if inserted:
                    if body.attachments and not saved_settings.uploads_enabled:
                        raise HTTPException(403, "File uploads are disabled")
                    for attachment_id in body.attachments:
                        file = conn.execute("SELECT owner_id,message_id FROM attachments WHERE id=?", (attachment_id,)).fetchone()
                        if not file or file["owner_id"] != member["id"] or file["message_id"] is not None:
                            raise HTTPException(422, "Attachments must be your own unsent uploads")
                    next_id = conn.execute("UPDATE message_sequence SET value=value+1 WHERE id=1 RETURNING value").fetchone()[0]
                    conn.execute("INSERT INTO messages(id,channel_id,user_id,content,nonce,created) VALUES(?,?,?,?,?,?)", (next_id, channel_id, member["id"], body.content.strip(), body.nonce, time.time()))
                    conn.executemany("UPDATE attachments SET message_id=? WHERE id=?", [(next_id, attachment_id) for attachment_id in body.attachments])
                row = conn.execute("SELECT messages.*,users.username,users.display_name,users.nickname_color,users.role AS author_role FROM messages JOIN users ON users.id=user_id WHERE user_id=? AND nonce=?", (member["id"], body.nonce)).fetchone()
                if row["channel_id"] != channel_id:
                    raise HTTPException(409, "This send identifier belongs to another channel")
                if inserted:
                    sync_mentions(conn, row["id"], row["content"])
                result = public_message(row, conn)
            if inserted:
                hub.publish({"type": "message", "message": result})
        return result

    @app.put("/api/messages/{message_id}")
    def edit_message(message_id: int, body: MessageEdit, request: Request):
        member = auth(request)
        content = body.content.strip()
        with message_lock:
            with db() as conn:
                conn.execute("BEGIN IMMEDIATE")
                require_active(conn, member)
                row = message_row(conn, message_id)
                if not content and not conn.execute("SELECT 1 FROM attachments WHERE message_id=?", (message_id,)).fetchone():
                    raise HTTPException(422, "Message cannot be blank")
                if row["user_id"] != member["id"]:
                    raise HTTPException(403, "Only the author can edit a message")
                if row["deleted"] is not None:
                    raise HTTPException(409, "Deleted messages cannot be edited")
                if row["version"] != body.version:
                    raise HTTPException(409, "Message changed; reload before editing")
                if content != row["content"]:
                    changed = time.time()
                    conn.execute("INSERT INTO message_versions VALUES(?,?,?,?,?,?)", (message_id, row["version"], row["content"], changed, member["id"], "edit"))
                    conn.execute("UPDATE messages SET content=?,edited=?,version=version+1 WHERE id=?", (content, changed, message_id))
                    sync_mentions(conn, message_id, content)
                result = public_message(message_row(conn, message_id), conn)
            hub.publish({"type": "message_update", "message": result})
        return result

    @app.delete("/api/messages/{message_id}")
    def delete_message(message_id: int, request: Request):
        member = auth(request)
        with message_lock:
            with db() as conn:
                conn.execute("BEGIN IMMEDIATE")
                actor = require_active(conn, member)
                row = message_row(conn, message_id)
                if row["user_id"] != member["id"]:
                    author = conn.execute("SELECT role FROM users WHERE id=?", (row["user_id"],)).fetchone()
                    if not actor["admin"] and not (actor["moderator"] and author["role"] in ("user", "banned")):
                        raise HTTPException(403, "Your role cannot delete this message")
                if row["deleted"] is None:
                    changed = time.time()
                    conn.execute("INSERT INTO message_versions VALUES(?,?,?,?,?,?)", (message_id, row["version"], row["content"], changed, member["id"], "delete"))
                    conn.execute("UPDATE messages SET deleted=?,deleted_by=?,version=version+1 WHERE id=?", (changed, member["id"], message_id))
                    sync_mentions(conn, message_id, "")
                    audit(conn, member, "message.delete", message_id, "channel #" + str(row["channel_id"]))
                result = public_message(message_row(conn, message_id), conn)
            hub.publish({"type": "message_update", "message": result})
        return result

    @app.get("/api/admin/messages/{message_id}/history")
    def message_audit(message_id: int, request: Request, before: int = 9223372036854775807):
        admin(request)
        with db() as conn:
            current = dict(message_row(conn, message_id))
            current.pop("nonce")
            current["attachments"] = [attachment_info(file) for file in conn.execute("SELECT * FROM attachments WHERE message_id=? ORDER BY id", (message_id,))]
            versions = [dict(row) for row in conn.execute("SELECT v.*,u.username AS actor_username FROM message_versions v LEFT JOIN users u ON u.id=v.actor_id WHERE message_id=? AND version<? ORDER BY version DESC LIMIT 50", (message_id, before))]
        return {"current": current, "versions": versions,
                "next_before": versions[-1]["version"] if len(versions) == 50 else None}

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
