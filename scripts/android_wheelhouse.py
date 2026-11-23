"""Verify the complete Python wheel set used to prepare an Android release."""
from email.parser import BytesParser
import hashlib
import io
import json
import re
import shutil
import zipfile


def load_lock(project):
    content = (project / "packaging/android/python-wheel-lock.json").read_bytes()
    wheels = json.loads(content)
    if not isinstance(wheels, dict) or not wheels:
        raise ValueError("The Android Python wheel lock is empty or invalid.")
    versions = {}
    for filename, digest in wheels.items():
        match = re.fullmatch(r"([a-z0-9_]+)-([0-9][a-z0-9.]*)-[a-z0-9_.]+-[a-z0-9_.]+-[a-z0-9_.]+\.whl", filename)
        if not match or not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise ValueError("The Android Python wheel lock contains an invalid filename or hash.")
        name, version = match.groups()
        if versions.setdefault(name, version) != version:
            raise ValueError("The Android Python wheel lock contains multiple versions of one distribution.")
    return hashlib.sha256(content).hexdigest(), wheels, versions


def stage_wheels(source, target, wheels, versions):
    """Copy only locked bytes into the private directory Chaquopy will search."""
    if {file.name for file in source.glob("*.whl")} != wheels.keys():
        raise ValueError("Supply exactly the wheels listed in the Android Python wheel lock.")
    target.mkdir(mode=0o700)
    for filename, expected in wheels.items():
        wheel = source / filename
        if wheel.is_symlink() or not wheel.is_file():
            raise ValueError(f"The locked wheel is not an ordinary file: {filename}")
        staged = target / filename
        shutil.copyfile(wheel, staged)
        with staged.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
                raise ValueError(f"Android Python wheel hash differs from the lock: {filename}")
        # --no-index disables indexes, but cannot disable a URL named directly in metadata.
        metadata_path = "-".join(filename.split("-")[:2]) + ".dist-info/METADATA"
        with zipfile.ZipFile(staged) as archive:
            try:
                metadata = BytesParser().parsebytes(archive.read(metadata_path), headersonly=True)
            except KeyError:
                raise ValueError(f"The locked wheel lacks its distribution metadata: {filename}") from None
        for dependency in metadata.get_all("Requires-Dist", []):
            if "@" in dependency or "://" in dependency:
                raise ValueError(f"Android Python wheel contains a direct dependency URL: {filename}")
    (target / "requirements.txt").write_text(
        "".join(f"{name}=={version}\n" for name, version in sorted(versions.items())), encoding="ascii")


def verify_apk_packages(apk, versions):
    """Chaquopy moves the distribution metadata for both ABIs into the common bundle."""
    found = []
    with zipfile.ZipFile(apk) as archive:
        for member in archive.infolist():
            if not re.fullmatch(r"assets/chaquopy/requirements-[\w-]+\.imy", member.filename):
                continue
            with zipfile.ZipFile(io.BytesIO(archive.read(member))) as bundle:
                found.extend(item.split("/")[0] for item in bundle.namelist() if item.endswith(".dist-info/METADATA"))
    expected = [f"{name}-{version}.dist-info" for name, version in versions.items()]
    if sorted(found) != sorted(expected):
        raise ValueError("The APK's Python distributions differ from the Android wheel lock.")
