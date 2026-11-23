"""Local transfer policy. Network identity and credentials never leave the device."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import threading
import time

from .archive import atomic_json
from .archive_paths import directory
from .models import ArchiveError
from .osdep import read_text
from .storage import safe_root


class NetworkPolicyHold(ArchiveError):
    """A policy pause, never a source refusal or an automatic network retry."""


def _local_path(scope):
    from .state_paths import state_location
    try:
        entry = scope.lstat()
    except FileNotFoundError:
        return None
    if (not stat.S_ISDIR(entry.st_mode)
            or getattr(entry, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
        raise ArchiveError('Choose a regular library folder for connection settings.')
    key = json.dumps([os.path.normcase(str(scope)), entry.st_dev, entry.st_ino]).encode()
    return state_location() / 'network-policies' / (hashlib.sha256(key).hexdigest() + '.json')


def _path(root, *, create=False):
    """Share the local connection rule across the sites linked to this library folder."""
    from .shared import folder
    root = safe_root(root)
    shared = folder(root)
    if sys.platform == 'android':
        # Only the app-private workspace uses this location. The portable working-library
        # importer does not copy the network policy file or the original shared link.
        return (shared or root / '.archive-state') / 'network-policy.json'
    scope = shared or root
    if create:
        directory(scope)
    return _local_path(scope)


def policy(root):
    path = _path(root)
    return _read(path)


def _read(path):
    if path is None:
        return {'schema_version': 1, 'wifi_only': True}
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ArchiveError('Network settings must not be symlinked.')
    if not path.exists():
        return {'schema_version': 1, 'wifi_only': True}
    try:
        if path.stat().st_size > 4096:
            raise ValueError
        value = json.loads(read_text(path))
        if (set(value) != {'schema_version', 'wifi_only'} or type(value['schema_version']) is not int
                or value['schema_version'] != 1 or type(value['wifi_only']) is not bool):
            raise ValueError
        return value
    except (OSError, ValueError, TypeError, KeyError):
        raise ArchiveError('Network settings are invalid; transfers are held.') from None


def configure(root, wifi_only):
    if type(wifi_only) is not bool:
        raise ArchiveError('Wi-Fi only must be an explicit boolean.')
    root = safe_root(root)
    # Independent of the import writer lock so a running transfer can be held.
    path = _path(root, create=True)
    value = {'schema_version': 1, 'wifi_only': wifi_only}
    _save(path, value)
    return status(root)


def _save(path, value):
    from .windows_privacy import folder as protect
    if path is None or path.is_symlink():
        raise ArchiveError('Connection settings location is unavailable.')
    directory(path.parent).chmod(0o700)
    protect(path.parent)
    atomic_json(path, value)
    path.chmod(0o600)


def join(root, shared):
    """Carry forward an explicit local choice when linking on the same device."""
    root = safe_root(root)
    if sys.platform == 'android':
        own, path = root / '.archive-state/network-policy.json', shared / 'network-policy.json'
    else:
        own, path = _local_path(root), _local_path(shared)
    if own is not None and path is not None and own.is_file() and not path.exists():
        _save(path, _read(own))


_SYSTEM_PATH = '/usr/sbin:/usr/bin:/sbin:/bin'


def _command(name, *args):
    executable = shutil.which(name, path=_SYSTEM_PATH)
    if executable is None or not os.path.isabs(executable):
        raise FileNotFoundError(name)
    result = subprocess.run((executable, *args), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, timeout=2, check=False,
                            env={'PATH': _SYSTEM_PATH, 'LC_ALL': 'C'})
    if result.returncode or len(result.stdout) > 65536:
        raise ValueError
    return result.stdout.decode('utf-8', errors='strict').strip()


def probe(root=None):
    """Require an unmetered route without ambiguous egress.

    Unknown platforms, VPN routes, absent metering and conflicting routes hold.
    Do not report addresses, SSIDs, connection names or interface identifiers.
    A computer has one answer for every library; the phone reads the report kept beside the library asked about.
    """
    unknown = {'connection': 'unknown', 'metered': None}
    from .network_android import configured as android_configured, probe as android_probe
    if android_configured():  # The app's own metering facts replace the desktop route probe.
        return android_probe(root)
    if sys.platform == 'darwin':
        from .macos import network_probe
        return network_probe()
    if sys.platform == 'win32':
        try:
            from .network_windows import probe as windows_probe
            return windows_probe()
        except Exception:  # Windows is asked through its own libraries; no failure there may stop a download outright.
            return unknown
    if sys.platform != 'linux':
        return unknown
    try:
        routes = json.loads(_command('ip', '-j', 'route', 'show', 'default'))
        routes += json.loads(_command('ip', '-j', '-6', 'route', 'show', 'default'))
        interfaces = {r['dev'] for r in routes if r.get('type', 'unicast') == 'unicast'}
        if not interfaces or len(interfaces) > 16:
            return unknown
        connections = []
        for interface in sorted(interfaces):
            if not isinstance(interface, str) or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.:-]{0,63}', interface):
                return unknown
            fields = _command('nmcli', '-g', 'GENERAL.TYPE,GENERAL.STATE,GENERAL.METERED',
                              'device', 'show', interface).splitlines()
            if len(fields) != 3 or not fields[1].startswith('100 '):
                return unknown
            kind = {'wifi': 'wifi', 'ethernet': 'wired', 'gsm': 'cellular', 'cdma': 'cellular'}.get(fields[0])
            if kind is None:
                return unknown
            metered = fields[2].lower()
            metered = {'no (guessed)':'guess-no', 'yes (guessed)':'guess-yes'}.get(metered,metered)
            if metered not in {'yes', 'no', 'guess-yes', 'guess-no'}:
                return unknown
            connections.append((kind, metered in {'yes', 'guess-yes'}))
        if any(k == 'cellular' for k, _ in connections):
            return {'connection': 'cellular', 'metered': True}
        return {'connection': 'wifi' if all(k == 'wifi' for k, _ in connections) else 'wired',
                'metered': any(m for _, m in connections)}
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
        return unknown


def status(root):
    value = policy(root)
    observed = probe(root)
    allowed = not value['wifi_only'] or (observed['connection'] in {'wifi', 'wired'} and observed['metered'] is False)
    reason = ('allowed' if allowed else 'metered_connection' if observed['metered'] is True
              else 'connection_unverified')
    return value | observed | {'allowed': allowed, 'reason': reason,
        'hotspot_backhaul_verified': False}


class NetworkGuard:
    def __init__(self, root):
        self.root = Path(root).absolute()
        self._lock = threading.Lock()
        self._checked = 0
        self._observed = None
        self._allowed = True

    def check(self):
        # Settings are reread even inside the short OS-probe cache window.
        if not policy(self.root)['wifi_only']:
            return
        with self._lock:
            now = time.monotonic()
            if self._observed is None or now - self._checked >= 1:
                self._observed = probe(self.root)
                self._checked = time.monotonic()
            allowed = self._observed['connection'] in {'wifi', 'wired'} and self._observed['metered'] is False
            if allowed != self._allowed:
                # For the downloader's log, when the answer changes: the kind of connection only, never its name.
                self._allowed = allowed
                print(f"Connection check: {self._observed['connection']}, metered {self._observed['metered']}; "
                      f"transfers {'allowed again' if allowed else 'held'}.", flush=True)
        if not allowed:
            raise NetworkPolicyHold('Wi-Fi only: transfers held. Connect to unmetered Wi-Fi or wired networking, then retry. Saved work is retained.')


def bind_adapter(adapter, root):
    """Bind real transports to the selected library, including the pooled browser."""
    for owner in (adapter, getattr(adapter, '_inner', None), getattr(adapter, 'transport', None)):
        guard = getattr(owner, 'network_guard', None)
        if isinstance(guard, NetworkGuard): guard.root = Path(root).absolute()


def wait_process(process, guard, timeout=900):
    """Stop only our child if the route becomes cellular, metered or unknown."""
    end = time.monotonic() + timeout
    try:
        while process.poll() is None:
            guard.check()
            if time.monotonic() >= end:
                raise subprocess.TimeoutExpired('cloud transfer', timeout)
            time.sleep(.2)
        guard.check()
        return process.returncode
    except BaseException:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait(timeout=3)
        raise
