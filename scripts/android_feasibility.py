#!/usr/bin/env python3
"""Android feasibility point: toolchain preflight, pinned zstandard sdist, whole-app build
(with the sdist cross-compiled for Android by Chaquopy's NDK toolchain), and the in-app
self-check on a job-owned emulator. Runs ONLY on a fleet host through the CI fleet helper,
never locally.

Isolation: the emulator phase creates its own AVD under a job-private ANDROID_AVD_HOME,
targets one adb serial for every command, uses a private port, and tears down only the
emulator and AVD it created. No host AVD, device or global state is read, mutated or killed.

Verdict: computed from every selected required phase. failed => exit 1, pending (capability
missing, including a missing emulator) => exit 3, and 0 only when all required evidence
passed. A tool's return code alone is never qualification. Artifacts (this report, the
gradle tail, the self-check line) are retained on failure as on success.
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

REPO = pathlib.Path(__file__).resolve().parent.parent
SDIST_DIR = REPO / "packaging/android/zstandard-wheel"
PINNED_SDIST = "zstandard-0.25.0.tar.gz"
PINNED_SHA256 = "7713e1179d162cf5c7906da876ec2ccb9c3a9dcbdffef0cc7f70c3667a205f0b"
PYPI_JSON = "https://pypi.org/pypi/zstandard/0.25.0/json"
APK = REPO / "android/app/build/outputs/apk/debug/app-debug.apk"
JOB_DIR = REPO / "android/.fleet-job"
SELF_IDENTITY = "comic-archiver/android-selfcheck/1"
SELF_TAG = "ComicArchiverSelfcheck"
APP_ACTIVITY = "io.github.bcrute.comicarchiver/.MainActivity"
ABI_FILTERS = {"arm64-v8a", "x86_64"}
# Required for the build: Java 17+, an Android SDK with an NDK (the sdist cross-compile uses
# it), Gradle >= 8.10. Optional: emulator tooling (in-app phase only).
JAVA_MIN = 17
GRADLE_MIN = (8, 10)
report = {"phases": {}, "argv": sys.argv[1:], "verdict": None}


def record(phase, status, **facts):
    report["phases"][phase] = {"status": status, **facts}
    print(f"[{phase}] {status}" + (f": {facts}" if facts else ""), flush=True)


def run_tool(argv, cwd=None, timeout=2400, env_extra=None):
    started = time.monotonic()
    env = {**os.environ, **(env_extra or {})}
    try:
        completed = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env)
        code, out = completed.returncode, completed.stdout + completed.stderr
    except subprocess.TimeoutExpired as expired:
        code, out = 124, str(expired)[:2000]
    return {"argv": [str(piece) for piece in argv], "minutes": round((time.monotonic() - started) / 60, 1),
            "returncode": code, "tail": out[-4000:]}


def _tool_output(argv, timeout=120):
    if shutil.which(argv[0]) is None:
        return None
    return run_tool(argv, timeout=timeout)["tail"]


def _remedy(missing, sdk):
    """Exact isolated remedy commands for a job-private SDK; recorded, never executed."""
    private_sdk = REPO / "android/.fleet-sdk"
    lines = [f'mkdir -p "{private_sdk}"']
    if "avdmanager" in missing or "emulator" in missing:
        lines.append(f'"{sdk}/cmdline-tools/latest/bin/sdkmanager" --sdk_root="{private_sdk}" '
                     '"cmdline-tools;latest" "emulator"')
    if "system_image" in missing:
        lines.append(f'"{sdk}/cmdline-tools/latest/bin/sdkmanager" --sdk_root="{private_sdk}" '
                     '"system-images;android-36;google_apis;x86_64"')
    if "ndk" in missing:
        lines.append(f'"{sdk}/cmdline-tools/latest/bin/sdkmanager" --sdk_root="{private_sdk}" "ndk;28.2.13676358"')
    report["remedy"] = {"sdk_root": str(private_sdk), "commands": lines,
                        "note": "Run into the job-private SDK above; never a host-global install."}


def phase_preflight():
    found, missing = {}, set()
    java_text = _tool_output(["java", "-version"])
    found["java"] = bool(java_text)
    java_version = None
    if java_text:
        match = re.search(r'version "(\d+)', java_text)
        java_version = int(match[1]) if match else None
        found["java"] = java_version is not None and java_version >= JAVA_MIN
        if not found["java"]:
            missing.add("java")
    else:
        missing.add("java")

    sdk = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
    sdk_path = pathlib.Path(sdk) if sdk else None
    found["android_sdk"] = bool(sdk_path and sdk_path.is_dir())
    if not found["android_sdk"]:
        missing.add("android_sdk")
        record("preflight", "pending", missing=sorted(missing), java=java_version)
        _remedy(missing, sdk_path)
        return False

    found["ndk"] = any(sdk_path.joinpath("ndk", entry).is_dir() for entry in
                       (sdk_path / "ndk").iterdir()) if (sdk_path / "ndk").is_dir() else False
    if not found["ndk"]:
        missing.add("ndk")

    gradle_text = _tool_output(["gradle", "--version"])
    gradle_version = None
    if gradle_text:
        match = re.search(r"^Gradle (\d+(?:\.\d+)?)", gradle_text, re.MULTILINE)
        if match:
            gradle_version = match[1]
            digits = tuple(int(piece) for piece in gradle_version.split(".")[:2])
            if digits < GRADLE_MIN:
                missing.add("gradle")
        else:
            missing.add("gradle")
    else:
        missing.add("gradle")

    # Optional: emulator tooling. Missing means the in-app phase is pending, not a failure.
    optional = {}
    optional["avdmanager"] = (sdk_path / "cmdline-tools/latest/bin/avdmanager").is_file()
    optional["emulator"] = (sdk_path / "emulator/emulator").is_file()
    images = []
    images_root = sdk_path / "system-images"
    if images_root.is_dir():
        images = [f"system-images;{android};{profile};{abi}"
                  for android in sorted(p.name for p in images_root.iterdir() if p.is_dir())
                  for profile in sorted(p.name for p in (images_root / android).iterdir() if p.is_dir())
                  for abi in sorted(p.name for p in (images_root / android / profile).iterdir() if p.is_dir())]
    optional["system_images"] = images
    optional["kvm"] = pathlib.Path("/dev/kvm").exists()

    required_ok = not missing
    record("preflight", "passed" if required_ok else "pending", missing=sorted(missing),
           java=java_version, gradle=gradle_version, optional=optional, sdk=str(sdk_path))
    if missing:
        _remedy(missing, sdk_path)
    return required_ok


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


def phase_build(sdist):
    if sdist is None:
        record("build", "skipped", reason="verified sdist unavailable")
        return False
    gradle = shutil.which("gradle")
    if gradle is None:
        record("build", "pending", reason="no gradle on PATH")
        return False
    result = run_tool([gradle, "--no-daemon", "-p", str(REPO / "android"), "assembleDebug",
                       f"-PzstandardSdist={sdist}"])
    ok = result["returncode"] == 0 and APK.is_file()
    tail = result["tail"]
    classification = None
    if not ok and re.search(r"zstandard|chaquopy.*pip|error: (command|link|compile)", tail, re.IGNORECASE):
        classification = "wheel_cross_build_failed"
    # Informational: which ABIs carry a zstandard native library in the APK. The binding
    # evidence is the in-app import and hostile-frame set, never this listing alone.
    wheel_abis = []
    if APK.is_file():
        import zipfile
        with zipfile.ZipFile(APK) as archive:
            wheel_abis = sorted({name.split("/")[1] for name in archive.namelist()
                                 if name.startswith("lib/") and "zstandard" in name.lower()})
    record("build", "passed" if ok else "failed", minutes=result["minutes"],
           classification=classification, apk=str(APK) if APK.is_file() else None,
           wheel_abis=wheel_abis, tail=tail)
    return ok


def _free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _adb(sdk):
    return str(sdk / "platform-tools/adb")


def phase_in_app():
    sdk = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
    if not sdk:
        record("in_app", "pending", reason="no Android SDK")
        return
    sdk_path = pathlib.Path(sdk)
    preflight = report["phases"].get("preflight", {}).get("optional", {})
    avdmanager = sdk_path / "cmdline-tools/latest/bin/avdmanager"
    emulator = sdk_path / "emulator/emulator"
    images = preflight.get("system_images") or []
    image = next((item for item in images if item.endswith(";x86_64")),
                 next((item for item in images if item.endswith(";arm64-v8a")), None))
    if not (avdmanager.is_file() and emulator.is_file() and image):
        record("in_app", "pending", reason="no avdmanager/emulator/system image",
               avdmanager=avdmanager.is_file(), emulator=emulator.is_file(), system_image=image)
        return

    # A job-owned AVD in a job-private home: nothing shared is read, reused or mutated.
    avd_home = JOB_DIR / "avd"
    avd_home.mkdir(parents=True, exist_ok=True)
    avd_name = f"comic-archiver-feasibility-{os.getpid()}"
    created = run_tool([str(avdmanager), "create", "avd", "-n", avd_name, "-k", image, "--force"],
                       timeout=300, env_extra={"ANDROID_AVD_HOME": str(avd_home)})
    if created["returncode"] != 0:
        record("in_app", "failed", reason="avd creation failed", tail=created["tail"])
        shutil.rmtree(avd_home, ignore_errors=True)
        return
    serial = None
    process = None
    try:
        port = _free_port()
        serial = f"emulator-{port}"
        process = subprocess.Popen(
            [str(emulator), "-avd", avd_name, "-port", str(port), "-no-window", "-no-snapshot",
             "-no-audio", "-no-boot-anim", "-gpu", "swiftshader_indirect"],
            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
            env={**os.environ, "ANDROID_AVD_HOME": str(avd_home)})
        run_tool([_adb(sdk_path), "-s", serial, "wait-for-device"], timeout=600)
        booted = False
        deadline = time.monotonic() + 900
        while time.monotonic() < deadline:
            ready = run_tool([_adb(sdk_path), "-s", serial, "shell", "getprop", "sys.boot_completed"],
                             timeout=60)
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
            probe = run_tool([_adb(sdk_path), "-s", serial, "shell", "getprop", prop], timeout=60)
            properties[key] = probe["tail"].strip()
        properties["page_size"] = "unknown"
        for prop in ("ro.bionic.page_size", "ro.product.page_size"):
            probe = run_tool([_adb(sdk_path), "-s", serial, "shell", "getprop", prop], timeout=60)
            value = probe["tail"].strip()
            if value:
                properties["page_size"] = value
                break
        install = run_tool([_adb(sdk_path), "-s", serial, "install", "-r", str(APK)], timeout=600)
        run_tool([_adb(sdk_path), "-s", serial, "logcat", "-c"], timeout=60)
        run_tool([_adb(sdk_path), "-s", serial, "shell", "am", "start", "-n", APP_ACTIVITY,
                  "--ez", "selfcheck", "true"], timeout=120)
        time.sleep(20)
        log = run_tool([_adb(sdk_path), "-s", serial, "logcat", "-d", "-s", SELF_TAG, "-v", "raw"],
                       timeout=120)
        line = None
        for piece in log["tail"].splitlines():
            try:
                candidate = json.loads(piece)
            except ValueError:
                continue
            if isinstance(candidate, dict) and candidate.get("identity") == SELF_IDENTITY:
                line = candidate
                break
        checks = _evaluate_selfcheck(line)
        checks["abi_allowed"] = properties["abi"] in ABI_FILTERS
        record("in_app", "passed" if checks and all(checks.values()) else "failed",
               install_ok=install["returncode"] == 0, evidence=line, checks=checks,
               abi=properties["abi"], api=properties["api"], device_page_size=properties["page_size"],
               tail=log["tail"][:2000])
    finally:
        # Bounded teardown of only this job's emulator and AVD.
        if serial:
            run_tool([_adb(sdk_path), "-s", serial, "emu", "kill"], timeout=120)
        if process is not None:
            for _ in range(60):
                if process.poll() is not None:
                    break
                time.sleep(1)
            process.kill()
        run_tool([str(avdmanager), "delete", "avd", "-n", avd_name], timeout=120,
                 env_extra={"ANDROID_AVD_HOME": str(avd_home)})
        shutil.rmtree(avd_home, ignore_errors=True)


def _evaluate_selfcheck(line):
    """Missing evidence is not success: every named check must be present and passed."""
    if not isinstance(line, dict):
        return {"selfcheck_line": False}
    zstd = line.get("zstandard")
    if not isinstance(zstd, dict):
        return {"selfcheck_line": False}
    version = str(zstd.get("version", "")).split(".")
    version_ok = len(version) >= 2 and (int(version[0]), int(version[1])) >= (0, 25)
    return {
        "selfcheck_line": True,
        "imported": zstd.get("imported") is True,
        "version_floor": version_ok,
        "roundtrip": zstd.get("roundtrip") is True,
        "trailing_frame": zstd.get("trailing_frame") is True,
        "truncation": zstd.get("truncation") is True,
        "checksum": zstd.get("checksum") == "rejected",
        "window_bound": zstd.get("window_bound") is True,
        "page_size": isinstance(line.get("page_size"), int),
    }


def _verdict():
    statuses = [phase["status"] for phase in report["phases"].values()]
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
        preflight_ok = phase_preflight() if args.phase in {"all", "preflight"} else None
        if args.phase == "sdist":
            phase_sdist()
        elif args.phase == "preflight":
            pass
        elif preflight_ok:
            # The wheel cross-build happens inside the app build: Chaquopy's pip compiles the
            # pinned sdist against its NDK toolchain (NDK presence was required above).
            if phase_build(phase_sdist()):
                phase_in_app()
            else:
                record("in_app", "skipped", reason="build did not pass")
        else:
            record("build", "skipped", reason="preflight did not pass")
            record("in_app", "skipped", reason="preflight did not pass")
    finally:
        code = _verdict()
        report["verdict"] = {0: "passed", 1: "failed", 3: "pending"}[code]
        (REPO / "android-feasibility.json").write_text(json.dumps(report, indent=2))
        print(json.dumps(report))
        return code


if __name__ == "__main__":
    sys.exit(main())
