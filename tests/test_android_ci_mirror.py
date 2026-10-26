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
import xml.etree.ElementTree as ET
import zipfile

from scripts import android_feasibility as feasibility


class CiPackageMirrorTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.paths = []
        self.bodies = {}
        self.responses = {}
        self.requests = []
        owner = self

        class Cache(BaseHTTPRequestHandler):
            def do_GET(self):
                owner.paths.append(self.path)
                owner.requests.append(dict(self.headers))
                if self.path in owner.responses:
                    status, headers, body = owner.responses[self.path].pop(0)
                    self.send_response(status)
                    for name, value in headers.items():
                        self.send_header(name, str(value))
                    self.end_headers()
                    self.wfile.write(body)
                    self.close_connection = True
                    return
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
        self.base = f'http://127.0.0.1:{server.server_port}/HTTPS/'
        configured = patch.dict(os.environ, {feasibility.CI_DOWNLOAD_MIRROR_ENV: self.base})
        configured.start()
        self.addCleanup(configured.stop)

    def test_actual_cache_download_keeps_the_original_pin_and_refuses_changed_bytes(self):
        path = '/HTTPS/vendor.invalid/tool.tar.gz'
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

    def _interrupted_pin(self, remainder, *, status=206, extent='bytes 3-7/8'):
        path = '/HTTPS/vendor.invalid/tool.tar.gz'
        self.responses[path] = [
            (200, {'Content-Length': 8, 'ETag': '"original"'}, b'abc'),
            (status, {'Content-Length': len(remainder), 'Content-Range': extent}, remainder),
        ]
        target = self.root / 'download.tar.gz'
        pin = {'url': 'https://vendor.invalid/tool.tar.gz',
               'sha256': hashlib.sha256(b'abcdefgh').hexdigest()}
        with patch.dict(feasibility.PINS, {'interrupted': pin}):
            result = feasibility._fetch_pinned('interrupted', target)
        return result, target

    def test_interrupted_transfer_resumes_original_pin_with_conditional_range(self):
        result, target = self._interrupted_pin(b'defgh')
        self.assertEqual(result, (True, hashlib.sha256(b'abcdefgh').hexdigest()))
        self.assertEqual(target.read_bytes(), b'abcdefgh')
        self.assertEqual(self.requests[1]['Range'], 'bytes=3-')
        self.assertEqual(self.requests[1]['If-Range'], '"original"')
        self.assertEqual(list(self.root.glob('*.part')), [])

    def test_changed_resumed_bytes_fail_original_digest_and_leave_no_archive(self):
        result, target = self._interrupted_pin(b'XXXXX')
        self.assertFalse(result[0])
        self.assertIn('sha256 mismatch', result[1])
        self.assertFalse(target.exists())
        self.assertEqual(list(self.root.glob('*.part')), [])

    def test_wrong_resume_offset_leaves_no_complete_or_partial_archive(self):
        result, target = self._interrupted_pin(b'defgh', extent='bytes 2-6/8')
        self.assertEqual(result, (False, 'download failed'))
        self.assertFalse(target.exists())
        self.assertEqual(list(self.root.glob('*.part')), [])

    def test_server_ignoring_range_replaces_prefix_instead_of_appending(self):
        result, target = self._interrupted_pin(b'abcdefgh', status=200)
        self.assertTrue(result[0])
        self.assertEqual(target.read_bytes(), b'abcdefgh')

    def test_refused_resume_does_not_repeat_without_progress(self):
        result, target = self._interrupted_pin(b'', status=403)
        self.assertEqual(result, (False, 'download failed'))
        self.assertFalse(target.exists())
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(list(self.root.glob('*.part')), [])

    def test_unpacked_emulator_keeps_vendor_metadata_and_refuses_wrong_cached_revision(self):
        packet = io.BytesIO()
        with zipfile.ZipFile(packet, 'w') as archive:
            archive.writestr('emulator/source.properties', 'Pkg.Revision=37.2.12\n')
            archive.writestr('emulator/emulator', b'fixture executable')
        original = packet.getvalue()
        digest = hashlib.sha1(original, usedforsecurity=False).hexdigest()
        self.bodies['/HTTPS/vendor.invalid/sdk/repository.xml'] = (
            '<repository xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
            'xmlns:generic="http://schemas.android.com/repository/android/generic/02">'
            '<license id="android-sdk-license" type="text">Vendor fixture terms</license>'
            '<remotePackage path="emulator"><type-details xsi:type="generic:genericDetailsType"/>'
            '<revision><major>37</major><minor>2</minor><micro>12</micro></revision>'
            '<display-name>Android Emulator</display-name><uses-license ref="android-sdk-license"/>'
            '<channelRef ref="channel-0"/><archives><archive><host-os>linux</host-os><complete>'
            f'<checksum type="sha1">{digest}</checksum><url>emulator.zip</url>'
            '</complete></archive></archives></remotePackage></repository>').encode()
        self.bodies['/HTTPS/vendor.invalid/sdk/emulator.zip'] = original
        with patch.object(feasibility, 'REPOSITORY_XMLS', ('https://vendor.invalid/sdk/repository.xml',)), \
                patch.object(feasibility, 'TOOLS_DIR', self.root / 'tools'):
            details = feasibility._sdk_repository_packages()['emulator']
            sdk = self.root / 'sdk'
            self.assertEqual(feasibility._install_sdk_package('emulator', details, sdk), (True, digest))
            metadata = sdk / 'emulator/package.xml'
            document = ET.fromstring(metadata.read_text())  # noqa: S314 - generated fixture metadata
            package = document.find('localPackage')
            self.assertEqual(package.get('path'), 'emulator')
            self.assertEqual(package.findtext('revision/micro'), '12')
            self.assertEqual(document.findtext('license'), 'Vendor fixture terms')
            self.assertNotIn('archives', metadata.read_text())
            self.assertNotIn('channelRef', metadata.read_text())
            metadata.unlink()
            self.assertEqual(feasibility._install_sdk_package('emulator', details, sdk), (True, 'cached'))
            self.assertTrue(metadata.is_file())
            metadata.unlink()
            (sdk / 'emulator/source.properties').write_text('Pkg.Revision=37.2.11\n')
            okay, reason = feasibility._install_sdk_package('emulator', details, sdk)
            self.assertFalse(okay)
            self.assertIn('revision mismatched', reason)
            self.assertFalse(metadata.exists())

    def test_sdk_listing_and_archive_use_the_cache_and_keep_the_published_digest(self):
        packet = io.BytesIO()
        with zipfile.ZipFile(packet, 'w') as archive:
            archive.writestr('tools/package.xml', b'<localPackage/>')
        original = packet.getvalue()
        digest = hashlib.sha1(original, usedforsecurity=False).hexdigest()
        listing = '/HTTPS/vendor.invalid/sdk/repository.xml'
        artifact = '/HTTPS/vendor.invalid/sdk/tools.zip'
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
