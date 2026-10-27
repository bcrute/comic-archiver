#!/usr/bin/env python3
"""Android fleet builds. The default dev-apk mode builds one phone APK from retained
dependency wheels, with only the required toolchain and the existing debug signer.
It runs no test suite, emulator, wheel build or release qualification.

Explicit --phase all is final qualification after feature parity: toolchain preflight,
pinned zstandard sdist, cp313 Android wheels built from that sdist with cibuildwheel (the builder
Chaquopy's own documentation names for Python 3.13+), whole-app Gradle build installing the
wheels through --find-links plus an exact requirement, and the in-app self-check on a
job-owned emulator. Runs ONLY on a fleet host through the CI fleet helper, never locally.

Inputs: every downloaded archive is pinned in packaging/android/toolchain-pins.json with a
committed sha256 that is verified before extraction. The host buildPython is a task-private
CPython 3.13 matching the app's embedded 3.13 major.minor (Chaquopy requirement), wired to
Gradle explicitly; the host's own 3.14 is never used.

Isolation: any missing tool is provisioned under android/.fleet-sdk (gitignored, job-private,
never host-global). The debug signing identity is managed the same way: one keystore per job,
injected from owner-private config or generated and retained as a private artifact, never a
secret inside the repo or its snapshot. The emulator phase creates its own AVD under a job-private
ANDROID_AVD_HOME, targets one adb serial for every command, uses a private port, and tears
down only the emulator and AVD it created. No host AVD, device or global state is touched.

Verdict: computed from every selected required phase. failed => exit 1, pending (capability
missing and not provisionable, e.g. no KVM) => exit 3, and 0 only when all required evidence
passed. A tool's return code alone is never qualification. Artifacts are retained on failure
as on success.
"""
import argparse
import base64
import binascii
import hashlib
import http.client
import io
import json
import os
import pathlib
import re
import shutil
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
import urllib.request
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET
import zipfile

REPO = pathlib.Path(__file__).resolve().parent.parent
SDIST_DIR = REPO / "packaging/android/zstandard-wheel"
PINS = json.loads((REPO / "packaging/android/toolchain-pins.json").read_text())
for _name, _pin in PINS.items():
    if isinstance(_pin, dict) and "sha256" in _pin \
            and not re.fullmatch(r"[0-9a-f]{64}", _pin["sha256"]):
        raise SystemExit(f"toolchain-pins.json: {_name} has no usable sha256 digest")
PINNED_SDIST = PINS["zstandard"]["sdist"]
PINNED_SHA256 = PINS["zstandard"]["sha256"]
PYPI_JSON = PINS["zstandard"]["pypi_json"]
WHEEL_ABI_ARCHS = ("arm64_v8a", "x86_64")
APK = REPO / "android/app/build/outputs/apk/debug/app-debug.apk"
TEST_APK = REPO / "android/app/build/outputs/apk/androidTest/debug/app-debug-androidTest.apk"
JOB_DIR = REPO / "android/.fleet-job"
TOOLS_DIR = REPO / "android/.fleet-sdk"
# One managed debug signing identity per job: an owner-private injected keystore
# (COMIC_ARCHIVER_DEBUG_KEYSTORE_B64, never in the repo or snapshot) or, with
# nothing injected, a NEW dedicated project debug key generated on the job and
# retained as a 0600 private artifact for the owner to keep injecting later.
DEBUG_KEYSTORE_ENV = "COMIC_ARCHIVER_DEBUG_KEYSTORE_B64"
DEBUG_CERT_SHA256_ENV = "COMIC_ARCHIVER_DEBUG_KEYSTORE_CERT_SHA256"
DEBUG_KEYSTORE = TOOLS_DIR / "debug-signing/debug.keystore"
SELF_IDENTITY = "comic-archiver/android-selfcheck/1"
SELF_TAG = "ComicArchiverSelfcheck"
APP_ACTIVITY = "io.github.bcrute.comicarchiver/.MainActivity"
ABI_FILTERS = {"arm64-v8a", "x86_64"}
report = {"phases": {}, "argv": sys.argv[1:], "verdict": None}
CI_DOWNLOAD_MIRROR_ENV = "COMIC_ARCHIVER_CI_DOWNLOAD_MIRROR"

# This optional job-private init script routes the project's existing public
# repositories through the configured acquiring cache, without a worker proxy.
# Register before settings/plugins resolve, and include later plugin repositories.
CI_PACKAGE_MIRROR_INIT = '''import org.gradle.api.artifacts.repositories.MavenArtifactRepository
// Prefer the native /HTTPS/ acquiring frontend: it has no significant repeated
// slashes for Apache HttpClient to normalize. Retain the legacy base spelling.
def mirror = System.getenv("COMIC_ARCHIVER_CI_DOWNLOAD_MIRROR")?.replace("/HTTPS///", "/HTTPS/%2F%2F")
def route = { repository ->
    if (repository instanceof MavenArtifactRepository) {
        def source = repository.url
        if (source?.scheme == "https") {
            if (source.userInfo != null) throw new GradleException("CI package sources must be public")
            // The fleet's native init.d hooks may already have selected its
            // Maven or artifact endpoint. Do not wrap a cache URL in itself.
            if (source.host == new URI(mirror).host) return
            // The qualified image maps Central to its dedicated Maven cache.
            // Generic acquisition rejects runtime artifacts such as .imy.
            if (source.host in ["repo.maven.apache.org", "repo1.maven.org"]) return
            repository.url = mirror + source.toString().substring(8)
            repository.allowInsecureProtocol = mirror.startsWith("http://")
        }
    }
}
gradle.beforeSettings { settings ->
    settings.pluginManagement.repositories.all(route)
    settings.dependencyResolutionManagement.repositories.all(route)
}
gradle.beforeProject { project ->
    project.buildscript.repositories.all(route)
    project.repositories.all(route)
}
'''


def record(phase, status, **facts):
    report["phases"][phase] = {"status": status, **facts}
    print(f"[{phase}] {status}" + (f": {facts}" if facts else ""), flush=True)


def run_tool(argv, cwd=None, timeout=2400, env_extra=None, input_bytes=None, keep_bytes=4000):
    started = time.monotonic()
    env = {**os.environ, **(env_extra or {})}
    try:
        completed = subprocess.run([str(piece) for piece in argv], cwd=cwd, capture_output=True,
                                   text=True, timeout=timeout, env=env, input=input_bytes)
        code, out = completed.returncode, completed.stdout + completed.stderr
    except subprocess.TimeoutExpired as expired:
        code, out = 124, str(expired)[:2000]
    except FileNotFoundError:
        # A missing executable (spawn ENOENT) is an observed tool failure, never a crash.
        code, out = 127, "executable not found: " + str(argv[0])
    except OSError as error:
        # A spawn refusal (permissions) is an observed tool failure too, not a crash.
        code, out = 126, f"cannot run {argv[0]}: {error}"
    return {"argv": [str(piece) for piece in argv], "minutes": round((time.monotonic() - started) / 60, 1),
            "returncode": code, "tail": out[-keep_bytes:]}


def _ci_download_mirror():
    base = os.environ.get(CI_DOWNLOAD_MIRROR_ENV, "").strip()
    if not base:
        return None
    parsed = urlsplit(base)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment
            or not base.endswith(("/HTTPS/", "/HTTPS///"))
            or any(ord(c) <= 32 for c in base)):
        raise ValueError("Supply a credential-free CI acquiring-cache base ending /HTTPS/")
    return base


def _acquisition_url(url):
    base = _ci_download_mirror()
    if base is None:
        return url
    source = urlsplit(url)
    if source.scheme != "https" or not source.hostname or source.username is not None or source.password is not None:
        raise ValueError("CI acquiring-cache inputs must be public HTTPS URLs")
    return base + url.split("://", 1)[1]


def _download(url, target, timeout=1800):
    if target.is_file():
        return True
    target.parent.mkdir(parents=True, exist_ok=True)
    source = _acquisition_url(url)
    deadline = time.monotonic() + timeout
    temporary = None
    transfer = {"file": target.name, "attempts": 0, "received_bytes": 0, "complete": False}
    try:
        # Keep interrupted bytes private. Each retry requests only the missing
        # suffix from the original stable URL, not a retained signed redirect.
        # The caller still verifies its original vendor digest before extraction.
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix=target.name + ".",
                                         suffix=".part", delete=False) as output:
            temporary = pathlib.Path(output.name)
            validator = None
            total = None
            complete = False
            for _attempt in range(32):
                transfer["attempts"] += 1
                start = output.tell()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                headers = {"Accept-Encoding": "identity"}
                if start:
                    headers["Range"] = f"bytes={start}-"
                    if validator:
                        headers["If-Range"] = validator
                try:
                    request = urllib.request.Request(source, headers=headers)
                    with urllib.request.urlopen(request, timeout=min(30, remaining)) as response:
                        length = response.headers.get("Content-Length")
                        length = int(length) if length is not None else None
                        if length is not None and length < 0:
                            return False
                        if response.status == 206:
                            extent = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)",
                                                  response.headers.get("Content-Range", ""))
                            if extent is None:
                                return False
                            first, last, size = map(int, extent.groups())
                            if (first != start or not first <= last < size
                                    or (total is not None and size != total)
                                    or (length is not None and length != last - first + 1)):
                                return False
                            total = size
                        elif response.status == 200:
                            # If-Range may reject a changed resource, or a server
                            # may ignore Range. A complete response replaces the
                            # private prefix instead of being appended to it.
                            output.seek(0)
                            output.truncate()
                            total = length
                        else:
                            return False
                        etag = response.headers.get("ETag")
                        validator = (etag if etag and not etag.startswith("W/") else
                                     response.headers.get("Last-Modified"))
                        while time.monotonic() < deadline:
                            chunk = response.read(256 * 1024)
                            if not chunk:
                                break
                            output.write(chunk)
                            transfer["received_bytes"] += len(chunk)
                        else:
                            return False
                        if output.tell() and (total is None or output.tell() == total):
                            complete = True
                            break
                except (OSError, http.client.HTTPException):
                    # No bytes means a refusal/stalled endpoint, not progress.
                    # In particular, do not hammer a permanent 403 or 404.
                    pass
                if output.tell() <= start or (total is not None and output.tell() > total):
                    return False
        if complete:
            temporary.replace(target)
            transfer["complete"] = True
            return True
        return False
    except (OSError, ValueError):
        return False
    finally:
        report.setdefault("download_transfers", []).append(transfer)
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _fetch_pinned(name, target):
    """Download one pinned input and verify its committed sha256 BEFORE any extraction.

    An already-downloaded bundle is re-verified too, so a cached file is as pinned as a
    fresh one. The digest mismatch case is an observed failure with the digest seen.
    """
    pin = PINS[name]
    try:
        if not target.is_file():
            if not _download(pin["url"], target):
                return False, "download failed"
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
    except OSError:
        return False, "read failed"
    if digest != pin["sha256"]:
        target.unlink(missing_ok=True)
        return False, f"sha256 mismatch: {digest}"
    return True, pin["sha256"]


