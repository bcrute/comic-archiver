"""Small, anonymous release checks shared by the desktop and Android apps."""
import json
from pathlib import Path
import re
import threading
import time
import httpx


ENDPOINT = 'https://comicarchiver.com/version.json'
DOWNLOAD = 'https://comicarchiver.com/download.html'
DAY = 24 * 60 * 60
MAX_BYTES = 256 * 1024
_LOCK = threading.Lock()


def version_key(value):
    match = re.fullmatch(r'(\d+)\.(\d+)\.(\d+)(?:-?(alpha|a|beta|b|rc)(\d+))?', value)
    if not match:
        raise ValueError('Unknown installed version')
    stage = {None: 3, 'alpha': 0, 'a': 0, 'beta': 1, 'b': 1, 'rc': 2}[match[4]]
    return (*map(int, match.group(1, 2, 3)), stage, int(match[5] or 0))


def release_version(value, platform='desktop'):
    """Read only stable versions from the website's versioned manifest."""
    if not isinstance(value, dict) or type(value.get('schema_version')) is not int or value['schema_version'] != 1:
        raise ValueError('Unknown release manifest')
    version = value.get(platform)
    if not isinstance(version, str) or not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise ValueError('No stable release for this platform')
    return version


def fetch(platform='desktop'):
    # No application credentials, cookies, library details or installed version are sent.
    with httpx.stream('GET', ENDPOINT, headers={
            'Accept': 'application/json', 'User-Agent': 'ComicArchiver-update-check'},
            timeout=8, follow_redirects=False) as response:
        response.raise_for_status()
        data = bytearray()
        for chunk in response.iter_bytes(chunk_size=8192):
            data.extend(chunk)
            if len(data) > MAX_BYTES:
                raise ValueError('Release response too large')
    return release_version(json.loads(data), platform)


def preferences(cache_file):
    try:
        value = json.loads(Path(cache_file).read_text('utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def automatic(cache_file):
    return preferences(cache_file).get('automatic') is True


def save(cache_file, value):
    path = Path(cache_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value), encoding='utf-8')
    temporary.replace(path)


def set_automatic(cache_file, enabled):
    with _LOCK:
        value = preferences(cache_file)
        value['automatic'] = enabled is True
        save(cache_file, value)


def check(current, cache_file, force=False, now=None, platform='desktop'):
    """Run off the UI thread. Only an explicit manual check or opt-in permits a request."""
    now = time.time() if now is None else now
    with _LOCK:
        cached = preferences(cache_file)
        if not force and cached.get('automatic') is not True:
            return {'status': 'disabled', 'latest': None, 'url': DOWNLOAD}
        checked = cached.get('checked')
        fresh = type(checked) in (int, float) and 0 <= now - checked < DAY
        if force or not fresh:
            try:
                latest = fetch(platform)
            except Exception:
                latest = None
            cached.update(checked=now, latest=latest)
            try:
                save(cache_file, cached)
            except OSError:
                pass
        latest = cached.get('latest')
        try:
            if not isinstance(latest, str) or not re.fullmatch(r'\d+\.\d+\.\d+', latest):
                raise ValueError('No valid release')
            available = version_key(latest) > version_key(current)
        except (ValueError, TypeError):
            return {'status': 'unavailable', 'latest': None, 'url': DOWNLOAD}
        return {'status': 'available' if available else 'current', 'latest': latest, 'url': DOWNLOAD}


def check_json(current, cache_file, force=False, platform='android'):
    """The native UI's small JSON result, using the same policy as the desktop."""
    return json.dumps(check(current, cache_file, force, platform=platform))
