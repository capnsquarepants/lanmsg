import sqlite3

from fastapi.testclient import TestClient

from lanmsg.server import create_app
from lanmsg.reliability import Limits
from tests.test_moderation import HEADERS, setup_members


def change(client, target, role):
    return client.put(f'/api/admin/members/{target}/role', headers=HEADERS, json={'role': role})


def test_global_moderator_permissions(tmp_path):
    with TestClient(create_app(tmp_path / 'chat.db', limits=Limits(write_burst=100))) as root, TestClient(root.app) as mod, TestClient(root.app) as user:
        setup_members(root, mod, user)
        assert change(root, 2, 'moderator').status_code == 200
        assert mod.get('/api/me').json()['role'] == 'moderator'
        console = mod.get('/api/admin/console').json()
        assert 'settings' not in console and console['invites'] == []
        assert mod.get('/api/admin/members').status_code == 200
        assert mod.get('/api/admin/audit').status_code == 200
        assert mod.post('/api/admin/invites', headers=HEADERS).status_code == 403
        assert mod.post('/api/admin/channels', headers=HEADERS, json={'name': 'forbidden'}).status_code == 403
        for target, role in [(1, 'user'), (2, 'user'), (3, 'admin'), (3, 'moderator')]:
            assert change(mod, target, role).status_code == 403
        channel = root.post('/api/admin/channels', headers=HEADERS, json={'name': 'other'}).json()['id']
        for channel_id in (1, channel):
            message = user.post(f'/api/channels/{channel_id}/messages', headers=HEADERS, json={'content': 'spam', 'nonce': str(channel_id)}).json()
            assert mod.delete(f"/api/messages/{message['id']}", headers=HEADERS).status_code == 200
            assert mod.get(f"/api/admin/messages/{message['id']}/history").status_code == 403
        message = root.post('/api/channels/1/messages', headers=HEADERS, json={'content': 'admin', 'nonce': 'admin'}).json()
        assert mod.delete(f"/api/messages/{message['id']}", headers=HEADERS).status_code == 403
        assert change(mod, 3, 'banned').status_code == 200
        assert user.get('/api/me').status_code == 401
        assert change(mod, 3, 'user').status_code == 200


def test_transfer_protects_new_owner_and_requires_password(tmp_path):
    path = tmp_path / 'chat.db'
    with TestClient(create_app(path, limits=Limits(write_burst=100))) as root, TestClient(root.app) as admin, TestClient(root.app) as user:
        setup_members(root, admin, user)
        for role in ('admin', 'moderator', 'user', 'banned'):
            assert change(root, 1, role).status_code == 403
        def transfer(client, target, password='a long test password'):
            return client.post('/api/admin/ownership', headers=HEADERS, json={'user_id': target, 'password': password})
        assert transfer(admin, 3).status_code == 403
        assert transfer(root, 2, 'incorrect password').status_code == 403
        assert transfer(root, 3).status_code == 409
        assert transfer(root, 1).status_code == 409
        assert root.get('/api/me').json()['owner'] == 1
        assert transfer(root, 2).status_code == 200
        assert root.get('/api/me').json()['owner'] == 0
        assert root.get('/api/me').json()['role'] == 'admin'
        assert admin.get('/api/me').json()['owner'] == 1
        assert transfer(root, 3).status_code == 403
        assert change(root, 2, 'banned').status_code == 403
        assert change(admin, 1, 'user').status_code == 200
        assert root.get('/api/admin/console').status_code == 403
        assert any(row['action'] == 'ownership.transfer' for row in admin.get('/api/admin/audit').json()['entries'])
    with sqlite3.connect(path) as conn:
        assert conn.execute('SELECT id FROM users WHERE owner=1').fetchall() == [(2,)]
    with TestClient(create_app(path)) as admin:
        admin.post('/api/login', headers=HEADERS, json={'username': 'moderator', 'password': 'a long test password'})
        assert admin.get('/api/me').json()['owner'] == 1


def test_banned_staff_unban_as_user_and_role_validation(tmp_path):
    path = tmp_path / 'chat.db'
    with TestClient(create_app(path)) as root, TestClient(root.app) as admin, TestClient(root.app) as user:
        setup_members(root, admin, user)
        assert change(root, 2, 'banned').status_code == 200
        assert change(root, 2, 'admin').status_code == 409
        with sqlite3.connect(path) as conn:
            assert conn.execute('SELECT role,admin,banned FROM users WHERE id=2').fetchone() == ('banned', 0, 1)
        assert change(root, 2, 'user').status_code == 200
        admin.post('/api/login', headers=HEADERS, json={'username': 'moderator', 'password': 'a long test password'})
        assert admin.get('/api/me').json()['role'] == 'user'
        assert admin.get('/api/admin/console').status_code == 403
        assert change(root, 3, 'superadmin').status_code == 422
        assert root.put('/api/admin/members/3/role', headers=HEADERS, json={'role': 'user', 'admin': True}).status_code == 422


def test_legacy_roles_migrate(tmp_path):
    path = tmp_path / 'chat.db'
    with TestClient(create_app(path)) as root, TestClient(root.app) as admin, TestClient(root.app) as user:
        setup_members(root, admin, user)
    with sqlite3.connect(path) as conn:
        conn.execute('ALTER TABLE users DROP COLUMN role')
        conn.execute('UPDATE users SET banned=1 WHERE id=2')
    with TestClient(create_app(path)) as root:
        root.post('/api/login', headers=HEADERS, json={'username': 'owner', 'password': 'a long test password'})
        assert [row['role'] for row in sorted(root.get('/api/admin/members').json(), key=lambda row: row['id'])] == ['admin', 'banned', 'user']
    with sqlite3.connect(path) as conn:
        assert conn.execute('SELECT admin FROM users WHERE id=2').fetchone()[0] == 0
