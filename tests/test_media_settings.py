from fastapi.testclient import TestClient

from lanmsg.server import create_app
from tests.test_server import register

HEADERS = {'X-LANmsg':'1'}


def test_server_default_and_independent_user_overrides(tmp_path):
    path = tmp_path/'chat.db'
    with TestClient(create_app(path)) as owner, TestClient(owner.app) as guest:
        register(owner)
        token = owner.post('/api/admin/invites', headers=HEADERS).json()['invite']
        register(guest, 'guest', token)
        settings = owner.get('/api/admin/console').json()['settings']
        assert owner.get('/api/status').json()['media_previews'] is False
        profile = guest.get('/api/me/profile').json()
        payload = {key:profile[key] for key in ('display_name','bio','nickname_color','media_previews')}
        assert payload['media_previews'] is None
        # A disabled server default still permits an explicit user opt-in.
        assert guest.put('/api/me/profile', headers=HEADERS, json={**payload,'media_previews':True}).status_code == 200
        assert guest.get('/api/me/profile').json()['media_previews'] is True
        assert guest.put('/api/admin/settings', headers=HEADERS, json={**settings,'media_previews':True}).status_code == 403
        with guest.websocket_connect('/ws', headers={'origin':'http://testserver'}) as socket:
            saved = owner.put('/api/admin/settings', headers=HEADERS, json={**settings,'media_previews':True}).json()
            assert saved['restart_required'] is False
            assert socket.receive_json()['settings']['media_previews'] is True
        # Explicit opt-out and inheritance are separate states.
        guest.put('/api/me/profile', headers=HEADERS, json={**payload,'media_previews':False})
        assert guest.get('/api/me/profile').json()['media_previews'] is False
        guest.put('/api/me/profile', headers=HEADERS, json=payload)
        assert guest.get('/api/me/profile').json()['media_previews'] is None
        assert 'media_previews' not in owner.get('/api/users/2/profile').json()
    with TestClient(create_app(path)) as guest:
        assert guest.get('/api/status').json()['media_previews'] is True
        guest.post('/api/login', headers=HEADERS, json={'username':'guest','password':'a long test password'})
        assert guest.get('/api/me/profile').json()['media_previews'] is None
