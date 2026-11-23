import base64
import socket
import unittest
from unittest.mock import Mock, patch

from comic_archive import browser_egress


def headers(stream):
    with stream.makefile('rb') as source:
        answer = bytearray()
        while not answer.endswith(b'\r\n\r\n') and len(answer) < 8192:
            byte = source.read(1)
            if not byte:
                break
            answer.extend(byte)
    return bytes(answer)


class BrowserEgressTests(unittest.TestCase):
    def setUp(self):
        self.egress = browser_egress.BrowserEgress()
        self.addCleanup(self.egress.close)
        self.host, self.port = self.egress.proxy_url().removeprefix('http://').split(':')

    def client(self):
        return socket.create_connection((self.host, int(self.port)), timeout=2)

    def test_only_authenticated_https_uses_the_pinned_connector(self):
        partner, relay = socket.socketpair()
        with partner, relay, patch.object(browser_egress, '_connect_public', return_value=relay) as connect:
            origin = b'CONNECT images.example.com:443 HTTP/1.1\r\n'
            with self.client() as client:
                client.sendall(origin + b'\r\n')
                self.assertTrue(headers(client).startswith(b'HTTP/1.1 407 '))
                connect.assert_not_called()
            auth = Mock()
            auth.realm.return_value = self.egress._realm
            self.egress.authenticate(None, auth, self.host)
            username, = auth.setUser.call_args.args
            password, = auth.setPassword.call_args.args
            credentials = base64.b64encode(f'{username}:{password}'.encode('ascii'))
            with self.client() as client:
                client.sendall(origin + b'Proxy-Authorization: Basic ' + credentials + b'\r\n\r\n')
                self.assertEqual(headers(client), browser_egress.CONNECTED)
                connect.assert_called_once_with('images.example.com', 443, browser_egress.SOCKET_SECONDS, None)
                partner.settimeout(2)
                client.sendall(b'fixture request')
                self.assertEqual(partner.recv(128), b'fixture request')
                partner.sendall(b'fixture response')
                self.assertEqual(client.recv(128), b'fixture response')
                client.shutdown(socket.SHUT_WR)
                self.assertEqual(partner.recv(128), b'')

    def test_plain_http_and_nonstandard_ports_do_not_resolve_or_connect(self):
        with patch.object(browser_egress, '_connect_public') as connect:
            for request in (b'GET http://images.example.com/ HTTP/1.1\r\n\r\n',
                            b'CONNECT images.example.com:8443 HTTP/1.1\r\n\r\n'):
                with self.client() as client:
                    client.sendall(request)
                    self.assertEqual(headers(client), browser_egress.REFUSED)
            connect.assert_not_called()

    def test_authentication_is_scoped_to_the_bound_listener_and_realm(self):
        auth = Mock()
        auth.realm.return_value = self.egress._realm
        self.egress.authenticate(None, auth, 'example.com')
        auth.realm.return_value = 'another realm'
        self.egress.authenticate(None, auth, self.host)
        auth.setUser.assert_not_called()
        auth.setPassword.assert_not_called()
        self.assertFalse(self.egress.password_for(self.host, 'another realm'))
        self.assertTrue(self.egress.password_for(self.host, self.egress._realm))
