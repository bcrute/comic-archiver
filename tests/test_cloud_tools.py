import hashlib
import io
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch
import zipfile

from comic_archive import cloud_tools
from comic_archive.models import ArchiveError
from comic_archive.network import NetworkPolicyHold

# The installed helper is the person's own program; Windows has no such modes and reports a plain file as 0o666.
PRIVATE_PROGRAM = 0o666 if os.name == 'nt' else 0o700


class CloudToolsTests(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory(); self.addCleanup(work.cleanup)
        self.root = Path(work.name); self.folder = self.root/'tools'
        self.addCleanup(patch.stopall)
        patch('comic_archive.cloud_tools.shutil.which', return_value=None).start()
        patch('comic_archive.cloud_tools.shutil.disk_usage', return_value=type('Space', (), {'free':2*1024**3})()).start()
        self.guard = patch('comic_archive.cloud_tools.NetworkGuard').start()
        self.download = patch('comic_archive.cloud_tools.httpx.stream').start()
        self.build(('linux', 'amd64'))

    def build(self, build):
        """The release of the helper for this kind of computer, as a stand-in for its download."""
        patch('comic_archive.cloud_tools.location', return_value=(self.folder, build)).start()
        data = io.BytesIO()
        with zipfile.ZipFile(data, 'w') as archive:
            archive.writestr(f'rclone-v{cloud_tools.VERSION}-{build[0]}-{build[1]}/{cloud_tools.program(build)}', b'VERIFIED_HELPER')
            archive.writestr('../outside', b'NOT_EXTRACTED')
        self.data = data.getvalue()
        patch.dict(cloud_tools.DIGESTS, {build:hashlib.sha256(self.data).hexdigest()}).start()
        response = MagicMock(); response.iter_bytes.return_value = [self.data]
        self.download.return_value.__enter__.return_value = response

    def test_signin_installs_only_verified_fixed_binary_without_admin_or_shell(self):
        binary = Path(cloud_tools.ensure(self.root, threading.Event()))
        self.assertEqual(binary.read_bytes(), b'VERIFIED_HELPER')
        self.assertEqual(binary.stat().st_mode&0o777, PRIVATE_PROGRAM)
        self.assertFalse((self.root/'outside').exists())
        self.assertFalse(list(self.folder.glob('install-*')))
        self.assertTrue(self.download.call_args.args[1].startswith('https://github.com/rclone/rclone/releases/download/'))
        self.download.reset_mock()
        self.assertEqual(cloud_tools.ensure(self.root, threading.Event()), str(binary))
        self.download.assert_not_called()

    def test_windows_gets_the_windows_helper_from_its_own_release(self):
        self.build(('windows', 'amd64'))
        binary = Path(cloud_tools.ensure(self.root, threading.Event()))
        self.assertEqual((binary.name, binary.read_bytes()), ('rclone.exe', b'VERIFIED_HELPER'))
        self.assertTrue(self.download.call_args.args[1].endswith(f'/rclone-v{cloud_tools.VERSION}-windows-amd64.zip'))

    def test_bad_download_cancellation_and_metered_network_do_not_publish_executable(self):
        patch.dict(cloud_tools.DIGESTS, {('linux', 'amd64'):'0'*64}).start()
        with self.assertRaises(ArchiveError): cloud_tools.ensure(self.root, threading.Event())
        self.assertFalse((self.folder/'rclone').exists())
        cancelled = threading.Event(); cancelled.set()
        with self.assertRaises(ArchiveError): cloud_tools.ensure(self.root, cancelled)
        self.assertFalse((self.folder/'rclone').exists())
        self.guard.return_value.check.side_effect = NetworkPolicyHold('Held')
        self.download.reset_mock()
        with self.assertRaises(NetworkPolicyHold): cloud_tools.ensure(self.root, threading.Event())
        self.download.assert_not_called()

    def test_symlinked_install_destination_is_refused(self):
        outside = self.root/'outside'; outside.mkdir(); self.folder.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ArchiveError): cloud_tools.ensure(self.root, threading.Event())
        self.download.assert_not_called()
        self.assertFalse(list(outside.iterdir()))

    def assert_nothing_installed(self):
        self.assertFalse((self.folder/'rclone').exists()); self.assertFalse(list(self.folder.glob('install-*')))

    def test_low_free_space_stops_before_anything_is_downloaded(self):
        patch('comic_archive.cloud_tools.shutil.disk_usage', return_value=type('Space', (), {'free':1024**3-1})()).start()
        with self.assertRaisesRegex(ArchiveError, 'Free some device space'): cloud_tools.ensure(self.root, threading.Event())
        self.download.assert_not_called(); self.assert_nothing_installed()

    def test_oversized_download_is_rejected_before_it_is_verified_or_installed(self):
        response = MagicMock(); response.iter_bytes.return_value = [b'\0'*(65*1024**2)]
        self.download.return_value.__enter__.return_value = response
        with self.assertRaisesRegex(ArchiveError, 'too large'): cloud_tools.ensure(self.root, threading.Event())
        self.assert_nothing_installed()


