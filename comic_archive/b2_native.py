"""B2 v4 transfer contract for the embedded runtime, without an executable helper.

The shared cloud controller still owns destinations, durable intents, verification
receipts and local deletion. This adapter never deletes a local original. Requests
use private bucket-scoped credentials, bounded metadata and the shared network guard.
"""
import configparser
import hashlib
import re
import threading
import time
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx

from . import b2_auth
from .cloud import Transfer, _config, transfer_timeout
from .cloud_errors import CloudTransferError
from .models import ArchiveError
from .network import NetworkGuard
from .publication import valid_export_id
from .transport import protect_http_logs

PART = 5 * 1024**2
MAX_PARTS = 10000
MAX_BUFFER = 64 * 1024**2
HEX1 = re.compile(r'[a-f0-9]{40}')
HEX256 = re.compile(r'[a-f0-9]{64}')
IDENTIFIER = re.compile(r'[A-Za-z0-9_-]{1,512}')


def _fail(code='cloud_verification'):
    raise CloudTransferError(code)


def _token(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 8192 or not value.isascii() or not value.isprintable():
        _fail('cloud_connection')
    return value


def _path(value):
    if not isinstance(value, str):
        _fail()
    try:
        if len(value.encode('utf-8')) > 1024:
            _fail()
    except UnicodeError:
        _fail()
    parts = value.split('/')
    if (len(parts) < 2 or parts[0] != 'Comic-Archive' or not valid_export_id(parts[1])
            or any(p in {'', '.', '..'} or '\\' in p or any(ord(c) < 32 or ord(c) == 127 for c in p) for p in parts)):
        _fail()
    return value


class NativeB2Transfer(Transfer):
    """Implements the existing transfer facade; no rclone command is executed."""
    def __init__(self, root, provider, target, *, client_factory=None):
        if provider != 'b2':
            raise ArchiveError('Choose a supported cloud provider.')
        self.provider, self.target = provider, dict(target)
        self.config = _config(target['config'])
        self.guard = NetworkGuard(root)
        self.full_readback = False
        self.cancel = threading.Event()
        self._factory = client_factory or httpx.Client
        self._authorization = None
        self._authorized_at = 0
        self._lock = threading.RLock()
        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read(self.config)
            section = parser[target['remote']]
            if section['type'] != 'b2':
                raise ValueError
            self.bucket = section['archive_bucket']
            self._credentials = {'key_id': section['account'], 'application_key': section['key'], 'bucket': self.bucket}
            b2_auth.validate(self._credentials)
        except (configparser.Error, KeyError, ValueError):
            raise CloudTransferError('cloud_login_required') from None
        protect_http_logs()

    def _check(self):
        if self.cancel.is_set():
            raise ArchiveError('Cloud transfer stopped. Saved files are retained.')
        self.guard.check()

    def _client(self):
        self._check()
        return self._factory(timeout=60, follow_redirects=False, trust_env=False)

    def _account(self):
        with self._lock:
            if self._authorization is None or time.monotonic() - self._authorized_at >= 600:
                with self._client() as client:
                    account = b2_auth.authorized(client, self._credentials, self.guard, self.cancel)
                try:
                    api = account['apiInfo']['storageApi']
                    bucket = api['allowed']['buckets'][0]
                    if (not IDENTIFIER.fullmatch(bucket['id']) or bucket['name'] != self.bucket
                            or not re.fullmatch(r'https://f[0-9]+\.backblazeb2\.com', api['downloadUrl'])):
                        raise ValueError
                    _token(account['authorizationToken'])
                except (KeyError, TypeError, ValueError):
                    raise CloudTransferError('cloud_connection') from None
                self._authorization, self._authorized_at = account, time.monotonic()
            return self._authorization

    def _call(self, operation, values):
        account = self._account()
        api = account['apiInfo']['storageApi']['apiUrl']
        with self._client() as client:
            return b2_auth._request(client, 'POST', api + '/b2api/v4/' + operation, self.guard, self.cancel,
                                    headers={'Authorization': account['authorizationToken']}, json=values)

    @property
    def bucket_id(self):
        return self._account()['apiInfo']['storageApi']['allowed']['buckets'][0]['id']

    def _file(self, item):
        try:
            if (not isinstance(item, dict) or item['bucketId'] != self.bucket_id or item['action'] != 'upload'
                    or not isinstance(item['fileId'], str) or not IDENTIFIER.fullmatch(item['fileId'])
                    or type(item['contentLength']) is not int or item['contentLength'] < 0
                    or not isinstance(item['contentSha1'], str)
                    or not (HEX1.fullmatch(item['contentSha1']) or item['contentSha1'] == 'none')):
                raise ValueError
            _path(item['fileName'])
        except (KeyError, TypeError, ValueError):
            raise CloudTransferError('cloud_verification') from None
        return item

    def _listing(self, prefix):
        _path(prefix.rstrip('/'))
        cursor, seen, cursors = None, set(), set()
        for _ in range(1000):
            value = self._call('b2_list_file_names', {'bucketId': self.bucket_id, 'prefix': prefix,
                'maxFileCount': 20, **({'startFileName': cursor} if cursor else {})})
            items = value.get('files')
            if not isinstance(items, list) or len(items) > 20:
                _fail()
            for raw in items:
                item = self._file(raw)
                if not item['fileName'].startswith(prefix) or item['fileName'] in seen:
                    _fail()
                seen.add(item['fileName'])
                yield item
            cursor = value.get('nextFileName')
            if cursor is None:
                return
            if not isinstance(cursor, str) or not cursor.startswith(prefix) or cursor in seen or cursor in cursors:
                _fail()
            # A cursor is an object name, not a new endpoint or arbitrary query.
            _path(cursor)
            cursors.add(cursor)
        _fail('cloud_transfer_limit')

    def _find(self, path):
        _path(path)
        for item in self._listing(path):
            if item['fileName'] == path:
                return item
            # Names are lexical; an exact name would be before a longer prefix match.
            return None
        return None

    def _upload_destination(self, result, operation):
        try:
            url = result['uploadUrl']
            if not isinstance(url, str):
                raise ValueError
            address = urlsplit(url)
            if (address.scheme != 'https' or address.username or address.password or address.port or address.fragment
                    or not re.fullmatch(r'pod-[a-zA-Z0-9-]+\.backblaze\.com|[a-zA-Z0-9-]+\.backblazeb2\.com', address.hostname or '')
                    or not (address.path == '/b2api/v4/' + operation or address.path.startswith('/b2api/v4/' + operation + '/'))
                    or not url.isascii() or not url.isprintable() or len(url) > 8192):
                raise ValueError
            token = _token(result['authorizationToken'])
        except (KeyError, TypeError, ValueError):
            raise CloudTransferError('cloud_connection') from None
        return url, token

    def _send(self, destination, content, size, digest, *, path=None, part=None, file_id=None):
        operation = 'b2_upload_part' if part is not None else 'b2_upload_file'
        identity = destination.get('fileId' if part is not None else 'bucketId')
        if identity != (file_id if part is not None else self.bucket_id):
            _fail()
        url, token = self._upload_destination(destination, operation)
        headers = {'Authorization': token, 'Content-Length': str(size), 'X-Bz-Content-Sha1': digest}
        headers.update({'X-Bz-Part-Number': str(part)} if part is not None else
                       {'X-Bz-File-Name': quote(_path(path), safe=''), 'Content-Type': 'b2/x-auto'})
        self._check()
        try:
            with self._client() as client, client.stream('POST', url, headers=headers, content=content) as response:
                self._status(response)
                return b2_auth._body(response, self.guard, self.cancel)
        except (httpx.HTTPError, ValueError):
            raise CloudTransferError('cloud_connection') from None

    def _status(self, response):
        if response.status_code == 200:
            return
        code = None
        try:
            code = b2_auth._body(response, self.guard, self.cancel).get('code')
        except (httpx.HTTPError, ValueError):
            pass
        if isinstance(code, str) and code.endswith('cap_exceeded'):
            _fail('cloud_cap')
        _fail({401: 'cloud_login_required', 403: 'cloud_permission', 429: 'cloud_rate_limited'}.get(
            response.status_code, 'cloud_connection'))

    def _chunks(self, stream, size=None):
        deadline = time.monotonic() + transfer_timeout(size)
        while True:
            self._check()
            if time.monotonic() > deadline:
                _fail('cloud_transfer_limit')
            chunk = stream.read(64 * 1024)
            if not chunk:
                return
            yield chunk

    def _hash(self, stream, size=None):
        whole, sha1, length = hashlib.sha256(), hashlib.sha1(usedforsecurity=False), 0
        for block in self._chunks(stream, size):
            whole.update(block); sha1.update(block); length += len(block)
        return whole.hexdigest(), sha1.hexdigest(), length

    def _download(self, item):
        account = self._account()
        url = account['apiInfo']['storageApi']['downloadUrl'] + '/b2api/v4/b2_download_file_by_id'
        whole, length = hashlib.sha256(), 0
        deadline = time.monotonic() + transfer_timeout(item['contentLength'])
        try:
            with self._client() as client, client.stream('GET', url, params={'fileId': item['fileId']},
                    headers={'Authorization': account['authorizationToken'], 'Accept-Encoding': 'identity'}) as response:
                self._status(response)
                # A provider response for another object cannot verify the selected file.
                if response.headers.get('X-Bz-File-Id') != item['fileId']:
                    _fail()
                for block in response.iter_bytes(64 * 1024):
                    self._check(); length += len(block)
                    if length > item['contentLength'] or time.monotonic() > deadline:
                        _fail()
                    whole.update(block)
        except httpx.HTTPError:
            raise CloudTransferError('cloud_connection') from None
        if length != item['contentLength']:
            _fail()
        return whole.hexdigest()

    def verify(self, remote_path, expected, *, source=None):
        if not isinstance(expected, str) or not HEX256.fullmatch(expected):
            _fail()
        item = self._find(remote_path)
        if item is None:
            _fail()
        if source is not None:
            with Path(source).open('rb') as stream:
                whole, sha1, length = self._hash(stream, Path(source).stat().st_size)
            if whole != expected or length != item['contentLength']:
                _fail()
            if not self.full_readback and HEX1.fullmatch(item['contentSha1']):
                if sha1 != item['contentSha1']:
                    _fail()
                return
        if self._download(item) != expected:
            _fail()

    def _existing(self, path, expected, size):
        item = self._find(path)
        if item is None:
            return False
        if item['contentLength'] != size or self._download(item) != expected:
            _fail()  # Immutable means a different object is never overwritten.
        return True

    def upload(self, source, remote_path, expected):
        with Path(source).open('rb') as stream:
            whole, sha1, length = self._hash(stream, Path(source).stat().st_size)
            if whole != expected:
                _fail()
            if self._existing(remote_path, expected, length):
                return
            stream.seek(0)
            if length > 5_000_000_000:
                part_size = max(PART, (length + MAX_PARTS - 1) // MAX_PARTS)
                pipe = NativeB2Stream(self, remote_path, part_size=part_size)
                try:
                    for block in self._chunks(stream, length):
                        pipe.write(block)
                    pipe.finish()
                finally:
                    pipe.close()
            else:
                destination = self._call('b2_get_upload_url', {'bucketId': self.bucket_id})
                item = self._file(self._send(destination, self._chunks(stream, length), length, sha1, path=remote_path))
                if item['fileName'] != remote_path or item['contentLength'] != length or item['contentSha1'] != sha1:
                    _fail()
        self.verify(remote_path, expected, source=source)

    def upload_bytes(self, data, remote_path):
        import io
        whole = hashlib.sha256(data).hexdigest()
        if self._existing(remote_path, whole, len(data)):
            return
        if len(data) > 5_000_000_000:
            pipe = self.start_stream(remote_path)
            try:
                for start in range(0, len(data), 64 * 1024):
                    pipe.write(memoryview(data)[start:start + 64 * 1024])
                pipe.finish()
            finally:
                pipe.close()
        else:
            sha1 = hashlib.sha1(data, usedforsecurity=False).hexdigest()
            destination = self._call('b2_get_upload_url', {'bucketId': self.bucket_id})
            item = self._file(self._send(destination, self._chunks(io.BytesIO(data), len(data)), len(data), sha1, path=remote_path))
            if item['fileName'] != remote_path or item['contentLength'] != len(data) or item['contentSha1'] != sha1:
                _fail()

    def start_stream(self, remote_path):
        if self._find(remote_path) is not None:
            _fail()  # Never replace an immutable published/staging object.
        return NativeB2Stream(self, remote_path)

    def sha1s(self, remote_path):
        found = self._find(remote_path)
        items = [found] if found is not None else self._listing(_path(remote_path) + '/')
        return {item['fileName'].rsplit('/', 1)[-1] if found is not None else item['fileName'][len(remote_path) + 1:]:
                item['contentSha1'] for item in items if HEX1.fullmatch(item['contentSha1'])}

    def _from_remote(self, value):
        prefix = self.target['remote'] + ':' + self.bucket + '/'
        if not isinstance(value, str) or not value.startswith(prefix):
            _fail()
        return _path(value[len(prefix):])

    def _copy(self, original, destination):
        if original['contentLength'] <= 5_000_000_000:
            copied = self._file(self._call('b2_copy_file', {'sourceFileId': original['fileId'],
                'fileName': destination, 'destinationBucketId': self.bucket_id, 'metadataDirective': 'COPY'}))
            if copied['fileName'] != destination or copied['contentLength'] != original['contentLength']:
                _fail()
        else:
            pipe = NativeB2Stream(self, destination)
            try:
                pipe.copy_from(original)
                pipe.finish()
            finally:
                pipe.close()

    def run(self, *arguments, missing_ok=False, size=0):
        # Only the two fixed legacy facade calls still used by shared cloud jobs.
        if len(arguments) == 3 and arguments[0] == 'lsf' and arguments[2] == '--files-only':
            prefix = self._from_remote(arguments[1]) + '/'
            names = [item['fileName'][len(prefix):] for item in self._listing(prefix)]
            if any('/' in name for name in names):
                _fail()
            return ''.join(name + '\n' for name in names).encode()
        if len(arguments) == 4 and arguments[0] == 'moveto' and arguments[3] == '--immutable':
            source, destination = self._from_remote(arguments[1]), self._from_remote(arguments[2])
            stage, final = source.split('/'), destination.split('/')
            if (len(stage) != 5 or stage[2] != '.staging' or not valid_export_id(stage[3])
                    or stage[4] != 'episode.cbz' or len(final) != 6 or stage[1] != final[1]
                    or not valid_export_id(final[2]) or not valid_export_id(final[3])
                    or not HEX256.fullmatch(final[4]) or final[5] != 'episode.cbz'):
                _fail()
            original = self._find(source)
            if original is None:
                _fail()
            digest = self._download(original)
            if not self._existing(destination, digest, original['contentLength']):
                self._copy(original, destination)
                self.verify(destination, digest)
            # The shared stage intent authorizes removal of exactly this source path.
            self.delete_test_file(source)
            return b''
        raise ArchiveError('Cloud transfer accepts only the library\'s fixed operations.')


class NativeB2Stream:
    """Sequential bounded multipart upload; incomplete objects retain core cleanup intent."""
    def __init__(self, transfer, path, *, part_size=PART):
        if not PART <= part_size <= MAX_BUFFER:
            _fail('cloud_transfer_limit')
        self.transfer, self.path, self.part_size = transfer, _path(path), part_size
        self.buffer = bytearray()
        self.parts = []
        self.length = 0
        self.finished = False
        self.closed = False
        started = transfer._call('b2_start_large_file', {'bucketId': transfer.bucket_id,
            'fileName': self.path, 'contentType': 'b2/x-auto'})
        self.file_id = started.get('fileId')
        if (not isinstance(self.file_id, str) or not IDENTIFIER.fullmatch(self.file_id)
                or started.get('fileName') != self.path or started.get('bucketId') != transfer.bucket_id):
            _fail()

    def _part(self):
        import io
        self.transfer._check()
        if len(self.parts) >= MAX_PARTS:
            _fail('cloud_transfer_limit')
        data = bytes(self.buffer); self.buffer.clear()
        sha1 = hashlib.sha1(data, usedforsecurity=False).hexdigest()
        destination = self.transfer._call('b2_get_upload_part_url', {'fileId': self.file_id})
        number = len(self.parts) + 1
        answer = self.transfer._send(destination, self.transfer._chunks(io.BytesIO(data), len(data)),
            len(data), sha1, part=number, file_id=self.file_id)
        if (answer.get('fileId') != self.file_id or type(answer.get('partNumber')) is not int
                or type(answer.get('contentLength')) is not int or answer.get('partNumber') != number
                or answer.get('contentLength') != len(data) or answer.get('contentSha1') != sha1):
            _fail()
        self.parts.append(sha1); self.length += len(data)

    def write(self, data):
        if self.closed or self.finished:
            raise ArchiveError('Cloud upload is already closed.')
        view = memoryview(data)
        while view:
            self.transfer._check()
            count = min(self.part_size - len(self.buffer), len(view))
            self.buffer.extend(view[:count]); view = view[count:]
            if len(self.buffer) == self.part_size:
                self._part()

    def copy_from(self, original):
        """Server-side large-file promotion needs no archive-sized memory buffer."""
        if self.closed or self.finished or self.parts or self.buffer:
            raise ArchiveError('Cloud copy is already in progress or closed.')
        width = 5_000_000_000
        if original['contentLength'] > width * MAX_PARTS:
            _fail('cloud_transfer_limit')
        for start in range(0, original['contentLength'], width):
            stop = min(start + width, original['contentLength'])
            number = len(self.parts) + 1
            answer = self.transfer._call('b2_copy_part', {'sourceFileId': original['fileId'],
                'largeFileId': self.file_id, 'partNumber': number, 'range': f'bytes={start}-{stop - 1}'})
            if (answer.get('fileId') != self.file_id or type(answer.get('partNumber')) is not int
                    or answer['partNumber'] != number or type(answer.get('contentLength')) is not int
                    or answer['contentLength'] != stop - start or not isinstance(answer.get('contentSha1'), str)
                    or not HEX1.fullmatch(answer['contentSha1'])):
                _fail()
            self.parts.append(answer['contentSha1']); self.length += stop - start

    def finish(self):
        if self.closed:
            raise ArchiveError('Cloud upload is already closed.')
        if self.finished:
            return
        if self.buffer:
            self._part()
        if not self.parts:
            _fail()  # Portable archives are nonempty; no empty multipart object.
        answer = self.transfer._file(self.transfer._call('b2_finish_large_file',
            {'fileId': self.file_id, 'partSha1Array': self.parts}))
        if answer['fileId'] != self.file_id or answer['fileName'] != self.path or answer['contentLength'] != self.length:
            _fail()
        self.finished = True

    def close(self):
        if self.closed:
            return
        self.closed = True; self.buffer.clear()
        if not self.finished:
            try:
                self.transfer._call('b2_cancel_large_file', {'fileId': self.file_id})
            except (ArchiveError, httpx.HTTPError):
                pass  # The shared durable exact-path intent still owns later cleanup.
