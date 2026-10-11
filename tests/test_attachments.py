import os
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from lanmsg.attachments import attachment_directory, clean_filename, preview_type
from lanmsg.backup import backup_database
from lanmsg.reliability import Limits
from lanmsg.server import create_app
from tests.test_server import register

HEADERS = {'X-LANmsg': '1'}
PNG = b'\x89PNG\r\n\x1a\n' + b'test payload'


def upload(client, filename='photo.png', content=PNG):
    return client.post('/api/attachments', params={'filename': filename}, headers={**HEADERS, 'Content-Type': 'application/octet-stream'}, content=content)


def test_upload_send_download_preferences_and_soft_deletion(tmp_path):
    path = tmp_path / 'chat.db'
    app = create_app(path)
    with TestClient(app) as owner, TestClient(app) as guest:
        register(owner)
        code = owner.post('/api/admin/invites', headers=HEADERS).json()['invite']
        register(guest, 'guest', code)
        attachment = upload(owner).json()
        assert attachment['size'] == len(PNG) and attachment['media_type'] == 'image/png'
        assert owner.get('/api/attachments').json() == [attachment]
        assert guest.get(attachment['url']).status_code == 404  # Unsent files remain private.
        assert guest.delete(attachment['url'], headers=HEADERS).status_code == 404
        assert guest.post('/api/channels/1/messages', headers=HEADERS, json={'nonce': 'steal', 'attachments': [attachment['id']]}).status_code == 422
        payload = {'nonce': 'file-send', 'content': '', 'attachments': [attachment['id']]}
        sent = owner.post('/api/channels/1/messages', headers=HEADERS, json=payload)
        assert sent.status_code == 200
        message = sent.json()
        assert message['content'] == '' and message['attachments'] == [attachment]
        assert owner.get('/api/attachments').json() == []
        assert owner.post('/api/channels/1/messages', headers=HEADERS, json=payload).json() == message
        assert owner.post('/api/channels/1/messages', headers=HEADERS, json={**payload, 'nonce': 'reuse'}).status_code == 422
        download = guest.get(attachment['url'])
        assert download.content == PNG and download.headers['content-type'] == 'application/octet-stream'
        assert download.headers['content-disposition'].startswith('attachment;')
        assert download.headers['x-content-type-options'] == 'nosniff'
        assert guest.get(attachment['url'] + '?inline=true').status_code == 403
        assert guest.put('/api/me/profile', headers=HEADERS, json={'display_name': 'Guest', 'media_previews': True}).status_code == 200
        inline = guest.get(attachment['url'] + '?inline=true')
        assert inline.status_code == 200 and inline.headers['content-type'] == 'image/png'
        assert inline.headers['cache-control'] == 'private, no-store'
        # Text-only edit cannot detach an attachment, and empty text remains valid for this message.
        edited = owner.put(f"/api/messages/{message['id']}", headers=HEADERS, json={'content': 'Caption', 'version': 1}).json()
        assert edited['attachments'] == [attachment]
        assert owner.delete(attachment['url'], headers=HEADERS).status_code == 409
        deleted = owner.delete(f"/api/messages/{message['id']}", headers=HEADERS).json()
        assert deleted['attachments'] == []
        assert guest.get(attachment['url']).status_code == 404
        assert owner.get(attachment['url']).content == PNG
        assert owner.get(f"/api/admin/messages/{message['id']}/history").json()['current']['attachments'] == [attachment]
        assert guest.get('/api/channels/1/messages').json()[0]['attachments'] == []


def test_upload_auth_sizes_names_and_download_only_types(tmp_path):
    app = create_app(tmp_path / 'chat.db')
    with TestClient(app) as client:
        assert upload(client).status_code == 401
        register(client)
        assert client.post('/api/attachments?filename=photo.png', content=PNG).status_code == 403
        assert upload(client, content=b'').status_code == 422
        assert upload(client, content=b'x' * (5 * 1024 * 1024 + 1)).status_code == 413
        for name in ('../evil', 'C:\\evil', 'bad\nname', '..', 'x'*181):
            assert upload(client, filename=name).status_code == 422
        html = upload(client, filename='page.html', content=b'<script>alert(1)</script>').json()
        client.put('/api/me/profile', headers=HEADERS, json={'display_name': 'Owner', 'media_previews': True})
        assert client.get(html['url'] + '?inline=true').status_code == 415
        assert client.get(html['url']).headers['content-type'] == 'application/octet-stream'
        assert client.delete(html['url'], headers=HEADERS).status_code == 200