class CloudToolsLocationTests(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory(); self.addCleanup(work.cleanup)
        self.home = Path(work.name); self.addCleanup(patch.stopall)
        patch('comic_archive.cloud_tools.Path.home', return_value=self.home).start()
        patch.dict(os.environ, {'LOCALAPPDATA': str(self.home / 'Local')}).start()

    def platform(self, system, machine):
        patch('comic_archive.cloud_tools.platform.system', return_value=system).start()
        patch('comic_archive.cloud_tools.platform.machine', return_value=machine).start()

    def test_only_supported_machines_get_a_pinned_helper_folder(self):
        local = self.home/'Local'
        for system, machine, build, folder in (
                ('Linux', 'x86_64', ('linux', 'amd64'), self.home/'.local/share/comic-archive/tools'),
                ('Linux', 'aarch64', ('linux', 'arm64'), self.home/'.local/share/comic-archive/tools'),
                ('Windows', 'AMD64', ('windows', 'amd64'), local/'comic-archiver/tools'),
                ('Windows', 'ARM64', ('windows', 'arm64'), local/'comic-archiver/tools')):
            with self.subTest(system=system, machine=machine), patch.dict(os.environ, {'LOCALAPPDATA': str(local)}):
                self.platform(system, machine)
                self.assertEqual(cloud_tools.location(), (folder/cloud_tools.VERSION/'-'.join(build), build))
                self.assertTrue(cloud_tools.offered())
        # Without its own variable, Windows keeps a person's local files in their profile's AppData.
        with patch.dict(os.environ, {'LOCALAPPDATA': ''}):
            self.assertEqual(cloud_tools.location()[0].parent.parent, self.home/'AppData/Local/comic-archiver/tools')
        for system, machine in (('Windows', 'x86'), ('Darwin', 'arm64'), ('Linux', 'riscv64')):
            with self.subTest(system=system, machine=machine):
                self.platform(system, machine)
                with self.assertRaises(ArchiveError): cloud_tools.location()
                # The pages hide cloud saving where it could not be set up.
                self.assertFalse(cloud_tools.offered())

    def test_installed_is_none_for_unsupported_devices_and_missing_helpers(self):
        self.platform('Windows', 'x86')
        self.assertIsNone(cloud_tools.installed())
        for system, machine, name in (('Linux', 'x86_64', 'rclone'), ('Windows', 'AMD64', 'rclone.exe')):
            with self.subTest(system):
                self.platform(system, machine)
                self.assertIsNone(cloud_tools.installed())
                folder, _ = cloud_tools.location(); folder.mkdir(parents=True)
                binary = folder/name; binary.write_bytes(b'x'); binary.chmod(0o700)
                self.assertEqual(cloud_tools.installed(), str(binary))

    @unittest.skipIf(os.name == 'nt', 'Windows has no permission to run a file apart from reading it.')
    def test_a_helper_that_may_not_be_run_is_not_installed(self):
        self.platform('Linux', 'x86_64')
        folder, _ = cloud_tools.location(); folder.mkdir(parents=True)
        binary = folder/'rclone'; binary.write_bytes(b'x'); binary.chmod(0o600)
        self.assertIsNone(cloud_tools.installed())
