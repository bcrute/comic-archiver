"""Button-driven Backblaze B2 sign-in in the desktop app.

Credentials are confined to memory and private provider config. Only fixed
status fields are returned to the UI.
"""
import configparser
import os
from pathlib import Path
import tempfile
import threading
import uuid

from . import cloud
from .archive_paths import directory
from .models import ArchiveError
from .locking import LibraryLock


def validate(provider, credentials):
    if not isinstance(provider, str) or provider not in cloud.PROVIDERS or not isinstance(credentials, dict):
        raise ArchiveError('Choose a cloud provider.')
    from .b2_auth import validate as validate_b2
    validate_b2(credentials)


def retire(root, folder, config):
    """Delete the connection file a new sign-in replaced, with the sign-in it holds.

    As when a library is first linked, a test still to be cleaned up keeps the file it names. A file
    named on the command line stays, and so does one the libraries sharing cloud storage use.
    """
    if (folder != root / '.archive-state/cloud' or config.parent != folder or not config.name.startswith('connection-')
            or any(t['config'] == str(config) for t in cloud.settings(root)['targets'].values())):
        return
    for receipt in (root / '.archive-state/cloud/tests').glob('*.json'):
        record = cloud._json(receipt)
        if (record.get('state') != 'cleaned' and isinstance(record.get('connection'), dict)
                and record['connection'].get('config') == str(config)):
            return
    config.unlink(missing_ok=True)


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

    def _run(self, provider, credentials):
        try:
            root = cloud.safe_root(self.root)
            from .cloud_tools import ensure
            self._update(stage='preparing')
            ensure(root, self._cancel)
            self._update(stage='authenticating')
            folder = directory(cloud.connection_folder(root)); folder.chmod(0o700)
            with tempfile.TemporaryDirectory(prefix='signin-', dir=folder) as work:
                config = Path(work) / 'rclone.conf'
                config.touch(mode=0o600)
                from .b2_auth import connection
                self._update(stage='checking_b2')
                parser = configparser.ConfigParser(interpolation=None)
                parser['b2'] = connection(root, credentials, self._cancel)
                credentials.clear()
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
                        value = cloud.settings(root)
                        previous = value and value['targets'].get(provider)
                        cloud.configure_target(root, provider, config=destination)
                    except BaseException:
                        destination.unlink(missing_ok=True)
                        raise
                    if previous:
                        try:
                            retire(root, folder, Path(previous['config']))
                        except (ArchiveError, OSError):
                            pass  # The old file stays; the new connection is saved either way.
                self._update(status='connected')
        except Exception as error:
            from .network import NetworkPolicyHold
            code = ('signin_network_held' if isinstance(error, NetworkPolicyHold)
                    else cloud.safe_code(error) if isinstance(error, cloud.CloudTransferError) else 'signin_failed')
            self._update(status='cancelled' if self._cancel.is_set() else 'error',
                         code='signin_cancelled' if self._cancel.is_set() else code)
        finally:
            credentials.clear()
