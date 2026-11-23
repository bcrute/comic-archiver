"""Scan an approved Android dependency inventory with a dated offline OSV snapshot."""
import argparse
from datetime import date
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import zipfile

try:
    from scripts.android_dependency_graph import MAX_GRAPH_BYTES, RELEASE_CONFIGURATIONS, _coordinate
except ImportError:  # Run as scripts/android_advisories.py.
    from android_dependency_graph import MAX_GRAPH_BYTES, RELEASE_CONFIGURATIONS, _coordinate

SANDBOX = Path("/usr/bin/bwrap")
ECOSYSTEMS = ("Maven", "PyPI")
SHA256 = re.compile("[a-f0-9]{64}")


def _read_json(path, expected=None, *, limit=MAX_GRAPH_BYTES):
    with path.open("rb") as stream:
        content = stream.read(limit + 1)
    if not content or len(content) > limit:
        raise ValueError("A release advisory input is missing or oversized.")
    digest = hashlib.sha256(content).hexdigest()
    if expected is not None and digest != expected:
        raise ValueError("A release advisory input differs from its approved SHA-256.")
    value = json.loads(content)
    if not isinstance(value, dict):
        raise ValueError("A release advisory input must be a JSON object.")
    return value


def _digest(value):
    return isinstance(value, str) and SHA256.fullmatch(value) is not None


