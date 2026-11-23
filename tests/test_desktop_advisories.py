import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import audit_bundle, desktop_advisories


PROJECT = Path(__file__).resolve().parents[1]
DIGEST = "a" * 64


class DesktopAdvisoryTests(unittest.TestCase):
    def test_scans_union_and_reports_exact_pins_from_each_checked_in_manifest(self):
        snapshot = PROJECT / "fixture"
        with patch.object(desktop_advisories, "scan_packages",
                          return_value={"snapshot_sha256": DIGEST, "findings": []}) as scanner:
            report = desktop_advisories.audit(PROJECT, snapshot, DIGEST)
        self.assertEqual(set(report["manifests"]), {"linux", "macos", "windows"})
        packages, _, _ = scanner.call_args.args
        self.assertIn(("PyPI", "pip", report["manifests"]["macos"]["packages"]["pip"]), packages)
        self.assertEqual(report["manifests"]["linux"]["packages"]["pip"],
                         report["manifests"]["macos"]["packages"]["pip"])
        self.assertIn(("PyPI", "tzdata", report["manifests"]["windows"]["packages"]["tzdata"]), packages)
        self.assertNotIn("tzdata", report["manifests"]["linux"]["packages"])
        self.assertEqual(packages, {("PyPI", name, version) for row in report["manifests"].values()
                                    for name, version in row["packages"].items()})
        for platform, data in report["manifests"].items():
            source = PROJECT / "packaging" / f"{platform}-requirements.txt"
            self.assertEqual(data["sha256"], hashlib.sha256(source.read_bytes()).hexdigest())
        scanner.assert_called_once_with(packages, snapshot, DIGEST)

    def test_refuses_unhashed_ranges_duplicate_aliases_and_empty_manifests(self):
        pin = f"sample_name==1.0 --hash=sha256:{DIGEST}"
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "requirements.txt"
            for contents in ("sample_name==1.0", pin.replace("==", ">="),
                             pin + "\n" + pin.replace("sample_name", "sample-name"), "# Empty\n"):
                with self.subTest(contents=contents), self.assertRaises(ValueError):
                    path.write_text(contents, encoding="utf-8")
                    desktop_advisories.read_manifest(path)

    def test_packager_uses_approved_evidence_for_exact_installed_platform(self):
        manifests = desktop_advisories.read_manifests(PROJECT)
        approved = {"schema_version": 1, "snapshot_date": "2026-11-21", "snapshot_sha256": DIGEST,
                    "findings": [], "manifests": manifests,
                    "package_count": len(desktop_advisories.package_set(manifests))}
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            receipt = root / "receipt.json"
            selected = root / "requirements-frozen.txt"
            output = root / "dependency-audit.json"

            def save(report):
                body = json.dumps(report).encode()
                receipt.write_bytes(body)
                return hashlib.sha256(body).hexdigest()

            receipt_sha256 = save(approved)
            for platform, manifest in manifests.items():
                with self.subTest(platform=platform):
                    third_party = ''.join(f'{package}=={version}\n' for package, version in manifest["packages"].items())
                    selected.write_text('comic-archive==0.5.5\n' + third_party, encoding='utf-8')
                    env = {audit_bundle.RECEIPT_ENV: str(receipt), audit_bundle.RECEIPT_SHA256_ENV: receipt_sha256}
                    argv = ['audit_bundle.py', str(selected), '--output', str(output), '--platform', platform]
                    with patch.dict(os.environ, env), patch.object(audit_bundle.sys, 'argv', argv), \
                            patch.object(audit_bundle, 'audit') as live_audit:
                        audit_bundle.main()
                        live_audit.assert_not_called()
                    report = json.loads(output.read_text())
                    self.assertEqual(report["method"], "offline-osv")
                    self.assertEqual(report["manifest_sha256"], manifest["sha256"])
                    self.assertEqual(report["receipt_sha256"], receipt_sha256)
                    self.assertEqual({row["name"]: row["version"] for row in report["dependencies"]},
                                     manifest["packages"])

            for overrides in ({"findings": [{"advisory": "synthetic"}]}, {"snapshot_date": "unknown"},
                              {"package_count": 1}, {"manifests": {"windows": manifests["windows"]}}):
                with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                    digest = save(approved | overrides)
                    desktop_advisories.verify_bundle(PROJECT, 'windows', selected.read_text(), receipt, digest)
            receipt_sha256 = save(approved)
            with self.assertRaises(ValueError):
                desktop_advisories.verify_bundle(PROJECT, 'windows', selected.read_text(), receipt, '1' * 64)
            for text in (selected.read_text() + 'unknown==1.0\n', selected.read_text().replace('tzdata==', 'other==')):
                with self.subTest(installed=text), self.assertRaises(ValueError):
                    desktop_advisories.verify_bundle(PROJECT, 'windows', text, receipt, receipt_sha256)

    def test_incomplete_approval_does_not_fall_back_to_the_live_feed(self):
        with patch.dict(os.environ, {audit_bundle.RECEIPT_ENV: 'receipt.json'}, clear=True):
            with self.assertRaises(ValueError):
                audit_bundle.approved_receipt()
