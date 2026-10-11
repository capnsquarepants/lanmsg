from concurrent.futures import ThreadPoolExecutor
from email.message import Message
import socket
import threading

import pytest
from fastapi.testclient import TestClient

from lanmsg import previews
from lanmsg.server import create_app
from tests.test_server import register

HEADERS = {'X-LANmsg':'1'}


def test_metadata_parsing_and_fallbacks():
    card = previews.parse_metadata('''<title>Fallback</title>
      <meta property="og:title" content="An &amp; example">
      <meta property="og:description" content=" Description   here ">
      <meta property="og:site_name" content="Example">
      <meta property="og:image" content="/thumbnail.jpg">''','https://example.com/page')
    assert card == {'url':'https://example.com/page','title':'An & example',
                    'description':'Description here','site_name':'Example',
                    'image':'https://example.com/thumbnail.jpg'}
    fallback = previews.parse_metadata('<title>Page title</title><meta name="description" content="Summary">','https://example.com/')
    assert fallback['title'] == 'Page title' and fallback['description'] == 'Summary'
    for value in ('javascript:alert(1)','http://127.0.0.1/private.png'):
        assert previews.parse_metadata(f'<meta property="og:image" content="{value}">','https://example.com/')['image'] is None


@pytest.mark.parametrize('value', ['file:///secret','javascript:alert(1)','ftp://example.com/',
    'https://user:pass@example.com/','https://example.com:8080/','http://[::1%25eth0]/',
    'http://example.com/\r\nHost:evil','http://example.com\\@localhost/'])
def test_invalid_fetch_urls(value):
    with pytest.raises(previews.PreviewError):
        previews.normalize_url(value)


@pytest.mark.parametrize('address', ['127.0.0.1','10.0.0.1','192.168.1.1','169.254.169.254',
    '100.100.100.200','0.0.0.0','224.0.0.1','::1','fd00::1','fe80::1','::ffff:127.0.0.1',
    '64:ff9b::7f00:1','2002:7f00:1::'])
def test_private_and_special_addresses_are_blocked(address):
    assert previews.public_address(address) is False


def test_mixed_public_private_dns_is_rejected(monkeypatch):
    monkeypatch.setattr(socket,'getaddrinfo',lambda *a, **k:[
        (socket.AF_INET,socket.SOCK_STREAM,6,'',('93.184.216.34',80)),
        (socket.AF_INET,socket.SOCK_STREAM,6,'',('127.0.0.1',80))])
    with pytest.raises(previews.PreviewError):
        previews.resolve_public('example.com',80)


class FakeResponse:
    def __init__(self, body=b'<title>Example</title>', status=200, **headers):
        self.body, self.status, self.offset = body, status, 0
        self.headers = Message()
        self.headers['Content-Type'] = headers.pop('Content-Type','text/html; charset=utf-8')
        for key,value in headers.items():
            self.headers[key] = value

    def getheader(self, name, default=None):
        return self.headers.get(name,default)

    def isclosed(self):
        return self.offset >= len(self.body)

    def read1(self, count):
        chunk = self.body[self.offset:self.offset+count]
        self.offset += len(chunk)
        return chunk


def fake_transport(monkeypatch, responses, dns=None):
    connections, resolutions = [], []
    def resolve(host,port,**kwargs):
        resolutions.append(host)
        address = dns(host) if dns else '93.184.216.34'
        return [(socket.AF_INET,socket.SOCK_STREAM,6,'',(address,port))]
    class FakeSocket:
        def settimeout(self,value): pass
        def connect(self,address): connections.append(address)
        def shutdown(self,how): pass
        def close(self): pass
    class FakeConnection:
        def __init__(self,*args,**kwargs): self.sock = None
        def request(self,*args,**kwargs):
            assert 'Cookie' not in kwargs['headers'] and 'Authorization' not in kwargs['headers']
        def getresponse(self): return responses.pop(0)
        def close(self): pass
    monkeypatch.setattr(previews.socket,'getaddrinfo',resolve)
    monkeypatch.setattr(previews.socket,'socket',lambda *a:FakeSocket())
    monkeypatch.setattr(previews.http.client,'HTTPConnection',FakeConnection)
    return connections,resolutions


def test_connections_are_pinned_and_redirects_revalidated(monkeypatch):
    connections,resolutions = fake_transport(monkeypatch,[FakeResponse()])
    assert previews.fetch_metadata('http://example.com/')['title'] == 'Example'
    assert connections == [('93.184.216.34',80)] and resolutions == ['example.com']
    connections,resolutions = fake_transport(monkeypatch,[FakeResponse(status=302,Location='http://private.example/')],
        lambda host:'127.0.0.1' if host == 'private.example' else '93.184.216.34')
    with pytest.raises(previews.PreviewError):
        previews.fetch_metadata('http://example.com/')
    assert len(connections) == 1 and resolutions == ['example.com','private.example']


@pytest.mark.parametrize('response', [FakeResponse(**{'Content-Type':'image/png'}),
    FakeResponse(**{'Content-Encoding':'gzip'}), FakeResponse(**{'Content-Length':str(previews.MAX_BYTES+1)}),
    FakeResponse(body=b'x'*(previews.MAX_BYTES+1))])
