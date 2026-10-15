"""Publisher-supplied desktop OAuth registration, separate from user sessions.

Release packages may include oauth-clients.json beside this module. Development
can point COMIC_ARCHIVE_OAUTH_CLIENTS at a private registration file. Neither
mechanism contains user tokens, nor asks ordinary users to register an app.
"""
import os
from pathlib import Path


def load(provider):
    from .cloud import _json, _config
    supplied = os.environ.get('COMIC_ARCHIVE_OAUTH_CLIENTS') or os.environ.get('TAPAS_ARCHIVE_OAUTH_CLIENTS')
    path = Path(supplied) if supplied else Path(__file__).with_name('oauth-clients.json')
    if not path.exists():
        return None
    if supplied:
        _config(path)
    value = _json(path, maximum=65536)
    client = value.get(provider)
    if client is None:
        return None
    if provider == 'google':
        from .google_client import validate
        return validate({'installed': client})
    from .dropbox_client import validate
    return validate(client)