def _extract_tar_data(archive, destination):
    """Unpack an untrusted tarball with the data filter: no absolute paths, links out, or devices."""
    destination.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(archive) as bundle:
            bundle.extractall(destination, filter="data")
        return True
    except (OSError, tarfile.TarError, ValueError):
        return False


def _extract_zip_data(archive, destination):
    """Unpack the pinned zip member by member: every name is kept inside destination, no
    extractall, the SDK's bin/ scripts keep an executable mode even when the archive
    records none (the observed failure: sdkmanager spawned EACCES at mode 0o644), and
    symlinks are restored as links with relative, contained targets."""
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    try:
        with zipfile.ZipFile(archive) as bundle:
            for member in bundle.infolist():
                resolved = (destination / member.filename).resolve()
                if resolved != root and root not in resolved.parents:
                    return False
                if member.is_dir():
                    resolved.mkdir(parents=True, exist_ok=True)
                    continue
                resolved.parent.mkdir(parents=True, exist_ok=True)
                stored = member.external_attr >> 16
                if stat.S_ISLNK(stored):
                    # A zip symlink's content is its target path. The NDK is full of
                    # them (bin/clang -> clang-18; observed attempt 13: flattened to an
                    # 8-byte text file, bash "ran" it, the cross-compile died at
                    # "clang-18: command not found"). Absolute or escaping targets are
                    # refused like any member that would write outside destination.
                    target_text = bundle.read(member).decode("utf-8", "replace")
                    normalized = os.path.normpath(
                        os.path.join(os.path.dirname(member.filename), target_text))
                    if not target_text or os.path.isabs(target_text) \
                            or normalized.startswith(".." + os.path.sep) or normalized == "..":
                        return False
                    resolved.unlink(missing_ok=True)
                    os.symlink(target_text, resolved)
                    continue
                with bundle.open(member) as source, open(resolved, "wb") as target:
                    shutil.copyfileobj(source, target)
                executable = "bin" in resolved.parts and resolved.suffix not in {".jar", ".txt"}
                os.chmod(resolved, stored if stored & 0o111 else 0o755 if executable else 0o644)
        return True
    except (OSError, zipfile.BadZipFile, ValueError):
        return False


REPOSITORY_XMLS = (
    # The main repository carries tools, platforms and the NDK; system images live only in
    # their own listings, whose artifact URLs are relative to the listing's own folder.
    # Digest and URL shapes verified against the live XMLs, 21 October 2026.
    "https://dl.google.com/android/repository/repository2-3.xml",
    # Schema1 omits current stable major.minor images and leaves native browser
    # checks on API36's WebView133. Schema3 retains those older packages too.
    "https://dl.google.com/android/repository/sys-img/google_apis/sys-img2-3.xml",
    "https://dl.google.com/android/repository/sys-img/android/sys-img2-1.xml",
)


def _emulator_local_metadata(package, repository):
    """Retain vendor revision/license metadata that AVD Manager needs after unzip."""
    xsi = "{http://www.w3.org/2001/XMLSchema-instance}type"
    details = package.find("{*}type-details")
    if details is None or details.get(xsi) != "generic:genericDetailsType":
        raise ValueError("Unknown emulator package type")
    local = ET.Element("localPackage", {"path": "emulator", "obsolete": "false"})
    for tag in ("type-details", "revision", "display-name", "uses-license", "dependencies"):
        child = package.find("{*}" + tag)
        if child is not None:
            local.append(ET.fromstring(ET.tostring(child)))  # noqa: S314 - fixed vendor XML
    revision = local.find("revision")
    if revision is None:
        raise ValueError("Missing emulator revision")
    version = tuple(int(revision.findtext(name, "0")) for name in ("major", "minor", "micro"))
    document = ET.Element("{http://schemas.android.com/repository/android/common/02}repository",
                          {"xmlns:generic": "http://schemas.android.com/repository/android/generic/02"})
    license_ref = local.find("uses-license")
    if license_ref is not None:
        license_node = next((node for node in repository.findall("{*}license")
                             if node.get("id") == license_ref.get("ref")), None)
        if license_node is None:
            raise ValueError("Missing emulator license")
        document.append(ET.fromstring(ET.tostring(license_node)))  # noqa: S314 - fixed vendor XML
    document.append(local)
    return ET.tostring(document, encoding="unicode"), version


def _install_emulator_metadata(details, target):
    if (target / "package.xml").is_file():
        return True
    try:
        source = (target / "source.properties").read_text()
        match = re.search(r"^\s*Pkg.Revision\s*=\s*(\d+(?:\.\d+){0,2})\s*$", source, re.MULTILINE)
        if match is None:
            return False
        observed = tuple(int(value) for value in match[1].split("."))
        if (observed + (0, 0, 0))[:3] != tuple(details["revision"]):
            return False
        (target / "package.xml").write_text(details["local_package_xml"], encoding="utf-8")
        return True
    except (KeyError, OSError, ValueError):
        return False


def _sdk_repository_packages():
    """The repository's own stable package table, fetched over the same TLS path every
    pinned input already uses, optionally through the CI acquiring cache.
    Two fleet attempts (4 and 5) showed sdkmanager's JVM
    fetching no manifests at all on this network even with explicit proxy properties,
    while urllib reached dl.google.com without complaint; so the JVM is never used to
    fetch. Each package's published sha1 is returned for verification before extraction.
    """
    packages = {}
    for listing in REPOSITORY_XMLS:
        try:
            with urllib.request.urlopen(_acquisition_url(listing), timeout=120) as response:
                # Direct TLS or the configured cache's upstream TLS; every artifact
                # retains its published sha1. Keep original source URLs in the table.
                root = ET.fromstring(response.read())  # noqa: S314 - fixed origin, verified artifacts
        except (OSError, ET.ParseError):
            return None
        folder = listing.rsplit("/", 1)[0]
        for package in root.findall("{*}remotePackage"):
            path = package.get("path", "")
            channel = package.find("{*}channelRef")
            if channel is not None and channel.get("ref") not in (None, "channel-0"):
                continue  # Stable channel only; preview packages are never selected silently.
            for archive in package.findall("{*}archives/{*}archive"):
                host = archive.find("{*}host-os")
                if host is not None and (host.text or "").strip() != "linux":
                    continue
                # The main listing types its digest checksum[type=sha1]; the sys-img
                # listings carry it untyped (sha1 by definition of the schema).
                sha1 = archive.find("{*}complete/{*}checksum[@type='sha1']")
                if sha1 is None:
                    sha1 = archive.find("{*}complete/{*}checksum")
                artifact = archive.find("{*}complete/{*}url")
                if sha1 is not None and artifact is not None:
                    packages[path] = {"sha1": sha1.text.strip(),
                                      "url": f"{folder}/{artifact.text.strip()}"}
                    if path == "emulator":
                        try:
                            metadata, revision = _emulator_local_metadata(package, root)
                        except ValueError:
                            return None
                        packages[path].update(local_package_xml=metadata, revision=revision)
                    break
    return packages


def _install_sdk_package(name, details, sdk):
    """Install one repository package: download over the Python network path, verify the
    repository's own sha1 before extraction, unpack with the validated extractor, and
    rename the archive's single top-level folder to the package's canonical SDK path
    (the zips' root folder names — android-37, android-ndk-r29, x86_64 — have never
    matched the install path; package;path segments do)."""
    target = sdk.joinpath(*name.split(";"))
    if target.is_dir():
        if name == "emulator" and not _install_emulator_metadata(details, target):
            return False, "emulator package metadata missing or revision mismatched"
        return True, "cached"
    bundle = TOOLS_DIR / f"downloads/{name.replace(';', '-')}.zip"
    try:
        # One retry: the 1.5 GB system-image zip has been lost to a single reset mid-run
        # (observed attempt 9), and a re-fetch is cheaper than re-running the job.
        if not _download(details["url"], bundle):
            bundle.unlink(missing_ok=True)
            if not _download(details["url"], bundle):
                return False, "download failed"
        # SHA-1 is the repository's own integrity digest, not a security primitive here.
        digest = hashlib.sha1(bundle.read_bytes(), usedforsecurity=False).hexdigest()
    except OSError:
        return False, "read failed"
    if digest != details["sha1"]:
        bundle.unlink(missing_ok=True)
        return False, f"sha1 mismatch: {digest}"
    staging = TOOLS_DIR / f"downloads/unpack-{name.replace(';', '-')}"
    shutil.rmtree(staging, ignore_errors=True)
    if not _extract_zip_data(bundle, staging):
        return False, "unpack refused"
    inner = [path for path in staging.iterdir() if path.is_dir()]
    if len(inner) != 1:
        return False, "unexpected archive layout"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(target, ignore_errors=True)
    inner[0].rename(target)
    shutil.rmtree(staging, ignore_errors=True)
    if name == "emulator" and not _install_emulator_metadata(details, target):
        return False, "emulator package metadata missing or revision mismatched"
    return True, details["sha1"]


