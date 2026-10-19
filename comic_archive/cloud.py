"""One-way completed-episode uploads. Authentication belongs to rclone.

No browsing, synchronization, sharing or management of users' cloud files.
Only public archives/metadata enter uploads; credentials and retry receipts
stay private. A live smoke test touches a bounded, uniquely named test folder.
"""
import hashlib
import configparser
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import uuid
from datetime import datetime, timezone

from .archive import atomic_json, episode_archives, sidecar_path, verify_episode, verify_series
from .archive_paths import directory
from .locking import LibraryLock
from .models import ArchiveError
from . import osdep
from .osdep import disk_bytes, sync_directory
from .publication import valid_export_id, validate_public_metadata
from .storage import safe_root
from .cloud_errors import CloudTransferError, CloudCleanupError, MESSAGES, safe_code, transfer_failure

PROVIDERS = {'b2': 'b2'}
STREAM_PROVIDERS = ('b2',)
# Providers earlier versions could connect. A library that saved one keeps its entry, but nothing uses it.
RETIRED = ('google', 'dropbox', 'proton')
LABELS = {'b2': 'Backblaze B2'}
REMOTE = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_-]{0,63}')
CLOUD_ROOT = 'Comic-Archive'
# Failures every upload to the connection would meet; the others are one episode's own.
CONNECTION_FAILURES = {'b2_key_permissions', 'b2_bucket_mismatch', 'b2_private_bucket',
                       'cloud_login_required', 'cloud_space', 'cloud_cap', 'cloud_rate_limited',
                       'cloud_connection', 'cloud_permission', 'cloud_network_held'}


def test_prefixes(library_id, test_id):
    return {f'{CLOUD_ROOT}/{library_id}/tests/{test_id}'}


def _json(path, maximum=1024*1024):
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ArchiveError('Cloud state must not be symlinked.')
    try:
        if path.stat().st_size > maximum:
            raise ValueError
        value = json.loads(path.read_text())
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (OSError, ValueError, TypeError):
        raise ArchiveError('Cloud state is unreadable; local episodes are retained.') from None


def _write(path, value):
    directory(path.parent).chmod(0o700)
    atomic_json(path, value)
    path.chmod(0o600)
    _directory_sync(path.parent)


def _own(root):
    """The library's own cloud record: its name in the cloud, and the connections it keeps itself."""
    path = root / '.archive-state/cloud/targets.json'
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ArchiveError('Cloud settings must not be symlinked.')
    if not path.exists():
        return None
    value = _json(path)
    if (set(value) != {'schema_version', 'library_id', 'targets'} or type(value['schema_version']) is not int or value['schema_version'] != 1
            or not valid_export_id(value['library_id'])):
        raise ArchiveError('Cloud settings are invalid; local episodes are retained.')
    _check_targets(value['targets'])
    return value


def _check_targets(targets):
    if not isinstance(targets, dict) or set(targets) - set(PROVIDERS) - set(RETIRED):
        raise ArchiveError('Cloud settings are invalid; local episodes are retained.')
    for target in targets.values():
        if (not isinstance(target, dict) or set(target) != {'remote','config','enabled','tested','target_id'}
                or not isinstance(target['remote'], str) or not REMOTE.fullmatch(target['remote'])
                or not isinstance(target['config'], str) or not Path(target['config']).is_absolute()
                or type(target['enabled']) is not bool or type(target['tested']) is not bool
                or not valid_export_id(target['target_id'])):
            raise ArchiveError('Cloud target is invalid; local episodes are retained.')


def _shared(store):
    """The connections every site in the library folder shares, kept in its shared settings folder."""
    path = store / 'targets.json'
    if not path.exists() and not path.is_symlink():
        return {}
    value = _json(path)
    if set(value) != {'schema_version', 'targets'} or type(value['schema_version']) is not int or value['schema_version'] != 1:
        raise ArchiveError('Cloud settings are invalid; local episodes are retained.')
    _check_targets(value['targets'])
    return value['targets']


def settings(root):
    """The library's name in the cloud and the connections it uses: the shared ones, unless it keeps its own."""
    from .shared import cloud_folder
    root = safe_root(root)
    own, store = _own(root), cloud_folder(root)
    value = own if store is None or own is None else own | {'targets': _shared(store)}
    return value and value | {'targets': {name: t for name, t in value['targets'].items() if name in PROVIDERS}}


def _save(root, value, provider):
    """Keep one changed connection where settings reads it from.

    The shared connections are read again with their folder locked: another library may
    have changed one since this library read them, and that change is kept.
    """
    from .shared import cloud_folder
    path, store = root / '.archive-state/cloud/targets.json', cloud_folder(root)
    if store is None:
        retired = {name: t for name, t in (_own(root) or {'targets': {}})['targets'].items() if name not in PROVIDERS}
        _write(path, value | {'targets': retired | value['targets']})
        return
    if not path.exists():
        _write(path, value | {'targets': {}})
    with LibraryLock(store.parent):
        targets = _shared(store) | {provider: value['targets'][provider]}
        _write(store / 'targets.json', {'schema_version': 1, 'targets': targets})


def connection_folder(root):
    """Where a new sign-in's private connection file goes: beside the connections it will be one of."""
    from .shared import cloud_folder
    return cloud_folder(root) or safe_root(root) / '.archive-state/cloud'


