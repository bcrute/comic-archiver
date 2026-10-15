"""Button-driven cloud sign-in on the trusted device, never on a hosted server.

Credentials are confined to memory and private provider config. Only fixed
status fields and the provider's loopback OAuth link are returned to the UI.
"""
import configparser
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import time
from urllib.parse import parse_qs, urlsplit
import uuid

from . import cloud
from .archive_paths import directory
from .models import ArchiveError
from .locking import LibraryLock
from .osdep import hidden


def validate(provider, credentials):
    if not isinstance(provider, str) or provider not in cloud.PROVIDERS or not isinstance(credentials, dict):
        raise ArchiveError('Choose a cloud provider.')
    if provider == 'b2':
        from .b2_auth import validate as validate_b2
        validate_b2(credentials)
    elif provider in cloud.OAUTH_PROVIDERS:
        if credentials:
            raise ArchiveError('Cloud sign-in opens in your browser.')
    else:
        if (set(credentials) - {'username', 'password', 'code', 'mailbox_password'}
                or any(not isinstance(v, str) or len(v) > 1024 or any(c in v for c in '\r\n\0') for v in credentials.values())
                or not credentials.get('username', '').strip() or not credentials.get('password')):
            raise ArchiveError('Enter your Proton account and password.')
        if credentials.get('code') and not re.fullmatch(r'[0-9]{6}', credentials['code']):
            raise ArchiveError('Enter the six-digit verification code.')


def oauth_link(text):
    for candidate in re.findall(r'https?://[^\s<>"\']+', text):
        try:
            url = urlsplit(candidate)
            query = parse_qs(url.query)
            if (url.scheme == 'http' and url.hostname in {'127.0.0.1', 'localhost'}
                    and url.port == 53682 and url.path == '/auth' and not url.username and not url.password
                    and not url.fragment and set(query) == {'state'} and len(query['state']) == 1
                    and re.fullmatch(r'[A-Za-z0-9_~.+/-]{16,256}', query['state'][0])):
                return candidate
        except ValueError:
            pass
    return None


