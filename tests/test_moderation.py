import sqlite3

from fastapi.testclient import TestClient

from lanmsg.server import create_app
from tests.test_server import register

HEADERS = {'X-LANmsg': '1'}


def setup_members(owner, moderator, guest):
    register(owner)
    for client, name in ((moderator, 'moderator'), (guest, 'guest')):
        code = owner.post('/api/admin/invites', headers=HEADERS).json()['invite']
        register(client, name, code)
    owner.put('/api/admin/members/2/role', headers=HEADERS, json={'admin': True})


def test_delegated_admin_permissions_and_owner_protection(tmp_path):
    app = create_app(tmp_path / 'chat.db')
    with TestClient(app) as owner, TestClient(app) as moderator, TestClient(app) as guest:
        setup_members(owner, moderator, guest)
        assert moderator.get('/api/me').json()['admin'] == 1
        assert moderator.get('/api/admin/console').status_code == 200
        assert moderator.get('/api/admin/members').status_code == 200
        assert guest.get('/api/admin/members').status_code == 403
        assert guest.get('/api/admin/audit').status_code == 403
        assert moderator.put('/api/admin/members/3/role', headers=HEADERS, json={'admin': True}).status_code == 403
        for target in (1, 2):
            assert moderator.put(f'/api/admin/members/{target}/ban', headers=HEADERS, json={'banned': True}).status_code == 403
        assert owner.put('/api/admin/members/1/role', headers=HEADERS, json={'admin': False}).status_code == 403
        channel = moderator.post('/api/admin/channels', headers=HEADERS, json={'name': 'moderated'})
        assert channel.status_code == 200
        text = guest.post('/api/channels/1/messages', headers=HEADERS, json={'content': 'Retained content', 'nonce': 'test'}).json()
        assert moderator.delete(f"/api/messages/{text['id']}", headers=HEADERS).status_code == 200
        assert moderator.get(f"/api/admin/messages/{text['id']}/history").json()['current']['content'] == 'Retained content'
        entries = moderator.get('/api/admin/audit').json()['entries']
        assert any(entry['action'] == 'message.delete' and entry['actor_username'] == 'moderator' for entry in entries)
        assert all('Retained content' not in entry['detail'] for entry in entries)
        owner.put('/api/admin/members/2/role', headers=HEADERS, json={'admin': False})
        assert moderator.get('/api/admin/console').status_code == 403
        assert moderator.post('/api/admin/channels', headers=HEADERS, json={'name': 'forbidden'}).status_code == 403


def test_bans_revoke_sessions_block_login_and_preserve_messages(tmp_path):
    path = tmp_path / 'chat.db'
    app = create_app(path)
    with TestClient(app) as owner, TestClient(app) as moderator, TestClient(app) as guest:
        setup_members(owner, moderator, guest)
        message = guest.post('/api/channels/1/messages', headers=HEADERS, json={'content': 'Historical message', 'nonce': 'history'}).json()
        with guest.websocket_connect('/ws', headers={'origin': 'http://testserver'}) as socket:
            result = moderator.put('/api/admin/members/3/ban', headers=HEADERS, json={'banned': True, 'reason': 'Repeated spam'})
            assert result.status_code == 200
            assert socket.receive()['type'] == 'websocket.close'
        assert guest.get('/api/me').status_code == 401
        assert guest.post('/api/channels/1/messages', headers=HEADERS, json={'content': 'Blocked', 'nonce': 'blocked'}).status_code == 401
        assert guest.post('/api/login', headers=HEADERS, json={'username': 'guest', 'password': 'a long test password'}).status_code == 403
        assert all(row['id'] != 3 for row in owner.get('/api/users').json())
        assert owner.get('/api/channels/1/messages').json()[0]['id'] == message['id']
        assert owner.put('/api/admin/members/3/role', headers=HEADERS, json={'admin': True}).status_code == 409
        assert moderator.put('/api/admin/members/3/ban', headers=HEADERS, json={'banned': False}).status_code == 200
        assert guest.get('/api/me').status_code == 401
        assert guest.post('/api/login', headers=HEADERS, json={'username': 'guest', 'password': 'a long test password'}).status_code == 200
        with sqlite3.connect(path) as conn:
            assert conn.execute('SELECT ban_reason FROM users WHERE id=3').fetchone()[0] == ''
    with TestClient(create_app(path)) as owner:
        owner.post('/api/login', headers=HEADERS, json={'username': 'owner', 'password': 'a long test password'})
        entries = owner.get('/api/admin/audit').json()['entries']
        assert any(row['action'] == 'member.ban' and row['detail'] == 'user -> banned: Repeated spam' for row in entries)


def test_admin_cannot_moderate_other_admins_and_audit_pagination(tmp_path):
    app = create_app(tmp_path / 'chat.db')
    with TestClient(app) as owner, TestClient(app) as moderator, TestClient(app) as guest:
        setup_members(owner, moderator, guest)
        owner.put('/api/admin/members/3/role', headers=HEADERS, json={'admin': True})
        assert moderator.put('/api/admin/members/3/ban', headers=HEADERS, json={'banned': True}).status_code == 403
        assert owner.put('/api/admin/members/3/ban', headers=HEADERS, json={'banned': True}).status_code == 200
    # Many events should page without dumping an unbounded audit history.
    with sqlite3.connect(tmp_path / 'chat.db') as conn:
        conn.executemany("INSERT INTO audit_log(created,actor_id,action,detail) VALUES(0,1,'test','')", [() for _ in range(55)])
    with TestClient(create_app(tmp_path / 'chat.db')) as owner:
        owner.post('/api/login', headers=HEADERS, json={'username': 'owner', 'password': 'a long test password'})
        first = owner.get('/api/admin/audit').json()
        assert len(first['entries']) == 50 and first['next_before'] is not None
        second = owner.get('/api/admin/audit', params={'before': first['next_before']}).json()
        assert second['entries'] and second['next_before'] is None
        assert {row['id'] for row in first['entries']}.isdisjoint(row['id'] for row in second['entries'])