def join(root, store):
    """Bring a library's connections into the shared ones as it is first linked; 'shared', or 'separate' to keep its own.

    Run by shared.link with both folders locked. A library that set up cloud storage before it was linked
    gives its connections to the shared settings if they have none; if they already have others, it keeps
    its own, so neither set is lost. The private connection files move with them.
    """
    root = safe_root(root)
    own, shared = _own(root), _shared(store)
    path = root / '.archive-state/cloud/targets.json'
    if own is None:
        _write(path, {'schema_version': 1, 'library_id': str(uuid.uuid4()), 'targets': {}})
        return 'shared'
    if not own['targets']:
        return 'shared'
    if shared and ({n: t['target_id'] for n, t in shared.items()} != {n: t['target_id'] for n, t in own['targets'].items()}):
        return 'separate'
    folder = directory(store); folder.chmod(0o700)
    moved, left = {}, []
    for name, target in own['targets'].items():
        config = Path(target['config'])
        if name in shared:
            # An earlier link moved these and stopped before it finished; what it wrote is used.
            moved[name] = shared[name]
        elif config.parent == root / '.archive-state/cloud':
            destination = folder / ('connection-' + uuid.uuid4().hex + '.conf')
            shutil.copyfile(_config(config), destination)
            destination.chmod(0o600)
            moved[name] = target | {'config': str(destination)}
        else:
            moved[name] = target  # A connection file the person named on the command line stays where it is.
        if moved[name]['config'] != str(config):
            left.append(config)
    _write(store / 'targets.json', {'schema_version': 1, 'targets': moved})
    _write(path, own | {'targets': {}})
    # A test still waiting to be cleaned up names its connection file, so that file stays until it is.
    waiting = set()
    for receipt in (root / '.archive-state/cloud/tests').glob('*.json'):
        record = _json(receipt)
        if record.get('state') != 'cleaned' and isinstance(record.get('connection'), dict):
            waiting.add(record['connection'].get('config'))
    for config in left:
        if str(config) not in waiting:
            config.unlink(missing_ok=True)
    return 'shared'


def _environment():
    # Prevent a globally defined remote or debug flag overriding the fixed
    # provider/config. Encrypted rclone configs can use their private password.
    return {k:v for k,v in os.environ.items() if not k.startswith('RCLONE_') or k == 'RCLONE_CONFIG_PASS'}


def _binary():
    from .cloud_tools import installed
    # The pinned, checksum-verified helper first; an rclone of the person's own only without it.
    executable = installed() or shutil.which('rclone')
    if not executable:
        raise ArchiveError('Cloud support is not installed. Click Sign in in the local app to set it up.')
    return executable


def transfer_timeout(size):
    """A quarter hour, and longer for a large file: it may go as slowly as 256 KiB a second."""
    return 900 + (size or 0) // (256 * 1024)


def _size(path):
    try:
        return Path(path).stat().st_size
    except OSError:
        return 0


def _config(path):
    path = safe_root(path)
    # On Windows the file is private by being in the person's own profile folder; there are no such modes.
    if not path.is_file() or (not osdep.WINDOWS and path.stat().st_mode & 0o077):
        raise ArchiveError('Cloud authentication needs a private rclone config file (permissions 600).')
    return path


class Transfer:
    def __init__(self, provider, target):
        if provider not in PROVIDERS:
            raise ArchiveError('Choose a supported cloud provider.')
        self.provider, self.target = provider, target
        self.binary = _binary()
        self.config = _config(target['config'])
        self.bucket = None
        if provider == 'b2':
            from .b2_auth import BUCKET
            parser = configparser.ConfigParser(interpolation=None)
            try:
                parser.read(self.config)
                self.bucket = parser[target['remote']]['archive_bucket']
                if not BUCKET.fullmatch(self.bucket):
                    raise ValueError
            except (configparser.Error, KeyError, ValueError):
                raise ArchiveError('Connect B2 using the setup walkthrough to choose a private library bucket.') from None
        # Verify the selected connection's backend without reading or printing
        # credentials, or accepting local/SFTP/alias targets through this API.
        names = self.run('listremotes', '--long').decode('utf-8', errors='replace')
        found = dict(re.findall(r'^([A-Za-z0-9_-]+):\s+(\S+)\s*$', names, re.M))
        if found.get(target['remote']) != PROVIDERS[provider]:
            raise ArchiveError('Cloud connection does not match the selected provider. Sign in to it again.')

    def run(self, *arguments, missing_ok=False, size=0):
        timeout = transfer_timeout(size)
        command = [self.binary, '--config', str(self.config), '--ask-password=false',
                   '--log-level', 'ERROR', '--stats', '0', '--retries', '2',
                   '--low-level-retries', '2', '--contimeout', '30s', '--timeout', '60s',
                   *(['--b2-hard-delete'] if getattr(self, 'provider', None) == 'b2' else []), *arguments]
        # Spool subprocess output instead of trusting a backend to return a
        # bounded string. Never surface its diagnostics, tokens or account IDs.
        try:
            with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
                guard = getattr(self, 'guard', None)
                if guard is not None and arguments[0] != 'listremotes':
                    from .network import wait_process
                    guard.check()
                    with subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                                          env=_environment(), **osdep.hidden()) as process:
                        code = wait_process(process, guard, timeout)
                else:
                    code = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                                          timeout=timeout, env=_environment(), check=False, **osdep.hidden()).returncode
                if missing_ok and code in (3,4):
                    return b''
                if code:
                    raise transfer_failure(stderr, code)
                stdout.seek(0)
                data = stdout.read(65537)
                if len(data) > 65536:
                    raise ArchiveError('Cloud transfer returned an oversized result; local episodes are retained.')
                return data
        except (OSError, subprocess.TimeoutExpired):
            raise CloudTransferError('cloud_connection') from None

    def remote(self, path):
        bucket = getattr(self, 'bucket', None)
        return self.target['remote'] + ':' + ((bucket + '/') if bucket else '') + path

    def upload(self, source, remote_path, expected):
        destination = self.remote(remote_path)
        self.run('copyto', str(source), destination, '--immutable', size=_size(source))
        self.verify(remote_path, expected, source=source)

    def verify(self, remote_path, expected, *, source=None):
        destination = self.remote(remote_path)
        actual = None
        if self.provider == 'b2' and source is not None and not getattr(self, 'full_readback', False):
            # B2 keeps the SHA-1 it checked on arrival; compare it instead of downloading the file again.
            whole, sha1 = hashlib.sha256(), hashlib.sha1(usedforsecurity=False)  # B2 lists SHA-1; SHA-256 is the check that counts
            with Path(source).open('rb') as data:
                while block := data.read(4 * 1024**2):
                    whole.update(block); sha1.update(block)
            if whole.hexdigest() != expected:
                raise CloudTransferError('cloud_verification')
            listed = self.sha1s(remote_path)
            if len(listed) == 1:
                if next(iter(listed.values())) != sha1.hexdigest():
                    raise CloudTransferError('cloud_verification')
                return
        if not actual:
            actual = self.run('hashsum', 'SHA256', destination, '--download', size=_size(source) if source else 0).split()
        if not actual or actual[0].decode('ascii', errors='replace') != expected:
            raise CloudTransferError('cloud_verification')

    def start_stream(self, remote_path):
        from .cloud_stream import PipeUpload
        return PipeUpload(self, remote_path)

    def upload_bytes(self, data, remote_path):
        """Send a file held in memory, whole, so B2 checks and keeps its SHA-1."""
        from .cloud_stream import PipeUpload
        pipe = PipeUpload(self, remote_path, size=len(data))
        try:
            view = memoryview(data)
            for start in range(0, len(view), 1024**2):
                pipe.write(view[start:start + 1024**2])
            pipe.finish()
        finally:
            pipe.close()

    def delete_upload(self, remote_path):
        """Delete every version of one file an interrupted direct upload recorded."""
        from .b2_cleanup import cleanup_file
        cleanup_file(self, remote_path, upload=True)

    def sha1s(self, remote_path):
        """The SHA-1 B2 lists for each file at or under a path, read without downloading.

        A file B2 has no whole SHA-1 for (one sent in parts without it) is left out.
        """
        listed = {}
        for line in self.run('hashsum', 'SHA1', self.remote(remote_path)).decode('utf-8', 'replace').splitlines():
            value, _, name = line.partition('  ')
            if re.fullmatch(r'[0-9a-f]{40}', value) and name:
                listed[name] = value
        return listed

    def delete_test_file(self, remote_path):
        from .b2_cleanup import cleanup_file
        cleanup_file(self, remote_path)


