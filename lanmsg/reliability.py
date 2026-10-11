"""Bounded request admission, rate limiting, and WebSocket delivery."""

import asyncio
import threading
import time
from dataclasses import dataclass

from fastapi import HTTPException
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

@dataclass(frozen=True)
class Limits:
    http_concurrency: int = 32
    auth_concurrency: int = 2
    body_bytes: int = 32768
    attachment_bytes: int = 5 * 1024 * 1024
    connections: int = 100
    connections_per_session: int = 3
    connections_per_ip: int = 20
    queue_size: int = 64
    send_timeout: float = 5
    idle_timeout: float = 60
    api_rate: float = 20
    api_burst: int = 100
    auth_rate: float = 1 / 12
    auth_burst: int = 5
    message_rate: float = 2
    message_burst: int = 10
    write_rate: float = 1
    write_burst: int = 10
    rate_keys: int = 4096
    cleanup_interval: float = 3600
    minimum_free_bytes: int = 50 * 1024 * 1024
    retention_days: int = 0


class RateLimiter:
    def __init__(self, max_keys, clock=time.monotonic):
        self.max_keys, self.clock = max_keys, clock
        self.entries = {}
        self.lock = threading.Lock()

    def allow(self, key, rate, burst):
        now = self.clock()
        with self.lock:
            if key not in self.entries and len(self.entries) >= self.max_keys:
                # Expire idle buckets, but never evict active limits to admit new keys.
                self.entries = {k: v for k, v in self.entries.items() if now - v[1] < 300}
                if len(self.entries) >= self.max_keys:
                    return False
            tokens, previous = self.entries.get(key, (burst, now))
            tokens = min(burst, tokens + max(0, now - previous) * rate)
            allowed = tokens >= 1
            self.entries[key] = (tokens - 1 if allowed else tokens, now)
            return allowed


class AdmissionMiddleware:
    def __init__(self, app, limits, limiter):
        self.app, self.limits, self.limiter = app, limits, limiter
        self.active = 0
        self.auth_active = 0

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith("/api/"):
            await self.app(scope, receive, send)
            return
        headers = dict(scope["headers"])
        body_limit = self.limits.attachment_bytes if scope["path"] == "/api/attachments" and scope["method"] == "POST" else self.limits.body_bytes
        length = headers.get(b"content-length", b"0")
        try:
            oversized = int(length) > body_limit
        except ValueError:
            await JSONResponse({"detail": "Invalid Content-Length"}, 400)(scope, receive, send)
            return
        ip = (scope.get("client") or ("unknown",))[0]
        auth = scope["path"] in ("/api/login", "/api/register")
        group, rate, burst = ("auth", self.limits.auth_rate, self.limits.auth_burst) if auth else ("api", self.limits.api_rate, self.limits.api_burst)
        if oversized:
            response = JSONResponse({"detail": "Request body is too large"}, 413)
        elif self.active >= self.limits.http_concurrency or (auth and self.auth_active >= self.limits.auth_concurrency):
            response = JSONResponse({"detail": "Server is busy; retry shortly"}, 503, headers={"Retry-After": "1"})
        elif not self.limiter.allow((group, ip), rate, burst):
            response = JSONResponse({"detail": "Too many requests; retry shortly"}, 429, headers={"Retry-After": str(max(1, int(1 / rate) + 1))})
        else:
            response = None
        if response:
            await response(scope, receive, send)
            return
        self.active += 1
        self.auth_active += int(auth)
        received = 0
        async def bounded_receive():
            nonlocal received
            try:
                message = await asyncio.wait_for(receive(), 10)
            except asyncio.TimeoutError:
                raise HTTPException(408, "Request body timed out")
            received += len(message.get("body", b""))
            if received > body_limit:
                raise HTTPException(413, "Request body is too large")
            return message
        try:
            await self.app(scope, bounded_receive, send)
        finally:
            self.active -= 1
            self.auth_active -= int(auth)


class Hub:
    """Each client has one bounded queue and one sender; slow clients are disconnected."""

    def __init__(self, limits, authenticate):
        self.limits, self.authenticate = limits, authenticate
        self.peers = {}
        self.closers = set()
        self.loop = None
        self.pending = 0

    def start(self):
        self.loop = asyncio.get_running_loop()

    def publish(self, event):
        self.loop.call_soon_threadsafe(self.broadcast, event)

    def broadcast(self, event):
        for socket, peer in list(self.peers.items()):
            if peer.get("closing"):
                continue
            try:
                peer["queue"].put_nowait(event)
            except asyncio.QueueFull:
                self.drop(socket)

    def drop(self, socket):
        peer = self.peers.get(socket)
        if not peer or peer.get("closing"):
            return
        peer["closing"] = True
        if peer.get("sender"):
            peer["sender"].cancel()
        # This also closes a sender cancelled before its coroutine ever started.
        task = asyncio.create_task(self.close_socket(socket))
        self.closers.add(task)
        task.add_done_callback(self.closers.discard)

    async def close_socket(self, socket):
        try:
            await asyncio.wait_for(socket.close(code=1013), 1)
        except Exception:
            pass
        finally:
            self.peers.pop(socket, None)

    def revoke(self, token):
        def remove():
            for socket, peer in list(self.peers.items()):
                if peer["token"] == token:
                    self.drop(socket)
        self.loop.call_soon_threadsafe(remove)

    async def connect(self, socket, token, ip):
        if (len(self.peers) >= self.limits.connections
                or sum(p["token"] == token for p in self.peers.values()) >= self.limits.connections_per_session
                or sum(p["ip"] == ip for p in self.peers.values()) >= self.limits.connections_per_ip):
            await socket.close(code=1013)
            return False
        peer = {"token": token, "ip": ip, "queue": asyncio.Queue(self.limits.queue_size)}
        self.peers[socket] = peer
        try:
            await socket.accept()
        except Exception:
            self.peers.pop(socket, None)
            raise
        async def sender():
            try:
                while True:
                    event = await peer["queue"].get()
                    await run_in_threadpool(self.authenticate, token)
                    await asyncio.wait_for(socket.send_json(event), self.limits.send_timeout)
            except (Exception, asyncio.CancelledError):
                pass
            finally:
                if not peer.get("closing"):
                    await self.close_socket(socket)
        peer["sender"] = asyncio.create_task(sender())
        return True

    def reply(self, socket, event):
        peer = self.peers.get(socket)
        if peer and not peer.get("closing"):
            try:
                peer["queue"].put_nowait(event)
            except asyncio.QueueFull:
                self.drop(socket)

    async def close(self):
        tasks = [peer["sender"] for peer in self.peers.values() if peer.get("sender")]
        for socket in list(self.peers):
            self.drop(socket)
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(*list(self.closers), return_exceptions=True)
        self.peers.clear()
