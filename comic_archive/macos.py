"""Native process and volume information through psutil's Darwin implementation."""
import json
import os
from pathlib import Path
import subprocess

import psutil

_awake = None


def network_probe():
    """Ask the bundled Network.framework helper; absent or failed probes hold transfers."""
    unknown = {'connection':'unknown', 'metered':None}
    program = Path(__file__).parent/'native/macos-network-probe'
    try:
        done = subprocess.run([str(program)], stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, check=True, timeout=2)
        if len(done.stdout) > 1024:
            return unknown
        value = json.loads(done.stdout)
        if (not isinstance(value, dict) or set(value) != {'connection', 'metered'}
                or value['connection'] not in ('unknown', 'wifi', 'wired', 'cellular')
                or (value['metered'] is not None and type(value['metered']) is not bool)):
            return unknown
        return value
    except (OSError, ValueError, subprocess.SubprocessError):
        return unknown


def keep_awake(running):
    """Hold an idle-sleep assertion during downloads; it expires if this app dies."""
    global _awake
    if _awake is not None and _awake.poll() is not None:
        _awake = None
    if running and _awake is None:
        try:
            _awake = subprocess.Popen(['/usr/bin/caffeinate', '-i', '-w', str(os.getpid())],
                                      stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL)
        except OSError:
            pass  # Best effort, as on the other desktop platforms.
    elif not running and _awake is not None:
        process, _awake = _awake, None
        try:
            process.terminate()
            process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
                process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass


def runs_command(pid, words):
    """Match complete arguments, never substrings of another application's command."""
    if type(pid) is not int or pid <= 0:
        return False
    try:
        given = psutil.Process(pid).cmdline()
        return bool(given) and all(str(word) in given for word in words)
    except (psutil.Error, OSError):
        # Exited or inaccessible processes must never qualify for termination.
        return False


def drive(path):
    """Report the deepest mounted volume containing this path."""
    path = Path(os.path.realpath(path))
    try:
        entries = psutil.disk_partitions(all=True)
    except (psutil.Error, OSError):
        return 'unknown', 'unknown'
    found = [entry for entry in entries
             if Path(entry.mountpoint) == path or Path(entry.mountpoint) in path.parents]
    if not found:
        return 'unknown', 'unknown'
    entry = max(found, key=lambda item: len(Path(item.mountpoint).parts))
    if entry.fstype in {'smbfs', 'nfs', 'afpfs', 'webdav'}:
        return 'network', entry.fstype
    if entry.fstype in {'cd9660', 'udf'}:
        return 'disc', entry.fstype
    return 'local', entry.fstype