def transfer_for(root, provider, target, *, full_readback=False):
    from .network import NetworkGuard
    transfer = Transfer(provider, target)
    transfer.guard = NetworkGuard(root)
    transfer.full_readback = full_readback
    return transfer


def configure_target(root, provider, *, remote=None, config=None):
    if provider not in PROVIDERS:
        raise ArchiveError('Choose a supported cloud provider.')
    root = safe_root(root)
    remote = provider if remote is None else remote
    if not isinstance(remote, str) or not REMOTE.fullmatch(remote):
        raise ArchiveError('Cloud connection name is invalid.')
    config = _config(config or root / '.archive-state/cloud/rclone.conf')
    target = {'remote':remote, 'config':str(config), 'enabled':False, 'tested':False, 'target_id':str(uuid.uuid4())}
    Transfer(provider, target)
    with LibraryLock(root):
        value = settings(root) or {'schema_version':1, 'library_id':str(uuid.uuid4()), 'targets':{}}
        previous = value['targets'].get(provider)
        if previous:
            # Older tests did not pin a connection. Save it before a repair
            # replaces the target, so cleanup cannot silently check a different
            # account/client and falsely claim that old copies were deleted.
            for receipt in (root / '.archive-state/cloud/tests').glob('*.json'):
                record = _json(receipt)
                if (record.get('provider') == provider and record.get('state') != 'cleaned'
                        and 'connection' not in record and valid_export_id(receipt.stem)
                        and record.get('prefix') in test_prefixes(value['library_id'], receipt.stem)):
                    record['connection'] = _test_connection(previous)
                    _write(receipt, record)
        value['targets'][provider] = target
        _save(root, value, provider)
    return status(root)


def enable(root, provider, enabled):
    if provider not in PROVIDERS or type(enabled) is not bool:
        raise ArchiveError('Choose a provider and an explicit upload preference.')
    root = safe_root(root)
    with LibraryLock(root):
        value = settings(root)
        if not value or provider not in value['targets']:
            raise ArchiveError('Connect this cloud provider first.')
        target = value['targets'][provider]
        if enabled and not target['tested']:
            raise ArchiveError('Run the small cloud test and cleanup before enabling automatic uploads.')
        target['enabled'] = enabled
        _save(root, value, provider)
    return status(root)


def _bucket(target):
    """The B2 bucket a connection saves to, for the person to see; None when unreadable."""
    from .b2_auth import BUCKET
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(target['config'])
        name = parser[target['remote']]['archive_bucket']
    except (configparser.Error, KeyError, OSError):
        return None
    return name if BUCKET.fullmatch(name) else None


