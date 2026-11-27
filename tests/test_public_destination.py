"""Written-host policy and its application to the real provider route handlers."""
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from comic_archive.adapters.tapas import BrowserReadTransport, TapasAdapter
from comic_archive.public_destination import private_destination
from comic_archive.qt_engine import Request, Route


class PublicDestinationTests(unittest.TestCase):
    def test_local_names_and_special_address_classes_are_refused(self):
        for host in ('localhost', 'router', 'device.LOCAL.', 'router.home.arpa', '127.0.0.1',
                     '10.10.0.7', '100.64.0.1', '[fd00::1]', '::ffff:10.0.0.1',
                     '2002::1', '64:ff9b::1', 'bad:address'):
            with self.subTest(host=host):
                self.assertTrue(private_destination(host))
        for host in ('tapas.io', 'accounts.google.com', 'media.example.com', '8.8.8.8',
                     '2606:4700:4700::1111', '::ffff:8.8.8.8', ''):
            with self.subTest(host=host):
                self.assertFalse(private_destination(host))

    def test_an_address_translated_for_an_ipv6_only_network_is_judged_by_the_ipv4_address_it_carries(self):
        for host in ('64:ff9b::808:808', '[64:ff9b::8.8.8.8]', '64:ff9b::6812:1a2b'):
            with self.subTest(host=host):
                self.assertFalse(private_destination(host))
        # A local or special address is no more reachable through the translation than directly.
        for host in ('64:ff9b::a0a:7', '64:ff9b::7f00:1', '64:ff9b::c0a8:101', '64:ff9b::6440:1', '64:ff9b::c000:1',
                     '64:ff9b::e000:1', '64:ff9b:1::808:808', '64:ff9a::808:808'):
            with self.subTest(host=host):
                self.assertTrue(private_destination(host))

    @patch('comic_archive.adapters.tapas.Transport')
    def test_local_destinations_are_refused_by_every_provider_browser_route(self, _):
        adapter = TapasAdapter(Path('session'))
        adapter.network_guard = Mock()
        transport = BrowserReadTransport(Mock(), Mock())
        for handler, allowed in ((adapter.network_request, 'fallback'), (adapter.network_read_only_request, 'continue'),
                                 (transport.read_only_request, 'continue')):
            for address in ('https://127.0.0.1/', 'https://router.local/', 'https://[fd00::1]/'):
                with self.subTest(handler=handler.__name__, address=address):
                    route = Route(Request(address))
                    handler(route)
                    self.assertEqual(route.outcome, 'abort')
            for address in ('https://tapas.io/', 'https://media.example.com/panel.png', 'data:image/png;base64,AA=='):
                route = Route(Request(address))
                handler(route)
                self.assertEqual(route.outcome, allowed)
