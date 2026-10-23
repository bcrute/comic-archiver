"""Native connectivity facts expire; a stopped writer cannot leave transfers admitted."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from comic_archive import network_android


class FreshNetworkFactsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / 'net.json'
        network_android.configure(None)
        self.addCleanup(network_android.configure, None)
        network_android.configure(self.path, require_fresh=True)

    def write(self, **changes):
        report = {'schema_version': 1, 'observed_at_ms': 100_000,
                  'connection': 'wifi', 'metered': False}
        self.path.write_text(json.dumps(report | changes))

    @patch('comic_archive.network_android.time.time', return_value=100.0)
    def test_only_a_fresh_complete_native_report_can_admit_a_connection(self, clock):
        self.write()
        self.assertEqual(network_android.probe(), {'connection': 'wifi', 'metered': False})
        self.write(connection='cellular', metered=True, observed_at_ms=70_000)
        self.assertEqual(network_android.probe(), {'connection': 'cellular', 'metered': True})
        for fields in ({'observed_at_ms': 69_999}, {'observed_at_ms': 100_001},
                       {'observed_at_ms': True}, {'observed_at_ms': '100000'},
                       {'schema_version': True}, {'schema_version': 2},
                       {'metered': 'false'}, {'connection': 'other'}):
            with self.subTest(fields=fields):
                self.write(**fields)
                self.assertEqual(network_android.probe(), network_android.UNKNOWN)

    @patch('comic_archive.network_android.time.time', return_value=100.0)
    def test_worker_reconfiguration_cannot_downgrade_the_same_native_path(self, clock):
        self.path.write_text('{"connection":"wifi","metered":false}')
        network_android.configure(self.path)
        self.assertEqual(network_android.probe(), network_android.UNKNOWN)
        self.write()
        self.assertEqual(network_android.probe()['connection'], 'wifi')
        clock.return_value = 131.0
        self.assertEqual(network_android.probe(), network_android.UNKNOWN)

    @patch('comic_archive.network_android.time.time', return_value=100.0)
    def test_legacy_callers_remain_compatible_but_partial_new_reports_are_refused(self, clock):
        network_android.configure(None)
        network_android.configure(self.path)
        self.path.write_text('{"connection":"wifi","metered":false}')
        self.assertEqual(network_android.probe()['connection'], 'wifi')
        for extra in ({'schema_version': 1}, {'observed_at_ms': 100_000}):
            self.path.write_text(json.dumps({'connection': 'wifi', 'metered': False} | extra))
            self.assertEqual(network_android.probe(), network_android.UNKNOWN)

    def test_a_symlink_or_failed_writer_is_never_a_network_permission(self):
        other = self.path.with_name('old.json')
        other.write_text('{"connection":"wifi","metered":false}')
        self.path.symlink_to(other)
        self.assertEqual(network_android.probe(), network_android.UNKNOWN)
        self.path.unlink()
        self.path.write_text('partial write')
        self.assertEqual(network_android.probe(), network_android.UNKNOWN)
