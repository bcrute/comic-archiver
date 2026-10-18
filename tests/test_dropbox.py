import base64
import configparser
import hashlib
from http.client import HTTPConnection
from http.server import HTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
from urllib.parse import parse_qs, urlsplit
import unittest
from unittest.mock import Mock, patch

from support import PRIVATE_FILE
from comic_archive import cloud, dropbox_auth, dropbox_client, oauth_registration
from comic_archive.cloud_auth import SignIn, validate
from comic_archive.cloud_errors import CloudTransferError
from comic_archive.models import ArchiveError


class DropboxTests(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory(); self.addCleanup(work.cleanup)
        self.base = Path(work.name); self.root = self.base / 'Library'; self.root.mkdir()
        self.client = {'client_id': 'synthetic-app', 'client_secret': 'synthetic-secret'}
        cloud._write(self.root / '.archive-state/cloud/dropbox-client.json', self.client)
        self.signin = SignIn(self.root); self.addCleanup(self.signin.close)

    def finish(self):
        self.signin._thread.join(5)
        self.assertFalse(self.signin._thread.is_alive())
        return self.signin.status()

    def saved(self):
        path = self.root / '.archive-state/cloud/saved.conf'
        parser = configparser.ConfigParser(interpolation=None)
        parser['dropbox'] = {'type': 'dropbox', **self.client, 'token': 'PRIVATE_SAVED_TOKEN'}
        with path.open('w') as stream: parser.write(stream)
        path.chmod(0o600)
        with patch('comic_archive.cloud.Transfer'):
            cloud.configure_target(self.root, 'dropbox', config=path)
        return path

    def test_saved_session_survives_restart_without_browser_or_target_replacement(self):
        path = self.saved(); before = cloud.settings(self.root)
        for _ in range(2):
            signin = SignIn(self.root)
            with patch('comic_archive.cloud_tools.ensure'), patch('comic_archive.cloud.transfer_for', return_value=Mock()), patch('comic_archive.dropbox_auth.authorize') as browser:
                signin.start('dropbox', {}); signin._thread.join(5)
                self.assertTrue(signin.status()['reused_session']); browser.assert_not_called()
                self.assertEqual(cloud.settings(self.root), before)
                signin.close()
        self.assertIn('PRIVATE_SAVED_TOKEN', path.read_text())

    def test_revoked_session_reauthenticates_once_and_keeps_old_cleanup_connection(self):
        old = self.saved()
        transfer = Mock(); transfer.run.side_effect = CloudTransferError('cloud_login_required')
        with patch('comic_archive.cloud_tools.ensure'), patch('comic_archive.cloud.transfer_for', return_value=transfer), patch('comic_archive.dropbox_auth.authorize', return_value={'refresh_token': 'PRIVATE_NEW_TOKEN'}) as browser, patch('comic_archive.cloud.Transfer'):
            self.signin.start('dropbox', {})
            self.assertEqual(self.finish()['status'], 'connected'); browser.assert_called_once()
        destination = Path(cloud.settings(self.root)['targets']['dropbox']['config'])
        self.assertNotEqual(destination, old)
        self.assertEqual(destination.stat().st_mode & 0o777, PRIVATE_FILE)
        self.assertIn('PRIVATE_NEW_TOKEN', destination.read_text())
        self.assertIn('PRIVATE_SAVED_TOKEN', old.read_text())
        self.assertNotIn('PRIVATE', json.dumps(self.signin.status()))

    def test_rate_limit_does_not_open_browser_or_erase_saved_connection(self):
        path = self.saved(); before = cloud.settings(self.root)
        transfer = Mock(); transfer.run.side_effect = CloudTransferError('cloud_rate_limited')
        with patch('comic_archive.cloud_tools.ensure'), patch('comic_archive.cloud.transfer_for', return_value=transfer), patch('comic_archive.dropbox_auth.authorize') as browser:
            self.signin.start('dropbox', {})
            self.assertEqual(self.finish()['code'], 'cloud_rate_limited'); browser.assert_not_called()
        self.assertEqual(cloud.settings(self.root), before)
        self.assertIn('PRIVATE_SAVED_TOKEN', path.read_text())

    def test_missing_registration_fails_safely_without_using_shared_helper_app(self):
        (self.root / '.archive-state/cloud/dropbox-client.json').unlink()
        with patch('comic_archive.cloud_tools.ensure'), patch('comic_archive.dropbox_auth.authorize') as browser:
            self.signin.start('dropbox', {})
            self.assertEqual(self.finish()['code'], 'cloud_client_required'); browser.assert_not_called()
        self.assertIsNone(cloud.settings(self.root))
        with self.assertRaises(ArchiveError): validate('dropbox', {'password': 'PRIVATE'})

    def test_missing_refresh_token_never_replaces_saved_session(self):
        path = self.saved(); before = cloud.settings(self.root)
        transfer = Mock(); transfer.run.side_effect = CloudTransferError('cloud_login_required')
        with patch('comic_archive.cloud_tools.ensure'), patch('comic_archive.cloud.transfer_for', return_value=transfer), patch('comic_archive.dropbox_auth.authorize', return_value={'access_token': 'PRIVATE'}):
            self.signin.start('dropbox', {})
            self.assertEqual(self.finish()['status'], 'error')
        self.assertEqual(cloud.settings(self.root), before)
        self.assertIn('PRIVATE_SAVED_TOKEN', path.read_text())
        self.assertFalse(list(path.parent.glob('connection-*')))

    def test_publisher_registration_works_for_multiple_fresh_libraries_without_user_setup(self):
        path = self.base / 'publisher.json'
        cloud._write(path, {'dropbox': self.client, 'google': {
            'client_id': 'synthetic.apps.googleusercontent.com', 'client_secret': 'synthetic-secret'}})
        with patch.dict(os.environ, {'COMIC_ARCHIVE_OAUTH_CLIENTS': str(path)}):
            self.assertEqual(oauth_registration.load('dropbox'), self.client)
            self.assertEqual(dropbox_client.load(self.base / 'fresh-one'), self.client)
            self.assertEqual(dropbox_client.load(self.base / 'fresh-two'), self.client)
            from comic_archive.google_client import load
            self.assertEqual(load(self.base / 'fresh-google')['client_id'], 'synthetic.apps.googleusercontent.com')
            self.assertEqual(oauth_registration.load('unknown'), None)

    def test_packaged_registration_needs_no_per_user_file_and_library_override_takes_precedence(self):
        package = self.base / 'package'; package.mkdir()
        registration = package / 'oauth-clients.json'
        registration.write_text(json.dumps({'dropbox': self.client}))
        registration.chmod(0o644)
        with patch.dict(os.environ, {}, clear=True), patch.object(oauth_registration, '__file__', str(package/'oauth_registration.py')):
            self.assertEqual(dropbox_client.load(self.base/'new-user'), self.client)
            override = {'client_id': 'override-app', 'client_secret': 'override-secret'}
            cloud._write(self.root/'.archive-state/cloud/dropbox-client.json', override)
            self.assertEqual(dropbox_client.load(self.root), override)
        source = self.base / 'single.json'; source.write_text(json.dumps(self.client))
        dropbox_client.install(self.root, source)
        self.assertEqual(dropbox_client.load(self.root), self.client)
        for bad in ({}, self.client | {'token': 'PRIVATE'}, self.client | {'client_id': 'bad\nvalue'}):
            with self.assertRaises(ArchiveError): dropbox_client.validate(bad)

    @unittest.skipIf(os.name == 'nt', 'Windows keeps no permission bits; the file is private through the profile folder.')
    def test_named_registration_readable_by_others_is_refused(self):
        registration = self.base / 'shared.json'
        registration.write_text(json.dumps({'dropbox': self.client})); registration.chmod(0o644)
        with patch.dict(os.environ, {'COMIC_ARCHIVE_OAUTH_CLIENTS': str(registration)}), self.assertRaises(ArchiveError):
            oauth_registration.load('dropbox')

    def authorize(self, payload=None, denied=False):
        server_holder, opened, errors, stages = {}, [], [], []
        cancel = threading.Event()
        token = payload or {'access_token': 'PRIVATE_ACCESS', 'refresh_token': 'PRIVATE_REFRESH', 'token_type': 'bearer', 'expires_in': 14400}

        def server(address, handler):
            value = HTTPServer(('127.0.0.1', 0), handler)
            server_holder['value'] = value
            return value

        def request_browser(url):
            def callbacks():
                try:
                    state = parse_qs(urlsplit(url).query)['state'][0]
                    connection = HTTPConnection('127.0.0.1', server_holder['value'].server_port, timeout=3)
                    connection.request('GET', '/?code=PRIVATE_CODE&state=wrong')
                    response = connection.getresponse(); self.assertEqual(response.status, 400); response.read()
                    connection.request('GET', urlsplit(url).path + '?' + urlsplit(url).query)
                    response = connection.getresponse(); self.assertEqual(response.status, 302)
                    query = parse_qs(urlsplit(response.getheader('Location')).query); response.read()
                    self.assertEqual(query['token_access_type'], ['offline'])
                    self.assertEqual(query['code_challenge_method'], ['S256'])
                    self.assertNotIn('sharing.write', query['scope'][0])
                    self.assertNotIn('client_secret', query)
                    opened.append(query)
                    suffix = 'error=access_denied' if denied else 'code=PRIVATE_CODE'
                    connection.request('GET', '/?' + suffix + '&state=' + state)
                    response = connection.getresponse(); self.assertEqual(response.status, 200)
                    self.assertNotIn(b'PRIVATE', response.read()); connection.close()
                except BaseException as error:
                    errors.append(error); cancel.set()
            thread = threading.Thread(target=callbacks); server_holder['thread'] = thread; thread.start()
            return True

        with patch('comic_archive.dropbox_auth.NetworkGuard'), patch('comic_archive.dropbox_auth.HTTPServer', side_effect=server), patch('comic_archive.dropbox_auth.webbrowser.open', side_effect=request_browser), patch('comic_archive.dropbox_auth.httpx.stream') as exchange:
            response = exchange.return_value.__enter__.return_value
            response.status_code = 200; response.iter_bytes.return_value = [json.dumps(token).encode()]
            try:
                result = dropbox_auth.authorize(self.root, self.client, cancel, lambda **fields: stages.append(fields))
                sent = exchange.call_args.kwargs['data']
                challenge = base64.urlsafe_b64encode(hashlib.sha256(sent['code_verifier'].encode()).digest()).rstrip(b'=').decode()
                self.assertEqual(opened[0]['code_challenge'], [challenge])
                self.assertEqual(sent['code'], 'PRIVATE_CODE')
                self.assertNotIn('client_secret', sent)
                self.assertFalse(exchange.call_args.kwargs['trust_env'])
                self.assertNotIn('PRIVATE', json.dumps(stages))
                return result
            finally:
                server_holder['thread'].join(5)
                self.assertFalse(server_holder['thread'].is_alive())
                self.assertEqual(errors, [])
                self.assertEqual(server_holder['value'].fileno(), -1)

    def test_pkce_browser_callback_rejects_wrong_state_and_saves_refresh_token(self):
        token = self.authorize()
        self.assertEqual(token['refresh_token'], 'PRIVATE_REFRESH')
        self.assertEqual(token['token_type'], 'Bearer'); self.assertIn('expiry', token)

    def test_declined_consent_and_invalid_token_responses_never_save_a_session(self):
        with self.assertRaises(CloudTransferError): self.authorize(denied=True)
        for payload in ({'access_token': 'PRIVATE', 'expires_in': 3600, 'token_type': 'bearer'},
                        {'access_token': 'PRIVATE', 'refresh_token': 'PRIVATE', 'expires_in': True, 'token_type': 'bearer'},
                        {'access_token': 'PRIVATE', 'refresh_token': 'PRIVATE', 'expires_in': 3600, 'token_type': []}):
            with self.assertRaises(ArchiveError): self.authorize(payload)

    def test_cancelled_browser_authorization_closes_local_callback_server(self):
        cancelled = threading.Event(); cancelled.set()
        server = Mock(); server.__enter__ = Mock(return_value=server); server.__exit__ = Mock()
        with patch('comic_archive.dropbox_auth.NetworkGuard'), patch('comic_archive.dropbox_auth.HTTPServer', return_value=server), patch('comic_archive.dropbox_auth.webbrowser.open'), patch('comic_archive.dropbox_auth.httpx.stream') as exchange, self.assertRaises(ArchiveError):
            dropbox_auth.authorize(self.root, self.client, cancelled, Mock())
        server.__exit__.assert_called_once(); exchange.assert_not_called()

    def test_provider_native_checksum_verifies_blocks_and_refuses_changed_local_or_remote_bytes(self):
        path = self.base / 'episode.cbz'; data = b'a' * (4 * 1024**2) + b'b'; path.write_bytes(data)
        expected = hashlib.sha256(data).hexdigest()
        native = hashlib.sha256(hashlib.sha256(data[:-1]).digest() + hashlib.sha256(b'b').digest()).hexdigest()
        transfer = object.__new__(cloud.Transfer); transfer.provider = 'dropbox'; transfer.target = {'remote': 'dropbox'}
        transfer.run = Mock(return_value=(native + '  episode.cbz').encode())
        transfer.verify('episode.cbz', expected, source=path)
        transfer.run.assert_called_once_with('hashsum', 'DropboxHash', 'dropbox:episode.cbz')
        transfer.run.return_value = b'0' * 64
        with self.assertRaises(CloudTransferError): transfer.verify('episode.cbz', expected, source=path)
        path.write_bytes(b'changed'); transfer.run.reset_mock()
        with self.assertRaises(CloudTransferError): transfer.verify('episode.cbz', expected, source=path)
        transfer.run.assert_not_called()

    def test_live_pilot_forces_complete_sha256_readback_even_with_provider_hash(self):
        transfer = object.__new__(cloud.Transfer); transfer.provider = 'dropbox'; transfer.target = {'remote': 'dropbox'}
        transfer.full_readback = True; transfer.run = Mock(return_value=b'123  episode.cbz')
        transfer.verify('episode.cbz', '123', source=self.base / 'not-needed')
        # The read-back's time limit grows with the file; a source that is not there counts as empty.
        transfer.run.assert_called_once_with('hashsum', 'SHA256', 'dropbox:episode.cbz', '--download', size=0)
