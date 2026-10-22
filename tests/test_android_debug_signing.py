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
from unittest.mock import Mock, patch
import zipfile

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
            if not keystore.parent.is_dir():
                raise AssertionError("keytool was asked to write into a missing directory")
            keystore.write_bytes(b"generated-store")
            return {"returncode": 0, "tail": ""}
        if "-list" in argv:
            if fail_list:
                return {"returncode": 1, "tail": ""}
            if argv[argv.index("-alias") + 1] != "androiddebugkey":
                raise AssertionError("certificate listing must pin the debug alias")
            colonated = ":".join(FINGERPRINT[index:index + 2]
                                 for index in range(0, len(FINGERPRINT), 2))
            # keytool -list -v indents every certificate field, fingerprint included.
            return {"returncode": 0,
                    "tail": "Alias name: androiddebugkey\n"
                            "Entry type: PrivateKeyEntry\n"
                            "Certificate[1]:\n"
                            "Owner: CN=Comic Archiver Debug, O=Comic Archiver, C=US\n"
                            f"    SHA256: {colonated.upper()}\n"}
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

    def test_empty_injection_fails_closed(self):
        runner = keytool_fake()
        result = self.phase({feasibility.DEBUG_KEYSTORE_ENV: "   \t\n"}, runner=runner)
        self.assertIsNone(result)
        self.assertEqual(self.recorded()["status"], "failed")
        self.assertEqual(self.recorded()["reason"], "injection is present but empty")
        # Fail-closed, not fail-into-generation: keytool never ran, nothing written.
        self.assertEqual(runner.calls, [])
        self.assertFalse(self.keystore.exists())

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


class PrivateFileTests(DebugSigningTestCase):
    def test_existing_loose_modes_are_tightened(self):
        # A leftover 0644 file inside a 0755 directory must not survive: O_CREAT's
        # mode never applies to an existing file, and mkdir's mode never applies
        # to an existing directory.
        directory = self.root / "loose"
        directory.mkdir(mode=0o755)
        target = directory / "debug.keystore"
        target.write_bytes(b"stale-public-store")
        os.chmod(target, 0o644)
        feasibility._write_private_file(target, b"new-private-store")
        self.assertEqual(target.read_bytes(), b"new-private-store")
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)

    def test_parent_exists_before_keytool_writes(self):
        # The generated path must hand keytool an existing private directory.
        self.phase({})
        self.assertEqual(stat.S_IMODE(self.keystore.parent.stat().st_mode), 0o700)
        self.assertTrue(self.keystore.is_file())


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


