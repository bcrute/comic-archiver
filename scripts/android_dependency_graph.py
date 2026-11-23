"""Build a release inventory from resolved Gradle modules and verified Python wheels."""
import hashlib
import json
import re

RELEASE_CONFIGURATIONS = {":classpath", ":app:releaseCompileClasspath", ":app:releaseRuntimeClasspath"}
MAX_GRAPH_BYTES = 16 * 1024 * 1024
COORDINATE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+\-]*")


def _coordinate(value):
    return isinstance(value, str) and COORDINATE.fullmatch(value) is not None


def build_inventory(resolutions, verified_modules, python_versions, *, wheel_sha256, gradle_sha256, apk_sha256):
    with resolutions.open("rb") as source:
        raw = source.read(MAX_GRAPH_BYTES + 1)
    if not raw or len(raw) > MAX_GRAPH_BYTES:
        raise ValueError("Missing or oversized Gradle dependency resolution inventory.")

    configurations = {}
    for line in raw.splitlines():
        entry = json.loads(line)
        if (not isinstance(entry, dict) or not isinstance(entry.get("configuration"), str)
                or not entry["configuration"].startswith(":") or type(entry.get("unresolved")) is not int
                or entry["unresolved"] != 0 or not isinstance(entry.get("modules"), list)):
            raise ValueError("The Gradle dependency inventory contains incomplete resolution records.")
        modules = configurations.setdefault(entry["configuration"], set())
        for coordinate in entry["modules"]:
            if (not isinstance(coordinate, list) or len(coordinate) != 3
                    or not all(_coordinate(part) for part in coordinate)):
                raise ValueError("The Gradle dependency inventory contains an invalid module coordinate.")
            module = tuple(coordinate)
            if module not in verified_modules:
                raise ValueError("The Gradle dependency inventory includes a module missing from verification metadata.")
            modules.add(module)

    if any(not configurations.get(name) for name in RELEASE_CONFIGURATIONS):
        raise ValueError("The Gradle dependency inventory is missing a required release configuration.")
    if not python_versions or not all(_coordinate(name) and _coordinate(version)
                                      for name, version in python_versions.items()):
        raise ValueError("The release dependency inventory is missing verified Python packages.")

    return {
        "schema_version": 1,
        "apk_sha256": apk_sha256,
        "wheel_lock_sha256": wheel_sha256,
        "gradle_verification_sha256": gradle_sha256,
        "python_packages": dict(sorted(python_versions.items())),
        "gradle_configurations": {name: sorted(modules) for name, modules in sorted(configurations.items())},
    }


def inventory_bytes(inventory):
    content = (json.dumps(inventory, sort_keys=True, indent=2) + "\n").encode("utf-8")
    return content, hashlib.sha256(content).hexdigest()