def _jvm_net_options(java):
    """-D flags that let a JVM reach the network on this fleet: the proxy from the
    environment (Java ignores http_proxy/https_proxy) and a truststore holding the
    system's CA set. The proxy re-signs TLS with a CA the system OpenSSL store trusts —
    every Python download works — while the JDK's own cacerts does not (observed:
    sdkmanager fetching no manifests on attempts 4-5; Gradle resolving no plugin from
    google/mavenCentral for 48 minutes on attempt 10). Only raw-JVM downloads need
    these; cibuildwheel's Python-side Maven fetch worked without them.
    """
    from urllib.parse import urlsplit
    flags = ["-Djava.net.preferIPv4Stack=true"]
    for name, scheme in (("http_proxy", "http"), ("https_proxy", "https")):
        value = os.environ.get(name) or os.environ.get(name.upper())
        if value:
            parsed = urlsplit(value if "//" in value else "//" + value)
            if parsed.hostname:
                flags.append(f"-D{scheme}.proxyHost={parsed.hostname}")
                if parsed.port:
                    flags.append(f"-D{scheme}.proxyPort={parsed.port}")
    truststore, store_type = _jvm_truststore(java)
    if truststore is not None:
        flags.append(f"-Djavax.net.ssl.trustStore={truststore}")
        flags.append(f"-Djavax.net.ssl.trustStoreType={store_type}")
        flags.append("-Djavax.net.ssl.trustStorePassword=changeit")
    return flags


def _jvm_truststore(java):
    """A JVM-readable copy of the system CA set: the distro's own java cacerts when it
    exists, else one built with the pinned JDK's keytool from /etc/ssl/certs. The store
    type is read from the file's magic (JKS starts FEEDFEED; PKCS12 is an ASN.1
    SEQUENCE). None when nothing to build from — the flags are then simply omitted
    and the failure remains the JVM's own words in the receipt."""
    system = pathlib.Path("/etc/ssl/certs/java/cacerts")
    if system.is_file():
        return str(system), ("JKS" if system.read_bytes()[:4] == b"\xfe\xed\xfe\xed" else "PKCS12")
    target = TOOLS_DIR / "jssecacerts"
    if target.is_file():
        return str(target), "JKS"
    certs = sorted(pathlib.Path("/etc/ssl/certs").glob("*.pem"))
    if not certs or java is None:
        return None, None
    keytool = pathlib.Path(java).parent / "keytool"
    if not keytool.is_file():
        return None, None
    target.parent.mkdir(parents=True, exist_ok=True)
    target.unlink(missing_ok=True)
    for index, cert in enumerate(certs):
        run_tool([str(keytool), "-importcert", "-noprompt", "-storetype", "JKS",
                  "-alias", f"fleet-{index}", "-file", str(cert),
                  "-keystore", str(target), "-storepass", "changeit"], timeout=120)
    return (str(target), "JKS") if target.is_file() else (None, None)



def _emulator_capable():
    return os.access("/dev/kvm", os.R_OK | os.W_OK)


def _sdk_build_packages(names, *, emulator_capable):
    """Provision build inputs always; emulator inputs only on a usable fleet host."""
    names = set(names)
    platforms = sorted((name for name in names if re.fullmatch(r"platforms;android-\d+", name)),
                       key=lambda name: int(name.rsplit("-", 1)[1]))
    ndks = sorted((name for name in names if re.fullmatch(r"ndk;\d+(\.\d+)+", name)),
                  key=lambda name: tuple(int(piece) for piece in name.split(";")[1].split(".")))
    build_tools = sorted((name for name in names if re.fullmatch(r"build-tools;\d+(\.\d+){1,2}", name)),
                         key=lambda name: tuple(int(piece) for piece in name.split(";")[1].split(".")))
    wanted = ["platform-tools"]
    wanted += [group[-1] for group in (platforms, ndks, build_tools) if group]
    if emulator_capable:
        images = sorted((name for name in names
                         if re.fullmatch(r"system-images;android-\d+(?:\.\d+)?;[a-z0-9_]+;x86_64", name)),
                        key=lambda name: (tuple(int(part) for part in name.split(";")[1][8:].split(".")), name))
        images = [name for name in images if "ps16k" not in name] or images
        wanted.append("emulator")
        if images:
            wanted.append(images[-1])
    # AGP also requires this exact revision, independent of the newest build tools.
    if "build-tools;35.0.0" in names and "build-tools;35.0.0" not in wanted:
        wanted.append("build-tools;35.0.0")
    return wanted


def _development_sdk_packages(names, build_script):
    """Only the SDK inputs the app actually declares; no emulator or newest-NDK sweep."""
    compile_sdk = re.search(r"^\s*compileSdk\s*=\s*(\d+)", build_script, re.MULTILINE)
    ndk = re.search(r'^\s*ndkVersion\s*=\s*"([\d.]+)"', build_script, re.MULTILINE)
    if compile_sdk is None or ndk is None:
        raise ValueError("Development builds require explicit compileSdk and ndkVersion")
    # This is AGP's required revision, already included by the full provisioner.
    wanted = ["platform-tools", f"platforms;android-{compile_sdk[1]}",
              "build-tools;35.0.0", f"ndk;{ndk[1]}"]
    if not set(wanted) <= set(names):
        raise ValueError("The declared Android build inputs are unavailable")
    return wanted


def phase_provision(*, development=False):
    """Provision job-private JDK, Android SDK tools, Gradle, host CPython 3.13 and cibuildwheel.

    Development mode omits cibuildwheel and emulator inputs and uses the app's
    declared platform/NDK plus AGP's required build tools.
    Everything lands under android/.fleet-sdk and is thrown away with the job; no host-global
    toolchain is created or modified. Every archive's committed sha256 is verified before it
    is extracted. Missing pieces are reported, never fatal here.
    """
    steps = {}
    if os.uname().machine != "x86_64":
        # The pinned JDK and host-Python bundles are linux x86_64 artifacts; another host
        # architecture is a capability gap to report, never a silent substitution.
        record("provision", "pending", reason="pinned bundles are linux x86_64 only",
               machine=os.uname().machine)
        return None

    jdk = TOOLS_DIR / "jdk"
    if shutil.which("java") is None and not (jdk / "bin/java").is_file():
        bundle = TOOLS_DIR / "downloads/jdk17.tar.gz"
        ok, digest = _fetch_pinned("jdk", bundle)
        ok = ok and _extract_tar_data(bundle, TOOLS_DIR / "downloads/jdk")
        if ok:
            inner = next((path for path in (TOOLS_DIR / "downloads/jdk").iterdir() if path.is_dir()), None)
            if inner is not None and inner != jdk:
                inner.rename(jdk)
        steps["jdk"] = {"status": "installed" if (jdk / "bin/java").is_file() else "unavailable",
                        "version": PINS["jdk"]["version"], "sha256": digest if ok else None}
    java = shutil.which("java") or (str(jdk / "bin/java") if (jdk / "bin/java").is_file() else None)
    java_home = str(pathlib.Path(java).parent.parent) if java else None

    sdk = TOOLS_DIR / "sdk"
    manager = sdk / "cmdline-tools/latest/bin/sdkmanager"
    if not manager.is_file():
        bundle = TOOLS_DIR / "downloads/commandlinetools.zip"
        ok, digest = _fetch_pinned("android_commandline_tools", bundle)
        if ok and _extract_zip_data(bundle, TOOLS_DIR / "downloads/cmdline-tools"):
            inner = TOOLS_DIR / "downloads/cmdline-tools/cmdline-tools"
            (sdk / "cmdline-tools").mkdir(parents=True, exist_ok=True)
            inner.rename(sdk / "cmdline-tools/latest")
        steps["cmdline_tools"] = {"status": "installed" if manager.is_file() else "unavailable",
                                  "version": PINS["android_commandline_tools"]["version"],
                                  "sha256": digest if ok else None}

    gradle = shutil.which("gradle")
    if gradle is None and not (TOOLS_DIR / "gradle/bin/gradle").is_file():
        bundle = TOOLS_DIR / "downloads/gradle.zip"
        ok, digest = _fetch_pinned("gradle", bundle)
        ok = ok and _extract_zip_data(bundle, TOOLS_DIR / "downloads/gradle-x")
        if ok:
            inner = next((path for path in (TOOLS_DIR / "downloads/gradle-x").iterdir() if path.is_dir()), None)
            if inner is not None:
                inner.rename(TOOLS_DIR / "gradle")
        steps["gradle"] = {"status": "installed" if (TOOLS_DIR / "gradle/bin/gradle").is_file() else "unavailable",
                           "version": PINS["gradle"]["version"], "sha256": digest if ok else None}
    gradle = gradle or (str(TOOLS_DIR / "gradle/bin/gradle") if (TOOLS_DIR / "gradle/bin/gradle").is_file() else None)

    # Task-private CPython 3.13: it must match the app's embedded 3.13 major.minor to serve
    # as Chaquopy's buildPython, whatever python3 the host itself ships (the executor has 3.14).
    python313 = TOOLS_DIR / "python/bin/python3.13"
    venv_python = TOOLS_DIR / "cibw-venv/bin/python"
    if not python313.is_file():
        bundle = TOOLS_DIR / "downloads/host-python.tar.gz"
        ok, digest = _fetch_pinned("host_python", bundle)
        ok = ok and _extract_tar_data(bundle, TOOLS_DIR / "downloads/python-x")
        if ok:
            inner = next((path for path in (TOOLS_DIR / "downloads/python-x").iterdir()
                          if path.is_dir() and (path / "bin/python3.13").is_file()), None)
            if inner is not None:
                inner.rename(TOOLS_DIR / "python")
        steps["host_python"] = {"status": "installed" if python313.is_file() else "unavailable",
                                "version": PINS["host_python"]["version"], "sha256": digest if ok else None}
    host_version = run_tool([python313, "--version"], timeout=120)["tail"].strip() if python313.is_file() else None
    if not development and python313.is_file() and not venv_python.is_file():
        # pip requires the file's name to be a valid wheel name, so the pinned wheel keeps
        # its published one (observed attempt-4 failure: "Invalid wheel filename").
        wheel = TOOLS_DIR / f"downloads/cibuildwheel-{PINS['cibuildwheel']['version']}-py3-none-any.whl"
        ok, digest = _fetch_pinned("cibuildwheel", wheel)
        created = run_tool([python313, "-m", "venv", str(TOOLS_DIR / "cibw-venv")], timeout=300)
        # The pinned wheel stays the only entry point; its dependencies (bashlex, bracex,
        # pydantic, ...) resolve from the package index over the same Python/proxy path
        # the validate venv itself uses. --no-index was the attempt-5 failure: nothing
        # local provides them. What actually landed is recorded via pip freeze so the
        # builder's full input set is in the receipt, not just its pinned entry point.
        installed = run_tool([venv_python, "-m", "pip", "install", str(wheel)], timeout=900) \
            if created["returncode"] == 0 and ok else {"returncode": 1, "tail": "wheel unavailable"}
        frozen = run_tool([venv_python, "-m", "pip", "freeze"], timeout=120) \
            if installed["returncode"] == 0 else {"tail": ""}
        steps["cibuildwheel"] = {"status": "installed" if installed["returncode"] == 0 else "unavailable",
                                 "version": PINS["cibuildwheel"]["version"], "sha256": digest if ok else None,
                                 "tail": installed["tail"][-500:] if installed["returncode"] else None,
                                 "environment": frozen["tail"][-1500:]}

    if manager.is_file() and java:
        # Install what the build and emulator phases need, driven by the repository's own
        # stable package table (never guessed pins) over the Python network path.
        # sdkmanager writes the accepted-license records Gradle and avdmanager check;
        # its metadata refresh uses the same acquiring cache when configured.
        packages = _sdk_repository_packages()
        if packages is None:
            steps["sdk_packages"] = {"status": "unavailable",
                                     "reason": "repository listing unfetchable over the Python path"}
        else:
            emulator_capable = not development and _emulator_capable()
            wanted = (_development_sdk_packages(packages, (REPO / "android/app/build.gradle.kts").read_text())
                      if development else _sdk_build_packages(packages, emulator_capable=emulator_capable))
            installed, failures = [], {}
            for name in wanted:
                ok, note = (_install_sdk_package(name, packages[name], sdk) if name in packages
                            else (False, "not offered for linux by the repository"))
                if ok:
                    installed.append({"package": name, "sha1": note})
                else:
                    failures[name] = note
            license_env = {"JAVA_HOME": java_home,
                           "PATH": f"{pathlib.Path(java).parent}:{os.environ['PATH']}"}
            if _ci_download_mirror():
                # sdkmanager has its own repository client. Use its upstream-base
                # override for the same real Google metadata, never a fixture repo.
                license_env["SDK_TEST_BASE_URL"] = _acquisition_url(
                    "https://dl.google.com/android/repository/")
            licenses = run_tool([str(manager), f"--sdk_root={sdk}", "--licenses"], timeout=600,
                                input_bytes="y\n" * 20, env_extra=license_env)
            steps["sdk_packages"] = {"source": list(REPOSITORY_XMLS),
                                     "emulator_capable": emulator_capable,
                                     "installed": installed,
                                     "failed": failures,
                                     "licenses": {"returncode": licenses["returncode"],
                                                  "tail": licenses["tail"][-400:]}}

    tools = {"java": java,
             "sdk": str(sdk) if manager.is_file() else os.environ.get("ANDROID_HOME"),
             "gradle": gradle,
             "python313": str(python313) if python313.is_file() else None,
             "cibuildwheel": str(TOOLS_DIR / "cibw-venv/bin/cibuildwheel")
             if (TOOLS_DIR / "cibw-venv/bin/cibuildwheel").is_file() else None,
             "host_python_version": host_version}
    required_tools = ("java", "sdk", "gradle", "python313") + (() if development else ("cibuildwheel",))
    record("provision", "passed" if all(tools[key] for key in required_tools) else "pending",
           steps=steps, tools=tools, development_only=development)
    return tools


