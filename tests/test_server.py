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
