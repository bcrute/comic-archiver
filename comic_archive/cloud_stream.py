"""Bounded Drive/Dropbox uploads while assembling one recoverable episode."""
import hashlib
import os
from pathlib import Path
import select
import shutil
import subprocess
import zipfile
import tempfile
import threading
import time
import uuid


from .archive import digest, image_extension
from .models import ArchiveError
from .osdep import WINDOWS, anchored, hidden
from .errors import RunLimit
from .network import NetworkPolicyHold, wait_process


class PipeUpload:
    """A bounded pipe, a small provider buffer, and a monitored owned child.

    Given the size, rclone holds the whole file before sending it, so it knows
    the SHA-1 up front: B2 checks it on arrival and keeps it in its listing.
    """
    def __init__(self, transfer, remote_path, size=None):
        from .cloud import _environment
        self.transfer = transfer
        self.error = None
        self.stop = threading.Event()
        transfer.guard.check()
        self.logs = [tempfile.TemporaryFile(), tempfile.TemporaryFile()]
        try:
            self.process = subprocess.Popen([
                transfer.binary, '--config', str(transfer.config), '--ask-password=false',
                '--log-level', 'ERROR', '--stats', '0', '--retries', '1',
                '--low-level-retries', '2', '--contimeout', '30s', '--timeout', '60s',
                '--drive-chunk-size', '4Mi', '--dropbox-chunk-size', '4Mi',
                '--dropbox-batch-mode', 'off', '--streaming-upload-cutoff', '256Ki' if size is None else f'{size + 1}B',
                *(['--b2-chunk-size', '5Mi', '--b2-upload-concurrency', '1', '--b2-hard-delete']
                  if transfer.provider == 'b2' else []),
                'rcat', transfer.remote(remote_path)], stdin=subprocess.PIPE,
                stdout=self.logs[0], stderr=self.logs[1], env=_environment(), **hidden())
            os.set_blocking(self.process.stdin.fileno(), False)
        except BaseException:
            if hasattr(self, 'process'):
                self._terminate()
                if self.process.stdin is not None: self.process.stdin.close()
            for log in self.logs: log.close()
            raise
        self.watcher = threading.Thread(target=self._watch, name='cloud-network-policy', daemon=True)
        self.watcher.start()

    def _watch(self):
        try:
            while not self.stop.wait(.2) and self.process.poll() is None:
                self.transfer.guard.check()
        except BaseException as error:
            self.error = error
            self._terminate()

    def _terminate(self):
        if self.process.poll() is None:
            try:
                self.process.terminate()
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill(); self.process.wait(timeout=3)
            except ProcessLookupError:
                pass

    def write(self, data):
        remaining = memoryview(data)
        deadline = time.monotonic() + 65
        while remaining:
            if self.error is not None: raise self.error
            self.transfer.guard.check()
            if self.process.poll() is not None:
                from .cloud_errors import transfer_failure
                raise transfer_failure(self.logs[1], self.process.returncode)
            if time.monotonic() >= deadline:
                from .cloud_errors import CloudTransferError
                raise CloudTransferError('cloud_connection')
            if not WINDOWS and not select.select([], [self.process.stdin], [], .2)[1]: continue
            try:
                size = os.write(self.process.stdin.fileno(), remaining[:65536])
            except BlockingIOError:
                size = 0
            if not size:
                # Windows cannot wait for room in a pipe, so the write is tried again shortly.
                time.sleep(.02)
            remaining = remaining[size:]

    def finish(self):
        self.process.stdin.close()
        try:
            if self.error is not None: raise self.error
            code = wait_process(self.process, self.transfer.guard)
            if code:
                from .cloud_errors import transfer_failure
                raise transfer_failure(self.logs[1], code)
            if self.error is not None: raise self.error
        finally:
            self.close()

    def close(self):
        self.stop.set()
        self._terminate()
        if not self.process.stdin.closed: self.process.stdin.close()
        self.watcher.join(timeout=10)
        for log in self.logs: log.close()