def _private_directory(path):
    """One job-private directory, tightened to owner-only even when it already
    exists (mkdir's mode applies only to directories it creates)."""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def _write_private_file(path, data):
    """Materialize one job-private file with owner-only permissions even when the
    path or its leaf directory already exists with looser modes."""
    _private_directory(path.parent)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    # O_CREAT's mode never reaches a file that already exists, so a leftover 0644
    # store is tightened through the open descriptor itself.
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "wb") as sink:
        sink.write(data)


def _decode_debug_keystore_b64(value):
    """Strictly decode an injected keystore: any flaw is a failure, never a cue to
    silently fall back to a fresh key (an update signed by it would be refused by
    every install carrying the previous identity)."""
    try:
        decoded = base64.b64decode(value.encode("ascii"), validate=True)
    except (binascii.Error, ValueError):
        return None, "not strict base64"
    if not decoded:
        return None, "decoded to zero bytes"
    return decoded, None


def _cert_sha256(java, keystore, runner):
    """The keystore certificate's SHA-256 fingerprint — public data — via the pinned
    JDK's keytool, which also proves the store reads back with the standard debug
    credentials. Nothing from keytool's output beyond the fingerprint is retained."""
    result = runner([str(pathlib.Path(java).parent / "keytool"), "-list", "-v",
                     "-alias", "androiddebugkey",
                     "-keystore", str(keystore), "-storepass", "android"], timeout=120)
    # keytool indents the certificate fields (observed output: "    SHA256: AA:.."),
    # and the alias is requested so the pin is this store's debug key, not the
    # first certificate any store happens to show.
    match = re.search(r"^\s*SHA256: ([0-9A-F:]+)\s*$", result["tail"], re.MULTILINE)
    if result["returncode"] != 0 or not match:
        return None
    return match.group(1).replace(":", "").lower()


def phase_debug_signing(tools, env=None, runner=run_tool, keystore_path=None,
                        artifacts_dir=None):
    """Materialize this job's ONE managed debug keystore, before any Gradle run.

    An injected COMIC_ARCHIVER_DEBUG_KEYSTORE_B64 (owner-private fleet config; the
    bytes never enter the repo, snapshot or source archive) is strict-decoded into a
    0600 job-private file; any defect fails the job outright — the phone's install
    must never be stranded behind an accidental new identity. With nothing injected,
    a NEW dedicated project debug key is generated here with the pinned JDK's
    keytool (never locally) and retained as a 0600 private artifact so the owner can
    store it in private config and inject it on every future job. That artifact is a
    dedicated debug-only keystore; release keys and account material are handled
    elsewhere and never pass through this path. An optional expected certificate
    SHA-256 pins the identity on both paths. Gradle later receives only the
    file's path, via a debug-only property; the release build type keeps no
    signing config.
    """
    env = os.environ if env is None else env
    keystore = DEBUG_KEYSTORE if keystore_path is None else pathlib.Path(keystore_path)
    artifacts = (REPO.parent / "artifacts" if artifacts_dir is None
                 else pathlib.Path(artifacts_dir))
    java = tools.get("java") if tools else None
    if not java or not (pathlib.Path(java).parent / "keytool").is_file():
        record("debug_signing", "pending", reason="no pinned JDK keytool available")
        return None
    raw_injection = env.get(DEBUG_KEYSTORE_ENV)
    provenance = "generated" if raw_injection is None else "injected"
    if raw_injection is not None:
        injected = raw_injection.strip()
        if not injected:
            # A present-but-empty injection is a misconfigured pin, not a request
            # to mint a new identity; a silent generation here could strand every
            # installed app behind a signature mismatch.
            record("debug_signing", "failed", provenance=provenance,
                   reason="injection is present but empty")
            return None
        decoded, reason = _decode_debug_keystore_b64(injected)
        if decoded is None:
            record("debug_signing", "failed", provenance=provenance, reason=reason)
            return None
        _write_private_file(keystore, decoded)
    else:
        # keytool does not create the store's directory, and the parent must be
        # private before the key ever lands in it.
        _private_directory(keystore.parent)
        generated = runner([str(pathlib.Path(java).parent / "keytool"), "-genkeypair",
                            "-keystore", str(keystore), "-alias", "androiddebugkey",
                            "-keyalg", "RSA", "-keysize", "2048", "-validity", "10000",
                            "-dname", "CN=Comic Archiver Debug,O=Comic Archiver,C=US",
                            "-storepass", "android", "-keypass", "android"], timeout=300)
        if generated["returncode"] != 0 or not keystore.is_file():
            record("debug_signing", "failed", provenance=provenance,
                   reason="keytool could not generate the keystore")
            return None
        os.chmod(keystore, 0o600)
    fingerprint = _cert_sha256(java, keystore, runner)
    if fingerprint is None:
        record("debug_signing", "failed", provenance=provenance,
               reason="keytool could not read the keystore back")
        return None
    expected = (env.get(DEBUG_CERT_SHA256_ENV) or "").strip().replace(":", "").lower()
    if expected and expected != fingerprint:
        record("debug_signing", "failed", provenance=provenance,
               reason="certificate fingerprint differs from the expected value")
        return None
    facts = {"provenance": provenance, "keystore": str(keystore),
             "cert_sha256": fingerprint}
    if provenance == "generated":
        # The retained copy is the whole point of the generated path: without it the
        # key dies with the job (the job-34 loss). The fleet archive's verified
        # copytree preserves these owner-only modes and publishes nothing.
        try:
            artifact = artifacts / "debug-keystore/comic-archiver-debug.keystore"
            _write_private_file(artifact, keystore.read_bytes())
        except OSError:
            record("debug_signing", "failed", provenance=provenance,
                   reason="generated keystore could not be retained as a private artifact")
            return None
        facts["private_artifact"] = str(artifact)
    record("debug_signing", "passed", **facts)
    return str(keystore)


def _java_version(java):
    probe = run_tool([java, "-version"], timeout=120)
    match = re.search(r'version "(\d+)', probe["tail"])
    return int(match[1]) if match else None


