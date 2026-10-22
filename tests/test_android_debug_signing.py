"""The managed debug signing identity in scripts/android_feasibility.py.

Job 34's debug keystore died with its job-private .fleet-sdk tree, stranding the
installed app behind a signature mismatch. These tests pin the two behaviors that
must never regress: a bad injection fails the job instead of silently minting a
different key, and a valid injection (or a job-generated key) is materialized as an
owner-only file whose path — never its bytes — is what Gradle receives. All keytool
interaction is faked, so no JDK is needed to run them.

No real key material appears here: the "keystores" are arbitrary bytes, and a real
key is generated only inside an accepted fleet job.
"""

import base64
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import unittest
from unittest.mock import patch

from scripts import android_feasibility as feasibility

FINGERPRINT = "ab" * 32


def fake_jdk(directory):
    """A JDK directory whose keytool exists as a file; the fakes below never run it."""
    directory.mkdir(parents=True, exist_ok=True)
    for name in ("java", "keytool"):
        tool = directory / name
        tool.write_text("#!/bin/sh\n")
        tool.chmod(0o755)
    return str(directory / "java")


def keytool_fake(fail_generate=False, fail_list=False):
    """A run_tool stand-in for keytool: -genkeypair writes the store, -list reports a
    fixed certificate fingerprint. Nothing real is executed or decoded."""

    calls = []

    def run(argv, timeout=None, **kwargs):
        calls.append(list(argv))
        if "-genkeypair" in argv:
            if fail_generate:
                return {"returncode": 1, "tail": ""}
            keystore = Path(argv[argv.index("-keystore") + 1])
            keystore.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            keystore.write_bytes(b"generated-store")
            return {"returncode": 0, "tail": ""}
        if "-list" in argv:
            if fail_list:
                return {"returncode": 1, "tail": ""}
            colonated = ":".join(FINGERPRINT[index:index + 2]
                                 for index in range(0, len(FINGERPRINT), 2))
            return {"returncode": 0,
                    "tail": f"Alias name: androiddebugkey\nSHA256: {colonated.upper()}\n"}
        return {"returncode": 1, "tail": ""}

    run.calls = calls
    return run


class DebugSigningTestCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.java = fake_jdk(self.root / "jdk/bin")
        self.keystore = self.root / "job/debug-signing/debug.keystore"
        self.artifacts = self.root / "artifacts"
        feasibility.report["phases"].pop("debug_signing", None)
        self.addCleanup(feasibility.report["phases"].pop, "debug_signing", None)

    def phase(self, env, runner=None):
        return feasibility.phase_debug_signing(
            {"java": self.java}, env=env, runner=runner or keytool_fake(),
            keystore_path=self.keystore, artifacts_dir=self.artifacts)

    def recorded(self):
        return feasibility.report["phases"]["debug_signing"]


class StrictInjectionTests(DebugSigningTestCase):
    def test_invalid_base64_fails_without_any_fallback(self):
        runner = keytool_fake()
        result = self.phase({feasibility.DEBUG_KEYSTORE_ENV: "### not base64 ###"},
                            runner=runner)
        self.assertIsNone(result)
        self.assertEqual(self.recorded()["status"], "failed")
        self.assertEqual(self.recorded()["reason"], "not strict base64")
        # Fail-closed means closed: no keystore materialized, and keytool was never
        # invoked, so no fresh key was silently generated either.
        self.assertFalse(self.keystore.exists())
        self.assertEqual(runner.calls, [])

    def test_bad_padding_is_rejected_by_the_decoder_itself(self):
        self.assertEqual(feasibility._decode_debug_keystore_b64("aGVsbG8")[0], None)
        self.assertEqual(feasibility._decode_debug_keystore_b64("")[0], None)

    def test_unreadable_injection_fails(self):
        payload = os.urandom(64)
        result = self.phase({feasibility.DEBUG_KEYSTORE_ENV: base64.b64encode(payload).decode()},
                            runner=keytool_fake(fail_list=True))
        self.assertIsNone(result)
        self.assertEqual(self.recorded()["status"], "failed")
        self.assertEqual(self.recorded()["reason"], "keytool could not read the keystore back")

    def test_fingerprint_mismatch_fails(self):
        payload = os.urandom(64)
        result = self.phase({feasibility.DEBUG_KEYSTORE_ENV: base64.b64encode(payload).decode(),
                             feasibility.DEBUG_CERT_SHA256_ENV: "cd" * 32})
        self.assertIsNone(result)
        self.assertEqual(self.recorded()["reason"],
                         "certificate fingerprint differs from the expected value")

    def test_valid_injection_materializes_private_keystore(self):
        payload = os.urandom(64)
        result = self.phase({feasibility.DEBUG_KEYSTORE_ENV: base64.b64encode(payload).decode(),
                             # colonated upper-case form must be accepted too
                             feasibility.DEBUG_CERT_SHA256_ENV:
                                 ":".join(FINGERPRINT[i:i + 2].upper()
                                          for i in range(0, 64, 2))})
        self.assertEqual(result, str(self.keystore))
        self.assertEqual(self.recorded()["status"], "passed")
        self.assertEqual(self.recorded()["provenance"], "injected")
        self.assertEqual(self.recorded()["cert_sha256"], FINGERPRINT)
        self.assertEqual(self.keystore.read_bytes(), payload)
        self.assertEqual(stat.S_IMODE(self.keystore.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.keystore.parent.stat().st_mode), 0o700)
        # An injected key is the owner's stored copy; it is never re-exported.
        self.assertNotIn("private_artifact", self.recorded())
        self.assertFalse(self.artifacts.exists())