def _stage(root, episode_id, provider='google'):
    from .cloud import _json
    from .publication import valid_export_id
    from .storage import safe_root
    from .cloud import STREAM_PROVIDERS, CLOUD_ROOT
    if not valid_export_id(episode_id) or provider not in STREAM_PROVIDERS:
        raise ArchiveError('Invalid streaming episode identity.')
    folder = Path(root) / '.archive-state/cloud/streams'
    # Preserve existing Google receipts; Dropbox has its own namespace.
    if provider != 'google': folder /= provider
    path = safe_root(folder / (episode_id + '.json'))
    if not path.exists(): return path, None
    record = _json(path)
    required = {'schema_version', 'library_id', 'target_id', 'episode_id', 'run_id',
                'remote_path', 'archive', 'state', 'sha256'}
    if (set(record) not in (required, required | {'provider'})
            or record.get('provider', 'google') != provider
            or type(record['schema_version']) is not int or record['schema_version'] != 1
            or any(not valid_export_id(record[k]) for k in ('library_id', 'target_id', 'episode_id', 'run_id'))
            or record['episode_id'] != episode_id or record['state'] not in {'streaming', 'uploaded'}
            or record['remote_path'] != f"{CLOUD_ROOT}/{record['library_id']}/.staging/{record['run_id']}/episode.cbz"
            or not isinstance(record['archive'], str) or anchored(record['archive'])
            or len(Path(record['archive']).parts) != 2 or '..' in Path(record['archive']).parts
            or Path(record['archive']).parts[0].startswith('.')
            or (record['sha256'] is not None and (not isinstance(record['sha256'], str)
                or len(record['sha256']) != 64 or any(c not in '0123456789abcdef' for c in record['sha256'])))):
        raise ArchiveError('Streaming recovery state is invalid; no cloud files were changed.')
    return path, record


def _matching(root, transfer, record):
    from .cloud import settings
    value = settings(root)
    provider = record.get('provider', 'google')
    target = value and value['targets'].get(provider)
    return bool(target and transfer.provider == provider and record['library_id'] == value['library_id']
                and record['target_id'] == target['target_id'])


def cleanup_stage(root, transfer, episode_id):
    path, record = _stage(root, episode_id, transfer.provider)
    if record is None: return
    if not _matching(root, transfer, record):
        raise ArchiveError('Streaming cleanup belongs to a previous connection; recovery state is retained.')
    if transfer.provider == 'b2':
        transfer.delete_test_file(record['remote_path'])
    else:
        transfer.run('deletefile', transfer.remote(record['remote_path']), missing_ok=True)
    path.unlink()


def promote_stream(root, transfer, bundle, destination):
    """Adopt only our exact, hash-verified staging file within its provider."""
    path, record = _stage(root, bundle['episode_id'], transfer.provider)
    if record is None: return False
    if not _matching(root, transfer, record): return False
    item = bundle['files'][0]
    if record['state'] != 'uploaded' or record['archive'] != item['path'] or record['sha256'] != item['sha256']:
        cleanup_stage(root, transfer, bundle['episode_id'])
        return False
    try:
        transfer.verify(record['remote_path'], item['sha256'], source=Path(root) / item['path'])
    except NetworkPolicyHold:
        raise
    except ArchiveError:
        # A crash after rename may leave this receipt with a missing staging
        # file. The regular immutable upload verifies the final destination.
        cleanup_stage(root, transfer, bundle['episode_id'])
        return False
    transfer.run('moveto', transfer.remote(record['remote_path']), transfer.remote(destination), '--immutable')
    transfer.verify(destination, item['sha256'], source=Path(root) / item['path'])
    path.unlink()
    return True


