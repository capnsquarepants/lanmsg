"""Bounded HTML metadata fetching with DNS-pinned public network connections."""

from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from html.parser import HTMLParser
import http.client
import ipaddress
import socket
import ssl
import threading
import time
import unicodedata
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

MAX_BYTES = 512 * 1024
TIMEOUT = 5


class PreviewError(Exception):
    def __init__(self, message="Preview unavailable", status=502):
        super().__init__(message)
        self.status = status


def normalize_url(value):
    try:
        if len(value) > 2048 or any(char.isspace() or ord(char) < 32 for char in value):
            raise ValueError()
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username is not None or parsed.password is not None:
            raise ValueError()
        host = parsed.hostname.encode("idna").decode("ascii").lower()
        if len(host) > 253 or "%" in host or "\\" in value:
            raise ValueError()
        port = 443 if parsed.scheme == "https" else 80
        if parsed.port not in (None, port):
            raise ValueError()
        authority = f"[{host}]" if ":" in host else host
        return urlunsplit((parsed.scheme, authority, quote(parsed.path or "/", safe="/%:@!$&'()*+,;=-._~"), quote(parsed.query, safe="%=&?/:@!$'()*+,;~-._"), ""))
    except (ValueError, UnicodeError):
        raise PreviewError("Unsupported preview URL", 422) from None


def public_address(value):
    try:
        ip = ipaddress.ip_address(value)
        if isinstance(ip, ipaddress.IPv6Address):
            if ip.ipv4_mapped:
                ip = ip.ipv4_mapped
            elif ip.sixtofour or ip.teredo or ip in ipaddress.ip_network("64:ff9b::/96"):
                return False
        return ip.is_global and not ip.is_multicast and not ip.is_reserved
    except ValueError:
        return False


def resolve_public(host, port):
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not addresses or any(not public_address(address[4][0]) for address in addresses):
        raise PreviewError("Private network previews are not supported", 422)
    return addresses


def remaining(deadline):
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise PreviewError("Preview timed out", 504)
    return seconds


class MetadataParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta, self.title = {}, []
        self.title_length = 0
        self.in_title = False

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self.in_title = True
        if tag == "meta":
            attrs = dict(attrs)
            key = (attrs.get("property") or attrs.get("name") or "").lower()
            if key in {"og:title", "og:description", "og:site_name", "og:image", "description"} and key not in self.meta:
                self.meta[key] = (attrs.get("content") or "")[:2048]

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False

    def handle_data(self, data):
        if self.in_title and self.title_length < 2048:
            part = data[:2048 - self.title_length]
            self.title.append(part)
            self.title_length += len(part)


def clean_text(value, length):
    return " ".join("".join(char for char in value if not unicodedata.category(char).startswith("C") or char.isspace()).split())[:length]


def parse_metadata(html, url):
    parser = MetadataParser()
    parser.feed(html)
    image = None
    if parser.meta.get("og:image"):
        try:
            candidate = normalize_url(urljoin(url, parser.meta["og:image"]))
            host = urlsplit(candidate).hostname
            # Thumbnails load in the browser, never through this service. Literal
            # private hosts are omitted; page hosts have already passed DNS checks.
            if "." in host and not host.endswith((".local", ".localhost", ".internal")):
                try:
                    allowed = public_address(str(ipaddress.ip_address(host)))
                except ValueError:
                    allowed = True
                if allowed:
                    image = candidate
        except PreviewError:
            pass
    host = urlsplit(url).hostname
    return {"url": url, "site_name": clean_text(parser.meta.get("og:site_name") or host, 100),
            "title": clean_text(parser.meta.get("og:title") or "".join(parser.title) or host, 300),
            "description": clean_text(parser.meta.get("og:description") or parser.meta.get("description") or "", 500),
            "image": image}