def phase_preflight(tools):
    missing, found = set(), {}
    java = tools.get("java") if tools else shutil.which("java")
    version = _java_version(java) if java else None
    found["java"] = version is not None and version >= 17
    if not found["java"]:
        missing.add("java")

    sdk = (tools.get("sdk") if tools else None) or os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
    sdk_path = pathlib.Path(sdk) if sdk else None
    found["android_sdk"] = bool(sdk_path and sdk_path.is_dir() and (sdk_path / "platform-tools").exists())
    if not found["android_sdk"]:
        missing.add("android_sdk")
        record("preflight", "pending", missing=sorted(missing), java=version)
        return False, None

    found["ndk"] = (sdk_path / "ndk").is_dir() and any((sdk_path / "ndk").iterdir())
    if not found["ndk"]:
        missing.add("ndk")

    gradle, gradle_version = None, None
    candidates = [value for value in ((tools.get("gradle") if tools else None), shutil.which("gradle"),
                                      str(TOOLS_DIR / "gradle/bin/gradle")) if value]
    gradle_probes = []
    for candidate in candidates:
        # Attempt 6's probe inherited neither JAVA_HOME nor a java on PATH, and the bare
        # host has none; the launcher refuses to run in that state. Hand it ours and keep
        # the probe's own words so a still-failing launcher says why in the receipt.
        probe = run_tool([candidate, "--version"], timeout=300,
                         env_extra={"JAVA_HOME": str(pathlib.Path(java).parent.parent),
                                    "PATH": f"{pathlib.Path(java).parent}:{os.environ['PATH']}"}
                         if java else None)
        match = re.search(r"^Gradle (\d+(?:\.\d+)?)", probe["tail"], re.MULTILINE)
        if match:
            digits = tuple(int(piece) for piece in match[1].split(".")[:2])
            if digits >= (8, 10):
                gradle, gradle_version = candidate, match[1]
                break
        gradle_probes.append({"candidate": candidate, "returncode": probe["returncode"],
                              "tail": probe["tail"][-300:]})
    if gradle is None:
        missing.add("gradle")

    # Chaquopy's buildPython must match the app's embedded Python 3.13 in major.minor; the
    # host's default python3 (3.14 on the executor) is not a substitute.
    buildpython = (tools.get("python313") if tools else None) or str(TOOLS_DIR / "python/bin/python3.13")
    probe = run_tool([buildpython, "--version"], timeout=120) if pathlib.Path(buildpython).is_file() else {}
    buildpython_version = probe.get("tail", "").strip() or None
    if not (buildpython_version or "").startswith("Python 3.13."):
        missing.add("buildpython_3_13")

    images = []
    images_root = sdk_path / "system-images"
    if images_root.is_dir():
        images = [f"system-images;{android};{profile};{abi}"
                  for android in sorted(p.name for p in images_root.iterdir() if p.is_dir())
                  for profile in sorted(p.name for p in (images_root / android).iterdir() if p.is_dir())
                  for abi in sorted(p.name for p in (images_root / android / profile).iterdir() if p.is_dir())]
    required_ok = not missing
    record("preflight", "passed" if required_ok else "pending", missing=sorted(missing), java=version,
           gradle=gradle_version, sdk=str(sdk_path), system_images=images,
           gradle_probes=gradle_probes if gradle is None else None,
           buildpython=buildpython_version, buildpython_path=buildpython,
           kvm=_emulator_capable())
    return required_ok, {"sdk": sdk_path, "gradle": gradle, "java": java, "buildpython": buildpython}


def phase_sdist():
    cache = SDIST_DIR / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / PINNED_SDIST
    try:
        if not target.is_file():
            with urllib.request.urlopen(_acquisition_url(PYPI_JSON), timeout=60) as response:
                links = json.load(response)["urls"]
            url = next(item["url"] for item in links if item["filename"] == PINNED_SDIST)
            urllib.request.urlretrieve(_acquisition_url(url), target)
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
    except (OSError, ValueError, StopIteration) as error:
        record("sdist", "failed", error=type(error).__name__)
        return None
    if digest != PINNED_SHA256:
        record("sdist", "failed", digest=digest)
        return None
    record("sdist", "passed", sha256=digest, path=str(target))
    return target


def _cibw_required_ndk(sdk, packages):
    """Install, over the Python network path, the NDK version cibuildwheel's pinned
    CPython Android build hard-requires. That build's android-env.sh runs the JVM
    sdkmanager — the network path broken on this fleet (observed attempts 4, 5, 11) —
    whenever $ANDROID_HOME/ndk/<version>/package.xml is missing, so the NDK is
    provisioned before cibuildwheel starts. The requirement is read from the pinned
    CPython artifact itself, its sha256 verified against the pinned builder's own
    resource table, keeping the version transitively pinned instead of hardcoded.
    """
    resources = TOOLS_DIR / ("cibw-venv/lib/python3.13/site-packages/"
                             "cibuildwheel/resources/build-platforms.toml")
    try:
        builds = tomllib.loads(resources.read_text())["android"]["python_configurations"]
        build = next(item for item in builds if item.get("identifier") == "cp313-android_x86_64")
    except (OSError, KeyError, StopIteration, ValueError):
        return None, "pinned builder's android table unreadable"
    bundle = TOOLS_DIR / "downloads/cpython-android.tar.gz"
    try:
        if not _download(build["url"], bundle):
            return None, "cpython android build download failed"
        digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
    except OSError:
        return None, "cpython android build unreadable"
    if digest != build["sha256"]:
        bundle.unlink(missing_ok=True)
        return None, f"cpython android build sha256 mismatch: {digest}"
    try:
        with tarfile.open(bundle) as archive:
            member = archive.extractfile("./android-env.sh")
            text = member.read().decode("utf-8", "replace") if member else ""
    except (OSError, tarfile.TarError):
        return None, "android-env.sh unreadable in the pinned build"
    match = re.search(r"^ndk_version=([0-9.]+)$", text, re.MULTILINE)
    if not match:
        return None, "android-env.sh states no ndk_version"
    version = match.group(1)
    if (sdk / "ndk" / version / "package.xml").is_file():
        return version, "present"
    name = f"ndk;{version}"
    if name not in (packages or {}):
        return version, "required ndk not offered by the repository"
    ok, note = _install_sdk_package(name, packages[name], sdk)
    ndk = sdk / "ndk" / version
    if not ok or not ndk.is_dir():
        return version, f"ndk install failed: {note}"
    marker = ndk / "package.xml"
    if marker.is_file():
        return version, "installed (zip carried package.xml)"
    # Google's plain NDK zip ships source.properties but no package.xml — that file is
    # written by sdkmanager when IT installs (observed attempt 12: android-env.sh probes
    # for the file's existence and otherwise reaches for the JVM sdkmanager, the network
    # path broken on this fleet, even though the sha1-verified NDK sits right there). The
    # zip's sha1 was checked against Google's own listing before extraction; cross-check
    # the version it ships, then write the marker android-env.sh looks for, stating this.
    properties = ndk / "source.properties"
    try:
        shipped = f"Pkg.Revision = {version}" in properties.read_text()
    except OSError:
        shipped = False
    if not shipped:
        return version, "ndk zip does not declare the required version"
    pieces = version.split(".")
    revision = "".join(f"<{part}>{int(pieces[index])}</{part}>"
                       for index, part in enumerate(("major", "minor", "micro")))
    marker.write_text(
        "<?xml version='1.0' encoding='utf-8'?>\n"
        "<!-- Installed from Google's sha1-verified repository zip by\n"
        "     scripts/android_feasibility.py. The zip ships source.properties but no\n"
        "     package.xml; this mirrors what sdkmanager itself writes on install: the\n"
        "     repository2-03 schema qualifies only the root element — children are\n"
        "     unqualified — and the package body is the listing's own definition with\n"
        "     the remote-only parts (channelRef, archives) dropped. -->\n"
        '<sdk:sdk-repository xmlns:sdk="http://schemas.android.com/sdk/android/repo/repository2/03"'
        ' xmlns:generic="http://schemas.android.com/repository/android/generic/02"'
        ' xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">\n'
        f'  <localPackage path="ndk;{version}">\n'
        '    <type-details xsi:type="generic:genericDetailsType"/>\n'
        f"    <revision>{revision}</revision>\n"
        f'    <display-name>NDK (Side by side) {version}</display-name>\n'
        "  </localPackage>\n"
        "</sdk:sdk-repository>\n")
    return version, "installed; package.xml marker written (zip ships source.properties only)"