class StreamingEpisode:
    """Source retrieval and upload overlap; pages and one spool survive failure.

    The producer owns SQLite. The worker only reads immutable cache blobs in
    page order, so out-of-order download completions never reorder the archive.
    """
    @classmethod
    def create(cls, store, series, episode, archive):
        from .cloud import settings, transfer_for, _write, STREAM_PROVIDERS
        value = settings(store.root)
        targets = value['targets'] if value else {}
        provider = next((p for p in STREAM_PROVIDERS if targets.get(p, {}).get('enabled')), None)
        target = targets.get(provider)
        if not target or not target['enabled'] or episode.content_type != 'comic' or archive.exists():
            return None
        if store.episode_is_offloaded(series, episode, archive): return None
        transfer = transfer_for(store.root, provider, target)
        if not callable(getattr(transfer, 'start_stream', None)): return None
        key = store.series_export_key(series.source, series.id, series.platform_id)
        episode_id = store.export_id(series.source, key, episode.id)
        cleanup_stage(store.root, transfer, episode_id)
        path, _ = _stage(store.root, episode_id, provider)
        run_id = str(uuid.uuid4())
        record = {'schema_version': 1, 'library_id': value['library_id'], 'target_id': target['target_id'],
                  'episode_id': episode_id, 'run_id': run_id, 'archive': archive.relative_to(store.root).as_posix(),
                  'remote_path': f"Comic-Archive/{value['library_id']}/.staging/{run_id}/episode.cbz",
                  'state': 'streaming', 'sha256': None, 'provider': provider}
        _write(path, record)  # Durable intent before any cloud bytes.
        return cls(store, episode, archive, transfer, path, record)

    def __init__(self, store, episode, archive, transfer, receipt, record):
        self.store, self.episode, self.archive = store, episode, archive
        self.temporary = archive.with_name(archive.name + '.tmp')
        if self.temporary.is_symlink(): raise ArchiveError('Unsafe streaming temporary file.')
        self.transfer, self.receipt, self.record = transfer, receipt, record
        self.condition = threading.Condition()
        self.pages = {}
        self.stopped = False
        self.error = None
        self.pipe = None
        self.stream_error = None
        self.sha = hashlib.sha256()
        self.thread = threading.Thread(target=self._assemble, name='cloud-episode', daemon=True)
        self.thread.start()

    def add_page(self, page, data):
        if self.error is not None: raise self.error
        name = f'{page.order:0{max(4, len(str(len(self.episode.pages))))}d}{image_extension(data)}'
        with self.condition:
            self.pages[page.order] = {'file': name, 'sha256': digest(data), 'size': len(data)}
            self.condition.notify_all()

    def _write(self, output, data):
        if shutil.disk_usage(self.store.root).free < len(data) + self.store.min_free_bytes:
            raise RunLimit('Insufficient space for streaming assembly; cached pages and upload recovery are retained.')
        output.write(data)
        self.sha.update(data)
        if self.pipe is not None and self.stream_error is None:
            try:
                self.pipe.write(data)
            except NetworkPolicyHold:
                raise
            except (ArchiveError, OSError) as error:
                self.stream_error = error
                self.pipe.close()
        return len(data)

    def _assemble(self):
        try:
            try:
                self.pipe = self.transfer.start_stream(self.record['remote_path'])
            except NetworkPolicyHold:
                raise
            except (ArchiveError, OSError) as error:
                self.stream_error = error
            with self.temporary.open('wb') as output:
                owner = self
                class Tee:
                    def write(self, data): return owner._write(output, data)
                    def flush(self): output.flush()
                with zipfile.ZipFile(Tee(), 'w', compression=zipfile.ZIP_STORED) as archive:
                    for original in sorted(self.episode.pages, key=lambda p: p.order):
                        with self.condition:
                            while original.order not in self.pages and not self.stopped:
                                self.condition.wait(.2)
                            if self.stopped: raise ArchiveError('Streaming assembly cancelled; cached pages are retained.')
                            page = self.pages[original.order]
                        blob = self.store.cache / page['sha256']
                        with blob.open('rb') as source:
                            data = source.read(page['size'] + 1)
                        if len(data) != page['size'] or digest(data) != page['sha256']:
                            raise ArchiveError('Cached streaming page changed; publication held.')
                        info = zipfile.ZipInfo(page['file'], date_time=(1980,1,1,0,0,0))
                        info.external_attr = 0o644 << 16
                        with archive.open(info, 'w', force_zip64=True) as target:
                            target.write(data)
                output.flush(); os.fsync(output.fileno())
            if self.pipe is not None and self.stream_error is None:
                try:
                    self.pipe.finish()
                except NetworkPolicyHold: raise
                except (ArchiveError, OSError) as error: self.stream_error = error
        except BaseException as error:
            self.error = error
        finally:
            if self.pipe is not None: self.pipe.close()

    def finish(self, metadata, check):
        from .cloud import _write
        from .archive import verify_episode_contents
        while self.thread.is_alive():
            if callable(check): check()
            self.thread.join(.2)
        if self.error is not None: raise self.error
        with self.temporary.open('rb') as source:
            if verify_episode_contents(source, metadata):
                raise ArchiveError('Streaming archive verification failed; cached pages are retained.')
        if self.stream_error is None:
            self.record.update(state='uploaded', sha256=self.sha.hexdigest())
            _write(self.receipt, self.record)
        self.temporary.replace(self.archive)

    def close(self):
        with self.condition:
            self.stopped = True; self.condition.notify_all()
        if self.pipe is not None: self.pipe.close()
        self.thread.join(timeout=15)
        if self.thread.is_alive():
            raise ArchiveError('Streaming writer is still stopping; retry after it finishes.')