class SignIn:
    def __init__(self, root):
        self.root = Path(root)
        self._lock = threading.Lock()
        self._state = {'status': 'idle'}
        self._cancel = threading.Event()
        self._thread = None

    def status(self):
        with self._lock:
            return dict(self._state)

    def _update(self, **fields):
        with self._lock:
            self._state.update(fields)

    def start(self, provider, credentials):
        validate(provider, credentials)
        with self._lock:
            if self._thread and self._thread.is_alive():
                raise ArchiveError('Cloud sign-in is already in progress.')
            self._cancel = threading.Event()
            self._state = {'status': 'running', 'provider': provider}
            self._thread = threading.Thread(target=self._run, args=(provider, dict(credentials)), daemon=True)
            self._thread.start()
        return self.status()

    def cancel(self):
        self._cancel.set()
        return self.status()

    def close(self):
        self.cancel()
        if self._thread:
            self._thread.join(timeout=3)

    def _command(self, args, log, *, input_data=None):
        executable = cloud._binary()
        with subprocess.Popen([executable, *args], stdin=subprocess.PIPE if input_data is not None else subprocess.DEVNULL,
                              stdout=log, stderr=log, env=cloud._environment(), **hidden()) as process:
            if input_data is not None:
                try:
                    process.communicate(input_data, timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill(); process.communicate()
                    raise ArchiveError('Cloud sign-in failed.') from None
                if process.returncode:
                    raise ArchiveError('Cloud sign-in failed.')
                return
            deadline = time.monotonic() + 600
            while process.poll() is None:
                if self._cancel.wait(0.2) or time.monotonic() >= deadline:
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill(); process.wait()
                    raise ArchiveError('Cloud sign-in stopped.')
                log.seek(0)
                text = log.read(65536).decode('utf-8', errors='replace')
                if link := oauth_link(text):
                    self._update(auth_url=link)
                log.seek(0, os.SEEK_END)
            if process.returncode:
                raise ArchiveError('Cloud sign-in failed.')

    def _obscure(self, value):
        with tempfile.TemporaryFile() as log:
            self._command(['obscure', '-'], log, input_data=value.encode())
            log.seek(0); result = log.read(4096).decode('ascii').strip()
            if not re.fullmatch(r'[A-Za-z0-9_-]{16,2048}', result):
                raise ArchiveError('Cloud sign-in failed.')
            return result

    def _run(self, provider, credentials):
        try:
            root = cloud.safe_root(self.root)
            from .cloud_tools import ensure
            self._update(stage='preparing')
            ensure(root, self._cancel)
            self._update(stage='authenticating')
            folder = directory(root / '.archive-state/cloud'); folder.chmod(0o700)
            with tempfile.TemporaryDirectory(prefix='signin-', dir=folder) as work:
                config = Path(work) / 'rclone.conf'
                config.touch(mode=0o600)
                common = ['--config', str(config), '--ask-password=false']
                with tempfile.TemporaryFile() as log:
                    if provider == 'b2':
                        from .b2_auth import connection
                        self._update(stage='checking_b2')
                        parser = configparser.ConfigParser(interpolation=None)
                        parser['b2'] = connection(root, credentials, self._cancel)
                        credentials.clear()
                        with config.open('w') as stream:
                            parser.write(stream)
                    elif provider in cloud.OAUTH_PROVIDERS:
                        from . import google_client, dropbox_client
                        registration = google_client if provider == 'google' else dropbox_client
                        client = registration.load(root)
                        value = cloud.settings(root)
                        previous = value and value['targets'].get(provider)
                        if previous:
                            matches = client is None
                            if client is not None:
                                saved = configparser.ConfigParser(interpolation=None)
                                try:
                                    saved.read(cloud._config(previous['config']))
                                    section = saved[previous['remote']]
                                    matches = all(section.get(k) == v for k, v in client.items())
                                except (configparser.Error, KeyError):
                                    # Externally encrypted configs are still
                                    # reusable by rclone when no new registration
                                    # was supplied; never decrypt them into logs.
                                    matches = False
                            # Refresh in the durable config itself: do not copy
                            # tokens into a temporary file or replace identity.
                            if matches:
                                self._update(stage='checking_saved_session')
                                transfer = cloud.transfer_for(root, provider, previous)
                                parent_guard = transfer.guard
                                cancel = self._cancel
                                class SignInGuard:
                                    def check(self):
                                        if cancel.is_set():
                                            raise ArchiveError('Cloud sign-in stopped.')
                                        parent_guard.check()
                                transfer.guard = SignInGuard()
                                try:
                                    transfer.run('lsf', transfer.remote('Comic-Archive/' + value['library_id']),
                                                 '--dirs-only', missing_ok=True)
                                except cloud.CloudTransferError as error:
                                    if error.cloud_code != 'cloud_login_required':
                                        raise
                                else:
                                    self._update(status='connected', auth_url=None, reused_session=True)
                                    return
                        if client is None:
                            raise cloud.CloudTransferError('cloud_client_required')
                        # Client credentials belong in the private file, never
                        # argv, logs, UI status or test snapshots.
                        parser = configparser.ConfigParser(interpolation=None)
                        parser[provider] = {'type': cloud.PROVIDERS[provider], **client}
                        if provider == 'google':
                            parser[provider]['scope'] = 'drive.file'
                            with config.open('w') as stream:
                                parser.write(stream)
                            self._command([*common, 'config', 'update', provider,
                                           'config_is_local', 'true', 'config_change_team_drive', 'false', '--no-output'], log)
                            parser.read(config)
                            token = json.loads(parser[provider].get('token', '{}'))
                        else:
                            from .dropbox_auth import authorize
                            token = authorize(root, client, self._cancel, self._update)
                            parser[provider]['token'] = json.dumps(token)
                            with config.open('w') as stream:
                                parser.write(stream)
                        if not isinstance(token, dict) or not isinstance(token.get('refresh_token'), str) or not token['refresh_token']:
                            raise ArchiveError('The provider did not save a reusable sign-in. Try signing in again.')
                    else:
                        parser = configparser.ConfigParser(interpolation=None)
                        parser['proton'] = {'type':'protondrive', 'username':credentials['username'].strip(),
                                            'password':self._obscure(credentials['password'])}
                        if credentials.get('mailbox_password'):
                            parser['proton']['mailbox_password'] = self._obscure(credentials['mailbox_password'])
                        if credentials.get('code'):
                            parser['proton']['2fa'] = credentials['code']
                        with config.open('w') as stream:
                            parser.write(stream)
                        credentials.clear()
                        # Opening the provider authenticates without uploading,
                        # deleting or managing the user's cloud contents.
                        self._command([*common, 'lsjson', 'proton:', '--stat'], log)
                        parser.read(config)
                        parser.remove_option('proton', '2fa')
                        with config.open('w') as stream:
                            parser.write(stream)
                if self._cancel.is_set():
                    raise ArchiveError('Cloud sign-in stopped.')
                # Each attempt has a new config; failed/cancelled repairs cannot
                # damage the previous authenticated connection or its receipts.
                destination = folder / ('connection-' + uuid.uuid4().hex + '.conf')
                with LibraryLock(root):
                    os.replace(config, destination); destination.chmod(0o600)
                    try:
                        cloud.configure_target(root, provider, config=destination)
                    except BaseException:
                        destination.unlink(missing_ok=True)
                        raise
                self._update(status='connected', auth_url=None)
        except Exception as error:
            from .network import NetworkPolicyHold
            code = ('signin_network_held' if isinstance(error, NetworkPolicyHold)
                    else cloud.safe_code(error) if isinstance(error, cloud.CloudTransferError) else 'signin_failed')
            self._update(status='cancelled' if self._cancel.is_set() else 'error', auth_url=None,
                         code='signin_cancelled' if self._cancel.is_set() else code)
        finally:
            credentials.clear()
