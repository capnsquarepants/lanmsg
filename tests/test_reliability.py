import asyncio
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from lanmsg.backup import backup_database
from lanmsg.reliability import AdmissionMiddleware, Hub, Limits, RateLimiter
from lanmsg.server import create_app
from tests.test_server import register


def test_rate_limit_refill_and_bounded_keys():
    clock = [0]
    limiter = RateLimiter(1, clock=lambda:clock[0])
    assert limiter.allow('a', 1, 2)
    assert limiter.allow('a', 1, 2)
    assert not limiter.allow('a', 1, 2)
    assert not limiter.allow('b', 1, 2)
    clock[0] = 1
    assert limiter.allow('a', 1, 2)
    clock[0] = 302
    assert limiter.allow('b', 1, 2)
    assert len(limiter.entries) == 1


def test_request_limits_and_duplicate_safe_retries(tmp_path):
    app = create_app(tmp_path/'chat.db', limits=Limits(message_burst=2, message_rate=.001, auth_burst=2))
    headers = {'X-LANmsg':'1'}
    with TestClient(app) as client:
        register(client)
        payload = {'content':'one message','nonce':'retry'}
        first = client.post('/api/channels/1/messages',headers=headers,json=payload)
        assert first.status_code == 200
        assert client.post('/api/channels/1/messages',headers=headers,json=payload).json()['id'] == first.json()['id']
        limited = client.post('/api/channels/1/messages',headers=headers,json=payload)
        assert limited.status_code == 429 and 'retry-after' in limited.headers
        assert len(client.get('/api/channels/1/messages').json()) == 1
        client.post('/api/login',headers=headers,json={'username':'owner','password':'wrong password'})
        assert client.post('/api/login',headers=headers,json={'username':'owner','password':'wrong password'}).status_code == 429
        assert client.put('/api/me',headers=headers,content=b'x'*32769).status_code == 413
        assert client.put('/api/me',headers=headers,content=iter([b'x'*17000,b'x'*17000])).status_code == 413


def test_cleanup_history_index_disk_guard_and_backup(tmp_path):
    path = tmp_path/'chat.db'
    with TestClient(create_app(path)) as client:
        register(client)
        client.post('/api/channels/1/messages',headers={'X-LANmsg':'1'},json={'content':'preserve me','nonce':'backup'})
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO sessions VALUES('expired',1,0)")
        conn.execute("INSERT INTO invites(token,expires) VALUES('expired',0)")
        assert any('messages_channel_history' in row[3] for row in conn.execute('EXPLAIN QUERY PLAN SELECT * FROM messages WHERE channel_id=1 AND id<100 ORDER BY id DESC LIMIT 50'))
    with TestClient(create_app(path, limits=Limits(minimum_free_bytes=10**30))) as client:
        assert client.post('/api/login',headers={'X-LANmsg':'1'},json={'username':'owner','password':'a long test password'}).status_code == 507
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT count(*) FROM sessions WHERE token='expired'").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM invites WHERE token='expired'").fetchone()[0] == 0
    target = backup_database(path,tmp_path/'snapshot.db')
    with sqlite3.connect(target) as conn:
        assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert conn.execute('SELECT content FROM messages').fetchone()[0] == 'preserve me'
    with pytest.raises(FileExistsError):
        backup_database(path,target)
    # Restore by starting a fresh instance on the snapshot.
    with TestClient(create_app(target)) as client:
        assert client.post('/api/login',headers={'X-LANmsg':'1'},json={'username':'owner','password':'a long test password'}).status_code == 200
        assert client.get('/api/channels/1/messages').json()[0]['content'] == 'preserve me'


def test_connection_cap_and_logout_revocation(tmp_path):
    with TestClient(create_app(tmp_path/'chat.db',limits=Limits(connections=1))) as client:
        register(client)
        with client.websocket_connect('/ws',headers={'origin':'http://testserver'}) as first:
            with pytest.raises(WebSocketDisconnect):
                with client.websocket_connect('/ws',headers={'origin':'http://testserver'}):
                    pass
            assert client.post('/api/session/logout',headers={'X-LANmsg':'1'}).status_code == 200
            with pytest.raises(WebSocketDisconnect):
                first.receive_json()


def test_slow_clients_and_queue_overflow_are_disconnected():
    class Socket:
        def __init__(self, slow=False):
            self.slow, self.events, self.closed = slow, [], False
        async def accept(self):
            pass
        async def send_json(self,event):
            if self.slow:
                await asyncio.sleep(100)
            self.events.append(event)
        async def close(self,code):
            self.closed = True
    async def exercise():
        hub = Hub(Limits(send_timeout=.05,queue_size=2), lambda token: {})
        hub.start()
        slow, fast = Socket(True), Socket()
        await hub.connect(slow,'a','ip-a')
        await hub.connect(fast,'b','ip-b')
        hub.broadcast({'type':'message'})
        await asyncio.sleep(.15)
        assert fast.events == [{'type':'message'}]
        assert slow.closed and not fast.closed
        # Fill a queue before its sender gets CPU time; it must still close safely.
        for _ in range(3):
            hub.broadcast({'type':'message'})
        await asyncio.sleep(.05)
        assert fast.closed and not hub.peers
        await hub.close()
    asyncio.run(exercise())


def test_concurrency_admission():
    async def exercise():
        started, release = asyncio.Event(), asyncio.Event()
        async def app(scope, receive, send):
            started.set()
            await release.wait()
        middleware = AdmissionMiddleware(app,Limits(http_concurrency=1),RateLimiter(10))
        scope = {'type':'http','path':'/api/me','headers':[], 'client':('ip',1)}
        sent = []
        async def send(event):
            sent.append(event)
        async def receive():
            return {'type':'http.request','body':b''}
        first = asyncio.create_task(middleware(scope,receive,send))
        await started.wait()
        await middleware(scope,receive,send)
        assert sent[0]['status'] == 503
        release.set()
        await first
        assert middleware.active == 0
    asyncio.run(exercise())


def test_retention_is_opt_in(tmp_path):
    path = tmp_path/'chat.db'
    with TestClient(create_app(path)) as client:
        register(client)
        client.post('/api/channels/1/messages',headers={'X-LANmsg':'1'},json={'content':'old','nonce':'old'})
    with sqlite3.connect(path) as conn:
        conn.execute('UPDATE messages SET created=?',(time.time()-10*86400,))
    create_app(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute('SELECT count(*) FROM messages').fetchone()[0] == 1
    create_app(path,limits=Limits(retention_days=7))
    with sqlite3.connect(path) as conn:
        assert conn.execute('SELECT count(*) FROM messages').fetchone()[0] == 0


def test_database_contention_returns_retryable_error(tmp_path):
    path = tmp_path/'chat.db'
    with TestClient(create_app(path)) as client:
        register(client)
        payload = {'content':'retry after writer releases','nonce':'locked-send'}
        with sqlite3.connect(path) as writer:
            writer.execute('BEGIN IMMEDIATE')
            response = client.post('/api/channels/1/messages',headers={'X-LANmsg':'1'},json=payload)
            assert response.status_code == 503 and response.headers['retry-after'] == '1'
            # Read-only requests remain available under a WAL write lock.
            assert client.get('/api/status').status_code == 200
        assert client.post('/api/channels/1/messages',headers={'X-LANmsg':'1'},json=payload).status_code == 200
        assert len(client.get('/api/channels/1/messages').json()) == 1
