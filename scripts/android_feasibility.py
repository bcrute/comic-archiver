#!/usr/bin/env python3
"""Android feasibility point: toolchain preflight (with job-private provisioning), pinned
zstandard sdist, whole-app build (the sdist cross-compiled for Android by Chaquopy's NDK
toolchain), and the in-app self-check on a job-owned emulator. Runs ONLY on a fleet host
through the CI fleet helper, never locally.

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
import time
import urllib.request
import zipfile

REPO = pathlib.Path(__file__).resolve().parent.parent
SDIST_DIR = REPO / "packaging/android/zstandard-wheel"
PINNED_SDIST = "zstandard-0.25.0.tar.gz"
PINNED_SHA256 = "7713e1179d162cf5c7906da876ec2ccb9c3a9dcbdffef0cc7f70c3667a205f0b"
PYPI_JSON = "https://pypi.org/pypi/zstandard/0.25.0/json"
APK = REPO / "android/app/build/outputs/apk/debug/app-debug.apk"
JOB_DIR = REPO / "android/.fleet-job"
TOOLS_DIR = REPO / "android/.fleet-sdk"
GRADLE_PIN = ("gradle", "8.13", "https://services.gradle.org/distributions/gradle-8.13-bin.zip")
CMDLINE_TOOLS_ZIP = "https://dl.google.com/android/repository/commandlinetools-linux-11076708_latest.zip"
ADOPTIUM = "https://api.adoptium.net/v3/binary/latest/17/ga/linux/{arch}/jdk/hotspot/normal/eclipse"
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


def _extract(archive, destination):
    destination.mkdir(parents=True, exist_ok=True)
    try:
        shutil.unpack_archive(str(archive), str(destination))
        return True
    except (OSError, shutil.Error, ValueError):
        return False


def phase_provision():
    """Provision job-private JDK, Android SDK tools and Gradle when the host lacks them.

    Everything lands under android/.fleet-sdk and is thrown away with the job; no host-global
    toolchain is created or modified. Missing pieces are reported, never fatal here.
    """
    steps = {}
    arch = {"x86_64": "x64", "aarch64": "aarch64"}.get(os.uname().machine, "x64")
    jdk = TOOLS_DIR / "jdk"
    if shutil.which("java") is None and not (jdk / "bin/java").is_file():
        bundle = TOOLS_DIR / "downloads/jdk17.tar.gz"
        ok = _download(ADOPTIUM.format(arch=arch), bundle) and _extract(bundle, TOOLS_DIR / "downloads/jdk")
        if ok:
            inner = next((path for path in (TOOLS_DIR / "downloads/jdk").iterdir() if path.is_dir()), None)
            if inner is not None and inner != jdk:
                inner.rename(jdk)
        steps["jdk"] = "installed" if (jdk / "bin/java").is_file() else "unavailable"
    java = shutil.which("java") or (str(jdk / "bin/java") if (jdk / "bin/java").is_file() else None)
    java_home = str(pathlib.Path(java).parent.parent) if java else None

    sdk = TOOLS_DIR / "sdk"
    manager = sdk / "cmdline-tools/latest/bin/sdkmanager"
    if not manager.is_file():
        bundle = TOOLS_DIR / "downloads/commandlinetools.zip"
        if _download(CMDLINE_TOOLS_ZIP, bundle):
            with zipfile.ZipFile(bundle) as archive:
                archive.extractall(str(TOOLS_DIR / "downloads/cmdline-tools"))
            inner = TOOLS_DIR / "downloads/cmdline-tools/cmdline-tools"
            (sdk / "cmdline-tools").mkdir(parents=True, exist_ok=True)
            inner.rename(sdk / "cmdline-tools/latest")
        steps["cmdline_tools"] = "installed" if manager.is_file() else "unavailable"

    gradle = shutil.which("gradle")
    if gradle is None and not (TOOLS_DIR / "gradle/bin/gradle").is_file():
        bundle = TOOLS_DIR / "downloads/gradle.zip"
        if _download(GRADLE_PIN[2], bundle) and _extract(bundle, TOOLS_DIR / "downloads/gradle-x"):
            inner = next((path for path in (TOOLS_DIR / "downloads/gradle-x").iterdir() if path.is_dir()), None)
            if inner is not None:
                inner.rename(TOOLS_DIR / "gradle")
        steps["gradle"] = "installed" if (TOOLS_DIR / "gradle/bin/gradle").is_file() else "unavailable"
    gradle = gradle or (str(TOOLS_DIR / "gradle/bin/gradle") if (TOOLS_DIR / "gradle/bin/gradle").is_file() else None)

    if manager.is_file() and java:
        # Accept licenses, then install what the build and emulator phases need; package
        # versions are chosen from the repository's own listing, never guessed pins. The
        # listing is kept whole: its package table is far larger than the default log tail.
        sdk_env = {"JAVA_HOME": java_home, "PATH": f"{pathlib.Path(java).parent}:{os.environ['PATH']}"}
        listing = run_tool([str(manager), f"--sdk_root={sdk}", "--list"], timeout=900, keep_bytes=2_000_000,
                           env_extra=sdk_env)
        names = {match.strip() for match in
                 re.findall(r"^\s*((?:platforms|ndk|build-tools|system-images|emulator|platform-tools)[;\w.]*)\s*\|",
                            listing["tail"], re.MULTILINE)}
        platforms = sorted((name for name in names if re.fullmatch(r"platforms;android-\d+", name)),
                           key=lambda name: int(name.rsplit("-", 1)[1]))
        ndks = sorted((name for name in names if re.fullmatch(r"ndk;\d+(\.\d+)+", name)),
                      key=lambda name: tuple(int(piece) for piece in name.split(";")[1].split(".")))
        build_tools = sorted((name for name in names if re.fullmatch(r"build-tools;\d+(\.\d+){1,2}", name)),
                             key=lambda name: tuple(int(piece) for piece in name.split(";")[1].split(".")))
        images = sorted(name for name in names
                        if re.fullmatch(r"system-images;android-\d+;[a-z0-9_]+;x86_64", name))
        wanted = ["platform-tools", "emulator"]
        wanted += [group[-1] for group in (platforms, ndks, build_tools, images) if group]
        run_tool([str(manager), f"--sdk_root={sdk}", "--licenses"], timeout=600, input_bytes="y\n" * 20,
                 env_extra=sdk_env)
        install = run_tool([str(manager), f"--sdk_root={sdk}", "--install", *wanted], timeout=2400,
                           env_extra=sdk_env)
        steps["sdk_packages"] = {"installed": wanted, "returncode": install["returncode"],
                                 "tail": install["tail"][-1500:]}

    tools = {"java": java,
             "sdk": str(sdk) if manager.is_file() else os.environ.get("ANDROID_HOME"),
             "gradle": gradle}
    record("provision", "passed" if all(tools.values()) else "pending", steps=steps, tools=tools)
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
    for candidate in candidates:
        probe = run_tool([candidate, "--version"], timeout=300)
        match = re.search(r"^Gradle (\d+(?:\.\d+)?)", probe["tail"], re.MULTILINE)
        if match:
            digits = tuple(int(piece) for piece in match[1].split(".")[:2])
            if digits >= (8, 10):
                gradle, gradle_version = candidate, match[1]
                break
    if gradle is None:
        missing.add("gradle")

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
           kvm=pathlib.Path("/dev/kvm").exists())
    return required_ok, {"sdk": sdk_path, "gradle": gradle, "java": java}


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


def phase_build(paths, sdist):
    if sdist is None:
        record("build", "skipped", reason="verified sdist unavailable")
        return False
    if not paths.get("gradle"):
        record("build", "pending", reason="no gradle available")
        return False
    env = {"ANDROID_HOME": str(paths["sdk"]), "JAVA_HOME": str(pathlib.Path(paths["java"]).parent.parent)}
    result = run_tool([paths["gradle"], "--no-daemon", "-p", str(REPO / "android"), "assembleDebug",
                       f"-PzstandardSdist={sdist}"], timeout=3600, env_extra=env)
    ok = result["returncode"] == 0 and APK.is_file()
    tail = result["tail"]
    classification = None
    if not ok and re.search(r"zstandard|chaquopy.*pip|error: (command|link|compile)", tail, re.IGNORECASE):
        classification = "wheel_cross_build_failed"
    wheel_abis = []
    if APK.is_file():
        with zipfile.ZipFile(APK) as archive:
            wheel_abis = sorted({name.split("/")[1] for name in archive.namelist()
                                 if name.startswith("lib/") and "zstandard" in name.lower()})
    record("build", "passed" if ok else "failed", minutes=result["minutes"], classification=classification,
           apk=str(APK) if APK.is_file() else None, wheel_abis=wheel_abis, tail=tail)
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


REQUIRED_PHASES = ("provision", "preflight", "sdist", "build", "in_app")


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
                if phase_build(preflight[1], sdist):
                    phase_in_app(preflight[1])
                else:
                    record("in_app", "skipped", reason="build did not pass")
            else:
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