def phase_wheels(paths, sdist):
    """Build the cp313 Android wheels from the pinned sdist with cibuildwheel on this host.

    Chaquopy 17 does not cross-build native sdsts; per chaquo/chaquopy server/pypi/README.md
    the supported builder for Python 3.13+ is cibuildwheel, run here against the job-private
    SDK with the 'build' frontend (Android does not support the pip frontend). Wheel tests
    stay off here: emulator qualification is the in-app phase's own evidence.
    """
    cibw = TOOLS_DIR / "cibw-venv/bin/cibuildwheel"
    if sdist is None:
        record("wheels", "skipped", reason="verified sdist unavailable")
        return None
    if not cibw.is_file() or not paths.get("buildpython"):
        record("wheels", "pending", reason="no pinned cibuildwheel/host Python available")
        return None
    source = JOB_DIR / "zstandard-src"
    JOB_DIR.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(source, ignore_errors=True)
    try:
        with tarfile.open(sdist) as bundle:
            bundle.extractall(source, filter="data")
        project = next(path for path in source.iterdir() if path.is_dir())
    except (OSError, tarfile.TarError, StopIteration, ValueError):
        record("wheels", "failed", reason="pinned sdist could not be unpacked")
        return None
    output = JOB_DIR / "wheels"
    shutil.rmtree(output, ignore_errors=True)
    output.mkdir(parents=True, exist_ok=True)
    # The sdist's own [tool.cibuildwheel] table names enable groups this pinned builder
    # version removed (observed attempt 8: "Unknown enable group: cpython-freethreading")
    # plus desktop-only pytest test-commands and a yum before-all; this config replaces it
    # so the Android build runs exactly the pinned builder's defaults, tests off.
    config = JOB_DIR / "cibw-config.toml"
    config.write_text('[tool.cibuildwheel]\nbuild-frontend = "build"\n')
    # The pinned CPython Android build's android-env.sh hard-requires one specific NDK
    # and otherwise installs it with the JVM sdkmanager (broken on this fleet).
    ndk_version, ndk_note = _cibw_required_ndk(paths["sdk"], _sdk_repository_packages())
    env = {"ANDROID_HOME": str(paths["sdk"]), "ANDROID_SDK_ROOT": str(paths["sdk"]),
           "JAVA_HOME": str(pathlib.Path(paths["java"]).parent.parent),
           "PATH": f"{pathlib.Path(paths['java']).parent}:{os.environ['PATH']}",
           "CIBW_BUILD": "cp313-*",
           "CIBW_BUILD_FRONTEND": "build"}
    result = run_tool([str(cibw), "--platform", "android", "--archs", "arm64_v8a,x86_64",
                       "--config-file", str(config),
                       "--output-dir", str(output), str(project)], timeout=3600, env_extra=env,
                      keep_bytes=8000)
    built = sorted(path.name for path in output.glob("zstandard-*.whl"))
    archs_seen = sorted({re.search(r"android_\d+_(.+)\.whl$", name).group(1)
                         for name in built if re.search(r"android_\d+_(.+)\.whl$", name)})
    ok = result["returncode"] == 0 and all(arch in archs_seen for arch in WHEEL_ABI_ARCHS) \
        and all(name.startswith("zstandard-0.25.0-cp313-") for name in built) and len(built) == 2
    record("wheels", "passed" if ok else "failed", minutes=result["minutes"], wheels=built,
           archs=archs_seen, returncode=result["returncode"],
           required_ndk={"version": ndk_version, "note": ndk_note}, tail=result["tail"])
    return output if ok else None


def _gradle_invocation(paths, task, wheels):
    """One Gradle invocation's environment and argv: the same proxy/truststore JVM flags,
    job-private SDK and buildPython, verified wheels and mirror index for every Gradle
    task this point runs (the build and the JVM suites)."""
    # Gradle's JVM is the one remaining raw-JVM downloader (the plugins and Chaquopy's
    # own artifacts); hand it the proxy and the system-CA truststore via GRADLE_OPTS,
    # which the launcher forwards to the single-use daemon it forks for --no-daemon.
    # JAVA_TOOL_OPTIONS carries the same flags to every JVM the build spawns — the
    # launcher only processes GRADLE_OPTS itself, and AGP's sdkmanager fallback died on
    # the fleet's MITM proxy even after Gradle itself could download (attempt 15's
    # traceback: manifests unfetchable, ~/.android read-only). ANDROID_USER_HOME moves
    # that cache somewhere writable, job-private as everything else here.
    jvm_flags = _jvm_net_options(paths["java"])
    mirror = _ci_download_mirror()
    if mirror:
        # Robolectric's resolver runs in the test JVM, independently of Gradle.
        # The native frontend needs no URL encoding; retain legacy spelling.
        java_mirror = mirror.replace("/HTTPS///", "/HTTPS/%2F%2F")
        jvm_flags.append("-Drobolectric.dependency.repo.url=" +
                         java_mirror + "repo1.maven.org/maven2")
    env = {"ANDROID_HOME": str(paths["sdk"]), "ANDROID_SDK_ROOT": str(paths["sdk"]),
           "JAVA_HOME": str(pathlib.Path(paths["java"]).parent.parent),
           "ANDROID_USER_HOME": str(TOOLS_DIR / "android-user"),
           "GRADLE_OPTS": " ".join(jvm_flags),
           "JAVA_TOOL_OPTIONS": " ".join(jvm_flags)}
    if mirror:
        env["SDK_TEST_BASE_URL"] = java_mirror + "dl.google.com/android/repository/"
    # Chaquopy's pip block consults chaquo.com/pypi-13.1 unless a user --index-url is
    # passed (verified in the pinned 17.1.0's pip_install.py — it also disables pip config
    # files, so only a command-line option reaches it); this fleet's proxy refuses that
    # host (observed attempt 14: CONNECT tunnel 403 for every httpx dependency) while the
    # mirror every other pip here uses is allowlisted. Hand the build the same index pip
    # itself would discover, and let the Gradle property apply it.
    index_url = os.environ.get("PIP_INDEX_URL", "").strip() or None
    probe = run_tool([paths["buildpython"], "-m", "pip", "config", "get", "global.index-url"],
                     timeout=120)
    for line in reversed((probe.get("tail") or "").splitlines()):
        if line.strip().startswith("http"):
            index_url = line.strip()
            break
    argv = [paths["gradle"], "--no-daemon", "-p", str(REPO / "android"), task,
            f"-PzstandardWheels={wheels}", f"-PbuildPython={paths['buildpython']}"]
    if index_url:
        argv.append(f"-PpipIndexUrl={index_url}")
    # The managed debug keystore's path is the only signing fact Gradle ever sees;
    # without it AGP's default per-user debug keystore behavior applies unchanged.
    if paths.get("debug_keystore"):
        argv.append(f"-PdebugKeystore={paths['debug_keystore']}")
    if mirror:
        init = TOOLS_DIR / "ci-package-mirror.init.gradle"
        init.parent.mkdir(parents=True, exist_ok=True)
        init.write_text(CI_PACKAGE_MIRROR_INIT)
        argv.extend(["--init-script", str(init)])
    return env, argv, jvm_flags, index_url


def _apk_signer_cert_sha256(sdk, java, apk, runner):
    """The APK's actual signing certificate digest, via the job's provisioned
    apksigner (newest installed build-tools revision). Pinning the keystore alone
    does not prove Gradle used it, so the shipped artifact's own certificate is
    read back. Returns (digest, diagnostics): digest is None with bounded public
    diagnostics (verifier return code and output tail) whenever the readback itself
    fails, so a broken verifier is never conflated with a true mismatch."""
    build_tools = pathlib.Path(sdk) / "build-tools"
    apksigner = None
    if build_tools.is_dir():
        installed = sorted(tool for tool in build_tools.iterdir()
                           if tool.is_dir() and (tool / "apksigner").is_file())
        apksigner = installed[-1] / "apksigner" if installed else None
    if apksigner is None:
        return None, {"returncode": None,
                      "reason": "no apksigner found in the job SDK's build-tools"}
    # JAVA_HOME names the pinned JDK for wrapper scripts that prefer it over PATH.
    result = runner([str(apksigner), "verify", "--print-certs", str(apk)], timeout=300,
                    keep_bytes=16000,
                    env_extra={"JAVA_HOME": str(pathlib.Path(java).parent.parent),
                               "PATH": f"{pathlib.Path(java).parent}:{os.environ['PATH']}"})
    # SDK 37 prints "V2 Signer:" where older tools printed "Signer #1".
    # Match signing certificates only (never public-key hashes or source stamps).
    # This debug-key path has no rotation or multiple identities: every reported
    # signer must agree, rather than accepting the first of conflicting digests.
    digests = re.findall(
        r"^(?:Signer #\d+|V[123] Signer:) certificate SHA-256 digest: "
        r"([0-9a-fA-F]{64})[ \t]*$", result["tail"], re.MULTILINE)
    identities = {digest.lower() for digest in digests}
    if result["returncode"] == 0 and len(identities) == 1:
        return identities.pop(), None
    # The verifier failed itself (job 35: the run's code and output were discarded,
    # leaving the cause unknowable). apksigner --print-certs emits only public
    # certificate data and warnings, so a bounded tail of it is safe to retain and
    # separates "verifier problem" from "true certificate mismatch".
    return None, {"returncode": result["returncode"], "tail": result["tail"][-800:]}


