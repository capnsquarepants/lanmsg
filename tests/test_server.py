import sqlite3

from fastapi.testclient import TestClient
from lanmsg.server import create_app


def register(client, username="owner", invite=""):
    return client.post('/api/register', headers={'X-LANmsg':'1'}, json={'username':username,'password':'a long test password','invite':invite})


def test_accounts_invites_and_permissions(tmp_path):
    app = create_app(tmp_path / 'chat.db')
    with TestClient(app) as owner, TestClient(app) as guest:
        assert register(owner).json()['owner'] == 1
        assert register(guest, 'guest').status_code == 403
        token = owner.post('/api/admin/invites', headers={'X-LANmsg':'1'}).json()['invite']
        assert register(guest, 'guest', token).status_code == 200
        assert register(guest, 'other', token).status_code == 403
        assert guest.post('/api/admin/invites', headers={'X-LANmsg':'1'}).status_code == 403
        assert owner.post('/api/admin/invites').status_code == 403
        assert guest.post('/api/session/logout', headers={'X-LANmsg':'1'}).status_code == 200
        assert guest.get('/api/me').status_code == 401


def test_messages_retries_history_and_websocket(tmp_path):
    app = create_app(tmp_path / 'chat.db')
    with TestClient(app) as client:
        register(client)
        with client.websocket_connect('/ws', headers={'origin':'http://testserver'}) as socket:
            payload = {'content':'hello <script>', 'nonce':'same-send'}
            first = client.post('/api/channels/1/messages', headers={'X-LANmsg':'1'}, json=payload).json()
            assert socket.receive_json()['message']['id'] == first['id']
            second = client.post('/api/channels/1/messages', headers={'X-LANmsg':'1'}, json=payload).json()
            assert first['id'] == second['id']
            assert len(client.get('/api/channels/1/messages').json()) == 1
            assert client.get(f"/api/channels/1/messages?before={first['id']}").json() == []
            assert client.post('/api/channels/99/messages', headers={'X-LANmsg':'1'}, json=payload).status_code == 404
    with TestClient(create_app(tmp_path / 'chat.db')) as client:
        assert client.get('/api/channels').status_code == 401
        assert client.post('/api/login', headers={'X-LANmsg':'1'}, json={'username':'owner','password':'a long test password'}).status_code == 200
        assert client.get('/api/channels/1/messages').json()[0]['content'] == 'hello <script>'


def test_appearance_permissions_validation_and_live_updates(tmp_path):
    app = create_app(tmp_path / 'chat.db')
    headers = {'X-LANmsg':'1'}
    with TestClient(app) as owner, TestClient(app) as guest:
        defaults = guest.get('/api/appearance').json()
        assert defaults['font'] == 'mono'
        assert guest.put('/api/admin/appearance', headers=headers, json=defaults).status_code == 401
        register(owner)
        token = owner.post('/api/admin/invites', headers=headers).json()['invite']
        register(guest, 'guest', token)
        assert guest.put('/api/admin/appearance', headers=headers, json=defaults).status_code == 403
        assert owner.put('/api/admin/appearance', json=defaults).status_code == 403
        for invalid in ({'accent':'url(https://example.com)'}, {'font_size':100}, {'line_height':0}, {'font':'arbitrary'}, {'custom_css':'body {display:none}'}, {'row_spacing':-1}):
            assert owner.put('/api/admin/appearance', headers=headers, json={**defaults, **invalid}).status_code == 422
        assert guest.get('/api/appearance').json() == defaults
        updated = {**defaults, 'accent':'#abcdef', 'font':'sans', 'font_size':16, 'row_spacing':1}
        with guest.websocket_connect('/ws', headers={'origin':'http://testserver'}) as socket:
            assert owner.put('/api/admin/appearance', headers=headers, json=updated).json() == updated
            assert socket.receive_json() == {'type':'appearance', 'appearance':updated}
        assert guest.get('/api/appearance').json() == updated
    # Saved settings survive an application restart and are available before sign-in.
    with TestClient(create_app(tmp_path / 'chat.db')) as client:
        assert client.get('/api/appearance').json() == updated


