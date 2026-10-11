from fastapi.testclient import TestClient

from lanmsg.server import create_app
from tests.test_server import register


HEADERS = {'X-LANmsg':'1'}


def test_channel_names_are_normalized_before_validation(tmp_path):
    with TestClient(create_app(tmp_path/'chat.db')) as owner:
        register(owner)
        result = owner.post('/api/admin/channels',headers=HEADERS,json={'name':'  Another   TEST  '})
        assert result.status_code == 200
        assert any(channel['name'] == 'another-test' for channel in owner.get('/api/channels').json())
        assert owner.post('/api/admin/channels',headers=HEADERS,json={'name':'ANOTHER TEST'}).status_code == 409
        for invalid in (' ', 'bad! name', 'A'*33):
            assert owner.post('/api/admin/channels',headers=HEADERS,json={'name':invalid}).status_code == 422


def test_console_is_owner_only(tmp_path):
    app = create_app(tmp_path/'chat.db')
    with TestClient(app) as owner, TestClient(app) as guest:
        assert guest.get('/api/admin/console').status_code == 401
        register(owner)
        settings = owner.get('/api/admin/console').json()['settings']
        token = owner.post('/api/admin/invites',headers=HEADERS).json()['invite']
        register(guest,'guest',token)
        assert guest.get('/api/admin/console').status_code == 403
        assert guest.put('/api/admin/settings',headers=HEADERS,json=settings).status_code == 403
        assert guest.delete('/api/admin/invites/any',headers=HEADERS).status_code == 403
        assert owner.put('/api/admin/settings',json=settings).status_code == 403
        assert owner.get('/api/admin/console').json()['stats']['users'] == 2


def test_live_identity_registration_and_invite_revocation(tmp_path):
    app = create_app(tmp_path/'chat.db')
    with TestClient(app) as owner, TestClient(app) as guest:
        register(owner)
        settings = owner.get('/api/admin/console').json()['settings']
        invite = owner.post('/api/admin/invites',headers=HEADERS).json()['invite']
        with owner.websocket_connect('/ws',headers={'origin':'http://testserver'}) as socket:
            updated = {**settings,'server_name':'  Friends LAN  ','server_description':'Our chat','registration_enabled':False}
            result = owner.put('/api/admin/settings',headers=HEADERS,json=updated)
            assert result.status_code == 200
            assert result.json()['restart_required'] is False
            event = socket.receive_json()
            assert event == {'type':'server_settings','settings':{'server_name':'Friends LAN','server_description':'Our chat','registration_enabled':False}}
        public = guest.get('/api/status').json()
        assert public['server_name'] == 'Friends LAN'
        assert 'connections_per_ip' not in public
        assert register(guest,'guest',invite).status_code == 403
        owner.put('/api/admin/settings',headers=HEADERS,json=settings)
        assert owner.delete('/api/admin/invites/'+invite,headers=HEADERS).status_code == 200
        assert register(guest,'guest',invite).status_code == 403
        assert owner.get('/api/admin/console').json()['invites'] == []


def test_saved_limits_apply_only_after_restart(tmp_path):
    path = tmp_path/'chat.db'
    with TestClient(create_app(path)) as owner:
        register(owner)
        before = owner.get('/api/admin/console').json()
        updated = {**before['settings'],'connections_per_ip':60,'message_burst':1,'message_rate':.001}
        result = owner.put('/api/admin/settings',headers=HEADERS,json=updated).json()
        assert result['restart_required'] is True
        assert result['effective_limits']['connections_per_ip'] == 20
        assert result['settings']['connections_per_ip'] == 60
        for n in range(2):
            assert owner.post('/api/channels/1/messages',headers=HEADERS,json={'content':'before restart','nonce':f'old{n}'}).status_code == 200
    with TestClient(create_app(path)) as owner:
        assert owner.post('/api/login',headers=HEADERS,json={'username':'owner','password':'a long test password'}).status_code == 200
        after = owner.get('/api/admin/console').json()
        assert after['restart_required'] is False
        assert after['effective_limits']['connections_per_ip'] == 60
        payload = {'content':'after restart','nonce':'after'}
        assert owner.post('/api/channels/1/messages',headers=HEADERS,json=payload).status_code == 200
        assert owner.post('/api/channels/1/messages',headers=HEADERS,json=payload).status_code == 429


def test_invalid_settings_do_not_change_saved_state(tmp_path):
    with TestClient(create_app(tmp_path/'chat.db')) as owner:
        register(owner)
        settings = owner.get('/api/admin/console').json()['settings']
        for invalid in ({'server_name':' '},{'server_name':'Bad\nName'},{'connections':0},{'message_rate':0},{'http_concurrency':9999},{'owner':True}):
            assert owner.put('/api/admin/settings',headers=HEADERS,json={**settings,**invalid}).status_code == 422
        assert owner.get('/api/admin/console').json()['settings'] == settings
