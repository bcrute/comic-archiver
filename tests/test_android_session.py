"""The Android sign-in record against a plain temporary folder: no device, no cookies."""
import json
import tempfile
import unittest
from pathlib import Path

from support import PRIVATE_FILE, PRIVATE_FOLDER
from comic_archive import android_session, sites
from comic_archive.models import ArchiveError
from comic_archive.session import load_session


class AndroidSessionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name) / 'session'

    def test_a_complete_record_names_the_site_and_writes_no_cookie_data(self):
        webtoon = sites.find('webtoon')
        android_session.mark_signed_in(self.directory, webtoon)
        self.assertTrue(android_session.is_signed_in(self.directory))
        self.assertIs(android_session.signed_in_site(self.directory), webtoon)
        # The existing core reads the same record, unchanged.
        self.assertIs(sites.of_session(self.directory), webtoon)
        self.assertEqual({path.name for path in self.directory.iterdir()}, {'site', 'android.json'})
        marker = json.loads((self.directory / 'android.json').read_text())
        self.assertEqual(marker, {'schema_version': 1, 'platform': 'android', 'cookies': 'webview-store'})
        # The whole record is the site's key and the fixed marker: no cookie name or value anywhere.
        self.assertEqual((self.directory / 'site').read_text(), 'webtoon')

    def test_the_compatibility_state_is_empty_and_readable_by_the_existing_core(self):
        android_session.mark_signed_in(self.directory, sites.DEFAULT, compatibility_state=True)
        state = load_session(self.directory)  # background.start validates a saved session this way.
        self.assertEqual(state['cookies'], [])
        self.assertEqual(state['origins'], [])

    def test_no_record_or_a_torn_or_foreign_record_is_not_a_sign_in(self):
        self.assertFalse(android_session.is_signed_in(self.directory))
        self.assertIsNone(android_session.signed_in_site(self.directory))
        android_session.mark_signed_in(self.directory, sites.DEFAULT)
        (self.directory / 'site').unlink()
        self.assertFalse(android_session.is_signed_in(self.directory))
        with self.assertRaises(ArchiveError):
            android_session.signed_in_site(self.directory)
        android_session.mark_signed_in(self.directory, sites.DEFAULT)
        (self.directory / 'site').write_text('nosuch')
        with self.assertRaises(ArchiveError):
            android_session.signed_in_site(self.directory)

    def test_a_changed_marker_shape_is_refused_rather_than_reinterpreted(self):
        android_session.mark_signed_in(self.directory, sites.DEFAULT)
        for value in ('{"schema_version": 2}', 'not json',
                      '{"schema_version": 1, "platform": "android", "cookies": ["sid=PRIVATE"]}'):
            (self.directory / 'android.json').write_text(value)
            with self.assertRaises(ArchiveError) as error:
                android_session.is_signed_in(self.directory)
            self.assertNotIn('PRIVATE', str(error.exception))

    def test_records_are_private_and_symlinks_are_refused(self):
        android_session.mark_signed_in(self.directory, sites.DEFAULT)
        self.assertEqual(self.directory.stat().st_mode & 0o777, PRIVATE_FOLDER)
        for name in ('site', 'android.json'):
            self.assertEqual((self.directory / name).stat().st_mode & 0o777, PRIVATE_FILE)
        elsewhere = self.directory.parent / 'elsewhere'; elsewhere.mkdir()
        for planted in ('site', 'android.json', 'state.json'):
            (self.directory / planted).unlink(missing_ok=True)
            (self.directory / planted).symlink_to(elsewhere / planted)
            with self.assertRaises(ArchiveError):
                android_session.mark_signed_in(self.directory, sites.DEFAULT)
            (self.directory / planted).unlink()
        linked = self.directory.parent / 'linked'
        linked.symlink_to(self.directory)
        try:
            with self.assertRaises(ArchiveError):
                android_session.mark_signed_in(linked, sites.DEFAULT)
        finally:
            linked.unlink()

    def test_forget_removes_every_record_and_tolerates_a_missing_folder(self):
        android_session.forget(self.directory)  # Nothing there yet: no error.
        android_session.mark_signed_in(self.directory, sites.DEFAULT, compatibility_state=True)
        android_session.forget(self.directory)
        self.assertEqual(list(self.directory.iterdir()), [])
        self.assertFalse(android_session.is_signed_in(self.directory))


if __name__ == '__main__':
    unittest.main()