def status(root):
    root = safe_root(root)
    value = settings(root)
    targets = []
    for provider in PROVIDERS:
        target = value['targets'].get(provider) if value else None
        counts = {'pending':0, 'failed':0, 'verified':0}
        failures = {}
        folder = root / '.archive-state/cloud/outbox' / provider
        if folder.exists():
            safe_root(folder)
            for path in folder.glob('*.json'):
                record = _json(path)
                state = record.get('state')
                if state in counts: counts[state] += 1
                if state == 'failed':
                    code = record.get('error_code')
                    code = code if isinstance(code, str) and code in MESSAGES else 'cloud_unknown'
                    failures[code] = failures.get(code, 0) + 1
        targets.append({'provider':provider, 'connected':bool(target), 'enabled':bool(target and target['enabled']),
                        'tested':bool(target and target['tested']),
                        'bucket':_bucket(target) if target else None,
                        'failure_reasons':failures, **counts})
    cleanup = []
    folder = safe_root(root / '.archive-state/cloud/tests')
    for path in folder.glob('*.json'):
        record = _json(path)
        if record.get('state') != 'cleaned' and valid_export_id(path.stem) and record.get('provider') in PROVIDERS:
            code = record.get('error_code')
            code = code if isinstance(code, str) and code in MESSAGES else None
            cleanup.append({'test_id':path.stem, 'provider':record['provider'],
                            **({'error_code':code} if code else {})})
    from .shared import cloud_mode
    return {'targets':targets, 'pending_test_cleanup':cleanup, 'delete_local_after_upload':True,
            # 'shared': every site uses the library folder's cloud storage; 'separate': this site has its own.
            'storage':cloud_mode(root),
            'cloud_availability_checked':False}


def _bundle(root, archive, data=None):
    """The files an episode uploads with; data is its archive when held in memory, not saved here."""
    root, archive = safe_root(root), safe_root(archive)
    if data is None:
        problems = verify_episode(archive)
    else:
        from .archive import verify_episode_contents
        import io
        problems = verify_episode_contents(io.BytesIO(data), _json(sidecar_path(archive), 16*1024**2))
    if (len(archive.relative_to(root).parts) != 2 or archive.parent.name.startswith('.')
            or problems or verify_series(archive.parent / 'series.json')):
        raise ArchiveError('Only verified completed public episodes can be uploaded.')
    metadata = _json(sidecar_path(archive), 16*1024**2)
    series = _json(archive.parent / 'series.json', 16*1024**2)
    validate_public_metadata(metadata)
    validate_public_metadata(series, series=True)
    files = [('episode' + ('.tar.zst' if metadata['format']=='tar.zst' else '.cbz' if metadata['format']=='cbz' else '.epub'), archive),
             ('episode.json', sidecar_path(archive)), ('series.json', archive.parent / 'series.json')]
    if series.get('cover_file'):
        cover = safe_root(archive.parent / series['cover_file'])
        if cover.parent != archive.parent:
            raise ArchiveError('Cloud cover path must stay within its public series.')
        files.append((cover.name, cover))
    items = []
    for name, path in files:
        safe_root(path)
        if data is not None and path == archive:
            sha = hashlib.sha256(data).hexdigest()
        else:
            with path.open('rb') as stream:
                sha = hashlib.file_digest(stream, 'sha256').hexdigest()
        items.append({'name':name, 'path':path.relative_to(root).as_posix(), 'sha256':sha})
    # Series descriptions/counts may change while an existing episode stays
    # identical. Do not repeatedly upload completed episodes for those changes.
    version = hashlib.sha256((items[0]['sha256'] + items[1]['sha256']).encode()).hexdigest()
    return {'episode_id':metadata['export_id'], 'series_id':series['export_id'], 'version':version, 'files':items}


def queue_episode(root, archive, *, provider=None):
    value = settings(root)
    if not value or not any(t['enabled'] for t in value['targets'].values()):
        return
    root = safe_root(root)
    archive = safe_root(archive)
    relative = archive.relative_to(root)
    if len(relative.parts) != 2 or relative.parts[0].startswith('.') or verify_episode(archive):
        raise ArchiveError('Only verified completed public episodes can be queued for upload.')
    public = _json(sidecar_path(archive), 16*1024**2)
    validate_public_metadata(public)
    with archive.open('rb') as stream:
        sha = hashlib.file_digest(stream, 'sha256').hexdigest()
    with sidecar_path(archive).open('rb') as stream:
        sidecar_sha = hashlib.file_digest(stream, 'sha256').hexdigest()
    bundle = {'episode_id':public['export_id'], 'archive':relative.as_posix(),
              'version':hashlib.sha256((sha + sidecar_sha).encode()).hexdigest(),
              'required_targets':[p for p,t in value['targets'].items() if t['enabled'] and provider in (None,p)]}
    for provider, target in value['targets'].items():
        if provider not in bundle['required_targets']:continue
        receipt = root / '.archive-state/cloud/outbox' / provider / (bundle['episode_id'] + '.json')
        if receipt.exists():
            previous = _json(receipt)
            if (previous.get('version') == bundle['version'] and previous.get('library_id') == value['library_id']
                    and previous.get('target_id') == target['target_id']):
                continue
            if previous.get('state') == 'verified':
                from .cloud_history import retain
                retain(root, provider, previous)
        _write(receipt, bundle | {'state':'pending', 'library_id':value['library_id'], 'target_id':target['target_id'],
                                 'remote_root':CLOUD_ROOT})


