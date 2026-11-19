"""No-network default, explicit consent, stable comparisons and bounded metadata reads."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from comic_archive import updates


class UpdateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / 'updates.json'

    def test_default_and_saved_opt_out_never_request_anything(self):
        with patch.object(updates, 'fetch') as fetch:
            self.assertEqual(updates.check('0.5.4', self.path)['status'], 'disabled')
            updates.set_automatic(self.path, True)
            updates.set_automatic(self.path, False)
            self.assertEqual(updates.check('0.5.4', self.path)['status'], 'disabled')
            fetch.assert_not_called()
        self.assertFalse(updates.automatic(self.path))

    def test_manual_check_does_not_enable_automatic_checks(self):
        with patch.object(updates, 'fetch', return_value='0.5.10') as fetch:
            value = updates.check('0.5.9', self.path, True, now=100)
            self.assertEqual(value['status'], 'available')
            self.assertFalse(updates.automatic(self.path))
            self.assertEqual(updates.check('0.5.9', self.path, now=101)['status'], 'disabled')
            fetch.assert_called_once_with('desktop')

    def test_opt_in_survives_restart_and_checks_at_most_daily(self):
        updates.set_automatic(self.path, True)
        with patch.object(updates, 'fetch', return_value='0.5.4') as fetch:
            for now in (100, 101, 100 + updates.DAY - 1):
                self.assertEqual(updates.check('0.5.4', self.path, now=now)['status'], 'current')
            self.assertEqual(fetch.call_count, 1)
            updates.check('0.5.4', self.path, now=100 + updates.DAY)
            self.assertEqual(fetch.call_count, 2)
        self.assertTrue(updates.automatic(self.path))

    def test_offline_or_rate_limited_check_is_not_up_to_date_or_a_retry_loop(self):
        updates.set_automatic(self.path, True)
        with patch.object(updates, 'fetch', side_effect=OSError('offline')) as fetch:
            for now in (100, 101):
                self.assertEqual(updates.check('0.5.4', self.path, now=now)['status'], 'unavailable')
            fetch.assert_called_once()
            updates.check('0.5.4', self.path, True, now=102)
            self.assertEqual(fetch.call_count, 2)

    def test_unknown_current_version_does_not_claim_up_to_date(self):
        with patch.object(updates, 'fetch', return_value='0.5.4'):
            self.assertEqual(updates.check('unknown', self.path, True)['status'], 'unavailable')
            self.assertEqual(updates.check('0.5.4-beta1', self.path, True)['status'], 'available')
            self.assertEqual(updates.check('0.6.0', self.path, True)['status'], 'current')

    def test_invalid_cache_cannot_enable_requests_and_wrong_clock_expires_cache(self):
        for data in ('[]', '{bad', '{"automatic":"true"}'):
            self.path.write_text(data)
            with patch.object(updates, 'fetch') as fetch:
                self.assertEqual(updates.check('0.5.4', self.path)['status'], 'disabled')
                fetch.assert_not_called()
        self.path.write_text(json.dumps({'automatic': True, 'checked': 999, 'latest': '0.5.4'}))
        with patch.object(updates, 'fetch', return_value='0.5.5') as fetch:
            self.assertEqual(updates.check('0.5.4', self.path, now=100)['status'], 'available')
            fetch.assert_called_once()

    def test_platform_manifest_refuses_previews_and_unknown_schema(self):
        value = {'schema_version': 1, 'desktop': '0.5.4', 'android': '0.5.3'}
        self.assertEqual(updates.release_version(value, 'android'), '0.5.3')
        for invalid in ({}, dict(value, android='0.5.5-beta1'), dict(value, schema_version=2)):
            with self.assertRaises(ValueError):
                updates.release_version(invalid, 'android')

    def test_metadata_request_is_bounded_and_never_uses_an_account_or_installed_version(self):
        response = Mock()
        response.iter_bytes.return_value = [b'{"schema_version":1,"desktop":"0.5.5"}']
        manager = Mock(); manager.__enter__ = Mock(return_value=response); manager.__exit__ = Mock(return_value=False)
        with patch.object(updates.httpx, 'stream', return_value=manager) as opened:
            self.assertEqual(updates.fetch(), '0.5.5')
            self.assertEqual(opened.call_args.args, ('GET', 'https://comicarchiver.com/version.json'))
            self.assertNotIn('Authorization', opened.call_args.kwargs['headers'])
            self.assertNotIn('Cookie', opened.call_args.kwargs['headers'])
            self.assertFalse(opened.call_args.kwargs['follow_redirects'])
            response.iter_bytes.assert_called_once_with(chunk_size=8192)
            response.iter_bytes.return_value = [b' ' * (updates.MAX_BYTES + 1)]
            with self.assertRaises(ValueError):updates.fetch()
