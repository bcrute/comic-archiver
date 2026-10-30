"""Keep expensive fleet provisioning aligned with actual emulator capacity, without
turning missing capacity into a qualification pass. No SDK/network/build is run here.
"""
import io
import json
import pathlib
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from scripts import android_feasibility as feasibility


class EmulatorCapacityTests(unittest.TestCase):
    PACKAGES = {"platform-tools", "emulator", "platforms;android-35", "platforms;android-36",
                "ndk;27.3.13750724", "ndk;30.0.16248370", "build-tools;35.0.0", "build-tools;37.0.0",
                "system-images;android-36;google_apis;x86_64",
                "system-images;android-36;google_apis_ps16k;x86_64"}

    def test_missing_kvm_provisions_every_build_input_but_no_unusable_emulator(self):
        wanted = feasibility._sdk_build_packages(self.PACKAGES, emulator_capable=False)
        self.assertEqual(set(wanted), {"platform-tools", "platforms;android-36", "ndk;30.0.16248370",
                                       "build-tools;35.0.0", "build-tools;37.0.0"})

    def test_usable_kvm_keeps_emulator_and_standard_image_in_the_required_inputs(self):
        wanted = feasibility._sdk_build_packages(self.PACKAGES, emulator_capable=True)
        self.assertIn("emulator", wanted)
        self.assertIn("system-images;android-36;google_apis;x86_64", wanted)
        self.assertNotIn("system-images;android-36;google_apis_ps16k;x86_64", wanted)

    def test_a_whole_api_level_is_preferred_over_minor_images_avd_manager_cannot_register(self):
        images = {"system-images;android-37.0;google_apis;x86_64",
                  "system-images;android-37.10;google_apis;x86_64"}
        wanted = feasibility._sdk_build_packages(self.PACKAGES | images, emulator_capable=True)
        self.assertEqual([name for name in wanted if name.startswith("system-images;")],
                         ["system-images;android-36;google_apis;x86_64"])

    def test_stable_minor_images_are_ordered_numerically_and_preview_names_are_excluded(self):
        images = {"system-images;android-37.2;google_apis;x86_64",
                  "system-images;android-37.10;google_apis;x86_64",
                  "system-images;android-38.0-beta1;google_apis;x86_64",
                  "system-images;android-38.0;google_apis_ps16k;x86_64"}
        whole = {"system-images;android-36;google_apis;x86_64",
                 "system-images;android-36;google_apis_ps16k;x86_64"}
        # Only when no whole-number image exists does the newest minor release stand in.
        wanted = feasibility._sdk_build_packages((self.PACKAGES - whole) | images, emulator_capable=True)
        self.assertEqual([name for name in wanted if name.startswith("system-images;")],
                         ["system-images;android-37.10;google_apis;x86_64"])
        wanted = feasibility._sdk_build_packages(self.PACKAGES | images, emulator_capable=False)
        self.assertFalse(any(name.startswith("system-images;") for name in wanted))

    def test_development_sdk_uses_declared_app_inputs_without_emulator_or_newest_ndk(self):
        wanted = feasibility._development_sdk_packages(
            self.PACKAGES, 'compileSdk = 36\nndkVersion = "27.3.13750724"\n')
        self.assertEqual(set(wanted), {"platform-tools", "platforms;android-36",
                                       "ndk;27.3.13750724", "build-tools;35.0.0"})

    def test_default_command_cannot_accidentally_start_full_qualification(self):
        with patch('sys.argv', ['android_feasibility.py']), patch('sys.stderr', io.StringIO()), patch.object(
                feasibility, 'phase_provision') as provision:
            with self.assertRaises(SystemExit) as error:
                feasibility.main()
            self.assertEqual(error.exception.code, 2)
            provision.assert_not_called()

    def test_skipped_provisioning_still_leaves_the_required_in_app_gate_pending(self):
        report = {"phases": {name: {"status": "passed"} for name in feasibility.REQUIRED_PHASES}}
        with patch.object(feasibility, "report", report), patch.object(
                feasibility, "_emulator_capable", return_value=False), patch.object(
                feasibility, "run_tool") as run:
            feasibility.phase_in_app({})
            self.assertEqual(report["phases"]["in_app"]["status"], "pending")
            self.assertEqual(feasibility._verdict(), 3)
            run.assert_not_called()

    def test_missing_beta_or_instrumentation_build_cannot_pass_the_full_verdict(self):
        for omitted in ('instrumentation_build', 'beta_integration'):
            report = {'phases': {name: {'status': 'passed'} for name in feasibility.REQUIRED_PHASES
                                 if name != omitted}}
            with self.subTest(phase=omitted), patch.object(feasibility, 'report', report):
                self.assertEqual(feasibility._verdict(), 3)

    def test_explicit_preflight_and_sdist_selectors_do_not_claim_a_whole_app_pass(self):
        for phase in ('preflight', 'sdist'):
            with patch.object(feasibility, 'report', {'phases': {phase: {'status': 'passed'}}}):
                self.assertEqual(feasibility._verdict((phase,)), 0)
                self.assertEqual(feasibility._verdict(), 3)


