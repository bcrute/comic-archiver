#!/usr/bin/env python3
"""Android feasibility point: toolchain preflight (with job-private provisioning), pinned
zstandard sdist, cp313 Android wheels built from that sdist with cibuildwheel (the builder
Chaquopy's own documentation names for Python 3.13+), whole-app Gradle build installing the
wheels through --find-links plus an exact requirement, and the in-app self-check on a
job-owned emulator. Runs ONLY on a fleet host through the CI fleet helper, never locally.

Inputs: every downloaded archive is pinned in packaging/android/toolchain-pins.json with a
committed sha256 that is verified before extraction. The host buildPython is a task-private
CPython 3.13 matching the app's embedded 3.13 major.minor (Chaquopy requirement), wired to
Gradle explicitly; the host's own 3.14 is never used.

Isolation: any missing tool is provisioned under android/.fleet-sdk (gitignored, job-private,
never host-global). The emulator phase creates its own AVD under a job-private
ANDROID_AVD_HOME, targets one adb serial for every command, uses a private port, and tears
down only the emulator and AVD it created. No host AVD, device or global state is touched.

Verdict: computed from every selected required phase. failed => exit 1, pending (capability
missing and not provisionable, e.g. no KVM) => exit 3, and 0 only when all required evidence
passed. A tool's return code alone is never qualification. Artifacts are retained on failure
as on success.
"""
import argparse
import hashlib
import json
import os
import pathlib
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import time
import urllib.request
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
JOB_DIR = REPO / "android/.fleet-job"
TOOLS_DIR = REPO / "android/.fleet-sdk"
SELF_IDENTITY = "comic-archiver/android-selfcheck/1"
SELF_TAG = "ComicArchiverSelfcheck"
APP_ACTIVITY = "io.github.bcrute.comicarchiver/.MainActivity"
ABI_FILTERS = {"arm64-v8a", "x86_64"}
report = {"phases": {}, "argv": sys.argv[1:], "verdict": None}


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


def _download(url, target, timeout=1800):
    if target.is_file():
        return True
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        urllib.request.urlretrieve(url, target)
        return target.is_file() and target.stat().st_size > 0
    except OSError:
        return False


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
    extractall, and the SDK's bin/ scripts keep an executable mode even when the archive
    records none (the observed failure: sdkmanager spawned EACCES at mode 0o644)."""
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
                with bundle.open(member) as source, open(resolved, "wb") as target:
                    shutil.copyfileobj(source, target)
                stored = member.external_attr >> 16
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
    "https://dl.google.com/android/repository/sys-img/google_apis/sys-img2-1.xml",
    "https://dl.google.com/android/repository/sys-img/android/sys-img2-1.xml",
)


def _sdk_repository_packages():
    """The repository's own stable package table, fetched over the same TLS path every
    pinned input already uses. Two fleet attempts (4 and 5) showed sdkmanager's JVM
    fetching no manifests at all on this network even with explicit proxy properties,
    while urllib reached dl.google.com without complaint; so the JVM is never used to
    fetch. Each package's published sha1 is returned for verification before extraction.
    """
    packages = {}
    for listing in REPOSITORY_XMLS:
        try:
            with urllib.request.urlopen(listing, timeout=120) as response:
                # The listing is dl.google.com over TLS and every artifact it names is
                # sha1-verified before use; only path/sha1/url are read from it.
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
    return True, details["sha1"]


def phase_provision():
    """Provision job-private JDK, Android SDK tools, Gradle, host CPython 3.13 and cibuildwheel.

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
    if python313.is_file() and not venv_python.is_file():
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
        # stable package table (never guessed pins) over the Python network path; the JVM
        # never fetches. sdkmanager stays for the one thing it does offline: writing the
        # accepted-license records Gradle and avdmanager check for.
        packages = _sdk_repository_packages()
        if packages is None:
            steps["sdk_packages"] = {"status": "unavailable",
                                     "reason": "repository listing unfetchable over the Python path"}
        else:
            names = set(packages)
            platforms = sorted((name for name in names if re.fullmatch(r"platforms;android-\d+", name)),
                               key=lambda name: int(name.rsplit("-", 1)[1]))
            ndks = sorted((name for name in names if re.fullmatch(r"ndk;\d+(\.\d+)+", name)),
                          key=lambda name: tuple(int(piece) for piece in name.split(";")[1].split(".")))
            build_tools = sorted((name for name in names if re.fullmatch(r"build-tools;\d+(\.\d+){1,2}", name)),
                                 key=lambda name: tuple(int(piece) for piece in name.split(";")[1].split(".")))
            images = sorted(name for name in names
                            if re.fullmatch(r"system-images;android-\d+;[a-z0-9_]+;x86_64", name))
            # A plain-tag image boots the emulator for the self-check; the 16 KB-page
            # variants are a separate qualification, not this phase's default.
            images = [name for name in images if "ps16k" not in name] or images
            wanted = ["platform-tools", "emulator"]
            wanted += [group[-1] for group in (platforms, ndks, build_tools, images) if group]
            installed, failures = [], {}
            for name in wanted:
                ok, note = (_install_sdk_package(name, packages[name], sdk) if name in packages
                            else (False, "not offered for linux by the repository"))
                if ok:
                    installed.append({"package": name, "sha1": note})
                else:
                    failures[name] = note
            licenses = run_tool([str(manager), f"--sdk_root={sdk}", "--licenses"], timeout=600,
                                input_bytes="y\n" * 20,
                                env_extra={"JAVA_HOME": java_home,
                                           "PATH": f"{pathlib.Path(java).parent}:{os.environ['PATH']}"})
            steps["sdk_packages"] = {"source": list(REPOSITORY_XMLS),
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
    record("provision", "passed" if all(tools[key] for key in ("java", "sdk", "gradle", "python313",
                                                               "cibuildwheel")) else "pending",
           steps=steps, tools=tools)
    return tools


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
           kvm=pathlib.Path("/dev/kvm").exists())
    return required_ok, {"sdk": sdk_path, "gradle": gradle, "java": java, "buildpython": buildpython}


