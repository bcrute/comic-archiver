"""Verify the actual pinned Linux helper download without any provider login.

Run only through the CI validation. The install stays inside this job's
private temporary home and the only secret supplied is a synthetic fixture.
"""
import json
import hashlib
import configparser
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
        result = subprocess.run([binary, 'obscure', '-'], input='synthetic-fixture-password', text=True,
                                capture_output=True, check=True, timeout=10)
        if not result.stdout.strip() or 'synthetic-fixture-password' in result.stdout:
            raise RuntimeError('Helper did not obscure its stdin fixture')
        config = root/'fixture.conf';config.touch(mode=0o600)
        parser = configparser.ConfigParser(interpolation=None)
        parser['google'] = {'type':'drive', 'scope':'drive.file',
                            'client_id':'synthetic.apps.googleusercontent.com', 'client_secret':'synthetic-secret'}
        with config.open('w') as file: parser.write(file)
        result = subprocess.run([binary, '--config', str(config), 'config', 'update', 'google',
                                 'config_is_local', 'false', '--non-interactive'],
                                text=True, capture_output=True, check=True, timeout=10)
        question = json.loads(result.stdout)
        answers = {'config_is_local':'false'}
        for expected in ('config_is_local',):
            if question.get('Option', {}).get('Name') != expected:
                # Explicit answers can skip these steps entirely. Some helper
                # versions still return a question with the supplied default.
                continue
            if question['Option']['Default'] is not (answers[expected] == 'true'):
                raise RuntimeError('Helper ignored the supplied Google setup answer')
            result = subprocess.run([binary, '--config', str(config), 'config', 'update', 'google',
                                     'config_is_local', 'false',
                                     '--continue', '--state', question['State'],
                                     '--result', answers[expected], '--non-interactive'], text=True, capture_output=True,
                                    check=True, timeout=10)
            question = json.loads(result.stdout)
        if not question.get('State') or question.get('Option', {}).get('Name') != 'config_token':
            raise RuntimeError('Helper Google setup protocol changed: '+str(question.get('Option', {}).get('Name')))
        parser.read(config)
        if parser['google']['client_id'] != 'synthetic.apps.googleusercontent.com':
            raise RuntimeError('Helper replaced the supplied private Google client')
        dropbox_help = subprocess.check_output([binary, 'help', 'backend', 'dropbox'], text=True, timeout=10)
        if not all(flag in dropbox_help for flag in ('--dropbox-chunk-size', '--dropbox-batch-mode')):
            raise RuntimeError('Helper Dropbox streaming options changed')
        b2_help = subprocess.check_output([binary, 'help', 'backend', 'b2'], text=True, timeout=10)
        if not all(flag in b2_help for flag in ('--b2-chunk-size', '--b2-upload-concurrency', '--b2-hard-delete')):
            raise RuntimeError('Helper B2 streaming/deletion options changed')
        fixture = root/'hash-fixture'; fixture.write_bytes(b'a' * (4 * 1024**2) + b'b')
        native = hashlib.sha256(hashlib.sha256(b'a' * (4 * 1024**2)).digest() + hashlib.sha256(b'b').digest()).hexdigest()
        actual = subprocess.check_output([binary, 'hashsum', 'DropboxHash', str(fixture), '--download'], text=True, timeout=10)
        if actual.split()[0] != native:
            raise RuntimeError('Helper Dropbox checksum algorithm changed')
        help_text = subprocess.check_output([binary, 'help', 'backend', 'protondrive'], text=True, timeout=10)
        if '--protondrive-username' not in help_text or '--protondrive-password' not in help_text:
            raise RuntimeError('Helper Proton authentication options changed')
    print(json.dumps({'pinned_cloud_helper_install':True,'version':cloud_tools.VERSION,
                      'provider_logins':0,'source_requests':0,'cloud_uploads':0}))


if __name__ == '__main__':
    main()
