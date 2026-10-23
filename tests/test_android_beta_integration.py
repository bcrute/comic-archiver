"""Host proof checks for the concrete native journey; mocks do not prove the journey ran."""
import copy
import json
import unittest

from scripts import android_beta_integration as gate


class BetaJourneyEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.token = 'a' * 32
        self.ready = {'run_token': self.token, 'phase': 'copy_blocked', 'native_pause_verified': True,
                      'native_recreation_verified': True, 'comic_and_novel_verified': True,
                      'native_fast_verified': True, 'native_discovery_verified': True,
                      'expected_cbz_bytes': 2000000, 'partial_bytes': 8192, 'document_id': 'root/comic/one.cbz',
                      'originals': [{'name': 'Library/comic/one.cbz', 'bytes': 2000000, 'sha256': 'b' * 64},
                                    {'name': 'Library/novel/one.epub', 'bytes': 2000, 'sha256': 'c' * 64}]}
        self.provider = {'blocked': True, 'partial_bytes': 8192,
                         'files': [{'id': 'root/comic/one.cbz', 'bytes': 8192, 'sha256': 'd' * 64}]}
        self.journal = ('comicarchiver-export-journal v2\n9001|1001|CBZ|one.cbz|one.cbz|2000000|' + 'b' * 64 +
                        '|COPYING|content://io.github.bcrute.comicarchiver.test.documents/tree/root/'
                        'document/root%2Fcomic%2Fone.cbz|1||' + gate.TREE + '\n')

    def valid(self, value=None, provider=None, journal=None, pid='123', running=True):
        return gate.valid_ready(self.ready if value is None else value,
            self.provider if provider is None else provider, self.journal if journal is None else journal,
            self.token, pid, running)

    def test_only_live_matching_partial_and_durable_copy_state_can_authorize_the_host_stop(self):
        self.assertTrue(self.valid())
        self.assertFalse(self.valid(pid=''))
        self.assertFalse(self.valid(running=False))
        self.assertFalse(self.valid(journal=self.journal.replace('COPYING', 'VERIFIED')))
        self.assertFalse(self.valid(journal=self.journal.replace('test.documents', 'archives')))
        self.assertFalse(self.valid(journal=self.journal.replace('2000000', '2000001')))
        self.assertFalse(self.valid(journal=self.journal.replace('|'+gate.TREE, '|content://other/tree/root')))

    def test_stale_token_missing_step_or_complete_file_is_not_a_ready_copy(self):
        for key, replacement in [('run_token', 'e' * 32), ('native_pause_verified', False),
                                 ('native_recreation_verified', None), ('comic_and_novel_verified', False),
                                 ('native_fast_verified', False), ('native_discovery_verified', None),
                                 ('partial_bytes', 0), ('partial_bytes', 2000000)]:
            with self.subTest(key=key, replacement=replacement):
                value = copy.deepcopy(self.ready)
                value[key] = replacement
                self.assertFalse(self.valid(value))
        provider = copy.deepcopy(self.provider)
        provider['blocked'] = False
        self.assertFalse(self.valid(provider=provider))
        for field, invalid in [('blocked', 'true'), ('partial_bytes', True), ('files', [None]),
                               ('files', None), ('files', self.provider['files'] * 2)]:
            provider = dict(self.provider, **{field: invalid})
            self.assertFalse(self.valid(provider=provider))
        provider = copy.deepcopy(self.provider)
        provider['files'][0]['bytes'] = 9000
        self.assertFalse(self.valid(provider=provider))

    def test_missing_novel_or_an_unsafe_original_path_cannot_qualify_the_beta(self):
        value = copy.deepcopy(self.ready)
        value['originals'].pop()
        self.assertFalse(self.valid(value))
        for path in ['/private/file.epub', 'Library/../session/file.epub', 'Library/comic/file.cbz']:
            value = copy.deepcopy(self.ready)
            value['originals'][1]['name'] = path
            self.assertFalse(self.valid(value))

    def test_provider_bundle_is_parsed_without_treating_missing_fields_as_success(self):
        text = 'Result: Bundle[{blocked=true, partial_bytes=8192, files=' + json.dumps(self.provider['files']) + '}]'
        self.assertEqual(gate.provider_status(text), self.provider)
        for invalid in ('', 'Error: provider unavailable', 'Bundle[{blocked=false}]'):
            with self.assertRaises(ValueError):
                gate.provider_status(invalid)

    @staticmethod
    def instrumentation(code=0, name='recoverAfterHostStopWithoutGo'):
        return (f'INSTRUMENTATION_STATUS: class={gate.TEST_CLASS}\n'
                f'INSTRUMENTATION_STATUS: test={name}\nINSTRUMENTATION_STATUS: numtests=1\n'
                f'INSTRUMENTATION_STATUS_CODE: {code}\nINSTRUMENTATION_CODE: -1\n')

    def test_recovery_requires_exact_passing_instrumentation_not_a_skipped_or_crashed_run(self):
        good = self.instrumentation()
        self.assertTrue(gate.recovery_test_passed(good))
        for bad in (self.instrumentation(-2), self.instrumentation(-3),
                    self.instrumentation(name='anotherTest'), '', good.replace('numtests=1', 'numtests=0'),
                    good.replace('INSTRUMENTATION_CODE: -1', ''), good + 'FAILURES!!!',
                    good + 'INSTRUMENTATION_CODE: 0\n'):
            with self.subTest(output=bad):
                self.assertFalse(gate.recovery_test_passed(bad))

    def test_recovered_bytes_held_restart_and_same_document_are_all_required(self):
        value = {'run_token': self.token, 'phase': 'recovered', 'verified_exports': 2,
                 'held_without_go': True, 'originals_unchanged': True,
                 'same_partial_document_recovered': True, 'comic_and_novel_verified': True,
                 'working_library_adopted': True, 'library_binding_preserved': True,
                 'library_originals_preserved': True, 'library_switch_held': True, 'managed_archive_grants': True,
                 'adopted_library_id': '11111111-1111-4111-8111-111111111111',
                 'adopted_archives': copy.deepcopy(self.ready['originals'])}
        provider = {'blocked': False, 'files': [
            {'id': 'root/comic/one.cbz', 'bytes': 2000000, 'sha256': 'b' * 64},
            {'id': 'root/novel/one.epub', 'bytes': 2000, 'sha256': 'c' * 64}]}
        self.assertTrue(gate.valid_recovery(value, self.ready, provider, self.token))
        for key in ('held_without_go', 'originals_unchanged', 'same_partial_document_recovered', 'comic_and_novel_verified',
                    'working_library_adopted', 'library_binding_preserved', 'library_originals_preserved', 'library_switch_held',
                    'managed_archive_grants'):
            changed = dict(value, **{key: False})
            self.assertFalse(gate.valid_recovery(changed, self.ready, provider, self.token))
        for changed in (dict(value, adopted_library_id='../PRIVATE'), dict(value, adopted_archives=[]),
                        dict(value, adopted_archives=[None, None])):
            self.assertFalse(gate.valid_recovery(changed, self.ready, provider, self.token))
        broken = copy.deepcopy(provider)
        broken['files'][0]['sha256'] = '0' * 64
        self.assertFalse(gate.valid_recovery(value, self.ready, broken, self.token))
        broken = copy.deepcopy(provider)
        broken['files'][0]['id'] = 'root/comic/duplicate.cbz'
        self.assertFalse(gate.valid_recovery(value, self.ready, broken, self.token))
        for bad in ({'files': provider['files']}, dict(provider, blocked='false'),
                    dict(provider, files=[None, None]), dict(provider, files=provider['files'][:1] * 2)):
            self.assertFalse(gate.valid_recovery(value, self.ready, bad, self.token))
