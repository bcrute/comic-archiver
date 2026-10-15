"""Private, operator-supplied desktop OAuth registration (never user tokens)."""
from pathlib import Path
import re

from . import cloud
from .models import ArchiveError
from .locking import LibraryLock


def load(root):
    path = Path(root) / '.archive-state/cloud/google-client.json'
    if not path.exists():
        from .oauth_registration import load as application_client
        return application_client('google')
    cloud._config(path)
    return validate(cloud._json(path, maximum=65536))


def validate(value):
    client = value.get('installed') if isinstance(value, dict) else None
    if (not isinstance(client, dict)
            or not isinstance(client.get('client_id'), str)
            or not re.fullmatch(r'[A-Za-z0-9_-]{1,256}\.apps\.googleusercontent\.com', client['client_id'])
            or not isinstance(client.get('client_secret'), str)
            or not re.fullmatch(r'[A-Za-z0-9_-]{1,256}', client['client_secret'])):
        raise ArchiveError('Choose the downloaded Google Desktop app client JSON.')
    return {'client_id': client['client_id'], 'client_secret': client['client_secret']}


def install(root, source):
    root = cloud.safe_root(root)
    client = validate(cloud._json(Path(source), maximum=65536))
    with LibraryLock(root):
        cloud._write(root / '.archive-state/cloud/google-client.json', {'installed': client})
    return client