class SelfcheckEvidenceTests(unittest.TestCase):
    """Exercise the actual MainActivity envelope, without claiming Android execution."""

    @staticmethod
    def envelope():
        return {
            "ping": {"ok": True, "result": {"python": "3.13.16", "bridge_version": 1}},
            "selfcheck": {
                "identity": feasibility.SELF_IDENTITY, "python": "3.13.16", "page_size": 16384,
                "zstandard": {"imported": True, "version": "0.25.0", "roundtrip": True,
                              "trailing_frame": True, "truncation": True, "checksum": "rejected",
                              "window_bound": True},
            },
        }

    def test_main_activity_envelope_requires_bridge_and_every_native_check(self):
        checks = feasibility._evaluate_selfcheck(self.envelope())
        self.assertTrue(checks["bridge_ping"])
        self.assertTrue(all(checks.values()))

    def test_bare_python_facts_and_invalid_envelopes_cannot_qualify_the_app(self):
        for value in (None, [], {}, {"error": "ImportError"}, self.envelope()["selfcheck"]):
            with self.subTest(value=value):
                self.assertFalse(all(feasibility._evaluate_selfcheck(value).values()))

    def test_missing_or_failed_named_checks_never_receive_success(self):
        for key in ("imported", "version", "roundtrip", "trailing_frame", "truncation", "checksum", "window_bound"):
            with self.subTest(key=key):
                value = self.envelope()
                del value["selfcheck"]["zstandard"][key]
                self.assertFalse(all(feasibility._evaluate_selfcheck(value).values()))
        value = self.envelope()
        value["selfcheck"]["zstandard"]["roundtrip"] = 1
        self.assertFalse(feasibility._evaluate_selfcheck(value)["roundtrip"])

    def test_unparseable_and_below_floor_versions_fail_without_raising(self):
        for version in ("", "old", "0.nope.0", "0.24.0", None, 0.25, "0.25rc1"):
            with self.subTest(version=version):
                value = self.envelope()
                value["selfcheck"]["zstandard"]["version"] = version
                self.assertFalse(feasibility._evaluate_selfcheck(value)["version_floor"])

    def test_missing_failed_or_mismatched_bridge_ping_does_not_qualify(self):
        for ping in (None, {}, {"ok": False},
                     {"ok": True, "result": {"bridge_version": True, "python": "3.13.16"}},
                     {"ok": True, "result": {"bridge_version": 1, "python": "3.12.0"}}):
            with self.subTest(ping=ping):
                value = self.envelope()
                value["ping"] = ping
                self.assertFalse(feasibility._evaluate_selfcheck(value)["bridge_ping"])

    def test_identity_and_observed_page_size_are_required(self):
        for identity in (None, feasibility.SELF_IDENTITY + "/other"):
            value = self.envelope()
            value["selfcheck"]["identity"] = identity
            self.assertFalse(feasibility._evaluate_selfcheck(value)["selfcheck_line"])
        for size in (None, True, 0, -4096, "16384"):
            value = self.envelope()
            value["selfcheck"]["page_size"] = size
            self.assertFalse(feasibility._evaluate_selfcheck(value)["page_size"])


class InAppObservationTests(unittest.TestCase):
    def observe(self, failure=None):
        with tempfile.TemporaryDirectory() as folder:
            sdk = pathlib.Path(folder) / "sdk"
            for relative in ("platform-tools/adb", "emulator/emulator", "cmdline-tools/latest/bin/avdmanager"):
                path = sdk / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
            payload = SelfcheckEvidenceTests.envelope()
            report = {"phases": {"preflight": {"system_images": ["system-images;android-36;google_apis;x86_64"]}}}
            process = MagicMock()
            process.poll.return_value = 0

            def run(argv, **kwargs):
                code, tail = 0, ""
                if "getprop" in argv:
                    tail = {"sys.boot_completed": "1", "ro.product.cpu.abi": "x86_64",
                            "ro.build.version.sdk": "36"}[argv[-1]]
                elif argv[-3:] == ['emu', 'avd', 'name']:
                    tail = f'comic-archiver-feasibility-{feasibility.os.getpid()}\nOK'
                elif "install" in argv:
                    code, tail = (1, "Failure") if failure == "install" else (0, "Success")
                elif "start" in argv:
                    tail = "Error: Activity not started" if failure == "launch" else "Status: ok"
                elif "logcat" in argv and "-c" in argv:
                    code = 1 if failure == "clear" else 0
                elif "logcat" in argv and "-d" in argv:
                    code = 1 if failure == "read" else 0
                    tail = json.dumps(payload)
                return {"returncode": code, "tail": tail}

            with patch.object(feasibility, "report", report), patch.object(
                    feasibility, "JOB_DIR", pathlib.Path(folder) / "job"), patch.object(
                    feasibility, "_emulator_capable", return_value=True), patch.object(
                    feasibility, "_free_port", return_value=5554), patch.object(
                    feasibility, "run_tool", side_effect=run), patch.object(
                    feasibility.subprocess, "Popen", return_value=process), patch.object(feasibility.time, "sleep"):
                feasibility.phase_in_app({"sdk": sdk, "java": "/fixture/jdk/bin/java"})
            return report["phases"]["in_app"]

    def test_success_needs_install_launch_clear_read_and_the_real_envelope(self):
        phase = self.observe()
        self.assertEqual(phase["status"], "passed")
        self.assertTrue(all(phase["checks"].values()))

    def test_stale_passing_log_cannot_hide_failed_install(self):
        phase = self.observe("install")
        self.assertEqual(phase["status"], "failed")
        self.assertFalse(phase["checks"]["install_ok"])

    def test_shell_zero_exit_without_successful_activity_start_is_not_success(self):
        phase = self.observe("launch")
        self.assertEqual(phase["status"], "failed")
        self.assertFalse(phase["checks"]["launch_ok"])

    def test_failed_log_clear_or_read_cannot_accept_stale_evidence(self):
        for stage, check in (("clear", "log_cleared"), ("read", "log_read")):
            with self.subTest(stage=stage):
                phase = self.observe(stage)
                self.assertEqual(phase["status"], "failed")
                self.assertFalse(phase["checks"][check])