def _pypi_name(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def _package_set(graph, apk_sha256):
    configurations = graph.get("gradle_configurations")
    python = graph.get("python_packages")
    if (graph.get("schema_version") != 1 or graph.get("apk_sha256") != apk_sha256
            or not all(_digest(graph.get(key)) for key in ("wheel_lock_sha256", "gradle_verification_sha256"))
            or not isinstance(configurations, dict) or not isinstance(python, dict) or not python
            or any(not configurations.get(name) for name in RELEASE_CONFIGURATIONS)):
        raise ValueError("The dependency inventory is incomplete or belongs to another APK.")
    packages = set()
    for name, version in python.items():
        if not (_coordinate(name) and _coordinate(version)):
            raise ValueError("The inventory contains an invalid Python package.")
        packages.add(("PyPI", _pypi_name(name), version))
    for modules in configurations.values():
        if not isinstance(modules, list):
            raise ValueError("The inventory contains an invalid Maven configuration.")
        for module in modules:
            if not isinstance(module, list) or len(module) != 3 or not all(map(_coordinate, module)):
                raise ValueError("The inventory contains an invalid Maven package.")
            group, artifact, version = module
            packages.add(("Maven", f"{group}:{artifact}", version))
    return packages


def _snapshot_file(source, target, expected):
    if source.is_symlink() or not source.is_file() or not _digest(expected):
        raise ValueError(f"Missing or unapproved snapshot input: {source.name}.")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    with target.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
            raise ValueError(f"Snapshot input differs from its approved SHA-256: {source.name}.")


def _snapshot_details(snapshot):
    captured = snapshot.get("snapshot_date")
    databases = snapshot.get("databases")
    if (snapshot.get("schema_version") != 1 or not isinstance(captured, str)
            or not isinstance(databases, dict) or databases.keys() != set(ECOSYSTEMS)
            or date.fromisoformat(captured).isoformat() != captured):
        raise ValueError("A dated advisory snapshot must cover Maven and PyPI.")
    return captured, databases


def _result(value, expected):
    scanned, findings = set(), []
    try:
        for source in value["results"]:
            for entry in source["packages"]:
                package = entry["package"]
                ecosystem, name, version = (package[key] for key in ("ecosystem", "name", "version"))
                key = ecosystem, _pypi_name(name) if ecosystem == "PyPI" else name, version
                scanned.add(key)
                for advisory in entry.get("vulnerabilities") or ():
                    findings.append({"ecosystem": key[0], "package": key[1], "version": key[2],
                                     "advisory": advisory["id"]})
    except (AttributeError, KeyError, TypeError):
        raise ValueError("OSV-Scanner returned an invalid package report.") from None
    if scanned != expected:
        raise ValueError("OSV-Scanner did not report the complete expected package set.")
    return sorted(findings, key=lambda item: tuple(item.values()))


def scan(inventory, inventory_sha256, apk_sha256, snapshot_dir, snapshot_sha256):
    if not all(map(_digest, (inventory_sha256, apk_sha256, snapshot_sha256))):
        raise ValueError("Supply independently approved SHA-256 values for the inventory, APK and snapshot.")
    graph = _read_json(inventory, inventory_sha256)
    packages = _package_set(graph, apk_sha256)
    return {"apk_sha256": apk_sha256, "inventory_sha256": inventory_sha256,
            **scan_packages(packages, snapshot_dir, snapshot_sha256)}


def scan_packages(packages, snapshot_dir, snapshot_sha256):
    """Scan an exact package set using only the independently approved local database."""
    if not packages or not _digest(snapshot_sha256):
        raise ValueError("Supply an exact package set and an independently approved snapshot SHA-256.")
    snapshot = _read_json(snapshot_dir / "snapshot.json", snapshot_sha256, limit=16384)
    captured, databases = _snapshot_details(snapshot)
    with tempfile.TemporaryDirectory(prefix="android-advisories-") as directory:
        work = Path(directory)
        binary = work / "osv-scanner"
        _snapshot_file(snapshot_dir / binary.name, binary, snapshot.get("scanner_sha256"))
        binary.chmod(0o500)
        for ecosystem in ECOSYSTEMS:
            relative = Path("db") / "osv-scalibr" / ecosystem / "all.zip"
            archive = work / relative
            _snapshot_file(snapshot_dir / relative, archive, databases[ecosystem])
            with zipfile.ZipFile(archive) as db:
                if not any(member.endswith(".json") for member in db.namelist()):
                    raise ValueError(f"The offline {ecosystem} database has no advisories.")
        lockfile = {"results": [{"packages": [
            {"package": dict(ecosystem=ecosystem, name=name, version=version)}
            for ecosystem, name, version in sorted(packages)
        ]}]}
        (work / "osv-scanner.json").write_text(json.dumps(lockfile), encoding="utf-8")
        argv = [SANDBOX, "--unshare-all", "--die-with-parent", "--new-session", "--cap-drop", "ALL",
                "--clearenv", "--proc", "/proc", "--dev", "/dev", "--ro-bind", work, "/work",
                "--tmpfs", "/tmp", "--setenv", "HOME", "/tmp", "--setenv", "TMPDIR", "/tmp",
                "--setenv", "OSV_SCANNER_LOCAL_DB_CACHE_DIRECTORY", "/work/db", "--chdir", "/work",
                "--", "/work/osv-scanner", "scan", "source", "--offline", "--all-packages", "--format", "json",
                "--lockfile", "osv-scanner:/work/osv-scanner.json"]
        with (work / "result.json").open("wb") as output:
            completed = subprocess.run(argv, stdout=output, stderr=subprocess.DEVNULL, timeout=180, check=False)
        if completed.returncode not in (0, 1):
            raise ValueError(f"The offline OSV scan failed with exit code {completed.returncode}.")
        findings = _result(_read_json(work / "result.json"), packages)
        if bool(findings) != bool(completed.returncode):
            raise ValueError("The OSV-Scanner result disagrees with its exit status.")
    return {"snapshot_sha256": snapshot_sha256, "snapshot_date": captured,
            "package_count": len(packages), "findings": findings}


def verify_preparation(receipt, expected_sha256, apk_sha256, wheel_sha256, identity):
    """Bind independently approved unsigned preparation evidence to the APK before signing."""
    if not _digest(expected_sha256):
        raise ValueError("An independently approved preparation receipt digest is required.")
    data = _read_json(receipt, expected_sha256, limit=1024**2)
    phases = data.get("phases")
    required = ("python_wheel_lock", "build_toolchain", "gradle_verification", "dependency_inventory", "dependency_advisories",
                "release_prepare")
    if (data.get("verdict") != "unsigned_apk_ready" or not isinstance(phases, dict)
            or any(not isinstance(phases.get(phase), dict) or phases[phase].get("status") != "passed"
                   for phase in required)):
        raise ValueError("The approved preparation receipt has incomplete checks.")
    build = phases["release_prepare"]
    inventory = phases["dependency_inventory"]
    advisory = phases["dependency_advisories"]
    toolchain = phases["build_toolchain"]
    inventory_sha256 = inventory.get("sha256")
    gradle_sha256 = phases["gradle_verification"].get("sha256")
    snapshot_date = advisory.get("snapshot_date")
    if (build.get("identity") != identity or build.get("apk_sha256") != apk_sha256
            or phases["python_wheel_lock"].get("sha256") != wheel_sha256
            or advisory.get("apk_sha256") != apk_sha256 or advisory.get("findings") != []
            or not all(_digest(toolchain.get(key)) for key in ("jdk_sha256", "gradle_sha256", "python_sha256", "sdk_sha256"))
            or not _digest(gradle_sha256)
            or not _digest(inventory_sha256) or advisory.get("inventory_sha256") != inventory_sha256
            or not _digest(advisory.get("snapshot_sha256"))
            or type(advisory.get("package_count")) is not int or advisory["package_count"] < 2
            or not isinstance(snapshot_date, str) or date.fromisoformat(snapshot_date).isoformat() != snapshot_date):
        raise ValueError("The approved preparation receipt does not verify this APK and its dependency scan.")
    return {"gradle_verification_sha256": gradle_sha256,
            **{key: advisory[key] for key in ("inventory_sha256", "snapshot_sha256", "snapshot_date")}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", required=True, type=Path)
    parser.add_argument("--inventory-sha256", required=True)
    parser.add_argument("--apk-sha256", required=True)
    parser.add_argument("--snapshot", required=True, type=Path,
                        help="Approved directory containing snapshot.json, osv-scanner and db/osv-scalibr.")
    parser.add_argument("--snapshot-sha256", required=True, help="Approved SHA-256 of snapshot.json.")
    args = parser.parse_args()
    try:
        report = scan(args.inventory, args.inventory_sha256, args.apk_sha256, args.snapshot, args.snapshot_sha256)
    except (OSError, ValueError, subprocess.TimeoutExpired, zipfile.BadZipFile) as error:
        parser.exit(2, f"Android advisory check incomplete. {error}\n")
    print(json.dumps(report, indent=2))
    return 1 if report["findings"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