def test_appearance_reset(tmp_path):
    with TestClient(create_app(tmp_path / 'chat.db')) as client:
        register(client)
        defaults = client.get('/api/appearance').json()
        headers = {'X-LANmsg':'1'}
        client.put('/api/admin/appearance', headers=headers, json={**defaults,'background':'#ffffff'})
        assert client.put('/api/admin/appearance', headers=headers, json=defaults).json() == defaults


def test_display_names_preserve_identity_and_update_history(tmp_path):
    app = create_app(tmp_path / 'chat.db')
    headers = {'X-LANmsg':'1'}
    with TestClient(app) as owner, TestClient(app) as guest:
        initial = register(owner).json()
        assert initial['display_name'] == 'owner'
        token = owner.post('/api/admin/invites', headers=headers).json()['invite']
        guest_id = register(guest, 'guest', token).json()['id']
        message = owner.post('/api/channels/1/messages', headers=headers, json={'content':'before rename','nonce':'original'}).json()
        with guest.websocket_connect('/ws', headers={'origin':'http://testserver'}) as socket:
            updated = owner.put('/api/me', headers=headers, json={'display_name':'  Alex  '}).json()
            assert updated == {**initial, 'display_name':'Alex'}
            event = socket.receive_json()
            assert event == {'type':'profile','profile':{'id':initial['id'],'username':'owner','display_name':'Alex'}}
        # Names need not be unique, while handles remain unique and messages keep their IDs.
        assert guest.put('/api/me', headers=headers, json={'display_name':'Alex'}).status_code == 200
        history = guest.get('/api/channels/1/messages').json()[0]
        assert history['id'] == message['id'] and history['user_id'] == initial['id']
        assert history['display_name'] == 'Alex' and history['username'] == 'owner'
        public = guest.get(f"/api/users/{initial['id']}").json()
        assert set(public) == {'id','username','display_name'}
        assert public['display_name'] == 'Alex'
        assert owner.get(f'/api/users/{guest_id}').json()['display_name'] == 'Alex'
        assert guest.put('/api/me', headers=headers, json={'display_name':'Other','id':initial['id']}).status_code == 422
        assert owner.get('/api/me').json()['display_name'] == 'Alex'
        assert guest.put('/api/me', headers=headers, json={'display_name':'Other','username':'owner'}).status_code == 422
        assert owner.get('/api/users/999').status_code == 404
        owner.post('/api/session/logout', headers=headers)
        assert owner.post('/api/login', headers=headers, json={'username':'Alex','password':'a long test password'}).status_code == 401
        assert owner.post('/api/login', headers=headers, json={'username':'owner','password':'a long test password'}).json()['id'] == initial['id']
    with TestClient(create_app(tmp_path / 'chat.db')) as client:
        assert client.post('/api/login', headers=headers, json={'username':'owner','password':'a long test password'}).json()['display_name'] == 'Alex'


def test_display_name_validation_and_authentication(tmp_path):
    headers = {'X-LANmsg':'1'}
    with TestClient(create_app(tmp_path / 'chat.db')) as client:
        assert client.put('/api/me', headers=headers, json={'display_name':'Alex'}).status_code == 401
        assert client.get('/api/users/1').status_code == 401
        register(client)
        assert client.put('/api/me', json={'display_name':'Alex'}).status_code == 403
        for value in ('', '   ', 'a'*33, 'Alex\nAdmin', 'Alex\u202e', '\u200b'):
            assert client.put('/api/me', headers=headers, json={'display_name':value}).status_code == 422
        assert client.get('/api/me').json()['display_name'] == 'owner'
        assert client.put('/api/me', headers=headers, json={'display_name':'Zoë 陈'}).json()['display_name'] == 'Zoë 陈'


def test_legacy_database_display_name_migration(tmp_path):
    path = tmp_path / 'legacy.db'
    headers = {'X-LANmsg':'1'}
    with TestClient(create_app(path)) as client:
        original = register(client).json()
        message = client.post('/api/channels/1/messages', headers=headers, json={'content':'keep this message','nonce':'legacy'}).json()
    # Recreate the users schema shipped before display names existed.
    with sqlite3.connect(path) as conn:
        conn.execute('ALTER TABLE users DROP COLUMN display_name')
    for _ in range(2):
        with TestClient(create_app(path)) as client:
            login = client.post('/api/login', headers=headers, json={'username':'owner','password':'a long test password'}).json()
            assert login == original
            assert client.get('/api/channels/1/messages').json()[0] == message
