import sqlite3

from fastapi.testclient import TestClient

from lanmsg.reliability import Limits
from lanmsg.server import create_app
from tests.test_server import register

HEADERS = {'X-LANmsg': '1'}


def test_persistent_reads_mentions_context_and_monotonicity(tmp_path):
    path = tmp_path / 'chat.db'
    app = create_app(path, Limits(message_rate=20, message_burst=100))
    with TestClient(app) as owner, TestClient(app) as guest:
        register(owner)
        invite = owner.post('/api/admin/invites', headers=HEADERS).json()['invite']
        register(guest, 'guest', invite)
        posted = []
        for index in range(60):
            response = owner.post('/api/channels/1/messages', headers=HEADERS, json={'content': f'@guest message {index}', 'nonce': str(index)})
            assert response.status_code == 200
            posted.append(response.json())
        state = guest.get('/api/read-state').json()['channels'][0]
        assert state['unread'] == state['mentions'] == 60 and state['first_unread'] == posted[0]['id']
        assert owner.get('/api/read-state').json()['channels'][0]['unread'] == 0
        inbox = guest.get('/api/mentions').json()
        assert len(inbox['messages']) == 50 and inbox['next_before'] is not None
        assert len(guest.get('/api/mentions', params={'before': inbox['next_before']}).json()['messages']) == 10
        view = guest.get('/api/channels/1/view').json()
        assert view['messages'][0]['id'] == posted[0]['id'] and view['has_newer'] is True
        around = guest.get('/api/channels/1/view', params={'around': posted[35]['id']}).json()
        assert any(row['id'] == posted[35]['id'] for row in around['messages'])
        assert around['has_older'] is True
        newer = guest.get('/api/channels/1/messages', params={'after': view['messages'][-1]['id']}).json()
        assert len(newer) == 30
        assert guest.put('/api/channels/1/read', headers=HEADERS, json={'message_id': posted[39]['id']}).status_code == 200
        assert guest.put('/api/channels/1/read', headers=HEADERS, json={'message_id': posted[9]['id']}).json()['read_id'] == posted[39]['id']
        assert guest.get('/api/read-state').json()['channels'][0]['unread'] == 20
        assert len(guest.get('/api/mentions').json()['messages']) == 20
        assert len(guest.get('/api/mentions?unread_only=false').json()['messages']) == 50
        # Edits removing a mention and soft deletes leave no inbox entry or hidden text.
        owner.put(f"/api/messages/{posted[-1]['id']}", headers=HEADERS, json={'content': 'No mention', 'version': 1})
        owner.delete(f"/api/messages/{posted[-2]['id']}", headers=HEADERS)
        state = guest.get('/api/read-state').json()['channels'][0]
        assert state['unread'] == 19 and state['mentions'] == 18
        assert guest.post('/api/read-all', headers=HEADERS).status_code == 200
        assert guest.get('/api/mentions').json()['messages'] == []
    with TestClient(create_app(path)) as guest:
        guest.post('/api/login', headers=HEADERS, json={'username': 'guest', 'password': 'a long test password'})
        assert guest.get('/api/read-state').json()['channels'][0]['unread'] == 0
        assert guest.get('/api/read-state').json()['channels'][0]['read_id'] == posted[-1]['id']


def test_reads_auth_validation_and_message_ids_after_retention(tmp_path):
    path = tmp_path / 'chat.db'
    with TestClient(create_app(path)) as client:
        assert client.get('/api/read-state').status_code == 401
        assert client.get('/api/mentions').status_code == 401
        register(client)
        row = client.post('/api/channels/1/messages', headers=HEADERS, json={'content': '@owner', 'nonce': 'first'}).json()
        assert client.put('/api/channels/1/read', json={'message_id': row['id']}).status_code == 403
        assert client.put('/api/channels/1/read', headers=HEADERS, json={'message_id': row['id']+100}).status_code == 422
        other = client.post('/api/admin/channels', headers=HEADERS, json={'name': 'other'}).json()['id']
        assert client.put(f'/api/channels/{other}/read', headers=HEADERS, json={'message_id': row['id']}).status_code == 422
        assert client.get(f"/api/channels/{other}/view?around={row['id']}").status_code == 404
        client.post('/api/read-all', headers=HEADERS)
        with sqlite3.connect(path) as conn:
            conn.execute('PRAGMA foreign_keys=ON')
            conn.execute('DELETE FROM messages')
        new = client.post('/api/channels/1/messages', headers=HEADERS, json={'content': 'After retention', 'nonce': 'new'}).json()
        assert new['id'] > row['id']
        with sqlite3.connect(path) as conn:
            assert conn.execute('SELECT count(*) FROM message_mentions').fetchone()[0] == 0


def test_upgrade_backfills_mentions_without_marking_old_history_unread(tmp_path):
    path = tmp_path / 'chat.db'
    with TestClient(create_app(path)) as client:
        register(client)
        row = client.post('/api/channels/1/messages', headers=HEADERS, json={'content': 'Before upgrade @owner', 'nonce': 'old'}).json()
    with sqlite3.connect(path) as conn:
        conn.execute('DROP TABLE message_mentions')
        conn.execute("DELETE FROM settings WHERE key='mention_index'")
        conn.execute('ALTER TABLE users DROP COLUMN read_start_id')
    with TestClient(create_app(path)) as client:
        client.post('/api/login', headers=HEADERS, json={'username': 'owner', 'password': 'a long test password'})
        assert client.get('/api/read-state').json()['channels'][0]['read_id'] == row['id']
        assert len(client.get('/api/mentions?unread_only=false').json()['messages']) == 0  # Self mentions never enter inbox.
        assert client.get('/api/channels/1/messages').json()[0]['mentions'][0]['username'] == 'owner'
