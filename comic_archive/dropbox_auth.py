"""Local Dropbox consent with PKCE, minimal scopes and durable refresh tokens."""
import base64
from datetime import datetime, timedelta, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import re
import secrets
import time
from urllib.parse import parse_qs, urlencode, urlsplit
import webbrowser

import httpx

from .cloud_errors import CloudTransferError
from .models import ArchiveError
from .network import NetworkGuard

SCOPES = 'files.metadata.read files.metadata.write files.content.read files.content.write account_info.read'
CALLBACK = 'http://127.0.0.1:53682/'


def authorize(root, client, cancel, update):
    guard = NetworkGuard(root)
    guard.check()
    state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    query = urlencode({'client_id': client['client_id'], 'response_type': 'code',
                       'redirect_uri': CALLBACK, 'state': state, 'token_access_type': 'offline',
                       'code_challenge': challenge, 'code_challenge_method': 'S256', 'scope': SCOPES})
    authorization = 'https://www.dropbox.com/oauth2/authorize?' + query
    result = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass

        def setup(self):
            super().setup()
            self.request.settimeout(2)

        def do_GET(self):
            url = urlsplit(self.path)
            parameters = parse_qs(url.query)
            valid = (len(self.path) <= 4096 and parameters.get('state') == [state])
            if valid and url.path == '/auth' and set(parameters) == {'state'}:
                self.send_response(302)
                self.send_header('Location', authorization)
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                return
            code = parameters.get('code', [])
            if valid and url.path == '/' and set(parameters) == {'state', 'code'} and len(code) == 1 and re.fullmatch(r'[A-Za-z0-9_.~-]{1,2048}', code[0]):
                result['code'] = code[0]
                status, message = 200, 'Dropbox sign-in received. Return to Comic Archiver.'
            elif valid and url.path == '/' and parameters.get('error') == ['access_denied']:
                result['denied'] = True
                status, message = 200, 'Dropbox sign-in cancelled. Return to Comic Archiver.'
            else:
                status, message = 400, 'Invalid sign-in response. Return to Comic Archiver.'
            data = message.encode()
            self.send_response(status)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Security-Policy', "default-src 'none'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(data)

    with HTTPServer(('127.0.0.1', 53682), Handler) as server:
        server.timeout = .2
        auth_url = CALLBACK + 'auth?' + urlencode({'state': state})
        update(auth_url=auth_url)
        webbrowser.open(auth_url)
        deadline = time.monotonic() + 600
        while not result:
            if cancel.is_set() or time.monotonic() >= deadline:
                raise ArchiveError('Cloud sign-in stopped.')
            guard.check()
            server.handle_request()
    if result.get('denied'):
        raise CloudTransferError('cloud_login_required')
    if cancel.is_set():
        raise ArchiveError('Cloud sign-in stopped.')
    guard.check()
    # This code, verifier and tokens stay on the device. No credentials in URLs
    # exposed to the UI, subprocess arguments, logs or exception messages.
    with httpx.stream('POST', 'https://api.dropboxapi.com/oauth2/token',
                      data={'code': result['code'], 'grant_type': 'authorization_code',
                            'client_id': client['client_id'], 'code_verifier': verifier,
                            'redirect_uri': CALLBACK}, timeout=20, trust_env=False) as response:
        if response.status_code != 200:
            raise CloudTransferError('cloud_login_required')
        data = bytearray()
        for chunk in response.iter_bytes():
            guard.check()
            if cancel.is_set():
                raise ArchiveError('Cloud sign-in stopped.')
            data.extend(chunk)
            if len(data) > 65536:
                raise ArchiveError('Cloud sign-in response is too large.')
    token = json.loads(data)
    if (not isinstance(token, dict)
            or any(not isinstance(token.get(k), str) or not token[k] or len(token[k]) > 8192
                   for k in ('access_token', 'refresh_token'))
            or not isinstance(token.get('token_type'), str) or token['token_type'].lower() != 'bearer'
            or type(token.get('expires_in')) is not int or not 0 < token['expires_in'] <= 86400):
        raise ArchiveError('Dropbox did not save a reusable sign-in.')
    return {k: token[k] for k in ('access_token', 'refresh_token')} | {
        'token_type': 'Bearer',
        'expiry': (datetime.now(timezone.utc) + timedelta(seconds=token['expires_in'])).isoformat()}
