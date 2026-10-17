"""Install the pinned cloud helper privately when a user clicks Sign in.

Linux and Windows. The downloaded ZIP must match the upstream release digest.
"""
import hashlib
import os
from pathlib import Path
import platform
import shutil
import tempfile
import zipfile

import httpx

from .archive_paths import directory
from .models import ArchiveError
from .network import NetworkGuard
from .storage import safe_root

VERSION = '1.75.2'
# https://github.com/rclone/rclone/releases/download/v1.75.2/SHA256SUMS, the same at downloads.rclone.org
DIGESTS = {
    ('linux', 'amd64'):'349ac8fba6ff65d6247043f1750cdcb518ec5d500ef91463a10d37c0ccdf3702',  # gitleaks:allow public upstream SHA-256
    ('linux', 'arm64'):'7e1e8d69654941b7b7df84ee74c5f7fb09cce7d5496947fb7bf16b55ff427d10',  # gitleaks:allow public upstream SHA-256
    ('windows', 'amd64'):'8a9db2bad1dc79a65b3348fd857069598e2aab7cf4d118b77b1df23055aff818',  # gitleaks:allow public upstream SHA-256
    ('windows', 'arm64'):'7eb9844c9e5b723b9ef28821891caa17f6d9d1a375c49365cbe04114cec8dcd9',  # gitleaks:allow public upstream SHA-256
}
SYSTEMS = {'Linux':'linux', 'Windows':'windows'}
ARCHES = {'x86_64':'amd64', 'AMD64':'amd64', 'aarch64':'arm64', 'arm64':'arm64', 'ARM64':'arm64'}


def location():
    """The folder the helper is kept in, and the name of its build for this computer."""
    build = (SYSTEMS.get(platform.system()), ARCHES.get(platform.machine()))
    if build not in DIGESTS:
        raise ArchiveError('Cloud support is not available for this device yet.')
    if build[0] == 'windows':
        base = Path(os.environ.get('LOCALAPPDATA') or '')
        folder = (base if base.is_absolute() else Path.home()/'AppData/Local')/'comic-archiver/tools'
    else:
        from .storage import safe_root
        folder = safe_root(Path.home()/'.local/share/comic-archive/tools')
    return folder/VERSION/'-'.join(build), build


def program(build):
    return 'rclone.exe' if build[0] == 'windows' else 'rclone'


def offered():
    """Whether cloud saving can be set up on this computer at all."""
    try:
        location()
    except ArchiveError:
        return False
    return True


def installed():
    try:
        folder, build = location()
        binary = safe_root(folder/program(build))
        return str(binary) if binary.is_file() and os.access(binary, os.X_OK) else None
    except ArchiveError:
        return None


def ensure(root, cancel):
    if binary := shutil.which('rclone') or installed():
        return binary
    folder, build = location()
    safe_root(folder); directory(folder).chmod(0o700)
    guard = NetworkGuard(root); guard.check()
    if shutil.disk_usage(folder).free < 1024**3:
        raise ArchiveError('Free some device space before connecting cloud storage.')
    name = f'rclone-v{VERSION}-{build[0]}-{build[1]}'
    url = f'https://github.com/rclone/rclone/releases/download/v{VERSION}/{name}.zip'
    with tempfile.TemporaryDirectory(prefix='install-', dir=folder) as work:
        package = Path(work)/'helper.zip'; digest = hashlib.sha256(); size = 0
        with httpx.stream('GET', url, follow_redirects=True, timeout=30) as response, package.open('wb') as stream:
            response.raise_for_status()
            for chunk in response.iter_bytes(1024*1024):
                guard.check()
                if cancel.is_set():
                    raise ArchiveError('Cloud sign-in stopped.')
                size += len(chunk)
                if size > 64*1024**2:
                    raise ArchiveError('Cloud support download was too large.')
                digest.update(chunk); stream.write(chunk)
        if digest.hexdigest() != DIGESTS[build]:
            raise ArchiveError('Cloud support download could not be verified.')
        with zipfile.ZipFile(package) as archive:
            member = archive.getinfo(name+'/'+program(build))
            if member.file_size > 256*1024**2:
                raise ArchiveError('Cloud support download was too large.')
            candidate = Path(work)/program(build)
            with archive.open(member) as source, candidate.open('wb') as stream:
                shutil.copyfileobj(source, stream)
            candidate.chmod(0o700)
            if cancel.is_set():
                raise ArchiveError('Cloud sign-in stopped.')
            safe_root(folder/program(build))
            os.replace(candidate, folder/program(build))
    return str(folder/program(build))
