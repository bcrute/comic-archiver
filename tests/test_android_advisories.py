import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from scripts import android_advisories as advisories
from scripts.android_dependency_graph import RELEASE_CONFIGURATIONS


class OfflineAdvisoryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.apk_digest = "a" * 64
        self.graph = self.root / "graph.json"
        self.graph.write_text(json.dumps({
            "schema_version": 1, "apk_sha256": self.apk_digest,
            "wheel_lock_sha256": "b" * 64, "gradle_verification_sha256": "c" * 64,
            "gradle_configurations": {name: [["org.fixture", "test", "2.0"]] for name in RELEASE_CONFIGURATIONS},
            "python_packages": {"python_example": "1.0"},
        }), encoding="utf-8")
        self.binary = self.root / "osv-scanner"
        self.binary.write_bytes(b"verified scanner fixture")
        self.db = {}
        for ecosystem in advisories.ECOSYSTEMS:
            archive = self.root / "db" / "osv-scalibr" / ecosystem / "all.zip"
            archive.parent.mkdir(parents=True)
            with zipfile.ZipFile(archive, "w") as db:
                db.writestr("OSV-fixture.json", "{}")
            self.db[ecosystem] = archive
        self.manifest = self.root / "snapshot.json"
        self.snapshot = {"schema_version": 1, "snapshot_date": "2026-11-01",
                         "scanner_sha256": self.digest(self.binary),
                         "databases": {name: self.digest(path) for name, path in self.db.items()}}
        self.save_manifest()

    @staticmethod
    def digest(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def save_manifest(self):
        self.manifest.write_text(json.dumps(self.snapshot), encoding="utf-8")

    def scan(self):
        return advisories.scan(self.graph, self.digest(self.graph), self.apk_digest,
                               self.root, self.digest(self.manifest))

    def scanner(self, argv, *, stdout, **kwargs):
        self.assertIn("--unshare-all", argv)
        self.assertIn("--clearenv", argv)
        self.assertIn("--offline", argv)
        self.assertIn("--all-packages", argv)
        self.assertEqual(argv[argv.index("--lockfile") + 1], "osv-scanner:/work/osv-scanner.json")
        work = argv[argv.index("--ro-bind") + 1]
        self.assertEqual(self.digest(work / self.binary.name), self.digest(self.binary))
        lockfile = json.loads((work / "osv-scanner.json").read_text())
        packages = lockfile["results"][0]["packages"]
        self.assertEqual(packages, [
            {"package": {"ecosystem": "Maven", "name": "org.fixture:test", "version": "2.0"}},
            {"package": {"ecosystem": "PyPI", "name": "python-example", "version": "1.0"}},
        ])
        return lockfile, packages

    def test_complete_offline_report_matches_verified_inventory_and_snapshot(self):
        def runner(argv, *, stdout, **kwargs):
            report, _ = self.scanner(argv, stdout=stdout, **kwargs)
            stdout.write(json.dumps(report).encode())
            return subprocess.CompletedProcess(argv, 0)

        with patch.object(advisories.subprocess, "run", side_effect=runner):
            report = self.scan()
        self.assertEqual(report["package_count"], 2)
        self.assertEqual(report["snapshot_date"], self.snapshot["snapshot_date"])
        self.assertEqual(report["inventory_sha256"], self.digest(self.graph))
        self.assertEqual(report["snapshot_sha256"], self.digest(self.manifest))
        self.assertEqual(report["findings"], [])

    def test_missing_ecosystem_and_unapproved_database_fail_before_running_scanner(self):
        original = self.snapshot["databases"].pop("Maven")
        self.save_manifest()
        with patch.object(advisories.subprocess, "run") as runner:
            with self.assertRaisesRegex(ValueError, "cover Maven and PyPI"):
                self.scan()
            self.snapshot["databases"]["Maven"] = original
            self.save_manifest()
            self.db["Maven"].write_bytes(b"unverified replacement")
            with self.assertRaisesRegex(ValueError, "differs from its approved SHA-256"):
                self.scan()
            runner.assert_not_called()

    def test_scanner_cannot_pass_after_omitting_a_required_package(self):
        def runner(argv, *, stdout, **kwargs):
            report, packages = self.scanner(argv, stdout=stdout, **kwargs)
            packages.pop()
            stdout.write(json.dumps(report).encode())
            return subprocess.CompletedProcess(argv, 0)

        with patch.object(advisories.subprocess, "run", side_effect=runner):
            with self.assertRaisesRegex(ValueError, "complete expected package set"):
                self.scan()