def upload_pending(root, *, limit=None, provider=None):
    if limit is not None and (type(limit) is not int or limit < 1):
        raise ArchiveError('Upload limit must be a positive episode count.')
    if provider is not None and provider not in PROVIDERS:
        raise ArchiveError('Choose a supported cloud provider.')
    root = safe_root(root)
    report = {'uploaded_episodes':0, 'local_archives_deleted':0, 'freed_bytes':0}
    if settings(root) is None:return report
    failure = None
    with LibraryLock(root):
        value = settings(root)
        if not value:return report
        if 'b2' in value['targets']:
            try:
                cleanup_direct(root)
            except ArchiveError as error:
                from .network import NetworkPolicyHold
                if isinstance(error, NetworkPolicyHold):
                    raise
                # Leftovers wait for the next upload; they never hold up a retry.
        for name, target in value['targets'].items():
            if not target['enabled'] or provider not in (None, name):continue
            transfer = None
            folder = root / '.archive-state/cloud/outbox' / name
            safe_root(folder)
            for path in sorted(folder.glob('*.json')):
                if limit is not None and report['uploaded_episodes'] >= limit:break
                record = _json(path)
                if record.get('state') == 'verified' and record.get('target_id') == target['target_id']:continue
                try:
                    if record.get('library_id') != value['library_id'] or not isinstance(record.get('archive'), str):
                        raise ArchiveError('Cloud retry state is invalid.')
                    archive = safe_root(root / record['archive'])
                    if record.get('state') == 'verified':
                        from .cloud_history import retain
                        historical = _receipt_for(root, archive, name)
                        if historical is not None:
                            retain(root, name, historical)
                            if not archive.exists() and historical.get('eviction_requested') is True:
                                continue  # The old account keeps this copy; there are no local bytes to send elsewhere.
                    if _orphaned(root, archive):
                        # Deleted or moved away before it was uploaded: there is nothing to send, so it is set
                        # aside instead of failing every retry. It is sent once the episode is back.
                        if record.get('state') != 'orphaned':
                            _write(path, {k: v for k, v in record.items() if k != 'error_code'} | {'state':'orphaned'})
                        continue
                    bundle = _bundle(root, archive)
                    if any(bundle[k] != record.get(k) for k in ('episode_id','version')):
                        raise ArchiveError('Cloud retry no longer matches the completed episode.')
                    if transfer is None:transfer = transfer_for(root, name, target)
                    remote_root = record.get('remote_root')
                    if remote_root != CLOUD_ROOT:
                        raise ArchiveError('Cloud retry namespace is invalid.')
                    prefix = f"{remote_root}/{value['library_id']}/{bundle['series_id']}/{bundle['episode_id']}/{bundle['version']}"
                    for item in bundle['files']:
                        from .cloud_stream import promote_stream
                        promoted = (item is bundle['files'][0] and name in STREAM_PROVIDERS
                                    and promote_stream(root, transfer, bundle, prefix + '/' + item['name']))
                        if not promoted:
                            transfer.upload(root / item['path'], prefix + '/' + item['name'], item['sha256'])
                    verified = record | bundle | {'state':'verified', 'target_id':target['target_id'],
                                                   'verified_at':datetime.now(timezone.utc).isoformat()}
                    verified.pop('error_code', None)
                    _write(path, verified)
                    report['uploaded_episodes'] += 1
                except (ArchiveError, OSError, ValueError, KeyError, TypeError) as error:
                    code = safe_code(error)
                    _write(path, record | {'state':'failed', 'error_code':code})
                    from .network import NetworkPolicyHold
                    if isinstance(error, NetworkPolicyHold):
                        raise
                    if code in CONNECTION_FAILURES:
                        raise CloudTransferError(code) from None
                    failure = failure or code  # This episode waits for its retry; the others still go up.
        deleted = evict_uploaded(root)
        report.update(deleted)
    if failure:
        raise CloudTransferError(failure)
    return report


def _orphaned(root, archive):
    """An episode in the library whose file is gone, though no upload let it go."""
    parts = archive.relative_to(root).parts
    return (len(parts) == 2 and '..' not in parts and not parts[0].startswith('.')
            and not archive.exists() and not archive.is_symlink() and not offloaded(root, archive))


def direct_target(root):
    """B2's target when it alone takes new episodes, so they can go to it without being saved here."""
    value = settings(root)
    enabled = [name for name, target in value['targets'].items() if target['enabled']] if value else []
    return value['targets']['b2'] if enabled == ['b2'] else None


DIRECT_NAMES = r'episode\.(?:cbz|epub|tar\.zst|json)|series\.json|cover\.(?:jpg|png|webp|gif)'


def cleanup_direct(root, transfer=None):
    """Delete what an interrupted direct upload left in B2, by the paths it recorded first.

    An upload later recorded as verified at the same paths is kept. Records made
    for an earlier connection are kept too, since that account may not be this one.
    """
    root = safe_root(root)
    folder = safe_root(root / '.archive-state/cloud/direct/b2')
    if not folder.is_dir():
        return transfer
    value = settings(root)
    target = value and value['targets'].get('b2')
    if not target:
        return transfer
    records = []
    for path in sorted(folder.glob('*.json')):
        try:
            record = _json(path)
            parts = record['prefix'].split('/') if isinstance(record.get('prefix'), str) else []
            if (set(record) != {'schema_version', 'library_id', 'target_id', 'episode_id', 'prefix', 'names'}
                    or record['schema_version'] != 1 or not valid_export_id(record['episode_id'])
                    or path.stem != record['episode_id'] or len(parts) != 5 or parts[0] != CLOUD_ROOT
                    or parts[1] != record['library_id'] or not valid_export_id(parts[2]) or parts[3] != record['episode_id']
                    or not re.fullmatch(r'[0-9a-f]{64}', parts[4]) or not isinstance(record['names'], list)
                    or not all(isinstance(n, str) and re.fullmatch(DIRECT_NAMES, n) for n in record['names'])):
                raise ArchiveError('Direct upload recovery state is invalid; no cloud files were changed.')
        except ArchiveError:
            # Paths from a damaged record cannot be trusted: it is set aside, and nothing in B2 is changed.
            os.replace(path, path.with_suffix('.damaged-' + uuid.uuid4().hex))
            _directory_sync(folder)
            continue
        if record['library_id'] == value['library_id'] and record['target_id'] == target['target_id']:
            records.append((path, record))
    for path, record in records[:20]:
        version = record['prefix'].rsplit('/', 1)[-1]
        receipt = root / '.archive-state/cloud/outbox/b2' / (record['episode_id'] + '.json')
        history = (root / '.archive-state/cloud/history/b2' / record['episode_id']
                   / (target['target_id'] + '-' + version + '.json'))
        # The outbox or the history saying it was uploaded each keep it: either may be all that was written.
        kept = [_json(file) for file in (receipt, history) if file.is_file()]
        finished = any(saved.get('state') == 'verified' and saved.get('target_id') == target['target_id']
                       and saved.get('version') == version for saved in kept)
        if not finished:
            if transfer is None:
                transfer = transfer_for(root, 'b2', target)
            for name in record['names']:
                transfer.delete_upload(record['prefix'] + '/' + name)
        path.unlink()
        _directory_sync(folder)
    return transfer