def phase_build(paths, wheels):
    if wheels is None:
        record("build", "skipped", reason="verified Android wheels unavailable")
        return False
    if not paths.get("gradle"):
        record("build", "pending", reason="no gradle available")
        return False
    env, argv, jvm_flags, index_url = _gradle_invocation(paths, "assembleDebug", wheels)
    result = run_tool(argv, timeout=3600, env_extra=env, keep_bytes=8000)
    ok = result["returncode"] == 0 and APK.is_file()
    tail = result["tail"]
    classification = None
    apk_cert = None
    verifier = None
    if ok:
        # The managed identity must be proven in the shipped APK itself: Gradle
        # could have fallen back to another keystore (the observed per-job
        # signature mismatch), so an unproven or differing certificate fails the
        # build rather than shipping an APK no install would accept as an update.
        expected_cert = report["phases"].get("debug_signing", {}).get("cert_sha256")
        apk_cert, verifier = _apk_signer_cert_sha256(paths["sdk"], paths["java"], APK, run_tool)
        if not expected_cert or apk_cert is None:
            ok = False
            classification = "signing_verification_failed"
        elif apk_cert != expected_cert:
            ok = False
            classification = "signing_certificate_mismatch"
    if not ok and classification is None:
        # The last task Gradle marked FAILED, when present, is the truthful failure
        # point. The bare "zstandard" match once mislabelled attempt 17 as a wheel
        # failure because Chaquopy's SUCCESSFUL pip output names zstandard, and job
        # 35 showed the same heuristic overwriting an honest signing classification
        # on a BUILD SUCCESSFUL log — so relabelling applies only when no more
        # specific classification is already set.
        failed_tasks = re.findall(r"> Task :(\S+) FAILED", tail)
        if failed_tasks:
            classification = f"task_failed:{failed_tasks[-1]}"
        elif re.search(r"zstandard|chaquopy.*pip|error: (command|link|compile)", tail, re.IGNORECASE):
            classification = "wheel_install_failed"
    wheel_abis = []
    if APK.is_file():
        with zipfile.ZipFile(APK) as archive:
            # Chaquopy packs each ABI's pip requirements as a NESTED zip asset,
            # requirements-<abi>.imy (Common.assetZip renames .zip so aapt2 will not
            # recompress it); the wheel's compiled backend lives inside that zip, not
            # under lib/. Attempt 19's APK probe scanned lib/ and reported nothing.
            for name in archive.namelist():
                # Android ABI names mix separators (arm64-v8a dashes, x86_64
                # underscores); accept either rather than only the underscored kind
                # (attempt 20's [^-]+ silently dropped arm64-v8a).
                match = re.fullmatch(r"assets/chaquopy/requirements-(.+)\.imy", name)
                if not match:
                    continue
                with zipfile.ZipFile(io.BytesIO(archive.read(name))) as reqs:
                    if any(part.startswith("zstandard/") and part.endswith(".so")
                           for part in reqs.namelist()):
                        wheel_abis.append(match.group(1))
        wheel_abis = sorted(set(wheel_abis))
    artifact = None
    artifact_sha256 = None
    if result["returncode"] == 0 and APK.is_file():
        # The executor retires repo/ as soon as the job completes, taking the built APK
        # with it; the job's artifacts/ sibling survives cleanup. Preserve the exact APK
        # (and its digest, computed over the copy as it is written) so the point leaves
        # retrievable evidence instead of a path that dies with the repo. A Gradle-built
        # APK whose only failure is the signing certificate proof is retained too, under
        # an explicitly UNQUALIFIED name — inspecting the real artifact beats rebuilding
        # blind, and the distinct name keeps unproven evidence from masquerading.
        digest = hashlib.sha256()
        try:
            artifacts_dir = REPO.parent / "artifacts"
            artifacts_dir.mkdir(exist_ok=True)
            artifact = artifacts_dir / ("app-debug.apk" if ok else "app-debug-unqualified.apk")
            with APK.open("rb") as source, artifact.open("wb") as sink:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
                    sink.write(chunk)
            artifact_sha256 = digest.hexdigest()
        except OSError:
            artifact = None
            artifact_sha256 = None
    record("build", "passed" if ok else "failed", minutes=result["minutes"], classification=classification,
           apk=str(APK) if APK.is_file() else None, wheel_abis=wheel_abis, jvm_flags=jvm_flags,
           apk_cert_sha256=apk_cert, signing_verifier=verifier,
           apk_artifact=str(artifact) if artifact else None,
           apk_sha256=artifact_sha256, apk_bytes=APK.stat().st_size if APK.is_file() else None,
           pip_index_url=index_url, buildpython=paths.get("buildpython"), tail=tail)
    return ok


def phase_jvm_tests(paths, wheels):
    """The app's JVM unit suites (the UI/export line's exporter and mapping tests) on the
    same wired Gradle invocation as the build: unit-test compilation still runs Chaquopy's
    packaging, so the buildPython/wheels/index properties are as required here."""
    if wheels is None:
        record("jvm_tests", "skipped", reason="verified Android wheels unavailable")
        return False
    if not paths.get("gradle"):
        record("jvm_tests", "pending", reason="no gradle available")
        return False
    env, argv, _jvm_flags, _index_url = _gradle_invocation(paths, "testDebugUnitTest", wheels)
    result = run_tool(argv, timeout=1800, env_extra=env, keep_bytes=8000)
    tail = result["tail"]
    failed_tasks = re.findall(r"> Task :(\S+) FAILED", tail)
    classification = f"task_failed:{failed_tasks[-1]}" if failed_tasks else (
        "tests_failed" if result["returncode"] != 0 else None)
    record("jvm_tests", "passed" if result["returncode"] == 0 else "failed",
           minutes=result["minutes"], classification=classification, tail=tail)
    return result["returncode"] == 0


def _free_port():
    # Android's console/adb pairs have a documented range, unlike arbitrary
    # ephemeral TCP ports. Check both endpoints before starting this job's AVD.
    for port in range(5554, 5683, 2):
        try:
            with socket.socket() as console, socket.socket() as adb:
                console.bind(("127.0.0.1", port))
                adb.bind(("127.0.0.1", port + 1))
                return port
        except OSError:
            continue
    raise RuntimeError("No free Android emulator console/adb port pair")


def _evaluate_selfcheck(line):
    """Missing evidence is not success: every named check must be present and passed."""
    # MainActivity emits {ping: <bridge envelope>, selfcheck: <Python facts>}.
    # A bare Python result cannot prove that the packaged bridge worked too.
    facts = line.get("selfcheck") if isinstance(line, dict) else None
    if not isinstance(facts, dict) or not isinstance(facts.get("zstandard"), dict):
        return {"selfcheck_line": False}
    ping = line.get("ping")
    result = ping.get("result") if isinstance(ping, dict) else None
    zstd = facts["zstandard"]
    version = zstd.get("version")
    parsed = re.fullmatch(r"(\d+)\.(\d+)(?:\.\d+)?", version) if isinstance(version, str) else None
    return {
        "selfcheck_line": facts.get("identity") == SELF_IDENTITY,
        "bridge_ping": isinstance(result, dict) and ping.get("ok") is True and
                       type(result.get("bridge_version")) is int and result["bridge_version"] == 1 and
                       isinstance(result.get("python"), str) and bool(result["python"]) and
                       result["python"] == facts.get("python"),
        "imported": zstd.get("imported") is True,
        "version_floor": parsed is not None and tuple(map(int, parsed.groups()[:2])) >= (0, 25),
        "roundtrip": zstd.get("roundtrip") is True,
        "trailing_frame": zstd.get("trailing_frame") is True,
        "truncation": zstd.get("truncation") is True,
        "checksum": zstd.get("checksum") == "rejected",
        "window_bound": zstd.get("window_bound") is True,
        "page_size": type(facts.get("page_size")) is int and facts["page_size"] > 0,
    }


def phase_instrumentation_build(paths, wheels):
    """Build the required test-only APK and verify it uses the retained target signer."""
    env, argv, _flags, _index = _gradle_invocation(paths, "assembleDebugAndroidTest", wheels)
    result = run_tool(argv, timeout=1800, env_extra=env, keep_bytes=16000)
    cert, diagnostics = None, None
    expected = report["phases"].get("build", {}).get("apk_cert_sha256")
    ok = result["returncode"] == 0 and TEST_APK.is_file()
    if ok:
        cert, diagnostics = _apk_signer_cert_sha256(paths["sdk"], paths["java"], TEST_APK, run_tool)
        ok = cert is not None and expected is not None and cert == expected
    digest = None
    if ok:
        artifacts = REPO.parent / "artifacts"
        artifacts.mkdir(exist_ok=True)
        retained = artifacts / "app-debug-androidTest.apk"
        shutil.copyfile(TEST_APK, retained)
        digest = hashlib.sha256(retained.read_bytes()).hexdigest()
    record("instrumentation_build", "passed" if ok else "failed", minutes=result["minutes"],
           apk_sha256=digest, apk_cert_sha256=cert, signing_verifier=diagnostics, tail=result["tail"])
    return ok


