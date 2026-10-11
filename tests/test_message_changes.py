import sqlite3

from fastapi.testclient import TestClient

from lanmsg.reliability import Limits
from lanmsg.server import create_app
from tests.test_server import register

HEADERS = {'X-LANmsg': '1'}


def send(client, nonce='original', content='original secret'):
    return client.post('/api/channels/1/messages', headers=HEADERS,
                       json={'content': content, 'nonce': nonce}).json()


def test_edits_deletes_live_events_and_private_history(tmp_path):
    path = tmp_path/'chat.db'
    with TestClient(create_app(path)) as owner, TestClient(owner.app) as guest:
        register(owner)
        invite = owner.post('/api/admin/invites', headers=HEADERS).json()['invite']
        register(guest, 'guest', invite)
        original = send(guest)
        mid = original['id']
        assert owner.put(f'/api/messages/{mid}', headers=HEADERS, json={'content':'owner edit','version':1}).status_code == 403
        with guest.websocket_connect('/ws', headers={'origin':'http://testserver'}) as socket:
            edited = guest.put(f'/api/messages/{mid}', headers=HEADERS,
                               json={'content':'new text', 'version':1})
            assert edited.status_code == 200
            assert edited.json()['version'] == 2 and edited.json()['edited']
            event = socket.receive_json()
            assert event == {'type':'message_update', 'message':edited.json()}
            assert 'original secret' not in str(event)
            deleted = owner.delete(f'/api/messages/{mid}', headers=HEADERS)
            assert deleted.status_code == 200
            assert deleted.json()['content'] == '' and deleted.json()['deleted']
            event = socket.receive_json()
            assert event['message'] == deleted.json()
            assert 'new text' not in str(event) and 'deleted_by' not in event['message']
        public = guest.get('/api/channels/1/messages').json()[0]
        assert public == deleted.json()
        # Replaying the original send must not resurrect or reveal its retained text.
        assert send(guest) == public
        assert guest.get(f'/api/admin/messages/{mid}/history').status_code == 403
        audit = owner.get(f'/api/admin/messages/{mid}/history').json()
        assert [v['content'] for v in audit['versions']] == ['new text', 'original secret']
        assert [v['action'] for v in audit['versions']] == ['delete', 'edit']
        assert [v['actor_username'] for v in audit['versions']] == ['owner', 'guest']
        assert audit['current']['content'] == 'new text'
        assert owner.delete(f'/api/messages/{mid}', headers=HEADERS).json() == public
        assert len(owner.get(f'/api/admin/messages/{mid}/history').json()['versions']) == 2
    with TestClient(create_app(path)) as owner:
        assert owner.get(f'/api/admin/messages/{mid}/history').status_code == 401
        owner.post('/api/login', headers=HEADERS, json={'username':'owner','password':'a long test password'})
        assert len(owner.get(f'/api/admin/messages/{mid}/history').json()['versions']) == 2
        assert owner.get('/api/channels/1/messages').json()[0]['content'] == ''


def test_message_mutation_permissions_validation_and_conflicts(tmp_path):
    with TestClient(create_app(tmp_path/'chat.db')) as owner, TestClient(owner.app) as guest:
        register(owner)
        invite = owner.post('/api/admin/invites', headers=HEADERS).json()['invite']
        register(guest, 'guest', invite)
        mid = send(owner)['id']
        route = f'/api/messages/{mid}'
        edit = {'content':'changed', 'version':1}
        assert guest.put(route, headers=HEADERS, json=edit).status_code == 403
        assert guest.delete(route, headers=HEADERS).status_code == 403
        assert owner.put(route, json=edit).status_code == 403
        for invalid in ({'content':' ', 'version':1}, {'content':'x'*4001,'version':1}, {**edit,'version':0}, {**edit,'user_id':2}):
            assert owner.put(route, headers=HEADERS, json=invalid).status_code == 422
        assert owner.put(route, headers=HEADERS, json=edit).status_code == 200
        assert owner.put(route, headers=HEADERS, json=edit).status_code == 409
        # Identical edits do not manufacture another version.
        assert owner.put(route, headers=HEADERS, json={**edit,'version':2}).json()['version'] == 2
        assert owner.delete(route, headers=HEADERS).status_code == 200
        assert owner.put(route, headers=HEADERS, json={**edit,'version':3}).status_code == 409
        assert owner.get('/api/admin/messages/999/history').status_code == 404


def test_legacy_message_migration(tmp_path):
    path = tmp_path/'legacy.db'
    with TestClient(create_app(path)) as owner:
        register(owner)
        mid = send(owner)['id']
    with sqlite3.connect(path) as conn:
        conn.execute('DROP TABLE message_versions')
        for column in ('version', 'edited', 'deleted', 'deleted_by'):
            conn.execute(f'ALTER TABLE messages DROP COLUMN {column}')
    for _ in range(2):
        with TestClient(create_app(path)) as owner:
            owner.post('/api/login', headers=HEADERS, json={'username':'owner','password':'a long test password'})
            message = owner.get('/api/channels/1/messages').json()[0]
            assert message['id'] == mid and message['version'] == 1
            assert message['content'] == 'original secret'


def test_retention_purges_retained_versions(tmp_path):
    path = tmp_path/'chat.db'
    with TestClient(create_app(path)) as owner:
        register(owner)
        mid = send(owner)['id']
        owner.put(f'/api/messages/{mid}', headers=HEADERS, json={'content':'edited','version':1})
    with sqlite3.connect(path) as conn:
        conn.execute('UPDATE messages SET created=0')
    with TestClient(create_app(path, Limits(retention_days=1))):
        with sqlite3.connect(path) as conn:
            assert conn.execute('SELECT count(*) FROM messages').fetchone()[0] == 0
            assert conn.execute('SELECT count(*) FROM message_versions').fetchone()[0] == 0


def test_audit_pagination_and_concurrent_edits(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    path = tmp_path/'chat.db'
    with TestClient(create_app(path)) as owner:
        register(owner)
        mid = send(owner)['id']
        def edit(content):
            return owner.put(f'/api/messages/{mid}', headers=HEADERS,
                             json={'content':content, 'version':1}).status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sorted(pool.map(edit, ['first', 'second'])) == [200, 409]
        assert len(owner.get(f'/api/admin/messages/{mid}/history').json()['versions']) == 1
        # Seed a long audit trail to verify bounded, non-overlapping pages.
        with sqlite3.connect(path) as conn:
            conn.executemany('INSERT INTO message_versions VALUES(?,?,?,?,?,?)',
                             [(mid, n, f'version {n}', 123, 1, 'edit') for n in range(2, 62)])
            conn.execute('UPDATE messages SET version=62 WHERE id=?', (mid,))
        first = owner.get(f'/api/admin/messages/{mid}/history').json()
        assert len(first['versions']) == 50 and first['next_before'] == 12
        second = owner.get(f'/api/admin/messages/{mid}/history?before=12').json()
        assert second['next_before'] is None
        versions = [v['version'] for v in first['versions'] + second['versions']]
        assert versions == list(range(61, 0, -1))
