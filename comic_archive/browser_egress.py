"""Authenticated loopback CONNECT endpoint for the app's embedded browsers."""
import base64
import hmac
import re
import secrets
import select
import socket
import threading
import time
from urllib.parse import urlsplit

from .provider_http import _connect_public
from .public_destination import public_https_url


HEADER_LIMIT = 8192
MAX_TUNNELS = 32
HEADER_SECONDS = 5
SOCKET_SECONDS = 10
IDLE_SECONDS = 60
RECORD_SIZE = 64 * 1024
CONNECT = re.compile(rb'CONNECT ([!-~]+) HTTP/1\.[01]')
CONNECTED = b'HTTP/1.1 200 Connection Established\r\n\r\n'
REFUSED = b'HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n'


def _request(client):
    deadline = time.monotonic() + HEADER_SECONDS
    header = bytearray()
    while b'\r\n\r\n' not in header:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or len(header) == HEADER_LIMIT:
            return None, b''
        client.settimeout(remaining)
        chunk = client.recv(HEADER_LIMIT - len(header))
        if not chunk:
            return None, b''
        header.extend(chunk)
    raw, _, extra = bytes(header).partition(b'\r\n\r\n')
    lines = raw.split(b'\r\n')
    match = CONNECT.fullmatch(lines[0])
    if not match or extra:
        return None, b''
    authority = match[1].decode('ascii')
    url = 'https://' + authority
    if not authority.endswith(':443') or any(c in authority for c in '/?#') or not public_https_url(url):
        return None, b''
    credentials = []
    for line in lines[1:]:
        name, separator, value = line.partition(b':')
        if not separator or not name or name != name.strip() or name.lower() in {b'content-length', b'transfer-encoding'}:
            return None, b''
        if name.lower() == b'proxy-authorization':
            credentials.append(bytes(value.strip()))
    return urlsplit(url).hostname, credentials[0] if len(credentials) == 1 else b''


class BrowserEgress:
    """Forward only authenticated public HTTPS, leaving TLS inside the browser."""

    def __init__(self):
        self._password = secrets.token_urlsafe(32)
        self._realm = 'Comic Archiver ' + secrets.token_hex(12)
        self._credentials = b'Basic ' + base64.b64encode(('browser:' + self._password).encode('ascii'))
        self._challenge = (b'HTTP/1.1 407 Proxy Authentication Required\r\nProxy-Authenticate: Basic realm="'
                           + self._realm.encode('ascii') + b'"\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
        self._stop = threading.Event()
        self._slots = threading.BoundedSemaphore(MAX_TUNNELS)
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
                self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            self._listener.bind(('127.0.0.1', 0))
            self._listener.listen(MAX_TUNNELS)
            self._listener.settimeout(.25)
            self._port = self._listener.getsockname()[1]
            threading.Thread(target=self._accept, daemon=True, name='browser-egress').start()
        except BaseException:
            self._listener.close()
            raise

    def switches(self):
        return ['--disable-background-networking', '--disable-quic',
                '--force-webrtc-ip-handling-policy=disable_non_proxied_udp']

    def configure_qt(self, network):
        proxy = network.QNetworkProxy
        proxy.setApplicationProxy(proxy(proxy.ProxyType.HttpProxy, '127.0.0.1', self._port))

    def proxy_url(self):
        return f'http://127.0.0.1:{self._port}'

    def password_for(self, host, realm):
        if (host in {'127.0.0.1', f'127.0.0.1:{self._port}'} and realm == self._realm
                and not self._stop.is_set()):
            return self._password
        return ''

    def authenticate(self, _url, authenticator, host):
        password = self.password_for(host, authenticator.realm())
        if password:
            authenticator.setUser('browser')
            authenticator.setPassword(password)

    def close(self):
        self._stop.set()
        self._listener.close()

    def _accept(self):
        while not self._stop.is_set():
            try:
                client, _ = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            if not self._slots.acquire(blocking=False):
                client.close()
                continue
            try:
                threading.Thread(target=self._handle, args=(client,), daemon=True).start()
            except RuntimeError:
                client.close()
                self._slots.release()

    def _handle(self, client):
        try:
            with client:
                host, credentials = _request(client)
                client.settimeout(SOCKET_SECONDS)
                if not host or self._stop.is_set():
                    client.sendall(REFUSED)
                elif not hmac.compare_digest(credentials, self._credentials):
                    client.sendall(self._challenge)
                else:
                    self._tunnel(client, host)
        except OSError:
            pass
        finally:
            self._slots.release()

    def _tunnel(self, client, host):
        try:
            server = _connect_public(host, 443, SOCKET_SECONDS, None)
        except OSError:
            client.sendall(REFUSED)
            return
        with server:
            if self._stop.is_set():
                return
            server.settimeout(SOCKET_SECONDS)
            client.sendall(CONNECTED)
            peers = {client: server, server: client}
            idle = time.monotonic() + IDLE_SECONDS
            while peers and not self._stop.is_set() and time.monotonic() < idle:
                ready, _, _ = select.select(list(peers), [], [], .25)
                for current in ready:
                    target = peers[current]
                    record = current.recv(RECORD_SIZE)
                    if record:
                        target.sendall(record)
                        idle = time.monotonic() + IDLE_SECONDS
                    else:
                        target.shutdown(socket.SHUT_WR)
                        del peers[current]