class DirectUpload:
    """A finished episode held in memory, going to B2 while the next one downloads; no copy is saved here.

    Started with the library locked, it records the paths it will use, then a
    thread sends the episode and copies of its sidecar and series files as they
    were, and compares B2's own SHA-1 of each. The thread only uses the network.
    finish, on the thread that started it, records the episode as uploaded and
    removed from this computer, the same as one saved here, uploaded, then
    removed. If the upload failed, finish saves the episode here, queued for the
    retry button, and raises the error an upload from this computer would raise.
    """
    def __init__(self, root, archive, data):
        self.root, self.archive, self.data = safe_root(root), safe_root(archive), data
        self.value = settings(self.root)
        self.target = direct_target(self.root)
        if self.target is None:
            raise ArchiveError('Direct uploads need B2 to be the only cloud storage in use.')
        self.bundle = _bundle(self.root, self.archive, data)
        self.prefix = (f"{CLOUD_ROOT}/{self.value['library_id']}/{self.bundle['series_id']}"
                       f"/{self.bundle['episode_id']}/{self.bundle['version']}")
        self.intent = self.root / '.archive-state/cloud/direct/b2' / (self.bundle['episode_id'] + '.json')
        self.error = None
        self.thread = None
        self.kept = False  # Whether a failed upload left the episode saved here.
        try:
            files = [data] + [(self.root / item['path']).read_bytes() for item in self.bundle['files'][1:]]
            if [hashlib.sha256(f).hexdigest() for f in files] != [item['sha256'] for item in self.bundle['files']]:
                raise ArchiveError('Episode files changed while their upload was prepared.')
            self.files = [(item['name'], f) for item, f in zip(self.bundle['files'], files, strict=True)]
            transfer = cleanup_direct(self.root)
            from .cloud_stream import _stage, cleanup_stage
            if _stage(self.root, self.bundle['episode_id'], 'b2')[1] is not None:
                # Left by an interrupted upload before episodes went to B2 from memory.
                transfer = transfer or transfer_for(self.root, 'b2', self.target)
                cleanup_stage(self.root, transfer, self.bundle['episode_id'])
            # The paths go on record before any bytes, so an interruption can be cleaned up exactly.
            # After a failure the record stays: the cleanup it allows is harmless once the retry is verified.
            _write(self.intent, {'schema_version':1, 'library_id':self.value['library_id'],
                                 'target_id':self.target['target_id'], 'episode_id':self.bundle['episode_id'],
                                 'prefix':self.prefix, 'names':[name for name, _ in self.files]})
            self.transfer = transfer or transfer_for(self.root, 'b2', self.target)
        except (ArchiveError, OSError, ValueError, KeyError, TypeError) as error:
            self.error = error
            return
        self.thread = threading.Thread(target=self._send, name='cloud-direct', daemon=True)
        self.thread.start()

    def _send(self):
        try:
            for name, data in self.files:
                self.transfer.upload_bytes(data, self.prefix + '/' + name)
            listed = self.transfer.sha1s(self.prefix)
            if set(listed) - {name for name, _ in self.files}:
                raise CloudTransferError('cloud_verification')
            for name, data in self.files:
                if name not in listed:
                    # A file B2 took in parts has no SHA-1 there: its bytes are read back instead.
                    self.transfer.verify(self.prefix + '/' + name, hashlib.sha256(data).hexdigest())
                elif listed[name] != hashlib.sha1(data, usedforsecurity=False).hexdigest():
                    raise CloudTransferError('cloud_verification')
        except BaseException as error:
            self.error = error

    def finish(self):
        if self.thread is not None:
            self.thread.join()
        error, self.files = self.error, None
        if error is not None and not isinstance(error, (ArchiveError, OSError, ValueError, KeyError, TypeError)):
            raise error
        if error is not None:
            code = safe_code(error)
            try:
                keep_here(self.root, self.archive, self.data, code)
                self.kept = True
            except (ArchiveError, OSError):
                pass  # Not saved anywhere: the episode is downloaded again next time.
            from .network import NetworkPolicyHold
            if isinstance(error, NetworkPolicyHold):
                raise error
            raise CloudTransferError(code) from None
        queued = {'episode_id':self.bundle['episode_id'], 'archive':self.archive.relative_to(self.root).as_posix(),
                  'version':self.bundle['version'], 'required_targets':['b2'], 'state':'pending',
                  'library_id':self.value['library_id'], 'target_id':self.target['target_id'],
                  'remote_root':CLOUD_ROOT}
        from .cloud_history import retain
        verified = queued | self.bundle | {'state':'verified', 'target_id':self.target['target_id'],
                                          'verified_at':datetime.now(timezone.utc).isoformat(),
                                          'eviction_requested':True}
        # The outbox first: the recovery record reads it, so B2's only copy is never taken for leftovers.
        _write(self.root / '.archive-state/cloud/outbox/b2' / (self.bundle['episode_id'] + '.json'), verified)
        retain(self.root, 'b2', verified)
        self.intent.unlink()
        _directory_sync(self.intent.parent)
        _prepare_cache_for_offload(self.root, self.archive, saved=False)


