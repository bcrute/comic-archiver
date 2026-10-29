"""The release device matrix in scripts/android_device_matrix.py.

A row passes only on the observed API and page size, with the exact APK installed,
started, in front with embedded Python loaded and no crash of the app's own. The
emulator and adb are faked; no device or SDK is used.
"""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import android_device_matrix as matrix

IDENTITY = {"min_sdk": 29, "target_sdk": 36, "abis": ["arm64-v8a", "x86_64"]}
ROW = {"image": "system-images;android-36;google_apis_ps16k;x86_64", "api": 36, "page_size": 16384}
SCREEN = '<hierarchy><node text="Library" content-desc=""><node text="" content-desc="Settings"/></node></hierarchy>'


class FakeEmulator:
    def __init__(self, **changes):
        self.facts = {"api": "36", "getconf": "16384", "install": "Success", "start": "Status: ok", "pid": "4242",
                      "maps": "7f00-7f10 r-xp 0 00:00 1 /data/app/lib/x86_64/libpython3.13.so",
                      "focus": "  mCurrentFocus=Window{1 u0 io.github.bcrute.comicarchiver/.MainActivity}",
                      "crash": "", "type": "userdebug"} | changes
        self.calls = []

    def __call__(self, argv, timeout=None, keep_bytes=None, **_):
        argv = [str(part) for part in argv]
        self.calls.append(argv)
        verb, out = argv[3], ""
        command = argv[4:] if verb == "shell" else []
        if command[:2] == ["getprop", "ro.build.type"]:
            out = self.facts["type"]
        elif command[:2] == ["getprop", "ro.build.version.sdk"]:
            out = self.facts["api"]
        elif command[:2] == ["getprop", "ro.product.cpu.abi"]:
            out = "x86_64"
        elif command == ["getconf", "PAGE_SIZE"]:
            out = self.facts["getconf"]
        elif command == ["cat", "/proc/self/smaps"]:
            out = "Size:                  4 kB\nKernelPageSize:        4 kB\n"
        elif verb == "install":
            out = self.facts["install"]
        elif command[:2] == ["am", "start"]:
            out = self.facts["start"]
        elif command == ["id", "-u"]:
            out = self.facts.get("uid", "0")
        elif command[:1] == ["pidof"]:
            out = self.facts["pid"]
        elif command[:1] == ["cat"] and command[1].endswith("/maps"):
            out = self.facts["maps"]
        elif command[:3] == ["dumpsys", "activity", "exit-info"]:
            out = self.facts.get("exits", "")
        elif command[:2] == ["dumpsys", "window"]:
            out = self.facts["focus"]
        elif verb == "logcat" and "crash" in argv:
            out = self.facts["crash"]
        elif command == ["cat", "/sdcard/matrix-ui.xml"]:
            out = SCREEN
        return {"returncode": 0, "tail": out}


