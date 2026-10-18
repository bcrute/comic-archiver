"""The local dashboard as other programs on this computer reach it: the real server on 127.0.0.1, spoken to
in raw HTTP, with an empty library and nothing signed in. Nothing is sent off this computer."""
import http.client
import json
from pathlib import Path
import socket
import tempfile
import threading
import unittest

from comic_archive.local_web import COOKIE_NAME, create_server


class LocalServerOverHTTPTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.server = create_server(Path(folder.name), 0)
        self.port = self.server.server_port
        self.host = f'127.0.0.1:{self.port}'
        self.launch = '/?launch=' + self.server.get_app().launch_token
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        def stop():
            self.server.shutdown(); self.server.server_close(); thread.join(10)
        self.addCleanup(stop)

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        try:
            connection.request(method, path, body=body, headers={'Host': self.host} | (headers or {}))
            response = connection.getresponse()
            return response.status, {key.lower(): value for key, value in response.getheaders()}, response.read()
        finally:
            connection.close()

    def raw(self, data):
        """Send bytes as they are and read whatever comes back until the server closes the connection."""
        with socket.create_connection(('127.0.0.1', self.port), timeout=10) as connection:
            connection.sendall(data)
            connection.shutdown(socket.SHUT_WR)  # Nothing more is coming: the server answers what it has.
            received = []
            while chunk := connection.recv(65536):
                received.append(chunk)
        return b''.join(received)

    def signed_in(self):
        """The cookie the page gets, and the headers a request from the page itself carries."""
        status, headers, _ = self.request('GET', self.launch)
        self.assertEqual(status, 200)
        cookie = headers['set-cookie'].split(';', 1)[0]
        context = json.loads(self.request('GET', '/api/v1/context', headers={'Cookie': cookie})[2])
        return cookie, context, {'Cookie': cookie, 'Origin': 'http://' + self.host, 'Sec-Fetch-Site': 'same-origin',
                                 'Content-Type': 'application/json', 'X-CSRF-Token': context['csrf_token']}

    def network_choice(self, workspace):
        return '/api/v1/workspaces/' + workspace + '/network'

    def test_the_page_carries_its_protections_on_the_wire(self):
        status, headers, body = self.request('GET', self.launch)
        self.assertEqual(status, 200)
        self.assertIn(b'archive.js', body)
        policy = headers['content-security-policy']
        for part in ("default-src 'none'", "script-src 'self'", "frame-ancestors 'none'", "base-uri 'none'", "form-action 'self'"):
            self.assertIn(part, policy)
        self.assertNotIn('unsafe-inline', policy); self.assertNotIn('unsafe-eval', policy)
        self.assertEqual((headers['x-content-type-options'], headers['referrer-policy'], headers['cache-control']),
                         ('nosniff', 'no-referrer', 'no-store'))
        cookie = headers['set-cookie']
        self.assertTrue(cookie.startswith(f'{COOKIE_NAME}-{self.port}='))
        for part in ('HttpOnly', 'SameSite=Strict', 'Path=/'):
            self.assertIn(part, cookie)
        self.assertNotIn('Domain=', cookie)
        # The Server header says what answered, not which Python could be attacked.
        self.assertEqual(headers['server'], 'ComicArchiver')

    def test_another_host_name_another_site_or_a_frame_is_refused_and_given_no_cookie(self):
        cookie, _, _ = self.signed_in()
        refused = ({'Host': 'evil.example:' + str(self.port)}, {'Host': 'localhost:' + str(self.port)},
                   {'Host': '127.0.0.1:' + str(self.port + 1)}, {'Host': '127.0.0.1'},
                   {'Origin': 'http://evil.example'}, {'Sec-Fetch-Site': 'cross-site'},
                   {'Sec-Fetch-Site': 'cross-site', 'Sec-Fetch-Dest': 'iframe', 'Sec-Fetch-Mode': 'navigate'},
                   {'Referer': 'http://evil.example/'}, {'X-Forwarded-Host': self.host, 'Host': 'evil.example'})
        for extra in refused:
            for path in ('/', '/api/v1/context', '/assets/archive.js'):
                with self.subTest(extra=extra, path=path):
                    status, headers, body = self.request('GET', path, headers={'Cookie': cookie} | extra)
                    self.assertEqual(status, 403)
                    self.assertNotIn('set-cookie', headers)
                    self.assertNotIn(b'csrf', body)

    def test_another_program_on_this_computer_without_the_launch_secret_gets_no_session(self):
        token = self.server.get_app().launch_token
        for path in ('/', '/library', '/?launch=', '/?launch=' + token[:-1], '/?launch=' + 'A' * len(token),
                     '/api/v1/context?launch=' + token, '/assets/archive.js?launch=' + token):
            for extra in ({}, {'Sec-Fetch-Site': 'none'}, {'Origin': 'http://' + self.host}):
                with self.subTest(path=path, extra=extra):
                    status, headers, body = self.request('GET', path, headers=extra)
                    self.assertIn(status, {400, 401})
                    self.assertNotIn('set-cookie', headers); self.assertNotIn(b'csrf', body); self.assertNotIn(b'archive.js', body)
        # So it cannot read a CSRF token or make a change either.
        self.assertEqual(self.request('GET', '/api/v1/context', headers={'Origin': 'http://' + self.host})[0], 401)
        self.assertEqual(self.request('POST', '/api/v1/workspaces', '{}', {'Origin': 'http://' + self.host,
                                                                        'Content-Type': 'application/json'})[0], 401)

    def test_the_api_needs_the_cookie_and_a_change_needs_the_page_origin_and_its_token(self):
        for path in ('/api/v1/context', '/assets/archive.js', '/api/v1/workspaces'):
            self.assertEqual(self.request('GET', path)[0], 401)
        cookie, context, page = self.signed_in()
        path = self.network_choice(context['workspace_ids'][0])
        body = json.dumps({'wifi_only': True})
        for missing, status in (('X-CSRF-Token', 403), ('Origin', 403), ('Content-Type', 415), ('Cookie', 401)):
            with self.subTest(missing=missing):
                self.assertEqual(self.request('POST', path, body, {k: v for k, v in page.items() if k != missing})[0], status)
        for wrong in ({'X-CSRF-Token': '0' * 64}, {'Origin': 'null'}, {'Cookie': cookie[:-1] + ('0' if cookie[-1] != '0' else '1')}):
            with self.subTest(wrong=wrong):
                self.assertIn(self.request('POST', path, body, page | wrong)[0], {401, 403})
        self.assertEqual(self.request('POST', path, body, page)[0], 200)

    def test_paths_outside_the_app_are_never_served(self):
        cookie, _, _ = self.signed_in()
        for path in ('/assets/../../pyproject.toml', '/assets/..%2f..%2fpyproject.toml', '/assets/%2e%2e/%2e%2e/pyproject.toml',
                     '//etc/passwd', '/assets/archive.js%00.txt', '/assets/', '/static/archive.js', '/library/../../etc/passwd',
                     '/assets/archive.js/../../../pyproject.toml', '/%2e%2e/%2e%2e/etc/hostname'):
            with self.subTest(path=path):
                status, _, body = self.request('GET', path, headers={'Cookie': cookie})
                self.assertIn(status, {400, 403, 404})
                self.assertNotIn(b'[project]', body); self.assertNotIn(b'root:', body)

    def test_a_body_that_is_too_large_or_has_no_length_is_refused_before_it_is_read(self):
        _, context, page = self.signed_in()
        path = self.network_choice(context['workspace_ids'][0])
        head = ''.join(f'{key}: {value}\r\n' for key, value in page.items())
        # Only the headers are sent: the answer comes without the server waiting for ten megabytes.
        answer = self.raw(f'POST {path} HTTP/1.1\r\nHost: {self.host}\r\n{head}Content-Length: 10000000\r\nConnection: close\r\n\r\n'.encode())
        self.assertTrue(answer.startswith(b'HTTP/1.0 413') or answer.startswith(b'HTTP/1.1 413'), answer[:80])
        answer = self.raw(f'POST {path} HTTP/1.1\r\nHost: {self.host}\r\n{head}Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n'
                          '5\r\nhello\r\n0\r\n\r\n'.encode())
        self.assertRegex(answer[:12], rb'^HTTP/1\.[01] 4[01][0-9]')
        self.assertEqual(self.request('POST', path, '{"wifi_only": true' + ' ' * 20000 + '}', page)[0], 413)
        for broken in ('{"wifi_only": tru', '[]', '"x"', '{"wifi_only": true, "path": "/etc"}', '[' * 5000 + ']' * 5000):
            with self.subTest(body=broken[:20]):
                self.assertEqual(self.request('POST', path, broken, page)[0], 400)

    def test_methods_other_than_get_head_and_post_are_refused(self):
        cookie, _, _ = self.signed_in()
        for method in ('PUT', 'DELETE', 'PATCH', 'OPTIONS', 'TRACE', 'CONNECT', 'PROPFIND'):
            with self.subTest(method=method):
                status, _, body = self.request(method, '/api/v1/context', headers={'Cookie': cookie, 'X-Private': 'PRIVATE'})
                self.assertGreaterEqual(status, 400)
                self.assertNotIn(b'PRIVATE', body); self.assertNotIn(cookie.encode(), body)

    def test_broken_requests_and_idle_connections_do_not_stop_the_server(self):
        for junk in (b'GARBAGE\r\n\r\n', b'\x16\x03\x01\x02\x00\x01\x00\x01\xfc\x03\x03' + b'\0' * 64,
                     b'GET / HTTP/1.1\r\nHost: ' + b'a' * 70000 + b'\r\n\r\n',
                     b'GET / HTTP/1.1\r\n' + b''.join(b'X-%d: 1\r\n' % n for n in range(200)) + b'\r\n',
                     b'GET /' + b'a' * 70000 + b' HTTP/1.1\r\n\r\n', b'GET / HTTP/9.9\r\n\r\n'):
            with self.subTest(junk=junk[:16]):
                answer = self.raw(junk)
                self.assertFalse(answer.startswith(b'HTTP/1.1 200') or answer.startswith(b'HTTP/1.0 200'), answer[:60])
                self.assertNotIn(b'Traceback', answer)
        # Connections that send nothing do not keep the page from loading.
        idle = [socket.create_connection(('127.0.0.1', self.port), timeout=10) for _ in range(8)]
        try:
            self.assertEqual(self.request('GET', self.launch)[0], 200)
        finally:
            for connection in idle:
                connection.close()
        self.assertEqual(self.request('GET', self.launch)[0], 200)


if __name__ == '__main__':
    unittest.main()
