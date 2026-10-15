import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import android_preflight as probe


class AndroidPreflightTests(unittest.TestCase):
    def test_local_standalone_build_tools_are_detected_without_global_path_changes(self):
        with tempfile.TemporaryDirectory() as work:
            root=Path(work);base=root/'.comic-session/mobile/toolchain'
            for relative in ('jdk/bin/java','jdk/bin/javac','sdk/platforms/android-36/android.jar',
                    *('sdk/build-tools/36.0.0/'+name for name in ('aapt2','d8','apksigner','zipalign'))):
                path=base/relative;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'fixture')
            with patch.object(probe,'PROJECT',root),patch.object(probe.shutil,'which',return_value=None),patch.dict(probe.os.environ,{},clear=True):
                report=probe.toolchain()
            self.assertTrue(report['local_standalone_jdk']);self.assertTrue(report['jdk_and_sdk_detected'])
            self.assertIn(36,report['platform_apis']);self.assertIn('36.0.0',report['build_tools_versions'])
            self.assertFalse(any(report['tools_on_path'].values()))

    def inspect_with(self, output, replies=(), device_number=None, usb_phones=()):
        with patch.object(probe, "toolchain", return_value={"jdk_and_sdk_detected": False}), \
                patch.object(probe.shutil, "which", return_value="/usr/bin/adb"), \
                patch.object(probe,'usb_phone_observations',return_value=list(usb_phones)), \
                patch.object(probe, "run", side_effect=[(output, None), *replies]) as command:
            return probe.inspect(device_number), command.call_args_list

    def test_connected_phone_without_adb_is_not_replaced_by_emulator(self):
        phone={'model':'Pixel Test','adb_interface_exposed':False}
        for targets in ('List of devices attached\n','emulator-5554 device model:Virtual_Phone\n'):
            report,calls=self.inspect_with(targets,usb_phones=[phone])
            self.assertEqual(report['reason'],'usb_debugging_unavailable')
            self.assertIsNone(report['selected_device_number']);self.assertIsNone(report['device'])
            self.assertEqual(len(calls),1)
        # Explicit selection can still inspect an intentionally chosen emulator.
        report,calls=self.inspect_with('emulator-5554 device model:Virtual_Phone\n',
            [('Virtual Phone',None),('16',None),('36',None)],device_number=1,usb_phones=[phone])
        self.assertEqual(report['selected_device_number'],1);self.assertEqual(len(calls),4)

    def test_usb_observation_checks_interfaces_and_excludes_serials_and_nonphones(self):
        with tempfile.TemporaryDirectory() as work:
            root=Path(work)
            phone=root/'1-2';phone.mkdir();(phone/'idVendor').write_text('18d1')
            (phone/'product').write_text('Pixel Test');(phone/'serial').write_text('PRIVATE_SERIAL')
            interface=phone/'1-2:1.0';interface.mkdir()
            for name,value in zip(('bInterfaceClass','bInterfaceSubClass','bInterfaceProtocol'),('06','01','01')):
                (interface/name).write_text(value)
            token=root/'1-3';token.mkdir();(token/'idVendor').write_text('18d1');(token/'product').write_text('Titan Security Key')
            found=probe.usb_phone_observations(root)
            self.assertEqual(found,[{'model':'Pixel Test','adb_interface_exposed':False}])
            self.assertNotIn('PRIVATE',json.dumps(found))
            for name,value in zip(('bInterfaceClass','bInterfaceSubClass','bInterfaceProtocol'),('ff','42','01')):
                (interface/name).write_text(value)
            self.assertTrue(probe.usb_phone_observations(root)[0]['adb_interface_exposed'])
            self.assertEqual(probe.usb_phone_observations(root/'absent'),[])

    def test_device_test_reads_only_fixed_os_properties(self):
        report, calls = self.inspect_with("List of devices attached\nPRIVATE_SERIAL device product:x model:y\n",
            [("Test Phone\n", None), ("14\n", None), ("34\n", None)])
        self.assertEqual(report["status"], "device_ready_build_tools_missing")
        self.assertEqual(report["device"], {"model": "Test Phone", "android_version": "14", "api": 34})
        self.assertNotIn("PRIVATE_SERIAL", json.dumps(report))
        self.assertEqual([c.args[0] for c in calls], [
            ["/usr/bin/adb", "devices", "-l"],
            ["/usr/bin/adb", "-s", "PRIVATE_SERIAL", "shell", "getprop", "ro.product.model"],
            ["/usr/bin/adb", "-s", "PRIVATE_SERIAL", "shell", "getprop", "ro.build.version.release"],
            ["/usr/bin/adb", "-s", "PRIVATE_SERIAL", "shell", "getprop", "ro.build.version.sdk"]])

    def test_unauthorized_offline_permissions_and_multiple_devices_do_not_query_phone(self):
        for text, reason in (("PRIVATE unauthorized\n", "unauthorized"), ("PRIVATE offline\n", "offline"),
                ("PRIVATE no permissions (missing udev rules)\n", "no_permissions"),
                ("PRIVATE device\nOTHER device\n", "multiple_devices"), ("List of devices attached\n", "no_device")):
            with self.subTest(reason=reason):
                report, calls = self.inspect_with(text)
                self.assertEqual(report["reason"], reason)
                self.assertEqual(len(calls), 1)

    def test_failed_adb_never_returns_raw_error(self):
        with patch.object(probe, "toolchain", return_value={}), \
                patch.object(probe.shutil, "which", return_value="adb"), \
                patch.object(probe.subprocess, "run", return_value=subprocess.CompletedProcess(
                    [], 1, b"PRIVATE_SERIAL", b"PRIVATE_DEVICE_PATH")):
            report = probe.inspect()
        self.assertEqual(report["reason"], "adb_failed")
        self.assertNotIn("PRIVATE", json.dumps(report))

    def test_single_usb_target_is_selected_among_wireless_and_emulator_targets(self):
        report, calls = self.inspect_with(
            "PRIVATE_NETWORK device model:Test_Phone transport_id:1\n"
            "PRIVATE_USB device usb:1-2 product:x model:Test_Phone transport_id:2\n"
            "emulator-5554 device model:Virtual_Phone\n",
            [("Test Phone", None), ("17", None), ("37", None)])
        self.assertEqual(report["selected_device_number"], 2)
        self.assertEqual(report["device"]["api"], 37)
        self.assertEqual([item["connection"] for item in report["device_candidates"]],
                         ["other", "usb", "emulator"])
        self.assertNotIn("PRIVATE", json.dumps(report))
        for call in calls[1:]:
            self.assertEqual(call.args[0][1:3], ["-s", "PRIVATE_USB"])

    def test_explicit_selection_never_queries_other_targets(self):
        report, calls = self.inspect_with("OTHER device usb:1-1\nPRIVATE device usb:1-2\n",
            [("Test Phone", None), ("17", None), ("37", None)], device_number=2)
        self.assertEqual(report["selected_device_number"], 2)
        for call in calls[1:]:
            self.assertEqual(call.args[0][1:3], ["-s", "PRIVATE"])

    def test_ambiguous_usb_targets_and_invalid_selection_do_not_query_phone(self):
        for number, reason in ((None, "multiple_devices"), (0, "invalid_device_selection"),
                (3, "invalid_device_selection"), (True, "invalid_device_selection")):
            with self.subTest(number=number):
                report, calls = self.inspect_with("ONE device usb:1-1\nTWO device usb:1-2\n",
                    device_number=number)
                self.assertEqual(report["reason"], reason)
                self.assertEqual(len(calls), 1)

    def test_command_timeout_is_a_safe_error(self):
        with patch.object(probe.subprocess, "run", side_effect=subprocess.TimeoutExpired("PRIVATE", 1)):
            self.assertEqual(probe.run(["adb", "devices"]), (None, "timeout"))

    def test_invalid_api_is_not_a_ready_phone(self):
        report, _ = self.inspect_with("PRIVATE device\n", [("Model", None), ("14", None), ("unknown", None)])
        self.assertEqual(report["reason"], "unexpected_android_api")
        self.assertEqual(report["status"], "device_not_ready")

    def test_report_atomic_publication_and_symlink_refusal(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            report = {"status": "device_not_ready"}
            probe.save(report, root)
            path = root / ".comic-session/mobile/preflight.json"
            self.assertEqual(json.loads(path.read_text()), report)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(list(path.parent.glob(".preflight-*")), [])
            path.unlink()
            outside = root / "outside.json"
            outside.write_text("KEEP")
            path.symlink_to(outside)
            with self.assertRaises(OSError):
                probe.save(report, root)
            self.assertEqual(outside.read_text(), "KEEP")


if __name__ == "__main__":
    unittest.main()
