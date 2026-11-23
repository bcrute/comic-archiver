"""Scan the three exact desktop release manifests with a dated offline OSV snapshot."""
import argparse
from datetime import date
import hashlib
import json
from pathlib import Path
import re
import subprocess
import zipfile

try:
    from .android_advisories import _digest, _read_json, scan_packages
    from .audit_bundle import canonical, dependencies
except ImportError:  # Run as scripts/desktop_advisories.py.
    from android_advisories import _digest, _read_json, scan_packages
    from audit_bundle import canonical, dependencies


PLATFORMS = ("linux", "macos", "windows")
PIN = re.compile(r"([A-Za-z0-9][A-Za-z0-9_.-]*)==([A-Za-z0-9][A-Za-z0-9_.+!-]*)"
                 r"(?:[ \t]+--hash=sha256:[a-f0-9]{64})+")


def read_manifest(path):
    content = path.read_bytes()
    logical_lines = "\n".join(line for line in content.decode("utf-8").splitlines()
                              if not line.lstrip().startswith("#")).replace("\\\n", "").splitlines()
    pins = {}
    for line in logical_lines:
        if not line.strip():
            continue
        match = PIN.fullmatch(line.strip())
        if match is None:
            raise ValueError(f"{path.name} contains a package without an exact version and SHA-256.")
        name, version = canonical(match[1]), match[2]
        if name in pins:
            raise ValueError(f"{path.name} contains a duplicate package.")
        pins[name] = version
    if not pins:
        raise ValueError(f"{path.name} is empty.")
    return {"sha256": hashlib.sha256(content).hexdigest(), "packages": dict(sorted(pins.items()))}


def read_manifests(project):
    return {platform: read_manifest(project / "packaging" / f"{platform}-requirements.txt")
            for platform in PLATFORMS}


def package_set(manifests):
    return {("PyPI", name, version) for manifest in manifests.values()
            for name, version in manifest["packages"].items()}


def audit(project, snapshot, snapshot_sha256):
    manifests = read_manifests(project)
    return {"schema_version": 1, "manifests": manifests,
            **scan_packages(package_set(manifests), snapshot, snapshot_sha256)}


def verify_bundle(project, platform, requirements, receipt, receipt_sha256):
    """Use an independently approved scanner receipt for the exact installed bundle."""
    if platform not in PLATFORMS or not _digest(receipt_sha256):
        raise ValueError("Supply a desktop platform and independently approved audit receipt SHA-256.")
    report = _read_json(receipt, receipt_sha256, limit=256 * 1024)
    manifests = read_manifests(project)
    snapshot_date = report.get("snapshot_date")
    if (type(report.get("schema_version")) is not int or report["schema_version"] != 1
            or report.get("manifests") != manifests or not _digest(report.get("snapshot_sha256"))
            or not isinstance(snapshot_date, str)
            or date.fromisoformat(snapshot_date).isoformat() != snapshot_date
            or type(report.get("package_count")) is not int
            or report["package_count"] != len(package_set(manifests)) or report.get("findings") != []):
        raise ValueError("The approved desktop audit receipt does not cover the current clean manifests.")
    installed = sorted((canonical(name), version) for name, version in
                       (line.split("==", 1) for line in dependencies(requirements).splitlines()))
    manifest = manifests[platform]
    if installed != sorted(manifest["packages"].items()):
        raise ValueError("The installed desktop packages differ from the approved manifest.")
    return {
        "method": "offline-osv",
        "platform": platform,
        "receipt_sha256": receipt_sha256,
        "snapshot_date": snapshot_date,
        "snapshot_sha256": report["snapshot_sha256"],
        "manifest_sha256": manifest["sha256"],
        "dependencies": [{"name": name, "version": version, "vulns": []} for name, version in installed],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--snapshot-sha256", required=True)
    args = parser.parse_args()
    try:
        project = Path(__file__).resolve().parents[1]
        report = audit(project, args.snapshot, args.snapshot_sha256)
    except (OSError, ValueError, subprocess.TimeoutExpired, zipfile.BadZipFile) as error:
        parser.exit(2, f"Desktop advisory check incomplete. {error}\n")
    print(json.dumps(report, indent=2))
    return 1 if report["findings"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
