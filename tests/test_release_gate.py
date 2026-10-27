import copy
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import textwrap
import tomllib
import unittest
from unittest.mock import patch

from scripts import android_release
from scripts import verify_release as release


class ReleaseGateTests(unittest.TestCase):
    VERSION = '0.1.5'

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.commit = 'a' * 40
        self.version = self.VERSION
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
        for name in self.value['artifacts']:
            self.release['assets'].append(self.asset(name))

    def asset(self, name):
        path = self.folder / name
        return {'name': name, 'state': 'uploaded', 'uploader': {'login': 'builder'},
                'size': path.stat().st_size if path.is_file() else 1}

    def write_sums(self, text):
        (self.folder / release.SUMS).write_text(text, encoding='utf-8')
        metadata = copy.deepcopy(self.release)
        metadata['assets'].append(self.asset(release.SUMS))
        return metadata

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

    def test_refuses_any_draft_asset_the_evidence_does_not_cover(self):
        (self.folder / 'notes.txt').write_text('extra', encoding='utf-8')
        for assets in (self.release['assets'][:-1], self.release['assets'] + [self.asset('notes.txt')],
                       self.release['assets'] + [self.release['assets'][-1]], self.release['assets'] + ['notes.txt']):
            with self.subTest(names=[asset.get('name') if isinstance(asset, dict) else asset for asset in assets]):
                with self.assertRaises(ValueError):
                    self.verify(self.release | {'assets': assets})
        for field, value in (('state', 'starter'), ('size', 1)):
            with self.subTest(download_field=field):
                metadata = copy.deepcopy(self.release)
                metadata['assets'][-1][field] = value
                with self.assertRaises(ValueError):
                    self.verify(metadata)

    def test_accepts_a_checksum_list_only_when_it_matches_the_evidence_exactly(self):
        lines = [f"{record['sha256']}  {name}" for name, record in self.value['artifacts'].items()]
        self.assertEqual(self.verify(self.write_sums('\n'.join(lines) + '\n')), self.value)
        self.assertEqual(self.verify(self.write_sums('\n'.join(line.replace('  ', ' *') for line in lines))), self.value)
        name = next(iter(self.value['artifacts']))
        for text in ('\n'.join(lines[:2]), '\n'.join(lines + [lines[0]]), '\n'.join(lines + ['0' * 64 + '  other.exe']),
                     '\n'.join([lines[0].replace(lines[0][:64], '0' * 64)] + lines[1:]), '\n'.join(lines).upper(),
                     '\n'.join(lines + ['']) + '\n', '\n'.join(lines).replace(name, '../' + name), ''):
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.verify(self.write_sums(text))
        metadata = self.write_sums('\n'.join(lines))
        (self.folder / release.SUMS).unlink()
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