class ApkCertificateTests(DebugSigningTestCase):
    OTHER_FINGERPRINT = "cd" * 32

    def setUp(self):
        super().setUp()
        self.apk = self.root / "app-debug.apk"
        with zipfile.ZipFile(self.apk, "w") as archive:
            archive.writestr("AndroidManifest.xml", "")
        self.sdk = self.root / "sdk"
        tools = self.sdk / "build-tools/35.0.0"
        tools.mkdir(parents=True)
        (tools / "apksigner").write_text("#!/bin/sh\n")
        (tools / "apksigner").chmod(0o755)
        feasibility.report["phases"].pop("build", None)
        self.addCleanup(feasibility.report["phases"].pop, "build", None)
        feasibility.report["phases"]["debug_signing"] = {"status": "passed",
                                                        "cert_sha256": FINGERPRINT}

    def apksigner_fake(self, digest, returncode=0, warnings=False, label="Signer #1"):
        def run(argv, timeout=None, env_extra=None, **kwargs):
            if "--print-certs" in argv:
                # The wrapper script is handed the pinned JDK both ways.
                assert env_extra and env_extra.get("JAVA_HOME", "").endswith("/jdk")
                assert env_extra["PATH"].split(":")[0] == str(Path(self.java).parent)
                banner = ("WARNING: sun.security.validator.ValidatorException: "
                          "PKIX path building failed (proxy CA)\n" * 8) if warnings else ""
                return {"returncode": returncode, "tail": (
                    banner
                    + f"{label} certificate DN: CN=Comic Archiver Debug, O=Comic Archiver, C=US\n"
                    f"{label} certificate SHA-256 digest: {digest}\n"
                    f"{label} certificate SHA-1 digest: 0000000000000000000000000000000000000000\n"
                    + banner)}
            # A successful Gradle run whose Chaquopy pip tail names zstandard — job
            # 35's exact shape that the old heuristic misread as a wheel failure.
            return {"returncode": 0, "minutes": 1.6, "tail": (
                "> Task :app:packageDebug\n> Task :app:assembleDebug\n\n"
                "BUILD SUCCESSFUL in 1m 35s\n48 actionable tasks: 48 executed\n"
                "Successfully installed zstandard-0.25.0 httpx-0.27.2\n")}

        return run

    def run_build(self, digest, returncode=0, warnings=False):
        paths = {"sdk": self.sdk, "gradle": "gradle", "java": self.java,
                 "buildpython": "python3", "debug_keystore": str(self.keystore)}
        with patch.object(feasibility, "run_tool", self.apksigner_fake(digest, returncode, warnings)), \
                patch.object(feasibility, "APK", self.apk), \
                patch.object(feasibility, "REPO", self.root / "repo"):
            ok = feasibility.phase_build(paths, "wheels")
        return ok, feasibility.report["phases"]["build"]

    def test_parser_reads_signer_one_sha256_only(self):
        digest, diagnostics = feasibility._apk_signer_cert_sha256(
            self.sdk, self.java, self.apk, self.apksigner_fake(FINGERPRINT.upper()))
        self.assertEqual(digest, FINGERPRINT)
        self.assertIsNone(diagnostics)

    def test_sdk37_scheme_label_from_job36(self):
        digest, diagnostics = feasibility._apk_signer_cert_sha256(
            self.sdk, self.java, self.apk,
            self.apksigner_fake(FINGERPRINT, label="V2 Signer:"))
        self.assertEqual(digest, FINGERPRINT)
        self.assertIsNone(diagnostics)

    def test_conflicting_signers_cannot_qualify_as_one_debug_identity(self):
        output = (f"Signer #1 certificate SHA-256 digest: {FINGERPRINT}\n"
                  f"Signer #2 certificate SHA-256 digest: {self.OTHER_FINGERPRINT}\n")
        runner = Mock(return_value={"returncode": 0, "tail": output})
        digest, diagnostics = feasibility._apk_signer_cert_sha256(
            self.sdk, self.java, self.apk, runner)
        self.assertIsNone(digest)
        self.assertIsNotNone(diagnostics)

    def test_source_stamp_and_public_key_digests_are_not_signer_certificates(self):
        output = (f"V2 Signer: public key SHA-256 digest: {FINGERPRINT}\n"
                  f"Source Stamp Signer certificate SHA-256 digest: {FINGERPRINT}\n")
        runner = Mock(return_value={"returncode": 0, "tail": output})
        digest, diagnostics = feasibility._apk_signer_cert_sha256(
            self.sdk, self.java, self.apk, runner)
        self.assertIsNone(digest)
        self.assertIsNotNone(diagnostics)

    def test_parser_reads_through_verifier_warnings(self):
        digest, diagnostics = feasibility._apk_signer_cert_sha256(
            self.sdk, self.java, self.apk, self.apksigner_fake(FINGERPRINT, warnings=True))
        self.assertEqual(digest, FINGERPRINT)
        self.assertIsNone(diagnostics)

    def test_parser_rejects_incomplete_digest(self):
        digest, diagnostics = feasibility._apk_signer_cert_sha256(
            self.sdk, self.java, self.apk, self.apksigner_fake("abc123"))
        self.assertIsNone(digest)
        self.assertEqual(diagnostics["returncode"], 0)

    def test_parser_rejects_failed_or_absent_apksigner_with_diagnostics(self):
        digest, diagnostics = feasibility._apk_signer_cert_sha256(
            self.sdk, self.java, self.apk, self.apksigner_fake(FINGERPRINT, returncode=1))
        self.assertIsNone(digest)
        self.assertEqual(diagnostics["returncode"], 1)
        self.assertIn("SHA-256", diagnostics["tail"])
        digest, diagnostics = feasibility._apk_signer_cert_sha256(
            self.root / "no-build-tools", self.java, self.apk, self.apksigner_fake(FINGERPRINT))
        self.assertIsNone(digest)
        self.assertIn("no apksigner", diagnostics["reason"])

    def test_matching_certificate_passes_the_build(self):
        ok, phase = self.run_build(FINGERPRINT)
        self.assertTrue(ok)
        self.assertEqual(phase["status"], "passed")
        self.assertEqual(phase["apk_cert_sha256"], FINGERPRINT)
        self.assertIsNone(phase["classification"])
        self.assertTrue(phase["apk_artifact"].endswith("/artifacts/app-debug.apk"))

    def test_mismatched_certificate_fails_the_build(self):
        ok, phase = self.run_build(self.OTHER_FINGERPRINT)
        self.assertFalse(ok)
        self.assertEqual(phase["status"], "failed")
        self.assertEqual(phase["classification"], "signing_certificate_mismatch")
        self.assertEqual(phase["apk_cert_sha256"], self.OTHER_FINGERPRINT)

    def test_unprovable_certificate_fails_the_build(self):
        ok, phase = self.run_build(None, returncode=1)
        self.assertFalse(ok)
        self.assertEqual(phase["status"], "failed")
        self.assertEqual(phase["classification"], "signing_verification_failed")

    def test_absent_expected_certificate_cannot_qualify_an_apk(self):
        feasibility.report["phases"]["debug_signing"].pop("cert_sha256")
        ok, phase = self.run_build(FINGERPRINT)
        self.assertFalse(ok)
        self.assertEqual(phase["classification"], "signing_verification_failed")

    def test_successful_gradle_log_naming_zstandard_keeps_signing_classification(self):
        # Job 35's exact failure: BUILD SUCCESSFUL, Chaquopy pip tail names zstandard,
        # the certificate proof fails — the old heuristic relabelled the phase
        # wheel_install_failed and discarded everything about the verifier run.
        ok, phase = self.run_build(None, returncode=1)
        self.assertFalse(ok)
        self.assertEqual(phase["classification"], "signing_verification_failed")
        self.assertNotIn("wheel_install_failed", phase["classification"])
        self.assertIsNotNone(phase["signing_verifier"])
        self.assertEqual(phase["signing_verifier"]["returncode"], 1)

    def test_unqualified_apk_is_retained_on_signing_check_failure(self):
        ok, phase = self.run_build(self.OTHER_FINGERPRINT)
        self.assertFalse(ok)
        unqualified = self.root / "artifacts/app-debug-unqualified.apk"
        self.assertEqual(phase["apk_artifact"], str(unqualified))
        self.assertTrue(unqualified.is_file())
        self.assertEqual(unqualified.read_bytes(), self.apk.read_bytes())
        self.assertIsNotNone(phase["apk_sha256"])
        self.assertFalse((self.root / "artifacts/app-debug.apk").exists())


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
