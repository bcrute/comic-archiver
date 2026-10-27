"""The owner's release key in scripts/android_feasibility.py --phase release-apk.

Every update must carry the one release identity, so the job never generates a key,
refuses one whose certificate differs, never puts the password in an argument list or
receipt, and deletes its copy of the key however the build ends. keytool, Gradle and
the SDK are all faked; the "keystores" here are arbitrary bytes.
"""

import base64
import contextlib
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import unittest
from unittest.mock import patch

from scripts import android_feasibility as feasibility
from test_android_debug_signing import fake_jdk

FINGERPRINT = "9a" * 32
PASSWORD = "correct horse battery staple"
IDENTITY = {"package_id": "io.github.bcrute.comicarchiver", "version_name": "0.3.0-beta1",
            "version_code": 1, "min_sdk": 24, "target_sdk": 36, "abis": ["arm64-v8a", "x86_64"]}


def keytool_fake(fail_list=False, fingerprint=FINGERPRINT):
    calls = []

    def run(argv, timeout=None, **kwargs):
        calls.append(list(argv))
        if "-list" not in argv or fail_list:
            return {"returncode": 1, "tail": ""}
        if argv[argv.index("-alias") + 1] != "comic-archiver":
            raise AssertionError("the release certificate must be read from the release alias")
        if Path(argv[argv.index("-storepass:file") + 1]).read_text() != PASSWORD:
            raise AssertionError("keytool must read the injected password from its file")
        colonated = ":".join(fingerprint[i:i + 2] for i in range(0, 64, 2)).upper()
        return {"returncode": 0, "tail": f"Alias name: comic-archiver\n    SHA256: {colonated}\n"}

    run.calls = calls
    return run


class ReleaseSigningTestCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.java = fake_jdk(self.root / "jdk/bin")
        self.folder = self.root / "job/release-signing"
        self.store = os.urandom(64)
        self.env = {feasibility.RELEASE_KEYSTORE_ENV: base64.b64encode(self.store).decode(),
                    feasibility.RELEASE_PASSWORD_ENV: PASSWORD,
                    feasibility.RELEASE_CERT_SHA256_ENV: ":".join(FINGERPRINT[i:i + 2] for i in range(0, 64, 2))}
        phases = dict(feasibility.report["phases"])
        self.addCleanup(lambda: (feasibility.report["phases"].clear(), feasibility.report["phases"].update(phases)))

    def phase(self, env=None, runner=None):
        return feasibility.phase_release_signing({"java": self.java}, env=self.env if env is None else env,
                                                 runner=runner or keytool_fake(), directory=self.folder)

    def recorded(self):
        return feasibility.report["phases"]["release_signing"]

    @staticmethod
    def quiet_gradle():
        """No pip probe, proxy flags or mirror init script: only the argv matters here."""
        stack = contextlib.ExitStack()
        stack.enter_context(patch.object(feasibility, "run_tool", return_value={"returncode": 1, "tail": ""}))
        stack.enter_context(patch.object(feasibility, "_jvm_net_options", return_value=[]))
        stack.enter_context(patch.object(feasibility, "_ci_download_mirror", return_value=None))
        return stack


class InjectionTests(ReleaseSigningTestCase):
    def test_each_missing_part_fails_without_generating_or_writing_anything(self):
        for name in (feasibility.RELEASE_KEYSTORE_ENV, feasibility.RELEASE_PASSWORD_ENV,
                     feasibility.RELEASE_CERT_SHA256_ENV):
            with self.subTest(missing=name):
                runner = keytool_fake()
                self.assertIsNone(self.phase({**self.env, name: "  "}, runner))
                self.assertEqual(self.recorded()["status"], "failed")
                self.assertEqual(runner.calls, [])
                self.assertFalse(self.folder.exists())

    def test_a_damaged_store_fails(self):
        self.assertIsNone(self.phase({**self.env, feasibility.RELEASE_KEYSTORE_ENV: "### not base64 ###"}))
        self.assertEqual(self.recorded()["reason"], "not strict base64")
        self.assertFalse(self.folder.exists())

    def test_the_injected_key_is_materialized_privately_and_read_with_its_password_by_file(self):
        runner = keytool_fake()
        store, password = self.phase(runner=runner)
        self.assertEqual(self.recorded(), {"status": "passed", "cert_sha256": FINGERPRINT})
        self.assertEqual(Path(store).read_bytes(), self.store)
        self.assertEqual(Path(password).read_text(), PASSWORD)
        for path in (store, password):
            self.assertEqual(stat.S_IMODE(Path(path).stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.folder.stat().st_mode), 0o700)
        self.assertFalse(any(PASSWORD in argument for call in runner.calls for argument in call))

    def test_another_certificate_fails_and_the_copy_is_deleted(self):
        self.assertIsNone(self.phase(runner=keytool_fake(fingerprint="cd" * 32)))
        self.assertEqual(self.recorded()["reason"], "certificate fingerprint differs from the expected value")
        self.assertFalse(self.folder.exists())

    def test_a_store_keytool_cannot_open_fails_and_the_copy_is_deleted(self):
        self.assertIsNone(self.phase(runner=keytool_fake(fail_list=True)))
        self.assertEqual(self.recorded()["reason"], "keytool could not read the release key back")
        self.assertFalse(self.folder.exists())

    def test_no_secret_reaches_the_receipt(self):
        self.phase()
        receipt = json.dumps(feasibility.report["phases"]["release_signing"])
        for secret in (PASSWORD, self.env[feasibility.RELEASE_KEYSTORE_ENV], self.store.hex()):
            self.assertNotIn(secret, receipt)

    def test_gradle_receives_only_the_two_paths(self):
        paths = {"sdk": self.root / "sdk", "gradle": "gradle", "java": self.java, "buildpython": "python3",
                 "release_keystore": "/job/release.p12", "release_password_file": "/job/release.pass"}
        with self.quiet_gradle():
            _env, argv, _flags, _url = feasibility._gradle_invocation(paths, "assembleRelease", "wheels")
        self.assertIn("-PreleaseKeystore=/job/release.p12", argv)
        self.assertIn("-PreleaseKeystorePasswordFile=/job/release.pass", argv)
        for key in ("release_keystore", "release_password_file"):
            paths.pop(key)
        with self.quiet_gradle():
            _env, argv, _flags, _url = feasibility._gradle_invocation(paths, "assembleDebug", "wheels")
        self.assertFalse(any(argument.startswith("-PreleaseKeystore") for argument in argv))


