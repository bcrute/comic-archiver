"""Verify the actual pinned Linux helper download without any provider login.

Run only through the CI validation. The install stays inside this job's
private temporary home, and no secret is supplied.
"""
import json
import hashlib
from pathlib import Path
import subprocess
import tempfile
import threading
from unittest.mock import patch

from comic_archive import cloud_tools


def main():
    with tempfile.TemporaryDirectory(prefix='cloud-helper-probe-') as work:
        root = Path(work)
        with patch.object(Path, 'home', return_value=root), patch('comic_archive.cloud_tools.shutil.which', return_value=None), patch('comic_archive.cloud_tools.NetworkGuard'):
            binary = cloud_tools.ensure(root/'Library', threading.Event())
        version = subprocess.check_output([binary, 'version'], text=True, timeout=10)
        if version.splitlines()[0] != 'rclone v'+cloud_tools.VERSION:
            raise RuntimeError('Verified helper returned an unexpected version')
        b2_help = subprocess.check_output([binary, 'help', 'backend', 'b2'], text=True, timeout=10)
        if not all(flag in b2_help for flag in ('--b2-chunk-size', '--b2-upload-concurrency', '--b2-hard-delete')):
            raise RuntimeError('Helper B2 streaming/deletion options changed')
        # A copy is checked by reading it back when B2's own checksum is not listed.
        fixture = root/'hash-fixture'; fixture.write_bytes(b'a' * (4 * 1024**2) + b'b')
        actual = subprocess.check_output([binary, 'hashsum', 'SHA256', str(fixture), '--download'], text=True, timeout=10)
        if actual.split()[0] != hashlib.sha256(fixture.read_bytes()).hexdigest():
            raise RuntimeError('Helper SHA-256 read-back changed')
    print(json.dumps({'pinned_cloud_helper_install':True,'version':cloud_tools.VERSION,
                      'provider_logins':0,'source_requests':0,'cloud_uploads':0}))


if __name__ == '__main__':
    main()