class AndroidReleaseGateTests(ReleaseGateTests):
    """Run the existing desktop refusal cases against the additive schema too."""
    VERSION = '0.3.0-beta1'

    def setUp(self):
        super().setUp()
        self.certificate = 'c' * 64
        self.identity = {'package_id': 'io.github.bcrute.comicarchiver', 'version_name': self.version,
                         'version_code': 1, 'min_sdk': 24, 'target_sdk': 36,
                         'abis': ['arm64-v8a', 'x86_64']}
        self.apk = f'ComicArchiver-{self.version}-android-universal.apk'
        path = self.folder / self.apk
        path.write_bytes(b'synthetic APK; SDK subprocesses are mocked in unit tests')
        self.value['artifacts'][self.apk] = {'size': path.stat().st_size, 'sha256': release.digest(path)}
        self.value.update(schema_version=2, channel='beta', platforms=['desktop', 'android'],
                          android=self.identity | {
                              'artifact': self.apk, 'commit': self.commit, 'certificate_sha256': self.certificate,
                              'checks': dict.fromkeys(android_release.CHECKS, True),
                              'tests': {'tests_run': 300, 'failures': 0, 'errors': 0, 'skipped': []},
                              'device_matrix': [
                                  {'api': 24, 'abi': 'x86_64', 'page_size': 4096, 'passed': True},
                                  {'api': 35, 'abi': 'arm64-v8a', 'page_size': 16384, 'passed': True},
                                  {'api': 36, 'abi': 'x86_64', 'page_size': 4096, 'passed': True}]})
        self.save()
        self.signature = f'Verifies\nSigner #1 certificate SHA-256 digest: {self.certificate}\n'
        self.badging = (f"package: name='{self.identity['package_id']}' versionCode='1' versionName='{self.version}'\n"
                        "sdkVersion:'24'\ntargetSdkVersion:'36'\nnative-code: 'arm64-v8a' 'x86_64'\n")
        patcher = patch.object(android_release, 'tool_output', side_effect=self.sdk_output)
        self.tool = patcher.start()
        self.addCleanup(patcher.stop)

    def save(self):
        super().save()
        if self.value['schema_version'] == 2:
            self.release['prerelease'] = self.value.get('channel') == 'beta'

    def sdk_output(self, argv):
        name = argv[0].name
        if name == 'apksigner':
            self.assertEqual(argv[1:4], ['verify', '--verbose', '--print-certs'])
            return self.signature
        if name == 'aapt2':
            self.assertEqual(argv[1:3], ['dump', 'badging'])
            return self.badging
        self.assertEqual(name, 'zipalign')
        self.assertEqual(argv[1:6], ['-c', '-P', '16', '-v', '4'])
        return 'Verification successful\n'

    def verify(self, metadata=None, **kwargs):
        options = {'android_cert': self.certificate, 'android_identity': self.identity,
                   'android_build_tools': Path('/sdk/build-tools/37.0.0')}
        options.update(kwargs)
        return release.verify(self.folder, self.commit, self.release if metadata is None else metadata,
                              'owner', self.version, **options)

    def test_accepts_beta_without_promoting_it_and_preserves_all_desktop_requirements(self):
        self.assertEqual(self.verify(), self.value)
        self.assertEqual([call.args[0][0].name for call in self.tool.call_args_list], ['apksigner', 'aapt2', 'zipalign'])
        for name in release.CHECKS:
            with self.subTest(check=name):
                self.value['checks'][name] = False
                self.save()
                with self.assertRaises(ValueError):
                    self.verify()
                self.value['checks'][name] = True

    def test_requires_every_android_gate_and_unskipped_suites(self):
        original = copy.deepcopy(self.value)
        for key in android_release.CHECKS:
            for verdict in (False, 'pending', 1, None):
                with self.subTest(check=key, verdict=verdict):
                    self.value = copy.deepcopy(original)
                    self.value['android']['checks'][key] = verdict
                    self.save()
                    with self.assertRaises(ValueError):
                        self.verify()
        for key, value in (('checks', {}), ('tests', {'tests_run': 1, 'failures': 0, 'errors': 0, 'skipped': ['native']}),
                           ('tests', {'tests_run': True, 'failures': 0, 'errors': 0, 'skipped': []}),
                           ('tests', {'tests_run': 1, 'failures': False, 'errors': 0, 'skipped': []})):
            self.value = copy.deepcopy(original)
            self.value['android'][key] = value
            self.save()
            with self.subTest(field=key), self.assertRaises(ValueError):
                self.verify()

    def test_refuses_unapproved_or_misreported_certificate_and_changed_apk_identity(self):
        for certificate in ('', None, 'f' * 64, 'C' * 64):
            with self.subTest(certificate=certificate), self.assertRaises(ValueError):
                self.verify(android_cert=certificate)
        original = copy.deepcopy(self.value)
        for key, value in (('certificate_sha256', 'f' * 64), ('commit', 'b' * 40), ('version_code', 2),
                           ('version_name', '0.3.0-beta2'), ('package_id', 'other.app'),
                           ('abis', ['arm64-v8a']), ('min_sdk', 25), ('target_sdk', 35),
                           ('artifact', '../app.apk')):
            self.value = copy.deepcopy(original)
            self.value['android'][key] = value
            self.save()
            with self.subTest(field=key), self.assertRaises(ValueError):
                self.verify()
        self.value = original
        self.save()
        self.signature = self.signature.replace(self.certificate, 'f' * 64)
        with self.assertRaises(ValueError):
            self.verify()

    def test_inspects_compiled_package_version_sdk_abis_and_rejects_debug_or_test_builds(self):
        original = self.badging
        for value in (original.replace("versionCode='1'", "versionCode='2'"),
                      original.replace(self.version, '0.3.0-beta2'),
                      original.replace(self.identity['package_id'], 'other.app'),
                      original.replace("sdkVersion:'24'", "sdkVersion:'25'"),
                      original.replace("targetSdkVersion:'36'", "targetSdkVersion:'35'"),
                      original.replace(" 'x86_64'", ''), original + 'application-debuggable\n',
                      original + "testOnly='1'\n", original + "instrumentation: name='Runner'\n",
                      original.replace('package: ', "package: versionCodeMajor='1' "), '', original + original):
            self.badging = value
            with self.subTest(badging=value), self.assertRaises(ValueError):
                self.verify()

    def test_refuses_missing_sdk_tools_bad_signatures_and_failed_alignment(self):
        with self.assertRaises(ValueError):
            self.verify(android_build_tools=None)
        for signature in ('', 'DOES NOT VERIFY\n', self.signature + self.signature,
                          self.signature.replace('#1', '#2')):
            self.signature = signature
            with self.subTest(signature=signature), self.assertRaises(ValueError):
                self.verify()
        self.signature = f'Signer #1 certificate SHA-256 digest: {self.certificate}\n'
        normal = self.sdk_output
        def fail_alignment(argv):
            if argv[0].name == 'zipalign':
                raise ValueError('alignment refused')
            return normal(argv)
        self.tool.side_effect = fail_alignment
        with self.assertRaises(ValueError):
            self.verify()

    def test_accepts_scheme_labelled_original_signer_and_refuses_conflicting_or_multiple_signers(self):
        self.signature = f'V2 Signer: certificate SHA-256 digest: {self.certificate}\n'
        self.assertEqual(self.verify(), self.value)
        self.signature += f'V3 Signer: certificate SHA-256 digest: {self.certificate}\n'
        self.assertEqual(self.verify(), self.value)
        original = self.signature
        for value in (original.replace('V3 Signer: certificate SHA-256 digest: ' + self.certificate,
                                       'V3 Signer: certificate SHA-256 digest: ' + 'f' * 64),
                      original + 'Number of signers: 2\n', original + original):
            self.signature = value
            with self.subTest(signature=value), self.assertRaises(ValueError):
                self.verify()

    def test_accepts_modern_min_sdk_badging_and_refuses_ambiguous_or_missing_api_levels(self):
        legacy = self.badging
        self.badging = legacy.replace("sdkVersion:'24'", "minSdkVersion:'24'")
        self.assertEqual(self.verify(), self.value)
        modern = self.badging
        for value in (modern + "sdkVersion:'24'\n", modern + "minSdkVersion:'25'\n",
                      legacy + "minSdkVersion:'Preview'\n",
                      modern.replace("minSdkVersion:'24'", "minSdkVersion:'Preview'"),
                      modern.replace("targetSdkVersion:'36'\n", '')):
            self.badging = value
            with self.subTest(badging=value), self.assertRaises(ValueError):
                self.verify()

    def test_requires_real_matrix_coverage_and_rejects_duplicate_or_failed_rows(self):
        original = copy.deepcopy(self.value)
        matrix = original['android']['device_matrix']
        for rows in ([], matrix[1:], matrix[:2], [matrix[0], matrix[2]],
                     [row | {'abi': 'arm64-v8a'} for row in matrix], matrix + [matrix[0]],
                     matrix + [matrix[0] | {'passed': False}], matrix + [matrix[0] | {'api': True}],
                     matrix + [matrix[0] | {'page_size': 8192}], matrix + [matrix[0] | {'abi': 'armeabi-v7a'}]):
            self.value = copy.deepcopy(original)
            self.value['android']['device_matrix'] = rows
            self.save()
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                self.verify()

    def test_rejects_platform_downgrade_and_beta_marked_as_stable(self):
        original = copy.deepcopy(self.value)
        for platforms in ([], ['desktop'], ['android'], ['desktop', 'android', 'other'],
                          ['android', 'android'], None):
            self.value = copy.deepcopy(original)
            self.value['platforms'] = platforms
            self.save()
            with self.subTest(platforms=platforms), self.assertRaises(ValueError):
                self.verify()
        self.value = original
        self.save()
        with self.assertRaises(ValueError):
            self.verify(self.release | {'prerelease': False})
        self.value['channel'] = 'stable'
        self.save()
        with self.assertRaises(ValueError):
            self.verify()

    @unittest.skipIf(os.name == 'nt', 'The release workflow runs its shell steps on Linux.')
    def test_workflow_executes_beta_publication_without_promoting_latest(self):
        workflow = (Path(__file__).parents[1] / '.github/workflows/release.yml').read_text()
        script = textwrap.dedent(workflow[workflow.index('          if test '):])
        fake_bin = self.folder / 'bin'
        fake_bin.mkdir()
        gh = fake_bin / 'gh'
        gh.write_text('#!/bin/sh\nprintf \'%s\\n\' "$@" > "$GH_CALL_FILE"\n')
        gh.chmod(0o700)
        log = self.folder / 'gh-call.txt'
        env = os.environ | {'PATH': str(fake_bin) + os.pathsep + os.environ['PATH'],
                            'GH_CALL_FILE': str(log), 'RELEASE_TAG': 'v' + self.version}
        for prerelease in (True, False):
            (self.folder / 'release.json').write_text(json.dumps({'prerelease': prerelease}))
            result = subprocess.run(['bash', '-eu', '-c', script], cwd=self.folder, env=env,
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(log.read_text().splitlines(), ['release', 'edit', 'v' + self.version,
                                                           '--draft=false', '--latest=false' if prerelease else '--latest'])

    def test_changed_android_bytes_or_a_schema_one_downgrade_cannot_publish_the_apk(self):
        original = copy.deepcopy(self.value)
        self.value['schema_version'] = 1
        self.save()
        with self.assertRaises(ValueError):
            self.verify()
        del self.value['artifacts'][self.apk]
        self.save()
        self.release['assets'].append(self.asset(self.apk))
        with self.assertRaises(ValueError):
            self.verify()
        self.value = original
        self.save()
        (self.folder / self.apk).write_bytes(b'changed Android bytes')
        with self.assertRaises(ValueError):
            self.verify()

    def test_stable_channel_accepts_only_a_stable_source_version_and_release(self):
        previous = self.version
        self.version = '0.3.0'
        self.identity['version_name'] = self.version
        self.value['version'] = self.version
        self.value['channel'] = 'stable'
        self.value['android']['version_name'] = self.version
        self.value['android']['artifact'] = self.apk.replace(previous, self.version)
        renamed = {}
        for name, record in self.value['artifacts'].items():
            new_name = name.replace(previous, self.version)
            (self.folder / name).rename(self.folder / new_name)
            renamed[new_name] = record
        self.value['artifacts'] = renamed
        self.badging = self.badging.replace(previous, self.version)
        self.save()
        self.assertEqual(self.verify(), self.value)
        with self.assertRaises(ValueError):
            self.verify(self.release | {'prerelease': True})


class AndroidSDKReaderTests(unittest.TestCase):
    def test_desktop_cli_runs_from_another_directory_without_android_sdk_or_pythonpath(self):
        project = Path(__file__).parents[1]
        case = ReleaseGateTests('test_accepts_owner_attested_exact_commit_and_all_downloads')
        case.VERSION = tomllib.loads((project / 'pyproject.toml').read_text())['project']['version']
        case.setUp()
        self.addCleanup(case.doCleanups)
        metadata = case.folder / 'github-release.json'
        metadata.write_text(json.dumps(case.release))
        env = os.environ.copy()
        env.pop('PYTHONPATH', None)
        result = subprocess.run([sys.executable, str(project / 'scripts/verify_release.py'), str(case.folder),
                                 '--commit', case.commit, '--release', str(metadata), '--owner', 'owner'],
                                cwd=case.folder, env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('every release asset match', result.stdout)

    def test_actual_source_identity_is_read_without_executing_gradle(self):
        identity = android_release.identity_from_source(Path(__file__).parents[1])
        self.assertEqual(identity, {'package_id': 'io.github.bcrute.comicarchiver', 'version_name': '0.3.0-beta1',
                                    'version_code': 1, 'min_sdk': 24, 'target_sdk': 36,
                                    'abis': ['arm64-v8a', 'x86_64']})

    def test_refuses_missing_or_ambiguous_source_identity(self):
        source = (Path(__file__).parents[1] / 'android/app/build.gradle.kts').read_text()
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            path = project / 'android/app/build.gradle.kts'
            path.parent.mkdir(parents=True)
            for value in (source.replace('versionCode = 1', 'versionCode = nextVersion()'),
                          source + '\nversionCode = 2\n', source.replace('listOf("arm64-v8a", "x86_64")', 'abis()')):
                path.write_text(value)
                with self.assertRaises(ValueError):
                    android_release.identity_from_source(project)

    def test_sdk_subprocess_failures_timeouts_and_missing_tools_fail_closed(self):
        for result in (subprocess.CompletedProcess(['sdk'], 1, 'partial', 'refused'),
                       OSError('missing'), subprocess.TimeoutExpired(['sdk'], 90)):
            with self.subTest(result=result):
                options = {'side_effect': result} if isinstance(result, Exception) else {'return_value': result}
                with patch.object(android_release.subprocess, 'run', **options):
                    with self.assertRaises(ValueError):
                        android_release.tool_output([Path('/sdk/apksigner'), 'verify', Path('/stage/app.apk')])
