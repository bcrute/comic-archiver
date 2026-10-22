"""Keep expensive fleet provisioning aligned with actual emulator capacity, without
turning missing capacity into a qualification pass. No SDK/network/build is run here.
"""
import unittest
from unittest.mock import patch

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

    def test_skipped_provisioning_still_leaves_the_required_in_app_gate_pending(self):
        report = {"phases": {name: {"status": "passed"} for name in feasibility.REQUIRED_PHASES}}
        with patch.object(feasibility, "report", report), patch.object(
                feasibility, "_emulator_capable", return_value=False), patch.object(
                feasibility, "run_tool") as run:
            feasibility.phase_in_app({})
            self.assertEqual(report["phases"]["in_app"]["status"], "pending")
            self.assertEqual(feasibility._verdict(), 3)
            run.assert_not_called()
