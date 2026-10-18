import copy
import json
from pathlib import Path
import re
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
        self.release = {'target_commitish': self.commit, 'tag_name': 'v' + self.version, 'draft': True,
                        'author': {'login': 'owner'}, 'assets': [{'name': path.name, 'state': 'uploaded',
                        'uploader': {'login': 'owner'}, 'size': path.stat().st_size,
                        'digest': 'sha256:' + release.digest(path)}]}

    def verify(self, metadata=None):
        return release.verify(self.folder, self.commit, self.release if metadata is None else metadata, 'owner', self.version)

    def test_release_workflow_pins_every_action_to_an_exact_commit(self):
        workflow = (Path(__file__).parents[1] / '.github/workflows/release.yml').read_text(encoding='utf-8')
        actions = re.findall(r'^\s*-\s+uses:\s+([^\s#]+)', workflow, re.M)
        self.assertTrue(actions)
        for action in actions:
            self.assertRegex(action, r'^[^@]+@[0-9a-f]{40}$')

    def test_accepts_owner_attested_exact_commit_and_all_downloads(self):
        self.assertEqual(self.verify(), self.value)

    def test_refuses_untrusted_or_incomplete_uploads_and_another_commit(self):
        for field, value in (('author', {'login': 'someone-else'}), ('author', None), ('draft', False),
                             ('target_commitish', 'b' * 40), ('tag_name', 'v0.1.4'),
                             ('assets', []), ('assets', None), ('assets', self.release['assets'] * 2)):
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    self.verify(self.release | {field: value})
        for field, value in (('uploader', {'login': 'someone-else'}), ('uploader', None),
                             ('state', 'starter'), ('size', 0), ('digest', None),
                             ('digest', 'sha256:' + '0' * 64), ('name', 'other.json')):
            with self.subTest(asset_field=field):
                metadata = copy.deepcopy(self.release)
                metadata['assets'][0][field] = value
                with self.assertRaises(ValueError):
                    self.verify(metadata)

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