def phase_sdist():
    cache = SDIST_DIR / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / PINNED_SDIST
    try:
        if not target.is_file():
            with urllib.request.urlopen(PYPI_JSON, timeout=60) as response:
                links = json.load(response)["urls"]
            url = next(item["url"] for item in links if item["filename"] == PINNED_SDIST)
            urllib.request.urlretrieve(url, target)
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
    except (OSError, ValueError, StopIteration) as error:
        record("sdist", "failed", error=type(error).__name__)
        return None
    if digest != PINNED_SHA256:
        record("sdist", "failed", digest=digest)
        return None
    record("sdist", "passed", sha256=digest, path=str(target))
    return target


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
    env = {"ANDROID_HOME": str(paths["sdk"]),
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
           archs=archs_seen, returncode=result["returncode"], tail=result["tail"])
    return output if ok else None


def phase_build(paths, wheels):
    if wheels is None:
        record("build", "skipped", reason="verified Android wheels unavailable")
        return False
    if not paths.get("gradle"):
        record("build", "pending", reason="no gradle available")
        return False
    env = {"ANDROID_HOME": str(paths["sdk"]), "JAVA_HOME": str(pathlib.Path(paths["java"]).parent.parent)}
    result = run_tool([paths["gradle"], "--no-daemon", "-p", str(REPO / "android"), "assembleDebug",
                       f"-PzstandardWheels={wheels}",
                       f"-PbuildPython={paths['buildpython']}"], timeout=3600, env_extra=env)
    ok = result["returncode"] == 0 and APK.is_file()
    tail = result["tail"]
    classification = None
    if not ok and re.search(r"zstandard|chaquopy.*pip|error: (command|link|compile)", tail, re.IGNORECASE):
        classification = "wheel_install_failed"
    wheel_abis = []
    if APK.is_file():
        with zipfile.ZipFile(APK) as archive:
            wheel_abis = sorted({name.split("/")[1] for name in archive.namelist()
                                 if name.startswith("lib/") and "zstandard" in name.lower()})
    record("build", "passed" if ok else "failed", minutes=result["minutes"], classification=classification,
           apk=str(APK) if APK.is_file() else None, wheel_abis=wheel_abis,
           buildpython=paths.get("buildpython"), tail=tail)
    return ok


def _free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _evaluate_selfcheck(line):
    """Missing evidence is not success: every named check must be present and passed."""
    if not isinstance(line, dict) or not isinstance(line.get("zstandard"), dict):
        return {"selfcheck_line": False}
    zstd = line["zstandard"]
    version = str(zstd.get("version", "")).split(".")
    return {
        "selfcheck_line": True,
        "imported": zstd.get("imported") is True,
        "version_floor": len(version) >= 2 and (int(version[0]), int(version[1])) >= (0, 25),
        "roundtrip": zstd.get("roundtrip") is True,
        "trailing_frame": zstd.get("trailing_frame") is True,
        "truncation": zstd.get("truncation") is True,
        "checksum": zstd.get("checksum") == "rejected",
        "window_bound": zstd.get("window_bound") is True,
        "page_size": isinstance(line.get("page_size"), int),
    }


