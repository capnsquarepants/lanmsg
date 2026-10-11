from fastapi.testclient import TestClient

from lanmsg.server import create_app
from lanmsg.reliability import Hub
from tests.test_server import register

HEADERS = {"X-LANmsg": "1"}


def test_retry_does_not_send_duplicate_notifications(tmp_path, monkeypatch):
    events = []
    publish = Hub.publish
    def capture(self, event):
        events.append(event)
        publish(self, event)
    monkeypatch.setattr(Hub, 'publish', capture)
    with TestClient(create_app(tmp_path / 'chat.db')) as client:
        register(client)
        payload = {'content': 'Hello @owner', 'nonce': 'retry'}
        first = client.post('/api/channels/1/messages', headers=HEADERS, json=payload)
        second = client.post('/api/channels/1/messages', headers=HEADERS, json=payload)
        assert first.status_code == second.status_code == 200
        assert first.json() == second.json()
        assert len([event for event in events if event['type'] == 'message']) == 1


def test_mentions_match_handles_not_emails_urls_or_display_names(tmp_path):
    with TestClient(create_app(tmp_path / "chat.db")) as client:
        member = register(client).json()
        client.put('/api/me', headers=HEADERS, json={'display_name': 'Alex'})
        assert client.get('/api/users').json() == [{'id': member['id'], 'username': 'owner', 'display_name': 'Alex', 'role': 'admin', 'owner': 1}]
        def send(text, nonce):
            return client.post('/api/channels/1/messages', headers=HEADERS, json={'content': text, 'nonce': nonce}).json()
        message = send('Hello @OWNER, @owner! @Alex @unknown', 'mention')
        assert message['mentions'] == [{'id': member['id'], 'username': 'owner'}]
        assert send('me@owner.test https://example.org/@owner @owner-extra @owner' + 'x'*32, 'ignored')['mentions'] == []
        assert client.get('/api/channels/1/messages').json()[0]['mentions'] == message['mentions']
        edited = client.put(f"/api/messages/{message['id']}", headers=HEADERS, json={'content': 'No mention now', 'version': 1}).json()
        assert edited['mentions'] == []
        deleted = client.delete(f"/api/messages/{message['id']}", headers=HEADERS).json()
        assert deleted['mentions'] == [] and deleted['content'] == ''


def test_notification_preferences_persist_and_stay_private(tmp_path):
    path = tmp_path / 'chat.db'
    with TestClient(create_app(path)) as client:
        assert client.get('/api/users').status_code == 401
        member = register(client).json()
        profile = client.get('/api/me/profile').json()
        assert profile['notifications'] == 'mentions' and profile['desktop_notifications'] is False
        body = {key: profile[key] for key in ('display_name', 'bio', 'nickname_color', 'media_previews', 'notifications', 'desktop_notifications')}
        body.update(notifications='all', desktop_notifications=True)
        saved = client.put('/api/me/profile', headers=HEADERS, json=body)
        assert saved.status_code == 200 and saved.json()['notifications'] == 'all'
        public = client.get(f"/api/users/{member['id']}/profile").json()
        assert 'notifications' not in public and 'desktop_notifications' not in public
        assert 'notifications' not in client.get('/api/users').json()[0]
        assert client.put('/api/me/profile', headers=HEADERS, json={**body, 'notifications': 'everyone'}).status_code == 422
    with TestClient(create_app(path)) as client:
        client.post('/api/login', headers=HEADERS, json={'username': 'owner', 'password': 'a long test password'})
        assert client.get('/api/me/profile').json()['desktop_notifications'] is True
        assert client.get('/api/me/profile').json()['notifications'] == 'all'