def test_upload_limits_server_settings_and_pending_cleanup(tmp_path):
    path = tmp_path / 'chat.db'
    app = create_app(path, Limits(write_burst=30))
    with TestClient(app) as client:
        register(client)
        for index in range(3):
            assert upload(client, filename=f'{index}.txt', content=b'file').status_code == 200
        assert upload(client).status_code == 409
        pending = client.get('/api/attachments').json()
        assert client.delete(pending[0]['url'], headers=HEADERS).status_code == 200
        settings = client.get('/api/admin/console').json()['settings']
        client.put('/api/admin/settings', headers=HEADERS, json={**settings, 'uploads_enabled': False})
        assert upload(client).status_code == 403
        assert client.get(pending[1]['url']).content == b'file'
        client.put('/api/admin/settings', headers=HEADERS, json={**settings, 'attachment_max_mb': 1, 'attachment_storage_mb': 5})
        assert upload(client, content=b'x'*(1024*1024+1)).status_code == 413
        # Storage quota is checked before writing; rejected uploads leave no temporary files.
        with sqlite3.connect(path) as conn:
            conn.execute('UPDATE attachments SET size=5*1024*1024')
        assert upload(client).status_code == 507
        assert not list(attachment_directory(path).glob('*.tmp'))
        with sqlite3.connect(path) as conn:
            conn.execute('UPDATE attachments SET created=?', (time.time()-7200,))
        orphan = attachment_directory(path) / ('a'*32 + '.tmp')
        orphan.write_bytes(b'interrupted')
        os.utime(orphan, (time.time()-7200, time.time()-7200))
    with TestClient(create_app(path)) as client:
        client.post('/api/login', headers=HEADERS, json={'username': 'owner', 'password': 'a long test password'})
        assert client.get('/api/attachments').json() == []
        assert list(attachment_directory(path).iterdir()) == []


def test_attachment_backup_restore_and_retention_cleanup(tmp_path):
    path = tmp_path / 'chat.db'
    with TestClient(create_app(path)) as client:
        register(client)
        file = upload(client).json()
        message = client.post('/api/channels/1/messages', headers=HEADERS, json={'content': 'With a file', 'nonce': 'backup', 'attachments': [file['id']]}).json()
        snapshot = backup_database(path, tmp_path / 'snapshot.db')
        assert (attachment_directory(snapshot) / file['id']).read_bytes() == PNG
    with TestClient(create_app(snapshot)) as restored:
        restored.post('/api/login', headers=HEADERS, json={'username': 'owner', 'password': 'a long test password'})
        assert restored.get(file['url']).content == PNG
    with sqlite3.connect(path) as conn:
        conn.execute('UPDATE messages SET created=0 WHERE id=?', (message['id'],))
        conn.execute('UPDATE attachments SET created=0')
    with TestClient(create_app(path, Limits(retention_days=1))):
        assert not (attachment_directory(path) / file['id']).exists()
    with sqlite3.connect(path) as conn:
        assert conn.execute('SELECT count(*) FROM attachments').fetchone()[0] == 0


def test_file_type_sniffing_and_names():
    assert preview_type(b'<svg/>', 'image.svg') == 'application/octet-stream'
    assert preview_type(b'<script/>', 'image.png') == 'application/octet-stream'
    assert preview_type(b'\x00\x00\x00\x18ftypisom', 'movie.mp4') == 'video/mp4'
    assert clean_filename(' normal name.txt ') == 'normal name.txt'
    with pytest.raises(ValueError):
        clean_filename('folder/file.txt')


def test_streamed_upload_body_limit_and_no_files_after_rejection(tmp_path):
    path = tmp_path / 'chat.db'
    with TestClient(create_app(path)) as client:
        register(client)
        def chunks():
            for _ in range(6):
                yield b'x' * 1024 * 1024
        response = client.post('/api/attachments?filename=large.bin', headers=HEADERS, content=chunks())
        assert response.status_code == 413
        assert list(attachment_directory(path).iterdir()) == []
        assert client.get('/api/attachments').json() == []
        # A failed stream releases its upload slot and storage reservation.
        assert upload(client).status_code == 200


def test_banned_account_cannot_download_or_upload(tmp_path):
    app = create_app(tmp_path / 'chat.db')
    with TestClient(app) as owner, TestClient(app) as guest:
        register(owner)
        code = owner.post('/api/admin/invites', headers=HEADERS).json()['invite']
        register(guest, 'guest', code)
        attachment = upload(guest).json()
        guest.post('/api/channels/1/messages', headers=HEADERS, json={'nonce': 'file', 'attachments': [attachment['id']]})
        owner.put('/api/admin/members/2/ban', headers=HEADERS, json={'banned': True})
        assert guest.get(attachment['url']).status_code == 401
        assert upload(guest).status_code == 401


def test_failed_backup_does_not_leave_a_partial_snapshot(tmp_path):
    path = tmp_path / 'chat.db'
    with TestClient(create_app(path)) as client:
        register(client)
        file = upload(client).json()
    (attachment_directory(path) / file['id']).unlink()
    destination = tmp_path / 'failed.db'
    with pytest.raises(FileNotFoundError):
        backup_database(path, destination)
    assert not destination.exists() and not attachment_directory(destination).exists()


def test_backup_preserves_existing_companion_directory(tmp_path):
    source = tmp_path / 'chat.db'
    with TestClient(create_app(source)):
        pass
    destination = tmp_path / 'snapshot.db'
    companion = attachment_directory(destination)
    companion.mkdir()
    marker = companion / 'keep.txt'
    marker.write_text('Keep this file')
    with pytest.raises(FileExistsError):
        backup_database(source, destination)
    assert marker.read_text() == 'Keep this file' and not destination.exists()
