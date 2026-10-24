"""Real HTTP acquisition and immutable digest checks, not shared-cache qualification."""
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile

from scripts import android_feasibility as feasibility


class CiPackageMirrorTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.paths = []
        self.bodies = {}
        owner = self

        class Cache(BaseHTTPRequestHandler):
            def do_GET(self):
                owner.paths.append(self.path)
                body = owner.bodies.get(self.path)
                self.send_response(200 if body is not None else 404)
                self.end_headers()
                if body is not None:
                    self.wfile.write(body)

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Cache)
        worker = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .05}, daemon=True)
        worker.start()
        self.addCleanup(worker.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.base = f'http://127.0.0.1:{server.server_port}/HTTPS///'
        configured = patch.dict(os.environ, {feasibility.CI_DOWNLOAD_MIRROR_ENV: self.base})
        configured.start()
        self.addCleanup(configured.stop)

    def test_actual_cache_download_keeps_the_original_pin_and_refuses_changed_bytes(self):
        path = '/HTTPS///vendor.invalid/tool.tar.gz'
        self.bodies[path] = b'abc'
        pin = {'url': 'https://vendor.invalid/tool.tar.gz',
               'sha256': 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad'}
        target = self.root / 'download.tar.gz'
        with patch.dict(feasibility.PINS, {'cache-test': pin}):
            self.assertEqual(feasibility._fetch_pinned('cache-test', target), (True, pin['sha256']))
            self.assertEqual(target.read_bytes(), b'abc')
            target.unlink()
            self.bodies[path] = b'changed by cache'
            okay, reason = feasibility._fetch_pinned('cache-test', target)
        self.assertFalse(okay)
        self.assertIn('sha256 mismatch', reason)
        self.assertFalse(target.exists())
        self.assertEqual(self.paths, [path, path])

    def test_sdk_listing_and_archive_use_the_cache_and_keep_the_published_digest(self):
        packet = io.BytesIO()
        with zipfile.ZipFile(packet, 'w') as archive:
            archive.writestr('tools/package.xml', b'<localPackage/>')
        original = packet.getvalue()
        digest = hashlib.sha1(original, usedforsecurity=False).hexdigest()
        listing = '/HTTPS///vendor.invalid/sdk/repository.xml'
        artifact = '/HTTPS///vendor.invalid/sdk/tools.zip'
        self.bodies[listing] = ('<repository><remotePackage path="platform-tools"><archives><archive>'
            f'<host-os>linux</host-os><complete><checksum type="sha1">{digest}</checksum>'
            '<url>tools.zip</url></complete></archive></archives></remotePackage></repository>').encode()
        self.bodies[artifact] = original
        with patch.object(feasibility, 'REPOSITORY_XMLS', ('https://vendor.invalid/sdk/repository.xml',)), \
                patch.object(feasibility, 'TOOLS_DIR', self.root / 'tools'):
            packages = feasibility._sdk_repository_packages()
            self.assertEqual(packages['platform-tools'],
                             {'url': 'https://vendor.invalid/sdk/tools.zip', 'sha1': digest})
            sdk = self.root / 'sdk'
            self.assertEqual(feasibility._install_sdk_package('platform-tools', packages['platform-tools'], sdk),
                             (True, digest))
            self.assertEqual((sdk / 'platform-tools/package.xml').read_bytes(), b'<localPackage/>')
            # Keep the captured published digest; do not restamp the negative case.
            self.bodies[artifact] = b'changed by cache'
            (self.root / 'tools/downloads/platform-tools.zip').unlink()
            okay, reason = feasibility._install_sdk_package('other-tools', packages['platform-tools'], sdk)
            self.assertFalse(okay)
            self.assertIn('sha1 mismatch', reason)
            self.assertFalse((sdk / 'other-tools').exists())
        self.assertEqual(self.paths, [listing, artifact, artifact])
