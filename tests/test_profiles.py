import sqlite3

from fastapi.testclient import TestClient

from lanmsg.server import create_app
from tests.test_server import register

HEADERS = {'X-LANmsg':'1'}


def test_profile_management_preferences_and_public_privacy(tmp_path):
    path = tmp_path/'chat.db'
    with TestClient(create_app(path)) as owner, TestClient(owner.app) as guest:
        member = register(owner).json()
        token = owner.post('/api/admin/invites', headers=HEADERS).json()['invite']
        register(guest, 'guest', token)
        initial = owner.get('/api/me/profile').json()
        assert initial['bio'] == '' and initial['nickname_color'] == 'default'
        assert initial['media_previews'] is False
        changes = {'display_name':'Alex','bio':'  Hello\n<script>plain text</script>  ', 'nickname_color':'accent','media_previews':True}
        with guest.websocket_connect('/ws', headers={'origin':'http://testserver'}) as socket:
            result = owner.put('/api/me/profile', headers=HEADERS, json=changes)
            assert result.status_code == 200
            event = socket.receive_json()
            assert event['type'] == 'profile'
            assert 'media_previews' not in event['profile']
            assert event['profile']['bio'] == 'Hello\n<script>plain text</script>'
            assert event['profile']['nickname_color'] == 'accent'
        public = guest.get(f"/api/users/{member['id']}/profile").json()
        assert public == event['profile']
        assert 'media_previews' not in public
        assert owner.get('/api/me/profile').json()['media_previews'] is True
        assert guest.get('/api/me/profile').json()['media_previews'] is False
        message = owner.post('/api/channels/1/messages', headers=HEADERS, json={'content':'Hi','nonce':'profile-color'}).json()
        assert message['nickname_color'] == 'accent'
        assert 'bio' not in message and 'media_previews' not in message
        assert guest.get('/api/channels/1/messages').json()[0]['nickname_color'] == 'accent'
        # Legacy display-name updates preserve the new profile fields and preferences.
        owner.put('/api/me', headers=HEADERS, json={'display_name':'New name'})
        assert owner.get('/api/me/profile').json()['bio'] == public['bio']
        assert owner.get('/api/me/profile').json()['media_previews'] is True
    with TestClient(create_app(path)) as owner:
        owner.post('/api/login', headers=HEADERS, json={'username':'owner','password':'a long test password'})
        saved = owner.get('/api/me/profile').json()
        assert saved['display_name'] == 'New name' and saved['media_previews'] is True
        assert saved['nickname_color'] == 'accent'
        assert owner.put('/api/me/profile', headers=HEADERS, json={**changes,'media_previews':False}).status_code == 200
        assert owner.get('/api/me/profile').json()['media_previews'] is False


def test_profile_validation_and_self_only_updates(tmp_path):
    with TestClient(create_app(tmp_path/'chat.db')) as client:
        body = {'display_name':'Alex','bio':'Hi','nickname_color':'text','media_previews':False}
        assert client.get('/api/me/profile').status_code == 401
        assert client.get('/api/users/1/profile').status_code == 401
        assert client.put('/api/me/profile', headers=HEADERS, json=body).status_code == 401
        register(client)
        before = client.get('/api/me/profile').json()
        assert client.put('/api/me/profile', json=body).status_code == 403
        for invalid in ({'bio':'x'*161}, {'bio':'bad\u202e'}, {'nickname_color':'url(evil)'}, {'id':2}, {'owner':True}, {'display_name':' '}):
            assert client.put('/api/me/profile', headers=HEADERS, json={**body, **invalid}).status_code == 422
        assert client.get('/api/me/profile').json() == before
        assert client.get('/api/users/999/profile').status_code == 404


def test_existing_accounts_receive_profile_defaults(tmp_path):
    path = tmp_path/'legacy.db'
    with TestClient(create_app(path)) as owner:
        member = register(owner).json()
    with sqlite3.connect(path) as conn:
        for column in ('bio','nickname_color','media_previews'):
            conn.execute(f'ALTER TABLE users DROP COLUMN {column}')
    for _ in range(2):
        with TestClient(create_app(path)) as owner:
            owner.post('/api/login', headers=HEADERS, json={'username':'owner','password':'a long test password'})
            profile = owner.get('/api/me/profile').json()
            assert profile['id'] == member['id']
            assert profile['bio'] == '' and profile['nickname_color'] == 'default'
            assert profile['media_previews'] is False