def phase_in_app(paths):
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
        return
    if not pathlib.Path("/dev/kvm").exists():
        record("in_app", "pending", reason="no /dev/kvm on this host; an emulator cannot run")
        return

    avd_home = JOB_DIR / "avd"
    avd_home.mkdir(parents=True, exist_ok=True)
    avd_name = f"comic-archiver-feasibility-{os.getpid()}"
    env_java = {"JAVA_HOME": str(pathlib.Path(paths["java"]).parent.parent),
                "ANDROID_AVD_HOME": str(avd_home),
                "PATH": f"{pathlib.Path(paths['java']).parent}:{os.environ['PATH']}"}
    created = run_tool([str(avdmanager), "create", "avd", "-n", avd_name, "-k", image, "--force"],
                       timeout=300, env_extra=env_java)
    if created["returncode"] != 0:
        record("in_app", "failed", reason="avd creation failed", tail=created["tail"])
        shutil.rmtree(avd_home, ignore_errors=True)
        return
    serial, process = None, None
    try:
        port = _free_port()
        port += port % 2  # adb pairs the console port with port+1; keep the pair contiguous.
        serial = f"emulator-{port}"
        process = subprocess.Popen(
            [str(emulator), "-avd", avd_name, "-port", str(port), "-no-window", "-no-snapshot",
             "-no-audio", "-no-boot-anim", "-gpu", "swiftshader_indirect"],
            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
            env={**os.environ, "ANDROID_AVD_HOME": str(avd_home), "ANDROID_HOME": str(sdk)})
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
            return
        properties = {}
        for key, prop in (("abi", "ro.product.cpu.abi"), ("api", "ro.build.version.sdk")):
            properties[key] = run_tool([str(adb), "-s", serial, "shell", "getprop", prop], timeout=60)["tail"].strip()
        install = run_tool([str(adb), "-s", serial, "install", "-r", str(APK)], timeout=600)
        run_tool([str(adb), "-s", serial, "logcat", "-c"], timeout=60)
        run_tool([str(adb), "-s", serial, "shell", "am", "start", "-n", APP_ACTIVITY,
                  "--ez", "selfcheck", "true"], timeout=120)
        time.sleep(20)
        log = run_tool([str(adb), "-s", serial, "logcat", "-d", "-s", SELF_TAG, "-v", "raw"], timeout=120)
        line = next((json.loads(piece) for piece in log["tail"].splitlines()
                     if piece.strip().startswith("{") and SELF_IDENTITY in piece and _json_ok(piece)), None)
        checks = _evaluate_selfcheck(line)
        checks["abi_allowed"] = properties["abi"] in ABI_FILTERS
        record("in_app", "passed" if checks and all(checks.values()) else "failed",
               install_ok=install["returncode"] == 0, evidence=line, checks=checks,
               abi=properties["abi"], api=properties["api"], tail=log["tail"][:2000])
    except ValueError:
        record("in_app", "failed", reason="selfcheck line was not valid JSON")
    finally:
        if serial:
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


REQUIRED_PHASES = ("provision", "preflight", "sdist", "wheels", "build", "in_app")


def _verdict():
    # Every selected required phase counts: a single failed phase fails the point, a
    # capability gap (pending) or a phase skipped behind it holds it pending, and 0
    # requires every phase's evidence to have passed. Provision counts as required:
    # its remedy is what makes the later phases runnable on a bare host.
    statuses = [report["phases"][phase]["status"] for phase in REQUIRED_PHASES if phase in report["phases"]]
    if not statuses or "failed" in statuses:
        return 1
    if "pending" in statuses or "skipped" in statuses:
        return 3
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", default="all", choices=["all", "preflight", "sdist"])
    args = parser.parse_args()
    try:
        tools = phase_provision() if args.phase == "all" else {}
        preflight = phase_preflight(tools) if args.phase in {"all", "preflight"} else None
        if args.phase == "sdist":
            phase_sdist()
        elif args.phase == "preflight":
            pass
        else:
            # The sdist digest is cheap and independent; it always runs in the full phase.
            sdist = phase_sdist()
            if preflight is not None and preflight[0]:
                wheels = phase_wheels(preflight[1], sdist)
                if phase_build(preflight[1], wheels):
                    phase_in_app(preflight[1])
                else:
                    record("in_app", "skipped", reason="build did not pass")
            else:
                record("wheels", "skipped", reason="preflight did not pass")
                record("build", "skipped", reason="preflight did not pass")
                record("in_app", "skipped", reason="preflight did not pass")
    finally:
        code = _verdict()
        report["verdict"] = {0: "passed", 1: "failed", 2: "failed", 3: "pending"}[code]
        (REPO / "android-feasibility.json").write_text(json.dumps(report, indent=2))
        print(json.dumps(report))
    return code


if __name__ == "__main__":
    sys.exit(main())