class InstrumentationBuildTests(unittest.TestCase):
    def build(self, cert='a' * 64, *, exists=True):
        with tempfile.TemporaryDirectory() as folder:
            repo = pathlib.Path(folder) / 'checkout'
            repo.mkdir()
            apk = repo / 'fixture.apk'
            if exists:
                apk.write_bytes(b'test-only instrumentation apk')
            report = {'phases': {'build': {'apk_cert_sha256': 'a' * 64}}}
            with patch.object(feasibility, 'REPO', repo), patch.object(feasibility, 'TEST_APK', apk), \
                    patch.object(feasibility, 'report', report), patch.object(
                        feasibility, '_gradle_invocation', return_value=({}, ['gradle'], None, None)), \
                    patch.object(feasibility, 'run_tool', return_value={'returncode': 0, 'minutes': 0.1, 'tail': 'built'}), \
                    patch.object(feasibility, '_apk_signer_cert_sha256', return_value=(cert, {})):
                ok = feasibility.phase_instrumentation_build({'sdk': '/fixture/sdk', 'java': '/fixture/java'}, [])
            retained = repo.parent / 'artifacts/app-debug-androidTest.apk'
            return ok, report['phases']['instrumentation_build'], retained.read_bytes() if retained.exists() else None

    def test_test_apk_must_match_target_signer_and_retained_digest(self):
        ok, phase, data = self.build()
        self.assertTrue(ok)
        self.assertEqual(phase['status'], 'passed')
        self.assertEqual(phase['apk_sha256'], feasibility.hashlib.sha256(data).hexdigest())

    def test_wrong_signer_or_missing_test_apk_cannot_pass_or_be_retained(self):
        for arguments in ({'cert': 'b' * 64}, {'cert': None}, {'exists': False}):
            with self.subTest(arguments=arguments):
                ok, phase, data = self.build(**arguments)
                self.assertFalse(ok)
                self.assertEqual(phase['status'], 'failed')
                self.assertIsNone(data)


class JvmFailureTests(unittest.TestCase):
    def test_failed_tests_are_named_from_the_junit_reports(self):
        with tempfile.TemporaryDirectory() as folder:
            results = pathlib.Path(folder)
            (results / 'TEST-a.Passing.xml').write_text(
                '<testsuite><testcase classname="a.Passing" name="fine"/></testsuite>')
            (results / 'TEST-b.Failing.xml').write_text(
                '<testsuite><testcase classname="b.Failing" name="times_out">'
                '<failure message="ComposeTimeoutException: idle&#10;more">trace</failure></testcase>'
                '<testcase classname="b.Failing" name="throws"><error>NullPointerException\n\tat x</error></testcase>'
                '</testsuite>')
            (results / 'TEST-c.Broken.xml').write_text('<testsuite')
            self.assertEqual(feasibility.jvm_failures(results), [
                {'test': 'b.Failing.times_out', 'message': 'ComposeTimeoutException: idle'},
                {'test': 'b.Failing.throws', 'message': 'NullPointerException'}])
            self.assertEqual(feasibility.jvm_failures(results / 'missing'), [])


class EmulatorPortTests(unittest.TestCase):
    def test_console_and_adb_pair_must_both_be_free_in_androids_range(self):
        addresses = []

        def socket():
            value = MagicMock()
            value.__enter__.return_value = value

            def bind(address):
                addresses.append(address)
                if address[1] == 5555:
                    raise OSError("adb endpoint occupied")

            value.bind.side_effect = bind
            return value

        with patch.object(feasibility.socket, "socket", side_effect=socket):
            self.assertEqual(feasibility._free_port(), 5556)
        self.assertEqual([port for host, port in addresses], [5554, 5555, 5556, 5557])
        self.assertTrue(all(host == "127.0.0.1" for host, port in addresses))

    def test_exhausted_android_port_range_is_an_explicit_failure(self):
        value = MagicMock()
        value.__enter__.return_value = value
        value.bind.side_effect = OSError("occupied")
        with patch.object(feasibility.socket, "socket", return_value=value), self.assertRaises(RuntimeError):
            feasibility._free_port()