class GeneratedKeyTests(DebugSigningTestCase):
    def test_generated_key_is_retained_as_private_artifact(self):
        result = self.phase({})
        self.assertEqual(result, str(self.keystore))
        self.assertEqual(self.recorded()["status"], "passed")
        self.assertEqual(self.recorded()["provenance"], "generated")
        artifact = self.artifacts / "debug-keystore/comic-archiver-debug.keystore"
        self.assertEqual(artifact.read_bytes(), b"generated-store")
        self.assertEqual(stat.S_IMODE(artifact.stat().st_mode), 0o600)
        self.assertEqual(self.recorded()["private_artifact"], str(artifact))

    def test_failed_generation_fails_the_phase(self):
        result = self.phase({}, runner=keytool_fake(fail_generate=True))
        self.assertIsNone(result)
        self.assertEqual(self.recorded()["status"], "failed")
        self.assertEqual(self.recorded()["reason"],
                         "keytool could not generate the keystore")

    def test_missing_jdk_records_pending_not_generated(self):
        feasibility.report["phases"].pop("debug_signing", None)
        result = feasibility.phase_debug_signing(
            {"java": None}, env={}, runner=keytool_fake(),
            keystore_path=self.keystore, artifacts_dir=self.artifacts)
        self.assertIsNone(result)
        self.assertEqual(self.recorded()["status"], "pending")
        self.assertFalse(self.keystore.exists())


class ReceiptHygieneTests(DebugSigningTestCase):
    def test_no_secret_material_reaches_the_report(self):
        payload = os.urandom(64)
        encoded = base64.b64encode(payload).decode()
        self.phase({feasibility.DEBUG_KEYSTORE_ENV: encoded})
        injected_receipt = json.dumps(feasibility.report["phases"]["debug_signing"])
        self.phase({})
        generated_receipt = json.dumps(feasibility.report["phases"]["debug_signing"])
        # No keytool command lines, no key bytes in raw or encoded form, no store
        # contents; the fingerprint is public certificate data and stays.
        for receipt in (injected_receipt, generated_receipt):
            for forbidden in ("-storepass", "-keypass", "androiddebugkey",
                              payload.hex(), encoded, "generated-store"):
                self.assertNotIn(forbidden, receipt)
            self.assertIn("cert_sha256", receipt)

    def test_gradle_receives_only_the_keystore_path(self):
        paths = {"sdk": self.root / "sdk", "gradle": "gradle", "java": self.java,
                 "buildpython": "python3", "debug_keystore": str(self.keystore)}
        with patch.object(feasibility, "run_tool", return_value={"returncode": 1, "tail": ""}):
            _env, argv, _flags, _url = feasibility._gradle_invocation(
                paths, "assembleDebug", "wheels")
        self.assertIn(f"-PdebugKeystore={self.keystore}", argv)
        paths.pop("debug_keystore")
        with patch.object(feasibility, "run_tool", return_value={"returncode": 1, "tail": ""}):
            _env, argv, _flags, _url = feasibility._gradle_invocation(
                paths, "assembleDebug", "wheels")
        self.assertFalse(any(argument.startswith("-PdebugKeystore=") for argument in argv))


if __name__ == "__main__":
    unittest.main()
