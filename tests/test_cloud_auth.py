import configparser
import json
from pathlib import Path
import select
import signal
import subprocess
import tempfile
import threading
import time
import os
import unittest
from unittest.mock import patch

from support import PRIVATE_FILE, helper_program, posix_signals
from comic_archive import cloud, google_client
from comic_archive.cloud_auth import SignIn, oauth_link, validate
from comic_archive.models import ArchiveError
from comic_archive.network import NetworkPolicyHold
from comic_archive.cloud_errors import CloudTransferError
from unittest.mock import Mock


class CloudSignInTests(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory(); self.addCleanup(work.cleanup)
        self.root = Path(work.name)/'Library'; self.root.mkdir()
        self.signin = SignIn(self.root); self.addCleanup(self.signin.close)
        self.client = {'client_id': 'synthetic.apps.googleusercontent.com', 'client_secret': 'synthetic-secret'}
        cloud._write(self.root/'.archive-state/cloud/google-client.json', {'installed': self.client})

    def finish(self):
        self.signin._thread.join(timeout=30)
        self.assertFalse(self.signin._thread.is_alive())
        return self.signin.status()

    def test_google_opens_provider_browser_without_terminal_and_commits_private_config(self):
        def command(args, log, **kwargs):
            self.assertNotIn('--all', args)
            self.assertIn('config_is_local', args)
            self.assertNotIn('config_shared_client_id', args)
            self.assertNotIn('synthetic-secret', repr(args))
            self.assertEqual(args[args.index('config')+1], 'update')
            self.assertIn('--no-output', args)
            config = Path(args[args.index('--config')+1])
            self.assertEqual(config.stat().st_mode&0o777, PRIVATE_FILE)
            parser = configparser.ConfigParser(interpolation=None); parser.read(config)
            self.assertEqual(parser['google']['scope'], 'drive.file')
            self.assertEqual(parser['google']['client_secret'], 'synthetic-secret')
            with config.open('a') as file:
                file.write('token = {"refresh_token":"PRIVATE_TOKEN"}\n')
        with patch('comic_archive.cloud_tools.ensure'), patch.object(self.signin, '_command', side_effect=command), patch('comic_archive.cloud.configure_target') as configure:
            self.signin.start('google', {})
            status = self.finish()
            self.assertEqual(status['status'], 'connected')
            config = configure.call_args.kwargs['config']
            self.assertEqual(config.stat().st_mode&0o777, PRIVATE_FILE)
            self.assertIn('PRIVATE_TOKEN', config.read_text())
            self.assertNotIn('PRIVATE_TOKEN', json.dumps(status))
            self.assertFalse(list(config.parent.glob('signin-*')))

    def test_proton_uses_private_obscured_config_without_password_in_process_arguments(self):
        seen = []
        def command(args, log, **kwargs):
            self.assertNotIn('PRIVATE_PASSWORD', repr(args))
            config = Path(args[args.index('--config')+1])
            parser = configparser.ConfigParser(); parser.read(config)
            self.assertEqual(parser['proton']['password'], 'OBSCURED_PASSWORD')
            self.assertEqual(parser['proton']['2fa'], '123456')
            seen.append(args)
        with patch('comic_archive.cloud_tools.ensure'), patch.object(self.signin, '_obscure', return_value='OBSCURED_PASSWORD'), patch.object(self.signin, '_command', side_effect=command), patch('comic_archive.cloud.configure_target') as configure:
            credentials = {'username':'user@example.test', 'password':'PRIVATE_PASSWORD', 'code':'123456', 'mailbox_password':'PRIVATE_MAILBOX'}
            self.signin.start('proton', credentials)
            self.assertEqual(self.finish()['status'], 'connected')
            parser = configparser.ConfigParser(); parser.read(configure.call_args.kwargs['config'])
            self.assertNotIn('2fa', parser['proton'])
            self.assertEqual(parser['proton']['mailbox_password'], 'OBSCURED_PASSWORD')
            self.assertIn('--stat', seen[0])
            self.assertNotIn('PRIVATE_PASSWORD', json.dumps(self.signin.status()))

    def test_failed_or_cancelled_repair_preserves_previous_connection_and_cleans_stage(self):
        folder = self.root/'.archive-state/cloud'; folder.mkdir(parents=True, exist_ok=True)
        old = folder/'previous.conf'; old.write_text('PREVIOUS_TOKEN'); old.chmod(0o600)
        for cancel in (False, True):
            def command(*args, **kwargs):
                if cancel: self.signin.cancel()
                raise ArchiveError('PRIVATE_ERROR PRIVATE_PASSWORD')
            with patch('comic_archive.cloud_tools.ensure'), patch.object(self.signin, '_command', side_effect=command), patch('comic_archive.cloud.configure_target') as configure:
                self.signin.start('google', {})
                status = self.finish()
                self.assertEqual(status['status'], 'cancelled' if cancel else 'error')
                self.assertNotIn('PRIVATE', json.dumps(status)); configure.assert_not_called()
                self.assertEqual(old.read_text(), 'PREVIOUS_TOKEN')
                self.assertFalse(list(folder.glob('signin-*')))
                self.assertFalse(list(folder.glob('connection-*')))

    def saved_connection(self):
        config = self.root/'.archive-state/cloud/saved.conf'
        config.write_text('[google]\ntype=drive\nclient_id=synthetic.apps.googleusercontent.com\nclient_secret=synthetic-secret\ntoken=PRIVATE_REFRESH_TOKEN\n')
        config.chmod(0o600)
        with patch('comic_archive.cloud.Transfer'):
            cloud.configure_target(self.root, 'google', config=config)
        return config

    def test_restart_reuses_durable_session_and_preserves_target_and_receipts(self):
        config = self.saved_connection()
        before = cloud.settings(self.root)
        transfer = Mock()
        def refresh(*args, **kwargs):
            self.assertNotIn('PRIVATE', repr(args))
            config.write_text(config.read_text().replace('PRIVATE_REFRESH_TOKEN', 'PRIVATE_REFRESHED_TOKEN'))
            return b''
        transfer.run.side_effect = refresh
        for _ in range(2):
            signin = SignIn(self.root)
            with patch('comic_archive.cloud_tools.ensure'), patch('comic_archive.cloud.transfer_for', return_value=transfer), patch.object(signin, '_command') as command:
                signin.start('google', {}); signin._thread.join(5)
                self.assertEqual(signin.status()['status'], 'connected')
                self.assertTrue(signin.status()['reused_session'])
                command.assert_not_called()
                self.assertNotIn('PRIVATE', json.dumps(signin.status()))
                self.assertEqual(cloud.settings(self.root), before)
                self.assertFalse(list(config.parent.glob('connection-*')))
                signin.close()
        self.assertIn('PRIVATE_REFRESHED_TOKEN', config.read_text())

    def test_quota_failure_retains_saved_session_without_opening_browser(self):
        config = self.saved_connection(); before = config.read_bytes()
        target = cloud.settings(self.root)
        transfer = Mock(); transfer.run.side_effect = CloudTransferError('cloud_rate_limited')
        with patch('comic_archive.cloud_tools.ensure'), patch('comic_archive.cloud.transfer_for', return_value=transfer), patch.object(self.signin, '_command') as command:
            self.signin.start('google', {})
            self.assertEqual(self.finish()['code'], 'cloud_rate_limited'); command.assert_not_called()
        self.assertEqual(config.read_bytes(), before)
        self.assertEqual(cloud.settings(self.root), target)

    def test_adopted_encrypted_config_reuses_rclone_without_parsing_credentials(self):
        config = self.saved_connection()
        config.write_text('RCLONE_ENCRYPT_V0:\nPRIVATE_ENCRYPTED_SESSION\n')
        (self.root/'.archive-state/cloud/google-client.json').unlink()
        with patch('comic_archive.cloud_tools.ensure'), patch('comic_archive.cloud.transfer_for', return_value=Mock()), patch.object(self.signin, '_command') as command:
            self.signin.start('google', {})
            self.assertTrue(self.finish()['reused_session']); command.assert_not_called()
        self.assertIn('PRIVATE_ENCRYPTED_SESSION', config.read_text())

    def test_terminal_connect_also_reuses_saved_session_without_another_login(self):
        config = self.saved_connection(); before = cloud.settings(self.root)
        with patch('comic_archive.cloud_tools.ensure'), patch('comic_archive.cloud.transfer_for', return_value=Mock()), patch.object(SignIn, '_command') as command:
            status = cloud.connect(self.root, 'google')
            self.assertTrue(status['targets'][0]['connected']); command.assert_not_called()
        self.assertEqual(cloud.settings(self.root), before)
        self.assertTrue(config.exists())

    def test_revoked_refresh_token_requires_one_browser_signin_and_saves_new_session(self):
        previous = self.saved_connection()
        transfer = Mock(); transfer.run.side_effect = CloudTransferError('cloud_login_required')
        def command(args, log):
            config = Path(args[args.index('--config')+1])
            with config.open('a') as file: file.write('token={"refresh_token":"PRIVATE_NEW_SESSION"}\n')
        with patch('comic_archive.cloud_tools.ensure'), patch('comic_archive.cloud.transfer_for', return_value=transfer), patch.object(self.signin, '_command', side_effect=command), patch('comic_archive.cloud.Transfer'):
            self.signin.start('google', {})
            self.assertEqual(self.finish()['status'], 'connected')
        saved = Path(cloud.settings(self.root)['targets']['google']['config'])
        self.assertIn('PRIVATE_NEW_SESSION', saved.read_text())
        self.assertTrue(previous.exists())

    def test_new_client_starts_fresh_oauth_without_using_old_client_token(self):
        previous = self.saved_connection()
        previous.write_text(previous.read_text().replace('synthetic.apps.', 'old.apps.'))
        with patch('comic_archive.cloud_tools.ensure'), patch('comic_archive.cloud.transfer_for') as reuse, patch.object(self.signin, '_command', side_effect=ArchiveError('cancelled')):
            self.signin.start('google', {}); self.finish(); reuse.assert_not_called()
        self.assertIn('old.apps.', previous.read_text())

    def test_oauth_without_refresh_token_never_replaces_previous_session(self):
        previous = self.saved_connection(); before = cloud.settings(self.root)
        transfer = Mock(); transfer.run.side_effect = CloudTransferError('cloud_login_required')
        def command(args, log):
            with Path(args[args.index('--config')+1]).open('a') as file:
                file.write('token={"access_token":"PRIVATE_ACCESS_ONLY"}\n')
        with patch('comic_archive.cloud_tools.ensure'), patch('comic_archive.cloud.transfer_for', return_value=transfer), patch.object(self.signin, '_command', side_effect=command):
            self.signin.start('google', {})
            self.assertEqual(self.finish()['status'], 'error')
        self.assertEqual(cloud.settings(self.root), before)
        self.assertIn('PRIVATE_REFRESH_TOKEN', previous.read_text())
        self.assertFalse(list(previous.parent.glob('connection-*')))

    def test_saved_session_probe_cancels_its_owned_network_work(self):
        previous = self.saved_connection(); entered = threading.Event()
        transfer = Mock()
        def probe(*args, **kwargs):
            entered.set(); self.signin._cancel.wait(3); transfer.guard.check()
        transfer.run.side_effect = probe
        with patch('comic_archive.cloud_tools.ensure'), patch('comic_archive.cloud.transfer_for', return_value=transfer), patch.object(self.signin, '_command') as command:
            self.signin.start('google', {}); self.assertTrue(entered.wait(2))
            self.signin.close(); self.assertEqual(self.finish()['status'], 'cancelled')
            command.assert_not_called()
        self.assertTrue(previous.exists())

    def test_unregistered_app_never_silently_uses_shared_client(self):
        (self.root/'.archive-state/cloud/google-client.json').unlink()
        with patch('comic_archive.cloud_tools.ensure'), patch.object(self.signin, '_command') as command:
            self.signin.start('google', {})
            self.assertEqual(self.finish()['code'], 'cloud_client_required'); command.assert_not_called()

    def test_client_import_is_private_bounded_and_never_changes_saved_session(self):
        saved = self.saved_connection(); before = cloud.settings(self.root)
        source = self.root.parent/'client.json'
        source.write_text(json.dumps({'installed': self.client | {'project_id':'synthetic-project'}}))
        google_client.install(self.root, source)
        private = self.root/'.archive-state/cloud/google-client.json'
        self.assertEqual(private.stat().st_mode & 0o777, PRIVATE_FILE)
        self.assertEqual(google_client.load(self.root), self.client)
        self.assertEqual(cloud.settings(self.root), before)
        self.assertIn('PRIVATE_REFRESH_TOKEN', saved.read_text())
        for invalid in ({'web':self.client}, {'installed':self.client | {'client_secret':'secret\nvalue'}}, {'installed':self.client | {'client_id':'https://evil.test'}}):
            source.write_text(json.dumps(invalid))
            with self.assertRaises(ArchiveError): google_client.install(self.root, source)
        source.write_text('x'*65537)
        with self.assertRaises(ArchiveError): google_client.install(self.root, source)
        source.unlink(); source.symlink_to(private)
        with self.assertRaises(ArchiveError): google_client.install(self.root, source)

    def test_duplicate_signin_is_rejected_and_shutdown_cancels_owned_work(self):
        entered = threading.Event()
        def command(*args, **kwargs):
            entered.set(); self.signin._cancel.wait(3)
            raise ArchiveError('Cancelled')
        with patch('comic_archive.cloud_tools.ensure'), patch.object(self.signin, '_command', side_effect=command):
            self.signin.start('google', {}); self.assertTrue(entered.wait(2))
            with self.assertRaises(ArchiveError): self.signin.start('google', {})
            self.signin.close()
            self.assertEqual(self.finish()['status'], 'cancelled')

    @posix_signals
    def test_real_signin_child_publishes_only_callback_and_is_reaped_on_cancel(self):
        pidfile = self.root.parent/'child.pid'
        script = helper_program(self.root.parent/'fake-rclone', 'import os,time\nfrom pathlib import Path\n'
            + 'Path('+repr(str(pidfile))+').write_text(str(os.getpid()))\n'
            + 'print("PRIVATE_DIAGNOSTIC http://127.0.0.1:53682/auth?state='+('a'*32)+'", flush=True)\n'
            + 'time.sleep(30)\n')
        with patch('comic_archive.cloud_tools.ensure'), patch('comic_archive.cloud._binary', return_value=str(script)):
            self.signin.start('google', {})
            deadline = time.monotonic()+4
            while not self.signin.status().get('auth_url') and time.monotonic()<deadline:
                time.sleep(0.02)
            self.assertTrue(self.signin.status().get('auth_url'))
            self.assertNotIn('PRIVATE_DIAGNOSTIC', json.dumps(self.signin.status()))
            pid = int(pidfile.read_text())
            self.signin.close(); self.assertEqual(self.finish()['status'], 'cancelled')
            with self.assertRaises(ProcessLookupError): os.kill(pid, 0)

    def script(self, name, body):
        return helper_program(self.root.parent/name, body)

    def test_malformed_addresses_in_helper_output_are_skipped_not_fatal(self):
        good = 'http://127.0.0.1:53682/auth?state='+'a'*32
        text = 'http://[::1/auth http://127.0.0.1:99999/auth?state='+'a'*32+' '+good
        self.assertEqual(oauth_link(text), good)

    def test_secret_goes_to_the_obscure_helper_on_stdin_and_unusable_output_is_refused(self):
        echo = self.script('obscure-echo', 'import sys\nsys.stdout.write("X"+sys.stdin.buffer.read().hex()+"\\n")\n')
        with patch('comic_archive.cloud._binary', return_value=str(echo)):
            self.assertEqual(self.signin._obscure('long-secret-value'), 'X'+b'long-secret-value'.hex())
        for name, body in (('obscure-spaces', 'import sys\nsys.stdin.buffer.read()\nprint("not a usable value")\n'),
                           ('obscure-fails', 'import sys\nsys.stdin.buffer.read()\nsys.exit(3)\n')):
            with self.subTest(name), patch('comic_archive.cloud._binary', return_value=str(self.script(name, body))):
                with self.assertRaises(ArchiveError): self.signin._obscure('long-secret-value')

    @posix_signals
    def test_hung_obscure_helper_is_killed_and_the_signin_fails(self):
        hung = self.script('obscure-hung', 'import time\ntime.sleep(60)\n')
        children = []; real = subprocess.Popen.communicate; calls = []
        def communicate(process, *args, **kwargs):
            if not calls:
                calls.append(1); children.append(process)
                raise subprocess.TimeoutExpired(process.args, 10)
            return real(process, *args, **kwargs)
        with patch('comic_archive.cloud._binary', return_value=str(hung)), patch.object(subprocess.Popen, 'communicate', communicate):
            with tempfile.TemporaryFile() as log, self.assertRaises(ArchiveError):
                self.signin._command(['obscure', '-'], log, input_data=b'secret')
        self.assertEqual(children[0].returncode, -signal.SIGKILL)

    def test_helper_exit_status_decides_whether_the_signin_step_succeeded(self):
        for code, ok in ((0, True), (1, False)):
            helper = self.script(f'helper-{code}', f'import sys\nsys.exit({code})\n')
            with self.subTest(code=code), patch('comic_archive.cloud._binary', return_value=str(helper)), tempfile.TemporaryFile() as log:
                if ok: self.signin._command(['config'], log)
                else:
                    with self.assertRaises(ArchiveError): self.signin._command(['config'], log)

    @posix_signals
    def test_cancelled_signin_kills_a_helper_that_ignores_termination(self):
        ready = self.root.parent/'ready'; os.mkfifo(ready)
        helper = self.script('helper-stubborn', 'import os,signal,time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\n'
                             + f'open({str(ready)!r}, "w").write(str(os.getpid()))\ntime.sleep(60)\n')
        descriptor = os.open(ready, os.O_RDONLY | os.O_NONBLOCK); errors = []
        def run():
            with tempfile.TemporaryFile() as log:
                try: self.signin._command(['config'], log)
                except ArchiveError as error: errors.append(error)
        try:
            with patch('comic_archive.cloud._binary', return_value=str(helper)):
                thread = threading.Thread(target=run); thread.start()
                try:
                    self.assertTrue(select.select([descriptor], [], [], 30)[0])
                    pid = int(os.read(descriptor, 32))
                finally:
                    self.signin.cancel(); thread.join(timeout=30)
        finally: os.close(descriptor)
        self.assertFalse(thread.is_alive()); self.assertEqual(len(errors), 1)
        with self.assertRaises(ProcessLookupError): os.kill(pid, 0)

    def test_cancel_during_the_last_step_discards_the_new_connection(self):
        def command(args, log, **kwargs):
            Path(args[args.index('--config')+1]).write_text('[google]\ntype = drive\ntoken = {"refresh_token":"PRIVATE_TOKEN"}\n')
            self.signin.cancel()
        with patch('comic_archive.cloud_tools.ensure'), patch.object(self.signin, '_command', side_effect=command), patch('comic_archive.cloud.configure_target') as configure:
            self.signin.start('google', {})
            status = self.finish()
        self.assertEqual((status['status'], status['code']), ('cancelled', 'signin_cancelled'))
        configure.assert_not_called()
        folder = self.root/'.archive-state/cloud'
        self.assertFalse(list(folder.glob('connection-*'))); self.assertFalse(list(folder.glob('signin-*')))

    def test_rejected_connection_leaves_no_config_behind_and_reports_a_fixed_code(self):
        def command(args, log, **kwargs):
            Path(args[args.index('--config')+1]).write_text('[google]\ntype = drive\ntoken = {"refresh_token":"PRIVATE_TOKEN"}\n')
        folder = self.root/'.archive-state/cloud'
        for error, code in ((ArchiveError('PRIVATE_DETAIL'), 'signin_failed'), (NetworkPolicyHold('PRIVATE_DETAIL'), 'signin_network_held')):
            with self.subTest(code=code), patch('comic_archive.cloud_tools.ensure'), patch.object(self.signin, '_command', side_effect=command), \
                    patch('comic_archive.cloud.configure_target', side_effect=error):
                self.signin.start('google', {})
                status = self.finish()
                self.assertEqual((status['status'], status['code']), ('error', code))
                self.assertNotIn('PRIVATE', json.dumps(status))
                self.assertFalse(list(folder.glob('connection-*'))); self.assertFalse(list(folder.glob('signin-*')))

    def test_proton_without_code_or_mailbox_password_stores_neither(self):
        with patch('comic_archive.cloud_tools.ensure'), patch.object(self.signin, '_obscure', return_value='OBSCURED_PASSWORD'), \
                patch.object(self.signin, '_command'), patch('comic_archive.cloud.configure_target') as configure:
            self.signin.start('proton', {'username':' user@example.test ', 'password':'PRIVATE_PASSWORD'})
            self.assertEqual(self.finish()['status'], 'connected')
            parser = configparser.ConfigParser(); parser.read(configure.call_args.kwargs['config'])
            self.assertEqual(dict(parser['proton']), {'type':'protondrive', 'username':'user@example.test', 'password':'OBSCURED_PASSWORD'})

    def test_oauth_link_only_exposes_fixed_loopback_provider_callback(self):
        url = 'http://127.0.0.1:53682/auth?state='+'a'*32
        self.assertEqual(oauth_link('Private diagnostic '+url), url)
        for url in ('https://evil.test/auth?state='+'a'*32, 'http://127.0.0.1:8765/auth?state='+'a'*32,
                    'http://user@127.0.0.1:53682/auth?state='+'a'*32,
                    'http://127.0.0.1:53682/auth?state='+'a'*32+'&token=PRIVATE'):
            self.assertIsNone(oauth_link(url))

    def test_auth_payloads_are_fixed_and_bounded(self):
        for provider, credentials in [('google', {'password':'PRIVATE'}), ([], {}), ('ftp', {}),
                ('proton', {}), ('proton', {'username':'u', 'password':'p', 'path':'/outside'}),
                ('proton', {'username':'u', 'password':'p\nq'}),
                ('proton', {'username':'u', 'password':'p', 'code':'bad'}),
                ('proton', {'username':'u', 'password':'x'*1025})]:
            with self.assertRaises(ArchiveError): validate(provider, credentials)
