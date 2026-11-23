import hashlib
import io
from pathlib import Path
import tempfile
import unittest
import zipfile

from scripts import android_wheelhouse


class AndroidWheelhouseTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.inputs = self.root / "inputs"
        self.inputs.mkdir()
        self.wheel = self.inputs / "example-1.0-py3-none-any.whl"
        self.versions = {"example": "1.0"}

    def write_wheel(self, dependency=""):
        with zipfile.ZipFile(self.wheel, "w") as package:
            package.writestr("example-1.0.dist-info/METADATA",
                             f"Name: example\nVersion: 1.0\n{dependency}\n")
        return {self.wheel.name: hashlib.sha256(self.wheel.read_bytes()).hexdigest()}

    def test_copies_locked_bytes_and_generates_only_pinned_requirements(self):
        lock = self.write_wheel()
        staged = self.root / "staged"
        android_wheelhouse.stage_wheels(self.inputs, staged, lock, self.versions)
        self.assertEqual((staged / "requirements.txt").read_text(), "example==1.0\n")
        self.assertEqual((staged / self.wheel.name).read_bytes(), self.wheel.read_bytes())

    def test_rejects_a_changed_byte_a_missing_wheel_and_an_extra_distribution(self):
        lock = self.write_wheel()
        self.wheel.write_bytes(self.wheel.read_bytes() + b"changed")
        with self.assertRaisesRegex(ValueError, "hash differs"):
            android_wheelhouse.stage_wheels(self.inputs, self.root / "changed", lock, self.versions)
        self.wheel.unlink()
        with self.assertRaisesRegex(ValueError, "exactly the wheels"):
            android_wheelhouse.stage_wheels(self.inputs, self.root / "missing", lock, self.versions)
        self.write_wheel()
        (self.inputs / "another-1.0-py3-none-any.whl").touch()
        with self.assertRaisesRegex(ValueError, "exactly the wheels"):
            android_wheelhouse.stage_wheels(self.inputs, self.root / "extra", lock, self.versions)

    def test_rejects_direct_urls_in_locked_metadata(self):
        lock = self.write_wheel("Requires-Dist: another @ https://packages.example/another.whl\n")
        with self.assertRaisesRegex(ValueError, "direct dependency URL"):
            android_wheelhouse.stage_wheels(self.inputs, self.root / "staged", lock, self.versions)

    def test_apk_distributions_match_exactly(self):
        apk_path = self.root / "app.apk"
        for names, accepted in ((["example-1.0"], True), (["example-2.0"], False),
                                (["example-1.0", "extra-1.0"], False), ([], False)):
            with self.subTest(names=names):
                buffer = io.BytesIO()
                with zipfile.ZipFile(buffer, "w") as common:
                    for name in names:
                        common.writestr(f"{name}.dist-info/METADATA", "")
                with zipfile.ZipFile(apk_path, "w") as apk:
                    apk.writestr("assets/chaquopy/requirements-common.imy", buffer.getvalue())
                if accepted:
                    android_wheelhouse.verify_apk_packages(apk_path, self.versions)
                else:
                    with self.assertRaisesRegex(ValueError, "differ from the Android wheel lock"):
                        android_wheelhouse.verify_apk_packages(apk_path, self.versions)