def keep_here(root, archive, data, code=None):
    """Save an episode held in memory for B2, queued for upload; code marks it failed, for the retry button."""
    temporary = archive.with_name(archive.name + '.tmp')
    if any(p.is_symlink() for p in (archive, temporary)):
        raise ArchiveError('Refusing unsafe archive output.')
    if shutil.disk_usage(root).free < len(data) + 256 * 1024**2:
        raise ArchiveError('Not enough free space to keep the episode here.')
    try:
        with temporary.open('wb') as output:
            output.write(data); output.flush(); os.fsync(output.fileno())
        osdep.replace_file(temporary, archive)
    finally:
        if temporary.is_file() and not temporary.is_symlink(): temporary.unlink()
    queue_episode(root, archive)
    if code is not None:
        receipt = root / '.archive-state/cloud/outbox/b2' / (_json(sidecar_path(archive), 16*1024**2)['export_id'] + '.json')
        _write(receipt, _json(receipt) | {'state':'failed', 'error_code':code})


def validate_test_action(operation, options):
    """Dashboard tasks can test two episodes or replay one exact cleanup receipt."""
    if operation == 'cloud_test' and set(options) == {'provider'} and isinstance(options['provider'], str) and options['provider'] in PROVIDERS:
        return
    if operation == 'cloud_cleanup' and set(options) == {'test_id'} and valid_export_id(options['test_id']):
        return
    raise ArchiveError('Cloud tests accept only a provider; cleanup accepts only a recorded test identity.')


def _test_connection(target):
    return {k: target[k] for k in ('remote', 'config', 'target_id')}


def smoke_test(root, provider, *, episodes=2, stream=False):
    if (provider not in PROVIDERS or type(episodes) is not int or not 1 <= episodes <= 3
            or type(stream) is not bool or (stream and provider not in STREAM_PROVIDERS)):
        raise ArchiveError('Cloud tests require a supported provider and one to three episodes.')
    root = safe_root(root)
    with LibraryLock(root):
        value = settings(root)
        if not value or provider not in value['targets']:
            raise ArchiveError('Connect the cloud provider first.')
        transfer = transfer_for(root, provider, value['targets'][provider], full_readback=True)
        run_id = str(uuid.uuid4())
        prefix = f"Comic-Archive/{value['library_id']}/tests/{run_id}"
        receipt = root / '.archive-state/cloud/tests' / (run_id + '.json')
        record = {'schema_version':1, 'provider':provider, 'prefix':prefix,
                  'state':'uploading', 'files':[], 'verified_episodes':0,
                  'connection': _test_connection(value['targets'][provider])}
        _write(receipt, record)
        try:
            for folder in sorted(root.iterdir()):
                if not folder.is_dir() or folder.name.startswith('.'):continue
                for archive in episode_archives(folder):
                    if record['verified_episodes'] >= episodes:break
                    bundle = _bundle(root, archive)
                    for item in bundle['files']:
                        remote_path = prefix + '/' + bundle['episode_id'] + '/' + item['name']
                        # Journal intent first, so interrupted uploads have
                        # an exact, narrow cleanup list even before completion.
                        record['files'].append(remote_path)
                        _write(receipt, record)
                        source = root / item['path']
                        if stream and item == bundle['files'][0]:
                            # New episodes go to B2 whole from memory: B2's own SHA-1 is
                            # checked as for them, then the bytes are read back as well.
                            data = source.read_bytes()
                            transfer.upload_bytes(data, remote_path)
                            if transfer.sha1s(remote_path) != {item['name']: hashlib.sha1(data, usedforsecurity=False).hexdigest()}:
                                raise CloudTransferError('cloud_verification')
                            transfer.verify(remote_path, item['sha256'])
                        else:
                            transfer.upload(source, remote_path, item['sha256'])
                    record['verified_episodes'] += 1
                    _write(receipt, record)
                if record['verified_episodes'] >= episodes:break
            if record['verified_episodes'] != episodes:
                raise ArchiveError('Not enough completed episodes for this cloud test.')
            record['state'] = 'verified'
        finally:
            record['state'] = 'cleanup_pending'
            _write(receipt, record)
            # Only exact files journaled under this fresh test UUID are deleted.
            cleanup_test(root, run_id, transfer=transfer)
        value['targets'][provider]['tested'] = True
        _save(root, value, provider)
        result = {'provider':provider, 'verified_episodes':record['verified_episodes'],
                'test_files_deleted':True, 'local_archives_retained':True, 'test_id':run_id}
        if stream:
            result['streamed_episodes'] = record['verified_episodes']
        return result


def cleanup_test(root, test_id, *, transfer=None):
    root = safe_root(root)
    if not valid_export_id(test_id):raise ArchiveError('Invalid cloud test identity.')
    with LibraryLock(root):
        path = root / '.archive-state/cloud/tests' / (test_id + '.json')
        record, value = _json(path), settings(root)
        if not value or record.get('provider') not in value['targets']:
            raise ArchiveError('Cloud test connection is unavailable.')
        prefix = record.get('prefix')
        files = record.get('files')
        if (not isinstance(prefix, str) or prefix not in test_prefixes(value['library_id'], test_id)
                or not isinstance(files, list) or len(files) > 12):
            raise ArchiveError('Cloud test cleanup scope is invalid.')
        for remote_path in files:
            if (not isinstance(remote_path, str) or not remote_path.startswith(prefix + '/')
                    or '..' in remote_path.split('/') or len(remote_path.split('/')) != 6
                    or not valid_export_id(remote_path.split('/')[-2])
                    or remote_path.split('/')[-1] not in {'episode.cbz','episode.tar.zst','episode.epub','episode.json','series.json','cover.jpg','cover.png','cover.webp','cover.gif'}):
                raise ArchiveError('Cloud test cleanup file is invalid.')
        if record.get('state') == 'cleaned':return {'test_files_deleted':True}
        connection = record.get('connection')
        target = value['targets'][record['provider']]
        if connection is not None:
            if (not isinstance(connection, dict) or set(connection) != {'remote','config','target_id'}
                    or not isinstance(connection['remote'], str) or not REMOTE.fullmatch(connection['remote'])
                    or not isinstance(connection['config'], str) or not Path(connection['config']).is_absolute()
                    or not valid_export_id(connection['target_id'])):
                raise ArchiveError('Cloud test connection is invalid; cleanup is retained.')
            target = connection | {'enabled':False, 'tested':False}
        try:
            transfer = transfer or transfer_for(root, record['provider'], target, full_readback=True)
            for remote_path in files:
                parent, filename = remote_path.rsplit('/', 1)
                transfer.delete_test_file(remote_path)
                # The provider may use its trash; this verifies the uploaded
                # file is absent from the test folder, without emptying trash.
                listing = transfer.run('lsf', transfer.remote(parent), '--files-only', missing_ok=True).decode().splitlines()
                if filename in listing:raise ArchiveError('Cloud test file still exists.')
        except ArchiveError as error:
            code = safe_code(error)
            record.update(state='cleanup_pending', error_code=code);_write(path, record)
            from .network import NetworkPolicyHold
            if isinstance(error, NetworkPolicyHold):raise
            raise CloudCleanupError(code) from None
        record.pop('error_code', None)
        record['state'] = 'cleaned';_write(path, record)
        return {'test_files_deleted':True}


