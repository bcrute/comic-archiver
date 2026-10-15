"""Private maintainer registration for the archiver's Dropbox app."""
from pathlib import Path
import re

from . import cloud
from .locking import LibraryLock
from .models import ArchiveError


def validate(value):
    if (not isinstance(value, dict) or set(value) != {'client_id', 'client_secret'}
            or any(not isinstance(value.get(k), str)
                   or not re.fullmatch(r'[A-Za-z0-9_-]{1,256}', value[k])
                   for k in ('client_id', 'client_secret'))):
        raise ArchiveError('Choose the archiver maintainer’s Dropbox app registration JSON.')
    return dict(value)


def load(root):
    path = Path(root) / '.archive-state/cloud/dropbox-client.json'
    if path.exists():
        cloud._config(path)
        return validate(cloud._json(path, maximum=65536))
    from .oauth_registration import load as application_client
    return application_client('dropbox')


def install(root, source):
    root = cloud.safe_root(root)
    client = validate(cloud._json(Path(source), maximum=65536))
    with LibraryLock(root):
        cloud._write(root / '.archive-state/cloud/dropbox-client.json', client)
    return client