class ObserveTests(unittest.TestCase):
    def observe(self, **changes):
        fake = FakeEmulator(**changes)
        with tempfile.TemporaryDirectory() as folder, patch.object(matrix.time, "sleep"):
            return matrix.observe(Path("/sdk/adb"), "emulator-5554", Path("/apk"), ROW, Path(folder), fake), fake

    def test_a_running_app_with_python_loaded_on_the_expected_device_passes(self):
        row, _ = self.observe()
        self.assertTrue(row["passed"], row["checks"])
        self.assertEqual((row["api"], row["abi"], row["page_size"]), (36, "x86_64", 16384))
        self.assertEqual(row["screen_text"], ["Library", "Settings"])

    def test_each_missing_observation_fails_its_own_check(self):
        cases = {"page_size": {"getconf": "4096"}, "api": {"api": "35"}, "installed": {"install": "Failure"},
                 "launched": {"start": "Error: Activity not started"}, "running": {"pid": ""},
                 "python_loaded": {"maps": "7f00-7f10 r-xp 0 00:00 1 /system/lib64/libc.so"},
                 "in_front": {"focus": "  mCurrentFocus=Window{1 u0 com.android.launcher3/.Launcher}"},
                 "no_crash": {"crash": "FATAL EXCEPTION: main\nProcess: io.github.bcrute.comicarchiver"},
                 "userdebug": {"type": "user"}, "root": {"uid": "2000"}}
        for check, changes in cases.items():
            with self.subTest(check=check):
                row, _ = self.observe(**changes)
                self.assertFalse(row["passed"])
                self.assertFalse(row["checks"][check])

    def test_androids_exit_reasons_for_the_app_are_kept(self):
        exits = ("  package: io.github.bcrute.comicarchiver\n    ApplicationExitInfo #0:\n"
                 "      timestamp=2026-10-29 00:25:40.000 pid=4242 realUid=10190 packageUid=10190\n"
                 "      reason=3 (LOW_MEMORY) subreason=0 (UNKNOWN) status=0\n")
        row, _ = self.observe(exits=exits, pid="")
        self.assertEqual(row["exit_reasons"], ["LOW_MEMORY"])
        self.assertFalse(row["passed"])

    def test_the_exact_apk_is_installed_fresh_without_replacing_or_test_flags(self):
        _, fake = self.observe()
        installs = [argv for argv in fake.calls if argv[3] == "install"]
        self.assertEqual(installs, [["/sdk/adb", "-s", "emulator-5554", "install", "/apk"]])


class HelperTests(unittest.TestCase):
    def test_page_size_is_what_apps_see_with_the_kernels_only_without_getconf(self):
        smaps = "Size: 8 kB\nKernelPageSize:        4 kB\n"
        self.assertEqual(matrix.page_size("16384\n", smaps), 16384)
        self.assertEqual(matrix.page_size("/system/bin/sh: getconf: not found", smaps), 4096)
        self.assertIsNone(matrix.page_size("", "cat: /proc/self/smaps: No such file"))

    def test_python_shows_as_libpython_or_chaquopys_extracted_cpython_313_modules(self):
        maps = ("7f00-7f10 r-xp 00000000 fe:00 1 /data/app/~~x==/io.github.bcrute.comicarchiver-y==/base.apk\n"
                "7f10-7f20 r-xp 00000000 fe:00 2 /data/user/0/io.github.bcrute.comicarchiver/files/chaquopy/"
                "AssetFinder/stdlib-x86_64/_json.cpython-313-x86_64-linux-android.so\n"
                "7f20-7f30 r-xp 00000000 fe:00 3 /data/local/tmp/other.cpython-313.so\n"
                "7f30-7f40 rw-p 00000000 00:00 0 \n")
        self.assertEqual(matrix.python_mappings(maps), [
            "/data/user/0/io.github.bcrute.comicarchiver/files/chaquopy/AssetFinder/stdlib-x86_64/"
            "_json.cpython-313-x86_64-linux-android.so"])
        self.assertEqual(matrix.python_mappings("7f00-7f10 r-xp 0 00:00 1 /system/lib64/libc.so"), [])

    def test_unreadable_screen_dump_has_no_text(self):
        self.assertEqual(matrix.screen_text("ERROR: null root node"), [])

    def test_coverage_names_what_passed_rows_leave_open(self):
        rows = [{"api": 29, "abi": "x86_64", "page_size": 4096, "passed": True},
                {"api": 36, "abi": "x86_64", "page_size": 16384, "passed": True}]
        self.assertEqual(matrix.coverage(rows, IDENTITY), ["abi_arm64-v8a"])
        rows[1]["passed"] = False
        self.assertEqual(matrix.coverage(rows, IDENTITY),
                         ["abi_arm64-v8a", "api_35_plus", "page_size_16k_api_35_plus", "target_api"])


class EnvironmentTests(unittest.TestCase):
    def test_runs_only_as_a_fleet_job_of_the_android_project(self):
        with patch.dict(matrix.os.environ, {"CI": "true", "FLEET_STORAGE_PROJECT": "elsewhere"}), \
                self.assertRaisesRegex(ValueError, "comic-archiver/android"):
            matrix.run(Path("/apk"))


if __name__ == "__main__":
    unittest.main()