def fetch_metadata(url):
    deadline = time.monotonic() + TIMEOUT
    for redirects in range(4):
        url = normalize_url(url)
        parsed = urlsplit(url)
        port = 443 if parsed.scheme == "https" else 80
        addresses = resolve_public(parsed.hostname, port)
        family, socktype, proto, _, address = addresses[0]
        sock = socket.socket(family, socktype, proto)
        conn = None
        timer = None
        try:
            sock.settimeout(remaining(deadline))
            sock.connect(address)  # Numeric address: DNS is never resolved a second time.
            if parsed.scheme == "https":
                sock = ssl.create_default_context().wrap_socket(sock, server_hostname=parsed.hostname)
            conn = http.client.HTTPConnection(parsed.hostname, port, timeout=remaining(deadline))
            conn.sock = sock
            def interrupt():
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            timer = threading.Timer(remaining(deadline), interrupt)
            timer.daemon = True
            timer.start()
            sock.settimeout(remaining(deadline))
            conn.request("GET", urlunsplit(("", "", parsed.path, parsed.query, "")), headers={"User-Agent":"LANmsg-LinkPreview/1.0", "Accept":"text/html,application/xhtml+xml", "Accept-Encoding":"identity", "Connection":"close"})
            response = conn.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                target = response.getheader("Location")
                if not target or redirects == 3:
                    raise PreviewError()
                url = normalize_url(urljoin(url, target))
                continue
            if response.status != 200 or response.headers.get_content_type() not in ("text/html", "application/xhtml+xml"):
                raise PreviewError()
            if response.getheader("Content-Encoding", "identity").lower() != "identity":
                raise PreviewError()
            content_length = response.getheader("Content-Length")
            if content_length and int(content_length) > MAX_BYTES:
                raise PreviewError("Preview page is too large")
            chunks, size = [], 0
            while not response.isclosed():
                sock.settimeout(remaining(deadline))
                chunk = response.read1(min(16384, MAX_BYTES + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > MAX_BYTES:
                    raise PreviewError("Preview page is too large")
            charset = response.headers.get_content_charset() or "utf-8"
            try:
                html = b"".join(chunks).decode(charset, errors="replace")
            except LookupError:
                html = b"".join(chunks).decode("utf-8", errors="replace")
            result = parse_metadata(html, url)
            if result["image"]:
                try:
                    image_url = urlsplit(result["image"])
                    resolve_public(image_url.hostname, 443 if image_url.scheme == "https" else 80)
                    remaining(deadline)
                except Exception:
                    result["image"] = None
            return result
        finally:
            if timer:
                timer.cancel()
            if conn:
                conn.close()
            sock.close()
    raise PreviewError()


class PreviewService:
    def __init__(self):
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="link-preview")
        self.slots = threading.BoundedSemaphore(2)
        self.lock = threading.RLock()
        self.cache, self.pending = OrderedDict(), {}

    def _fetch(self, url):
        try:
            result = fetch_metadata(url)
        except PreviewError as error:
            result = (error.status, str(error))
        except Exception:
            result = (502, "Preview unavailable")
        with self.lock:
            self.cache[url] = (time.monotonic() + (60 if isinstance(result, tuple) else 600), result)
            self.cache.move_to_end(url)
            while len(self.cache) > 256:
                self.cache.popitem(last=False)
        return result

    def _complete(self, url):
        with self.lock:
            self.pending.pop(url, None)
        self.slots.release()

    def get(self, value):
        url = normalize_url(value)
        with self.lock:
            cached = self.cache.get(url)
            if cached and cached[0] > time.monotonic():
                self.cache.move_to_end(url)
                result = cached[1]
                if isinstance(result, tuple):
                    raise PreviewError(result[1], result[0])
                return result
            future = self.pending.get(url)
            if future is None:
                if not self.slots.acquire(blocking=False):
                    raise PreviewError("Preview service is busy; retry later", 503)
                future = self.pool.submit(self._fetch, url)
                self.pending[url] = future
                future.add_done_callback(lambda _: self._complete(url))
        try:
            result = future.result(timeout=TIMEOUT + 1)
        except TimeoutError:
            # Slow DNS may outlive the response deadline, but still occupies one
            # of just two slots. No unbounded queue or resolver threads are added.
            raise PreviewError("Preview timed out", 504) from None
        if isinstance(result, tuple):
            raise PreviewError(result[1], result[0])
        return result

    def close(self):
        self.pool.shutdown(wait=False, cancel_futures=True)