def test_fetch_bounds_and_html_only(monkeypatch,response):
    fake_transport(monkeypatch,[response])
    with pytest.raises(previews.PreviewError):
        previews.fetch_metadata('http://example.com/')


def test_thumbnail_dns_must_be_public(monkeypatch):
    fake_transport(monkeypatch,[FakeResponse(body=b'<title>Page</title><meta property="og:image" content="http://private.example/a.png">')],
        lambda host:'192.168.1.1' if host == 'private.example' else '93.184.216.34')
    assert previews.fetch_metadata('http://example.com/')['image'] is None


def test_tls_uses_original_hostname_with_pinned_socket(monkeypatch):
    connections,_ = fake_transport(monkeypatch,[FakeResponse()])
    names = []
    class Context:
        def wrap_socket(self,sock,server_hostname):
            names.append(server_hostname)
            return sock
    monkeypatch.setattr(previews.ssl,'create_default_context',lambda:Context())
    assert previews.fetch_metadata('https://example.com/')['title'] == 'Example'
    assert names == ['example.com'] and connections == [('93.184.216.34',443)]


def test_cache_capacity_and_failed_request_caching(monkeypatch):
    calls = []
    def fetch(url):
        calls.append(url)
        if 'failure' in url:
            raise previews.PreviewError()
        return {'url':url,'title':'Example'}
    monkeypatch.setattr(previews,'fetch_metadata',fetch)
    service = previews.PreviewService()
    try:
        for n in range(257):
            service._fetch(f'https://example.com/{n}')
        assert len(service.cache) == 256
        assert 'https://example.com/0' not in service.cache
        assert service.get('https://example.com/256')['title'] == 'Example'
        service._fetch('https://example.com/failure')
        with pytest.raises(previews.PreviewError):
            service.get('https://example.com/failure')
        assert len(calls) == 258
    finally:
        service.close()


def test_response_deadline_keeps_stalled_workers_bounded(monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(previews,'TIMEOUT',-.99)
    monkeypatch.setattr(previews,'fetch_metadata',lambda url:release.wait(2) or {})
    service = previews.PreviewService()
    try:
        for url in ('https://first.example/','https://second.example/'):
            with pytest.raises(previews.PreviewError) as timeout:
                service.get(url)
            assert timeout.value.status == 504
        with pytest.raises(previews.PreviewError) as busy:
            service.get('https://third.example/')
        assert busy.value.status == 503 and len(service.pending) == 2
    finally:
        futures = list(service.pending.values())
        release.set()
        for future in futures:
            future.result(timeout=1)
        service.close()


def test_cache_coalescing_and_worker_limit(monkeypatch):
    started, release = threading.Event(), threading.Event()
    calls = []
    def fetch(url):
        calls.append(url); started.set(); release.wait(2)
        return {'url':url,'title':'Cached'}
    monkeypatch.setattr(previews,'fetch_metadata',fetch)
    service = previews.PreviewService()
    try:
        with ThreadPoolExecutor(max_workers=3) as callers:
            first = callers.submit(service.get,'https://example.com/#fragment')
            assert started.wait(1)
            same = callers.submit(service.get,'https://example.com/')
            other = callers.submit(service.get,'https://another.example/')
            # Wait for both worker slots, without racing the submitting thread.
            for _ in range(100):
                with service.lock:
                    if len(service.pending) == 2:
                        break
                threading.Event().wait(.001)
            with pytest.raises(previews.PreviewError) as busy:
                service.get('https://third.example/')
            assert busy.value.status == 503
            release.set()
            assert first.result() == same.result()
            assert other.result()['url'] == 'https://another.example/'
        assert service.get('https://example.com/')['title'] == 'Cached'
        assert len(calls) == 2
    finally:
        release.set(); service.close()


def test_endpoint_enforces_preferences_before_cache_or_fetch(tmp_path,monkeypatch):
    app = create_app(tmp_path/'chat.db')
    calls = []
    monkeypatch.setattr(app.state.preview_service,'get',lambda url:calls.append(url) or {'url':url,'title':'Example'})
    with TestClient(app) as owner, TestClient(app) as guest:
        body = {'url':'https://example.com/'}
        assert guest.post('/api/previews',headers=HEADERS,json=body).status_code == 401
        register(owner)
        profile = owner.get('/api/me/profile').json()
        payload = {key:profile[key] for key in ('display_name','bio','nickname_color','media_previews')}
        assert owner.post('/api/previews',headers=HEADERS,json=body).status_code == 403
        assert calls == []
        owner.put('/api/me/profile',headers=HEADERS,json={**payload,'media_previews':True})
        assert owner.post('/api/previews',json=body).status_code == 403
        result = owner.post('/api/previews',headers=HEADERS,json=body)
        assert result.status_code == 200 and result.headers['cache-control'] == 'private, no-store'
        settings = owner.get('/api/admin/console').json()['settings']
        owner.put('/api/admin/settings',headers=HEADERS,json={**settings,'media_previews':True})
        owner.put('/api/me/profile',headers=HEADERS,json={**payload,'media_previews':False})
        assert owner.post('/api/previews',headers=HEADERS,json=body).status_code == 403
        assert len(calls) == 1
        owner.put('/api/me/profile',headers=HEADERS,json=payload)
        assert owner.post('/api/previews',headers=HEADERS,json=body).status_code == 200