class ReleaseApkTests(ReleaseSigningTestCase):
    def setUp(self):
        super().setUp()
        self.repo = self.root / "repo"
        self.apk = self.repo / "android/app/build/outputs/apk/release/app-release.apk"
        sdk = self.root / "sdk"
        for tool in ("apksigner", "aapt2"):
            (sdk / "build-tools/36.0.0").mkdir(parents=True, exist_ok=True)
            (sdk / "build-tools/36.0.0" / tool).write_text("")
        self.paths = {"sdk": sdk, "java": self.java, "gradle": "gradle", "buildpython": "python3"}
        for name, value in (("REPO", self.repo), ("RELEASE_APK", self.apk), ("RELEASE_SIGNING_DIR", self.folder)):
            patcher = patch.object(feasibility, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.repo.mkdir()
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)

    def build(self, gradle_ok=True, observed=None):
        def provision(**kwargs):
            feasibility.record("provision", "passed")
            return {"java": self.java}

        def gradle(argv, **kwargs):
            if "assembleRelease" in argv and gradle_ok:
                self.apk.parent.mkdir(parents=True, exist_ok=True)
                self.apk.write_bytes(b"signed release apk")
            self.signing_present = self.folder.exists()
            return {"returncode": 0 if gradle_ok else 1, "tail": "" if gradle_ok else "> Task :app:lintVitalRelease FAILED"}

        def preflight(tools):
            feasibility.record("preflight", "passed")
            return True, dict(self.paths)

        def sdist():
            feasibility.record("sdist", "passed")
            return "sdist"

        def wheels(paths, sdist):
            feasibility.record("wheels", "passed")
            return "wheels"

        signing = feasibility.phase_release_signing
        with patch.object(feasibility, "phase_provision", provision), \
                patch.object(feasibility, "phase_release_signing",
                             lambda tools: signing(tools, env=self.env, runner=keytool_fake(), directory=self.folder)), \
                patch.object(feasibility, "phase_preflight", preflight), \
                patch.object(feasibility, "phase_sdist", sdist), patch.object(feasibility, "phase_wheels", wheels), \
                patch.object(feasibility, "run_tool", gradle), \
                patch.object(feasibility, "_jvm_net_options", return_value=[]), \
                patch.object(feasibility, "_ci_download_mirror", return_value=None), \
                patch.object(feasibility.android_release, "identity_from_source", return_value=dict(IDENTITY)), \
                patch.object(feasibility.android_release, "inspect_apk",
                             return_value=observed or dict(IDENTITY, certificate_sha256=FINGERPRINT)), \
                patch("builtins.print"):
            return feasibility.release_apk()

    def test_a_matching_signed_apk_is_kept_under_its_release_name_and_the_key_is_deleted(self):
        self.assertEqual(self.build(), 0)
        self.assertTrue(self.signing_present)
        self.assertFalse(self.folder.exists())
        kept = self.root / "artifacts/ComicArchiver-0.3.0-beta1-android-universal.apk"
        self.assertEqual(kept.read_bytes(), b"signed release apk")
        self.assertEqual(feasibility.report["verdict"], "release_apk_ready")

    def test_a_failed_build_still_deletes_the_key(self):
        self.assertEqual(self.build(gradle_ok=False), 1)
        self.assertFalse(self.folder.exists())
        self.assertEqual(feasibility.report["phases"]["release_apk"]["classification"],
                         "task_failed:app:lintVitalRelease")

    def test_an_apk_with_another_signer_or_identity_is_refused(self):
        for observed in (dict(IDENTITY, certificate_sha256="cd" * 32),
                         dict(IDENTITY, certificate_sha256=FINGERPRINT, version_code=2)):
            with self.subTest(observed=observed):
                self.assertEqual(self.build(observed=observed), 1)
                self.assertFalse((self.root / "artifacts/ComicArchiver-0.3.0-beta1-android-universal.apk").exists())
                self.assertFalse(self.folder.exists())


if __name__ == "__main__":
    unittest.main()