def phase_in_app(paths, test_apk=None, *, journey=None, emulator_output=None):
    if not _emulator_capable():
        record("in_app", "pending", reason="no accessible /dev/kvm in this job; native emulator gate pending")
        record("beta_integration", "pending", reason="no accessible /dev/kvm in this job")
        return
    sdk = paths["sdk"]
    adb = sdk / "platform-tools/adb"
    emulator = sdk / "emulator/emulator"
    avdmanager = sdk / "cmdline-tools/latest/bin/avdmanager"
    preflight = report["phases"].get("preflight", {})
    images = preflight.get("system_images") or []
    image = next((item for item in images if item.endswith(";x86_64")),
                 next((item for item in images if item.endswith(";arm64-v8a")), None))
    if not (adb.is_file() and emulator.is_file() and avdmanager.is_file() and image):
        record("in_app", "pending", reason="no avdmanager/emulator/system image",
               adb=adb.is_file(), emulator=emulator.is_file(), avdmanager=avdmanager.is_file(),
               system_image=image)
        record("beta_integration", "pending", reason="native emulator prerequisites unavailable")
        return

    avd_home = JOB_DIR / "avd"
    avd_home.mkdir(parents=True, exist_ok=True)
    avd_name = f"comic-archiver-feasibility-{os.getpid()}"
    env_java = {"JAVA_HOME": str(pathlib.Path(paths["java"]).parent.parent),
                "ANDROID_HOME": str(sdk), "ANDROID_SDK_ROOT": str(sdk),
                "ANDROID_USER_HOME": str(TOOLS_DIR / "android-user"),
                "ANDROID_AVD_HOME": str(avd_home),
                "PATH": f"{pathlib.Path(paths['java']).parent}:{os.environ['PATH']}"}
    created = run_tool([str(avdmanager), "create", "avd", "-n", avd_name, "-k", image, "--force"],
                       timeout=300, env_extra=env_java, input_bytes="no\n")
    if created["returncode"] != 0:
        record("in_app", "failed", reason="avd creation failed", tail=created["tail"])
        record("beta_integration", "skipped", reason="avd creation failed")
        shutil.rmtree(avd_home, ignore_errors=True)
        return
    serial, process, owned = None, None, False
    try:
        port = _free_port()
        serial = f"emulator-{port}"
        process = subprocess.Popen(
            [str(emulator), "-avd", avd_name, "-port", str(port), "-no-window", "-no-snapshot",
             "-no-audio", "-no-boot-anim", "-gpu", "swiftshader_indirect"],
            stdout=emulator_output if emulator_output is not None else subprocess.DEVNULL, stderr=subprocess.STDOUT,
            env={**os.environ, "ANDROID_AVD_HOME": str(avd_home), "ANDROID_HOME": str(sdk),
                 "ANDROID_SDK_ROOT": str(sdk)})
        run_tool([str(adb), "-s", serial, "wait-for-device"], timeout=600)
        booted, deadline = False, time.monotonic() + 900
        while time.monotonic() < deadline:
            ready = run_tool([str(adb), "-s", serial, "shell", "getprop", "sys.boot_completed"], timeout=60)
            if ready["returncode"] == 0 and ready["tail"].strip().endswith("1"):
                booted = True
                break
            if process.poll() is not None:
                break
            time.sleep(5)
        if not booted:
            record("in_app", "failed", reason="job emulator did not finish booting")
            record("beta_integration", "skipped", reason="job emulator did not finish booting")
            return
        identity = run_tool([str(adb), "-s", serial, "emu", "avd", "name"], timeout=60)
        if identity["returncode"] != 0 or identity["tail"].splitlines()[:1] != [avd_name]:
            record("in_app", "failed", reason="selected emulator is not this job's AVD")
            record("beta_integration", "skipped", reason="emulator ownership was not verified")
            return
        owned = True
        properties = {}
        for key, prop in (("abi", "ro.product.cpu.abi"), ("api", "ro.build.version.sdk")):
            properties[key] = run_tool([str(adb), "-s", serial, "shell", "getprop", prop], timeout=60)["tail"].strip()
        install = run_tool([str(adb), "-s", serial, "install", "-r", str(APK)], timeout=600)
        cleared = run_tool([str(adb), "-s", serial, "logcat", "-c"], timeout=60)
        launched = run_tool([str(adb), "-s", serial, "shell", "am", "start", "-W", "-n", APP_ACTIVITY,
                             "--ez", "selfcheck", "true"], timeout=120)
        time.sleep(20)
        log = run_tool([str(adb), "-s", serial, "logcat", "-d", "-s", SELF_TAG, "-v", "raw"], timeout=120)
        line = next((json.loads(piece) for piece in log["tail"].splitlines()
                     if piece.strip().startswith("{") and SELF_IDENTITY in piece and _json_ok(piece)), None)
        checks = _evaluate_selfcheck(line)
        checks["install_ok"] = install["returncode"] == 0 and "Success" in install["tail"].splitlines()
        checks["log_cleared"] = cleared["returncode"] == 0
        checks["launch_ok"] = launched["returncode"] == 0 and "Status: ok" in launched["tail"].splitlines()
        checks["log_read"] = log["returncode"] == 0
        checks["abi_allowed"] = properties["abi"] in ABI_FILTERS
        record("in_app", "passed" if checks and all(checks.values()) else "failed",
               install_ok=checks["install_ok"], evidence=line, checks=checks,
               abi=properties["abi"], api=properties["api"], tail=log["tail"][:2000])
        if not all(checks.values()):
            record("beta_integration", "skipped", reason="native selfcheck did not pass")
        elif test_apk is None:
            record("beta_integration", "pending", reason="required instrumentation APK not selected")
        else:
            if __package__:
                from .android_beta_integration import run as beta_journey
            else:
                from android_beta_integration import run as beta_journey
            try:
                result = (journey or beta_journey)(adb, serial, avd_name, test_apk,
                                      REPO.parent / "artifacts/beta-integration", run_tool)
                record("beta_integration", "passed", **result)
            except (ValueError, OSError, RuntimeError, subprocess.TimeoutExpired) as error:
                record("beta_integration", "failed", error_type=type(error).__name__, reason=str(error)[:400])
    except (ValueError, OSError, RuntimeError) as error:
        record("in_app", "failed", reason="emulator or selfcheck observation failed",
               error_type=type(error).__name__)
        record("beta_integration", "skipped", reason="native execution failed")
    finally:
        if serial and owned:
            run_tool([str(adb), "-s", serial, "emu", "kill"], timeout=120)
        if process is not None:
            for _ in range(60):
                if process.poll() is not None:
                    break
                time.sleep(1)
            process.kill()
        run_tool([str(avdmanager), "delete", "avd", "-n", avd_name], timeout=120, env_extra=env_java)
        shutil.rmtree(avd_home, ignore_errors=True)


def _json_ok(text):
    try:
        json.loads(text)
        return True
    except ValueError:
        return False


REQUIRED_PHASES = ("provision", "debug_signing", "preflight", "sdist", "wheels", "build", "jvm_tests",
                   "instrumentation_build", "in_app", "beta_integration")


def _verdict(required=REQUIRED_PHASES):
    # Every selected required phase counts: a single failed phase fails the point, a
    # capability gap (pending) or a phase skipped behind it holds it pending, and 0
    # requires every phase's evidence to have passed. Provision counts as required:
    # its remedy is what makes the later phases runnable on a bare host.
    statuses = [report["phases"].get(phase, {}).get("status", "pending") for phase in required]
    if not statuses or "failed" in statuses:
        return 1
    if all(status == "passed" for status in statuses):
        return 0
    return 3


def development_apk(wheels):
    """Build one installable phone APK. Tests and final qualification are separate work."""
    required = ("provision", "debug_signing", "preflight", "development_apk")
    artifacts = REPO.parent / "artifacts"
    artifacts.mkdir(exist_ok=True)
    code = 1
    try:
        if not os.environ.get(DEBUG_KEYSTORE_ENV) or not os.environ.get(DEBUG_CERT_SHA256_ENV):
            raise ValueError("Use the private fleet configuration with the retained debug signing identity")
        wheels = pathlib.Path(wheels).resolve(strict=True)
        expected = {f"zstandard-{PINS['zstandard']['version']}-cp313-cp313-android_24_{abi}.whl"
                    for abi in WHEEL_ABI_ARCHS}
        if not wheels.is_dir() or not expected <= {path.name for path in wheels.glob("*.whl")}:
            raise ValueError("Supply the retained cp313 Android dependency wheels for both app ABIs")
        report["dependency_wheels"] = {name: hashlib.sha256((wheels / name).read_bytes()).hexdigest()
                                        for name in sorted(expected)}
        tools = phase_provision(development=True)
        if report["phases"].get("provision", {}).get("status") != "passed":
            code = _verdict(required)
            return code
        key = phase_debug_signing(tools)
        if key is None:
            code = _verdict(required)
            return code
        ready, paths = phase_preflight(tools)
        if not ready:
            code = _verdict(required)
            return code
        paths["debug_keystore"] = key
        env, argv, _, _ = _gradle_invocation(paths, "assembleDebug", wheels)
        result = run_tool(argv, timeout=1800, env_extra=env, keep_bytes=8000)
        if result["returncode"] != 0 or not APK.is_file():
            record("development_apk", "failed", **result)
        else:
            cert, diagnostics = _apk_signer_cert_sha256(paths["sdk"], paths["java"], APK, run_tool)
            if cert != report["phases"]["debug_signing"]["cert_sha256"]:
                record("development_apk", "failed", reason="APK does not retain the installed signing identity",
                       signing_verifier=diagnostics)
            else:
                target = artifacts / APK.name
                shutil.copyfile(APK, target)
                record("development_apk", "passed", apk_sha256=hashlib.sha256(target.read_bytes()).hexdigest(),
                       apk_bytes=target.stat().st_size, apk_cert_sha256=cert, artifact=str(target))
        code = _verdict(required)
    except (OSError, ValueError) as error:
        record("development_apk", "failed", reason=str(error)[:400])
    finally:
        report.update(selected_required_phases=list(required), tests_run=False, final_qualification=False,
                      verdict={0: "development_apk_ready", 1: "failed", 3: "pending"}[code])
        text = json.dumps(report, indent=2)
        (REPO / "android-development.json").write_text(text + "\n")
        (artifacts / "android-development.json").write_text(text + "\n")
        print(text)
    return code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", default="dev-apk", choices=["dev-apk", "all", "preflight", "sdist"],
                        help="dev-apk builds for phone work; all runs final qualification after parity")
    parser.add_argument("--wheels", type=pathlib.Path, help="Retained Android wheels (required for dev-apk)")
    args = parser.parse_args()
    if args.phase == "dev-apk":
        if args.wheels is None:
            parser.error("dev-apk needs --wheels; full qualification requires explicit --phase all after parity")
        return development_apk(args.wheels)
    try:
        tools = phase_provision() if args.phase == "all" else {}
        # The managed keystore exists (or the job fails) before any Gradle run; an
        # APK is never produced with an unmanaged, job-ephemeral debug identity.
        debug_keystore = phase_debug_signing(tools) if args.phase == "all" else None
        preflight = phase_preflight(tools) if args.phase in {"all", "preflight"} else None
        if args.phase == "sdist":
            phase_sdist()
        elif args.phase == "preflight":
            pass
        else:
            # The sdist digest is cheap and independent; it always runs in the full phase.
            sdist = phase_sdist()
            signing_ok = report["phases"].get("debug_signing", {}).get("status") == "passed"
            if preflight is not None and preflight[0] and signing_ok:
                preflight[1]["debug_keystore"] = debug_keystore
                wheels = phase_wheels(preflight[1], sdist)
                if phase_build(preflight[1], wheels):
                    if phase_jvm_tests(preflight[1], wheels):
                        if phase_instrumentation_build(preflight[1], wheels):
                            phase_in_app(preflight[1], TEST_APK)
                        else:
                            record("in_app", "skipped", reason="instrumentation build did not pass")
                            record("beta_integration", "skipped", reason="instrumentation build did not pass")
                    else:
                        record("in_app", "skipped", reason="jvm tests did not pass")
                else:
                    record("jvm_tests", "skipped", reason="build did not pass")
                    record("in_app", "skipped", reason="build did not pass")
            else:
                reason = ("preflight did not pass" if preflight is None or not preflight[0]
                          else "managed debug keystore unavailable")
                record("wheels", "skipped", reason=reason)
                record("build", "skipped", reason=reason)
                record("jvm_tests", "skipped", reason=reason)
                record("in_app", "skipped", reason=reason)
    finally:
        required = {"preflight": ("preflight",), "sdist": ("sdist",)}.get(args.phase, REQUIRED_PHASES)
        report["selected_required_phases"] = list(required)
        code = _verdict(required)
        report["verdict"] = {0: "passed", 1: "failed", 2: "failed", 3: "pending"}[code]
        (REPO / "android-feasibility.json").write_text(json.dumps(report, indent=2))
        print(json.dumps(report))
    return code


if __name__ == "__main__":
    sys.exit(main())
