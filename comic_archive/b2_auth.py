"""Connect one private bucket owned and billed directly to the user."""
import json
import re

import httpx

from .cloud_errors import CloudTransferError
from .models import ArchiveError
from .network import NetworkGuard
from .retry_after import retry_after_seconds

BUCKET = re.compile(r'[A-Za-z0-9-]{6,63}')


def validate(credentials):
    keys = {k: v for k, v in credentials.items() if k != 'bucket'}
    if (set(keys) != {'key_id', 'application_key'}
            or any(not isinstance(v, str) or not re.fullmatch(r'[A-Za-z0-9_+/=-]+', v)
                   or not 8 <= len(v) <= 256 for v in keys.values())):
        raise ArchiveError('Paste the keyID and applicationKey shown by Backblaze.')
    # The bucket the person named in setup, when given; the key must be for that bucket.
    if 'bucket' in credentials and (not isinstance(credentials['bucket'], str) or not BUCKET.fullmatch(credentials['bucket'])):
        raise ArchiveError('Enter your bucket name: 6 to 63 letters, numbers or hyphens.')


def _pause(seconds, cancel):
    if cancel.wait(seconds):
        raise ArchiveError('Cloud sign-in stopped.')


def _body(response, guard, cancel):
    data = bytearray()
    for block in response.iter_bytes(4096):
        guard.check()
        if cancel.is_set():
            raise ArchiveError('Cloud sign-in stopped.')
        data.extend(block)
        if len(data) > 65536:
            raise ValueError
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError
    return value


def _request(client, method, url, guard, cancel, **options):
    """One B2 call; a busy service is asked again a few times, waiting as it says, up to half a minute."""
    for attempt in range(4):
        if cancel.is_set():
            raise ArchiveError('Cloud sign-in stopped.')
        guard.check()
        try:
            with client.stream(method, url, **options) as response:
                if response.status_code in (429, 503) and attempt < 3:
                    wait = retry_after_seconds(response.headers.get('Retry-After'))
                    wait = min(30, 2 ** attempt if wait is None else wait)
                elif response.status_code == 403:
                    # Backblaze refuses a key's calls with 403 once a cap set on its Caps & Alerts page is reached.
                    try:
                        code = _body(response, guard, cancel).get('code')
                    except (httpx.HTTPError, ValueError):
                        code = None
                    raise CloudTransferError('cloud_cap' if isinstance(code, str) and code.endswith('cap_exceeded')
                                             else 'b2_key_permissions')
                elif response.status_code == 401:
                    raise CloudTransferError('b2_key_permissions')
                elif response.status_code == 429:
                    raise CloudTransferError('cloud_rate_limited')
                elif response.status_code != 200:
                    raise CloudTransferError('cloud_connection')
                else:
                    return _body(response, guard, cancel)
        except (httpx.HTTPError, ValueError):
            raise CloudTransferError('cloud_connection') from None
        _pause(wait, cancel)


def authorized(client, credentials, guard, cancel):
    """A validated private, bucket-scoped authorization for platform transfer adapters.

    Callers own the client and guard; the returned token stays in memory. The desktop
    connection format below remains unchanged.
    """
    validate(credentials)
    account = _request(client, 'GET', 'https://api.backblazeb2.com/b2api/v4/b2_authorize_account',
                       guard, cancel, auth=(credentials['key_id'], credentials['application_key']))
    try:
        api = account['apiInfo']['storageApi']
        allowed = api['allowed']
        buckets = allowed['buckets']
        capabilities = allowed['capabilities']
        if (not isinstance(buckets, list) or len(buckets) != 1
                or not isinstance(buckets[0], dict)
                or not isinstance(capabilities, list)
                or not {'listBuckets', 'listFiles', 'readFiles', 'writeFiles', 'deleteFiles'} <= set(capabilities)
                or allowed.get('namePrefix')):
            raise CloudTransferError('b2_key_permissions')
        bucket = buckets[0]
        if not isinstance(bucket.get('name'), str) or not BUCKET.fullmatch(bucket['name']):
            raise CloudTransferError('b2_key_permissions')
        if credentials.get('bucket') and credentials['bucket'].lower() != bucket['name'].lower():
            raise CloudTransferError('b2_bucket_mismatch')
        if (not isinstance(api['apiUrl'], str)
                or not re.fullmatch(r'https://api[0-9]+\.backblazeb2\.com', api['apiUrl'])
                or not isinstance(account['accountId'], str)
                or not isinstance(account['authorizationToken'], str)):
            raise ValueError
        listing = _request(client, 'POST', api['apiUrl'] + '/b2api/v4/b2_list_buckets', guard, cancel,
                           headers={'Authorization': account['authorizationToken']},
                           json={'accountId': account['accountId'], 'bucketName': bucket['name']})
        matches = listing['buckets']
        if (not isinstance(matches, list) or len(matches) != 1
                or matches[0].get('bucketName') != bucket['name']
                or matches[0].get('bucketId') != bucket['id']):
            raise ValueError
        if matches[0].get('bucketType') != 'allPrivate':
            raise CloudTransferError('b2_private_bucket')
    except (KeyError, TypeError, AttributeError, ValueError):
        raise CloudTransferError('cloud_connection') from None
    return account


def connection(root, credentials, cancel):
    """Discover settings from a bucket-scoped key; never request a password."""
    validate(credentials)
    guard = NetworkGuard(root)
    with httpx.Client(timeout=20, follow_redirects=False, trust_env=False) as client:
        account = authorized(client, credentials, guard, cancel)
    bucket = account['apiInfo']['storageApi']['allowed']['buckets'][0]
    return {'type': 'b2', 'account': credentials['key_id'], 'key': credentials['application_key'],
            'archive_bucket': bucket['name'], 'hard_delete': 'true',
            'chunk_size': '5Mi', 'upload_concurrency': '1'}
