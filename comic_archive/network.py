"""Local transfer policy. Network identity and credentials never leave the device."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time

from .archive import atomic_json
from .archive_paths import directory
from .models import ArchiveError
from .storage import safe_root


class NetworkPolicyHold(ArchiveError):
    """A policy pause, never a source refusal or an automatic network retry."""


def policy(root):
    path = safe_root(root) / '.archive-state/network-policy.json'
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ArchiveError('Network settings must not be symlinked.')
    if not path.exists():
        return {'schema_version': 1, 'wifi_only': True}
    try:
        if path.stat().st_size > 4096:
            raise ValueError
        value = json.loads(path.read_text())
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
    directory(root / '.archive-state').chmod(0o700)
    # Independent of the import writer lock so a running transfer can be held.
    path = root / '.archive-state/network-policy.json'
    value = {'schema_version': 1, 'wifi_only': wifi_only}
    atomic_json(path, value)
    path.chmod(0o600)
    return status(root)


def _command(*args):
    result = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, timeout=2, check=False,
                            env=dict(os.environ, LC_ALL='C'))
    if result.returncode or len(result.stdout) > 65536:
        raise ValueError
    return result.stdout.decode('utf-8', errors='strict').strip()


def probe():
    """Require every default-route interface to be unmetered.

    Unknown platforms, VPN routes, absent metering and conflicting routes hold.
    Do not report addresses, SSIDs, connection names or interface identifiers.
    """
    unknown = {'connection': 'unknown', 'metered': None}
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
    observed = probe()
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
                self._observed = probe()
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
