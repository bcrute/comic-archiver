"""Remove only exact journaled pilot/staging filenames, including partials."""
import configparser
import re
import threading

import httpx

from .b2_auth import _request
from .cloud_errors import CloudTransferError
from .models import ArchiveError
from .publication import valid_export_id


def cleanup_file(transfer, path, *, upload=False):
    """upload: a file an interrupted direct upload recorded at its library path, not a test or staging file."""
    from .cloud import CLOUD_ROOTS
    parts = path.split('/') if isinstance(path, str) else []
    names = {'episode.cbz','episode.epub','episode.tar.zst','episode.json','series.json',
             'cover.jpg','cover.png','cover.webp','cover.gif'}
    pilot = (not upload and len(parts) == 6 and parts[2] == 'tests' and valid_export_id(parts[4])
             and parts[5] in names)
    stage = not upload and len(parts) == 5 and parts[2] == '.staging' and parts[4] == 'episode.cbz'
    sent = (upload and len(parts) == 6 and valid_export_id(parts[2])
            and re.fullmatch(r'[0-9a-f]{64}', parts[4]) is not None and parts[5] in names)
    if (not (pilot or stage or sent) or parts[0] not in CLOUD_ROOTS
            or not valid_export_id(parts[1]) or not valid_export_id(parts[3])):
        raise ArchiveError('B2 cleanup accepts only exact recorded test, staging or interrupted upload files.')
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(transfer.config)
    try:
        section = parser[transfer.target['remote']]
        key = (section['account'], section['key'])
    except (configparser.Error, KeyError):
        raise CloudTransferError('cloud_login_required') from None
    cancel = threading.Event()
    with httpx.Client(timeout=20, follow_redirects=False, trust_env=False) as client:
        account = _request(client, 'GET', 'https://api.backblazeb2.com/b2api/v4/b2_authorize_account',
                           transfer.guard, cancel, auth=key)
        try:
            api = account['apiInfo']['storageApi']
            buckets = api['allowed']['buckets']
            if (not isinstance(buckets, list) or len(buckets) != 1
                    or buckets[0]['name'] != transfer.bucket
                    or not isinstance(buckets[0]['id'], str)
                    or not isinstance(api['apiUrl'], str)
                    or not re.fullmatch(r'https://api[0-9]+\.backblazeb2\.com', api['apiUrl'])
                    or not isinstance(account['authorizationToken'], str)):
                raise ValueError
            bucket_id = buckets[0]['id']
            headers = {'Authorization':account['authorizationToken']}
            def call(operation, values):
                return _request(client, 'POST', api['apiUrl'] + '/b2api/v4/' + operation,
                                transfer.guard, cancel, headers=headers, json=values)
            for unfinished in (True, False):
                operation = 'b2_list_unfinished_large_files' if unfinished else 'b2_list_file_versions'
                prefix_key = 'namePrefix' if unfinished else 'prefix'
                cursor = {}
                for _ in range(10):
                    result = call(operation, {'bucketId':bucket_id, prefix_key:path, 'maxFileCount':20, **cursor})
                    files = result['files']
                    if not isinstance(files, list) or len(files) > 20:
                        raise ValueError
                    for item in files:
                        if not isinstance(item, dict) or not isinstance(item.get('fileName'), str):
                            raise ValueError
                        if item['fileName'] != path:
                            continue
                        if (item.get('bucketId') != bucket_id or not isinstance(item.get('fileId'), str)
                                or not re.fullmatch(r'[A-Za-z0-9_-]{1,512}', item['fileId'])
                                or item.get('action') not in ({'start'} if unfinished else {'upload','hide'})):
                            raise ValueError
                        removal = 'b2_cancel_large_file' if unfinished else 'b2_delete_file_version'
                        call(removal, {'fileId':item['fileId'], **({} if unfinished else {'fileName':path})})
                    next_id = result.get('nextFileId')
                    if not next_id:
                        break
                    if not isinstance(next_id, str):
                        raise ValueError
                    cursor = {'startFileId':next_id}
                    if not unfinished:
                        name = result.get('nextFileName')
                        if not isinstance(name, str) or not name.startswith(path):
                            raise ValueError
                        cursor['startFileName'] = name
                else:
                    raise CloudTransferError('cloud_transfer_limit')
                result = call(operation, {'bucketId':bucket_id, prefix_key:path, 'maxFileCount':20})
                if (not isinstance(result.get('files'), list)
                        or result.get('nextFileId')
                        or any(not isinstance(item, dict) or item.get('fileName') == path for item in result['files'])):
                    raise CloudTransferError('cloud_verification')
        except (KeyError, TypeError, AttributeError, ValueError):
            raise CloudTransferError('cloud_connection') from None
