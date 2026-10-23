"""Native preparation capability; never a fast-dispatch operation or Go authority."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

from comic_archive.working_library import LOCATION_MARKER, RECEIPT
from test_app_bridge import app_bridge


class Control:
    def __init__(self, stop_after=None):
        self.calls = 0
        self.stop_after = stop_after

    def isCancelled(self):
        self.calls += 1
        return self.stop_after is not None and self.calls > self.stop_after


class AndroidWorkingLibraryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.owner = Path(temporary.name)
        self.ticket = str(uuid.uuid4())
        self.stage = self.owner / 'working-libraries/.staging' / self.ticket
        self.captured = self.stage / 'capture/workspace'
        self.payload = self.stage / 'payload'
        (self.captured / 'Library').mkdir(parents=True)
        self.payload.mkdir()
        self.source_key = 'a' * 64
        self.before = dict(app_bridge._workspace)
        self.addCleanup(lambda: app_bridge._workspace.update(self.before))
        app_bridge._workspace.update(root=self.owner / 'workspace', base=self.owner / 'workspace', site_key='tapas')

    def call(self, *, control=None, recover=False, captured=None, payload=None, ticket=None, source_key=None):
        return json.loads(app_bridge.working_library_prepare(str(captured or self.captured), str(payload or self.payload),
            ticket or self.ticket, source_key or self.source_key, control or Control(), recover))

    def test_private_native_preparation_creates_a_held_library_without_changing_active_context(self):
        active = dict(app_bridge._workspace); seams = dict(app_bridge._seams); cwd = Path.cwd()
        result = self.call()
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['result']['sites'], [{'key': 'tapas', 'series': 0, 'episodes': 0,
                                                     'cloud_only': 0, 'account_bound': False}])
        self.assertTrue((self.payload / 'Library/.archive-state/inventory.sqlite3').is_file())
        self.assertFalse((self.payload / '.comic-session').exists())
        self.assertEqual(app_bridge._workspace, active)
        self.assertEqual(app_bridge._seams, seams)
        self.assertEqual(Path.cwd(), cwd)
        self.assertEqual(list((self.captured / 'Library').iterdir()), [])

    def test_long_native_entry_is_unavailable_through_json_dispatch(self):
        result = json.loads(app_bridge.dispatch(json.dumps({'v': 1, 'op': 'working_library_prepare', 'args': {
            'captured': str(self.captured), 'payload': str(self.payload)}})))
        self.assertEqual(result['code'], 'unknown_op')
        self.assertFalse((self.payload / RECEIPT).exists())

    def test_cancel_before_or_during_verification_never_returns_a_preparation_proof(self):
        for limit in (0, 1):
            with self.subTest(limit=limit):
                result = self.call(control=Control(stop_after=limit))
                self.assertFalse(result['ok'])
                self.assertEqual(result['code'], 'library_import_cancelled')
                self.assertFalse((self.payload / RECEIPT).exists())

    def test_cold_recovery_requires_the_same_proof_and_native_marker_authority(self):
        self.assertIsNone(self.call(recover=True)['result'])
        prepared = self.call()
        self.assertTrue(prepared['ok'], prepared)
        self.assertEqual(self.call(recover=True), prepared)
        (self.payload / LOCATION_MARKER).write_text(json.dumps({'v': 1, 'id': self.ticket,
            'source': self.source_key, 'digest': prepared['result']['digest']}))
        self.assertEqual(self.call(recover=True), prepared)
        wrong = self.call(recover=True, source_key='b' * 64)
        self.assertFalse(wrong['ok'])
        self.assertEqual(self.call(recover=True), prepared)

    def test_ready_pending_recovery_also_checks_the_payload_renamed_before_registry_commit(self):
        prepared = self.call()
        self.assertTrue(prepared['ok'])
        (self.payload / LOCATION_MARKER).write_text(json.dumps({'v': 1, 'id': self.ticket,
            'source': self.source_key, 'digest': prepared['result']['digest']}))
        renamed = self.stage.parent.parent / self.ticket
        self.payload.rename(renamed)
        self.assertEqual(self.call(payload=renamed, recover=True), prepared)
        self.assertFalse(self.call(payload=renamed)['ok'])
        app_bridge._workspace['root'] = renamed
        self.assertEqual(self.call(payload=renamed, recover=True)['code'], 'refused')

    def test_unreserved_paths_tickets_and_active_library_ancestors_cannot_be_prepared(self):
        for args in ({'ticket': '../PRIVATE'}, {'captured': self.owner / 'PRIVATE'},
                     {'payload': self.owner / 'workspace'}, {'source_key': 'PRIVATE'}):
            with self.subTest(args=args):
                result = self.call(**args)
                self.assertFalse(result['ok'])
                self.assertNotIn('PRIVATE', result['message'])
        for active in (self.stage, self.stage / 'payload/Library', self.owner):
            with self.subTest(active=active):
                app_bridge._workspace['root'] = active
                result = self.call()
                self.assertEqual(result['code'], 'refused')
                self.assertFalse((self.payload / RECEIPT).exists())

    def test_incomplete_payload_recovery_does_not_silently_delete_a_failed_copy(self):
        partial = self.payload / 'PRIVATE'; partial.write_text('retained')
        recovery = self.call(recover=True)
        self.assertTrue(recovery['ok']); self.assertIsNone(recovery['result'])
        result = self.call()
        self.assertEqual(result['code'], 'library_import_destination')
        self.assertEqual(partial.read_text(), 'retained')

    def test_verifier_refusals_contain_no_source_filename_or_trace(self):
        (self.captured / 'Library/PRIVATE_MISSING_METADATA').mkdir()
        result = self.call()
        self.assertFalse(result['ok'])
        self.assertEqual(result['code'], 'library_import_incomplete')
        self.assertNotIn('PRIVATE', result['message'])
        self.assertNotIn('Traceback', result['message'])

    def test_unexpected_native_control_errors_remain_a_fixed_failure_envelope(self):
        control = Control()
        with patch.object(control, 'isCancelled', side_effect=RuntimeError('PRIVATE native callback')):
            result = self.call(control=control)
        self.assertFalse(result['ok'])
        self.assertEqual(result['code'], 'library_import_invalid')
        self.assertNotIn('PRIVATE', result['message'])
