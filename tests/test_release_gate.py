import copy
import json
from pathlib import Path
import tempfile
import unittest

from scripts import verify_release as release


class ReleaseGateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.commit = 'a' * 40
        self.version = '0.1.5'
        self.value = {'schema_version': 1, 'commit': self.commit, 'version': self.version,
                      'checks': dict.fromkeys(release.CHECKS, True),
                      'tests': {'tests_run': 1800, 'failures': 0, 'errors': 0, 'skipped': []}, 'artifacts': {}}
        for suffix in ('linux-x86_64.tar.gz', 'windows-x86_64.zip', 'windows-x86_64-setup.exe'):
            path = self.folder / f'ComicArchiver-{self.version}-{suffix}'
            path.write_bytes(b'synthetic download ' + suffix.encode())
            self.value['artifacts'][path.name] = {'size': path.stat().st_size, 'sha256': release.digest(path)}
        self.save()

    def save(self):
        path = self.folder / 'release-evidence.json'
        path.write_text(json.dumps(self.value), encoding='utf-8')
        self.statuses = {'sha': self.commit, 'statuses': [{'context': release.CONTEXT, 'state': 'success',
                         'creator': {'login': 'owner'}, 'description': 'Fleet evidence SHA256 ' + release.digest(path)}]}

    def verify(self, statuses=None):
        return release.verify(self.folder, self.commit, statuses or self.statuses, 'owner', self.version)

    def test_accepts_owner_attested_exact_commit_and_all_downloads(self):
        self.assertEqual(self.verify(), self.value)

    def test_refuses_untrusted_or_failed_status_and_another_commit(self):
        for field, value in (('creator', {'login': 'someone-else'}), ('state', 'pending'),
                             ('description', 'unchecked'), ('context', 'other check')):
            with self.subTest(field=field):
                statuses = copy.deepcopy(self.statuses)
                statuses['statuses'][0][field] = value
                with self.assertRaises(ValueError):
                    self.verify(statuses)
        statuses = self.statuses | {'sha': 'b' * 40}
        with self.assertRaises(ValueError):
            self.verify(statuses)

    def test_refuses_missing_gates_skips_missing_downloads_and_wrong_version(self):
        original = copy.deepcopy(self.value)
        for field, value in (('commit', 'b' * 40), ('version', '0.1.4'), ('checks', {'validation': True}),
                             ('tests', original['tests'] | {'skipped': ['required check']}), ('artifacts', {})):
            with self.subTest(field=field):
                self.value = original | {field: value}
                self.save()
                with self.assertRaises(ValueError):
                    self.verify()

    def test_refuses_a_changed_download_or_evidence_changed_after_attestation(self):
        name = next(iter(self.value['artifacts']))
        (self.folder / name).write_bytes(b'tampered download')
        with self.assertRaises(ValueError):
            self.verify()
        self.value['artifacts'][name] = {'size': 17, 'sha256': release.digest(self.folder / name)}
        (self.folder / 'release-evidence.json').write_text(json.dumps(self.value), encoding='utf-8')
        with self.assertRaises(ValueError):
            self.verify()