def receipts_for(root, archive, provider, *, outbox_only=False):
    """Historical saves use the library identity, independent of its active connections."""
    from .cloud_history import receipts
    root = safe_root(root)
    own = _own(root)
    return receipts(root, archive, provider, own['library_id'], outbox_only=outbox_only) if own else []


def _receipt_for(root, archive, provider):
    """The validated outbox entry; deletion additionally checks the active target explicitly."""
    records = receipts_for(root, archive, provider, outbox_only=True)
    return records[0] if records else None


def offloaded(root, archive):
    """Local deletion was journaled after a verified upload, not a missing file.

This is historical evidence only, never a fresh cloud availability assertion.
"""
    try:
        for provider in PROVIDERS:
            for record in receipts_for(root, archive, provider):
                if record.get('eviction_requested') is True:return record
    except (ArchiveError,OSError,ValueError,TypeError,KeyError):
        return None
    return None


def evict_uploaded(root):
    root = safe_root(root)
    report = {'local_archives_deleted':0, 'freed_bytes':0}
    with LibraryLock(root):
        seen = set()
        for provider in PROVIDERS:
            folder = safe_root(root / '.archive-state/cloud/outbox' / provider)
            for path in sorted(folder.glob('*.json')):
                record = _json(path)
                relative = record.get('archive')
                if record.get('state') != 'verified' or not isinstance(relative,str) or relative in seen:continue
                archive = safe_root(root / relative)
                providers = record.get('required_targets')
                if not isinstance(providers,list) or not providers or set(providers)-set(PROVIDERS):
                    raise ArchiveError('Cloud deletion receipt is invalid; local archives retained.')
                receipts = [_receipt_for(root, archive, p) for p in providers]
                if any(r is None or r['version'] != record['version'] for r in receipts):continue
                current = settings(root)
                if not current or any(saved['target_id'] != current['targets'].get(name, {}).get('target_id')
                                      for name,saved in zip(providers,receipts,strict=True)):
                    continue
                seen.add(relative)
                if archive.exists():
                    if verify_episode(archive):raise ArchiveError('Local archive changed; cloud deletion held.')
                    with archive.open('rb') as stream:
                        if hashlib.file_digest(stream,'sha256').hexdigest() != record['files'][0]['sha256']:
                            raise ArchiveError('Local archive differs from verified cloud copy; deletion held.')
                # Persist intent before unlink; restart can distinguish deliberate
                # cloud-only storage from accidental loss without downloading again.
                for name, saved in zip(providers, receipts, strict=True):
                    from .cloud_history import retain
                    saved = saved | {'eviction_requested':True}
                    _write(root / '.archive-state/cloud/outbox' / name / (saved['episode_id'] + '.json'), saved)
                    retain(root, name, saved)
                if archive.exists():
                    _prepare_cache_for_offload(root, archive)
                    size = disk_bytes(archive)
                    archive.unlink()
                    _directory_sync(archive.parent)
                    report['local_archives_deleted'] += 1
                    report['freed_bytes'] += size
    return report


def _prepare_cache_for_offload(root, archive, *, saved=True):
    """Retain pages needed by another unfinished import before their last
    local recovery archive disappears. Remove this completed episode's blobs.

    An episode never saved here has no archive to recover pages from: another
    import keeps only the pages it cached itself.
    """
    from contextlib import closing
    from .archive import Store
    from .publication import import_metadata
    from .purge import archive_page
    metadata = import_metadata(sidecar_path(archive))
    identity = (metadata['source'],metadata.get('cache_series_id',metadata['series_id']),metadata['source_episode_id'])
    with closing(Store(root)) as store:
        pages = {p['sha256']:p for p in metadata['content_hashes']}
        originals = {p['sha256'] for p in metadata.get('original_content_hashes',[])}
        for sha in set(pages) | originals:
            owners = store.db.execute('SELECT source,series_id,episode_id FROM cache_owners WHERE hash=?',(sha,)).fetchall()
            shared = any(tuple(owner) != identity for owner in owners)
            blob = store.cache / sha
            if shared:
                page = pages.get(sha)
                if saved and page and metadata['content_type'] == 'comic' and not blob.is_file():
                    data = archive_page(root,(archive.relative_to(root).as_posix(),page['file'],page['size']),sha)
                    if data is None:raise ArchiveError('Shared resume page cannot be retained; cloud deletion held.')
                    store.cache_image(data)
            elif blob.is_file() and not blob.is_symlink():
                blob.unlink()
        store.db.execute('DELETE FROM cache_owners WHERE source=? AND series_id=? AND episode_id=?',identity)
        store.db.commit()


def _directory_sync(folder):
    sync_directory(folder)
