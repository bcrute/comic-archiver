import contextlib
import ctypes
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from comic_archive import windows_privacy as privacy
from comic_archive.session import load_session, store_session


class PrivacyTests(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory(); self.addCleanup(work.cleanup)
        self.root = Path(work.name).resolve()
        privacy._protected.clear()
        self.addCleanup(privacy._protected.clear)

    def test_unix_never_loads_windows_apis_or_changes_paths(self):
        with patch.object(privacy, 'WINDOWS', False), patch.object(privacy, '_api') as api:
            privacy.folder(self.root / 'absent'); privacy.file(self.root / 'absent')
        api.assert_not_called()
        self.assertFalse((self.root / 'absent').exists())

    def test_existing_private_tree_is_repaired_once_but_replacement_is_checked_again(self):
        private = self.root / '.comic-session'; child = private / 'worker'; child.mkdir(parents=True)
        secret = child / 'state'; secret.write_bytes(b'dummy')
        archive = self.root / 'Library/Series/episode.cbz'; archive.parent.mkdir(parents=True); archive.write_bytes(b'archive')
        before = archive.stat()
        with patch.object(privacy, 'WINDOWS', True), patch.object(privacy, '_secure') as secure:
            privacy.folder(private); privacy.folder(private)
            self.assertEqual(secure.call_args_list, [unittest.mock.call(private, True), unittest.mock.call(child, True), unittest.mock.call(secret, False)])
            private.rename(self.root / 'old'); private.mkdir()
            privacy.folder(private)
            self.assertEqual(secure.call_count, 4)
        self.assertEqual((archive.read_bytes(), archive.stat().st_ino, archive.stat().st_mode), (b'archive', before.st_ino, before.st_mode))

    def test_partial_repair_is_not_cached_and_vanished_temporary_files_are_safe(self):
        private = self.root / '.comic-session'; private.mkdir(); (private / 'temporary').touch()
        with patch.object(privacy, 'WINDOWS', True), patch.object(privacy, '_secure', side_effect=PermissionError('denied')):
            with self.assertRaises(privacy.PrivateStorageError): privacy.folder(private)
        self.assertFalse(privacy._protected)
        with patch.object(privacy, 'WINDOWS', True), patch.object(privacy, '_secure', side_effect=[None, FileNotFoundError('retired')]):
            privacy.folder(private)
        self.assertEqual(len(privacy._protected), 1)

    def test_refusal_prevents_session_reads_and_writes(self):
        folder = self.root / 'session'; folder.mkdir(); state = folder / 'state.json'; state.write_bytes(b'original')
        with patch.object(privacy, 'folder', side_effect=privacy.PrivateStorageError(privacy.MESSAGE)):
            with self.assertRaises(privacy.PrivateStorageError): load_session(folder)
            with self.assertRaises(privacy.PrivateStorageError): store_session({'cookies': [], 'origins': []}, folder)
        self.assertEqual(state.read_bytes(), b'original')
        self.assertFalse((folder / 'state.tmp').exists())

    def test_each_private_folder_is_protected_before_it_is_returned(self):
        from comic_archive import state_paths
        with patch.object(privacy, 'folder', side_effect=privacy.PrivateStorageError(privacy.MESSAGE)) as protect:
            with self.assertRaises(privacy.PrivateStorageError): state_paths.private_folder(self.root, 'web')
        protect.assert_called_once_with(self.root / '.comic-web')

    def test_cloud_config_is_protected_before_it_is_returned_to_the_helper(self):
        from comic_archive import cloud
        path = self.root / 'config'; path.write_bytes(b'dummy credential')
        with patch.object(privacy, 'file', side_effect=privacy.PrivateStorageError(privacy.MESSAGE)) as protect:
            with self.assertRaises(privacy.PrivateStorageError): cloud._config(path)
        protect.assert_called_once_with(path)
        self.assertEqual(path.read_bytes(), b'dummy credential')

    def native_api(self, *, attributes=0, links=1, flags=8, dacl=True, result=0):
        api = {key: Mock(return_value=1) for key in ('open', 'info', 'volume', 'dacl', 'set', 'close')}
        api['open'].return_value = 123
        def information(handle, output):
            value = ctypes.cast(output, ctypes.POINTER(privacy.FileInformation)).contents
            value.attributes, value.links = attributes, links
            return 1
        def volume(handle, name, size, serial, maximum, output, fs, fs_size):
            ctypes.cast(output, ctypes.POINTER(privacy.DWORD)).contents.value = flags
            return 1
        def descriptor(value, present, output, defaulted):
            ctypes.cast(present, ctypes.POINTER(ctypes.c_int)).contents.value = bool(dacl)
            ctypes.cast(output, ctypes.POINTER(privacy.POINTER)).contents.value = 42 if dacl else None
            return 1
        api['info'].side_effect = information; api['volume'].side_effect = volume
        api['dacl'].side_effect = descriptor; api['set'].return_value = result
        return api

    def secure_with(self, api, directory=False):
        with patch.object(privacy, '_api', return_value=api), patch.object(privacy, '_descriptor', return_value=contextlib.nullcontext(privacy.POINTER(51))):
            privacy._secure(self.root / 'fixture', directory)

    def test_native_acl_replaces_inheritance_and_closes_the_exact_opened_entry(self):
        api = self.native_api(attributes=0x10)
        self.secure_with(api, True)
        api['open'].assert_called_once()
        self.assertEqual(api['open'].call_args.args[1:6], (0x60000, 7, None, 3, 0x02200000))
        self.assertEqual(api['set'].call_args.args[:3], (123, 1, 0x80000004))
        self.assertEqual(api['set'].call_args.args[5].value, 42)
        api['close'].assert_called_once_with(123)

    def test_no_acl_volume_reparse_point_hard_link_wrong_type_and_null_dacl_are_refused(self):
        for settings in ({'flags': 0}, {'attributes': 0x400}, {'links': 2}, {'attributes': 0x10}, {'dacl': False}):
            with self.subTest(settings=settings):
                api = self.native_api(**settings)
                with self.assertRaises(privacy.PrivateStorageError): self.secure_with(api)
                api['set'].assert_not_called(); api['close'].assert_called_once_with(123)

    def test_native_failure_fails_closed_and_preserves_handle_cleanup(self):
        api = self.native_api(result=5)
        with patch.object(ctypes, 'WinError', side_effect=lambda code: OSError(code, 'denied'), create=True):
            with self.assertRaises(OSError): self.secure_with(api)
        api['close'].assert_called_once_with(123)
        with patch.object(privacy, 'WINDOWS', True), patch.object(privacy, '_secure', side_effect=PermissionError(13, 'private path')), self.assertLogs('comic_archive.desktop', level='WARNING') as logs:
            with self.assertRaisesRegex(privacy.PrivateStorageError, 'Windows access permissions') as caught: privacy.file(self.root / 'secret')
        self.assertIn('operation=file error=PermissionError code=13', str(logs.output))
        self.assertNotIn('private path', str(logs.output))
        self.assertNotIn('private path', str(caught.exception))

    def test_real_windows_restricts_copied_session_and_nested_profile_without_changing_archives(self):
        if os.name != 'nt':
            with patch.object(privacy, '_secure') as secure:
                privacy.folder(self.root)
            secure.assert_not_called()
            return
        session = self.root / '.comic-session'; session.mkdir()
        profile = session / 'worker'; profile.mkdir(); existing = profile / 'old-cookie'; existing.write_bytes(b'dummy cookie')
        archive = self.root / 'Library/Series/episode.cbz'; archive.parent.mkdir(parents=True); archive.write_bytes(b'archive')
        subprocess.run(['icacls', str(session), '/grant', '*S-1-1-0:(OI)(CI)R', '/T', '/Q'], check=True, capture_output=True)
        def acl(path):
            literal = str(path).replace("'", "''")
            command = f"$ErrorActionPreference='Stop'; $a=Get-Acl -LiteralPath '{literal}'; @{{sddl=$a.Sddl; principals=@($a.Access | ForEach-Object {{$_.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value}})}} | ConvertTo-Json -Compress"
            done = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', command], check=True, capture_output=True, text=True)
            return json.loads(done.stdout)
        self.assertIn('WD', acl(existing)['sddl'])  # Everyone was explicitly granted access in the copied fixture.
        before = (archive.read_bytes(), archive.stat().st_ino, acl(archive))
        store_session({'cookies': [], 'origins': []}, session)
        sid = privacy._user_sid()
        for path in (session, profile, existing, session / 'state.json', session / 'site'):
            rules = acl(path); sddl = rules['sddl']
            if path in (session, profile, existing):
                self.assertIn('D:P', sddl, sddl)
            self.assertIn(sid, rules['principals'], rules)
            self.assertLessEqual(set(rules['principals']), {sid, 'S-1-5-18', 'S-1-5-32-544'})
            for principal in ('WD', 'BU', 'AU'):
                self.assertNotIn(';;;' + principal + ')', sddl, sddl)
        self.assertEqual(load_session(session), {'cookies': [], 'origins': []})
        self.assertEqual(existing.read_bytes(), b'dummy cookie')
        self.assertEqual((archive.read_bytes(), archive.stat().st_ino, acl(archive)), before)
        # A new file must already inherit restricted access before any credential is written to it.
        fresh = profile / 'new-cookie'; fresh.touch()
        self.assertNotIn(';;;WD)', acl(fresh)['sddl'])
        self.assertEqual(json.loads((session / 'state.json').read_text()), {'cookies': [], 'origins': []})
