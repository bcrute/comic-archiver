import importlib.util
import socket
import ssl
import unittest
from unittest.mock import Mock, patch

from comic_archive.provider_http import DestinationRefused, _connect_public, provider_client


def dns_answer(family, endpoint):
    return family, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', endpoint


V4 = ('1.1.1.1', 443)
V6 = ('2606:4700:4700::1111', 443, 0, 0)
PUBLIC_ANSWERS = [dns_answer(socket.AF_INET6, V6), dns_answer(socket.AF_INET, V4)]


class PublicSocketTests(unittest.TestCase):
    def test_all_addresses_are_checked_before_any_socket_opens(self):
        for invalid in (dns_answer(socket.AF_INET, ('192.0.2.10', 443)),
                        dns_answer(socket.AF_INET6, ('fd00::1', 443, 0, 0)),
                        dns_answer(socket.AF_INET6, (*V6[:3], 4)),
                        dns_answer(socket.AF_INET, (V4[0], 8443))):
            with self.subTest(answer=invalid):
                with patch('socket.getaddrinfo', return_value=[*PUBLIC_ANSWERS, invalid]), \
                     patch('socket.socket') as create:
                    with self.assertRaises(DestinationRefused):
                        _connect_public('cdn.tapas.io', 443, 30, None)
                    create.assert_not_called()

    def test_multiple_public_addresses_use_one_lookup_and_numeric_connects(self):
        first, second = Mock(), Mock()
        first.connect.side_effect = ConnectionRefusedError
        second.getpeername.return_value = V4
        with patch('socket.getaddrinfo', return_value=PUBLIC_ANSWERS) as lookup, \
             patch('socket.socket', side_effect=[first, second]):
            self.assertIs(_connect_public('cdn.tapas.io', 443, 30, None), second)
        lookup.assert_called_once_with('cdn.tapas.io', 443, socket.AF_UNSPEC, socket.SOCK_STREAM, socket.IPPROTO_TCP)
        first.connect.assert_called_once_with(V6)
        first.close.assert_called_once_with()
        second.connect.assert_called_once_with(V4)
        second.close.assert_not_called()

    def test_unexpected_peer_closes_the_socket(self):
        sock = Mock()
        sock.getpeername.return_value = ('8.8.8.8', 443)
        with patch('socket.getaddrinfo', return_value=PUBLIC_ANSWERS[1:]), \
             patch('socket.socket', return_value=sock):
            with self.assertRaises(DestinationRefused):
                _connect_public('cdn.tapas.io', 443, 30, None)
        sock.close.assert_called_once_with()

    def test_unapproved_port_and_literal_are_refused_before_lookup(self):
        with patch('socket.getaddrinfo') as lookup:
            for host, port in [('cdn.tapas.io', 8443), ('::1', 443), ('', 443)]:
                with self.subTest(host=host, port=port), self.assertRaises(DestinationRefused):
                    _connect_public(host, port, 30, None)
            lookup.assert_not_called()


@unittest.skipUnless(importlib.util.find_spec('httpx'), 'HTTPX is required for the HTTP adapter contract')
class ProviderHTTPContractTests(unittest.TestCase):
    def test_nonstandard_https_authorities_are_refused_before_connection(self):
        import httpx

        with provider_client() as client, patch('socket.getaddrinfo') as lookup:
            for url in ('https://person@cdn.tapas.io/page', 'https://cdn.tapas.io:8443/page', 'http://cdn.tapas.io/page'):
                with self.subTest(url=url), self.assertRaises(httpx.UnsupportedProtocol):
                    client.get(url)
            lookup.assert_not_called()

    def test_tls_and_http_keep_the_original_origin_and_redirect_is_not_followed(self):
        for workers in (None, 64):
            with self.subTest(workers=workers):
                raw, tls = Mock(), Mock()
                raw.getpeername.return_value = V4
                tls.send.side_effect = len
                tls.recv.return_value = (b'HTTP/1.1 302 Found\r\nLocation: https://another.example/\r\n'
                                         b'Content-Length: 0\r\nConnection: close\r\n\r\n')
                with provider_client(workers=workers) as client, \
                     patch('socket.getaddrinfo', return_value=PUBLIC_ANSWERS[1:]) as lookup, \
                     patch('socket.socket', return_value=raw), \
                     patch.object(ssl.SSLContext, 'wrap_socket', autospec=True, return_value=tls) as wrap:
                    response = client.get('https://cdn.tapas.io/page')
                self.assertEqual(response.status_code, 302)
                lookup.assert_called_once()
                raw.connect.assert_called_once_with(V4)
                context, wrapped_socket = wrap.call_args.args
                self.assertIs(wrapped_socket, raw)
                self.assertTrue(context.check_hostname)
                self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
                self.assertEqual(wrap.call_args.kwargs['server_hostname'], 'cdn.tapas.io')
                head = b''.join(call.args[0] for call in tls.send.call_args_list).lower()
                self.assertIn(b'\r\nhost: cdn.tapas.io\r\n', head)
