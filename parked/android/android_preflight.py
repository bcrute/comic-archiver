"""Run in a normal terminal to check the attached Android test device.

Only ADB device enumeration and fixed, read-only OS properties are requested.
This does not install anything, open Tapas, read app data or change the phone.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import sys


PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT))
from comic_archive.state_paths import session_path


def run(arguments, timeout=15):
    try:
        result = subprocess.run(arguments, capture_output=True, timeout=timeout,
                                check=False, shell=False)
    except subprocess.TimeoutExpired:
        return None, "timeout"
    except OSError:
        return None, "unavailable"
    if result.returncode != 0:
        return None, "failed"
    if len(result.stdout) > 65536:
        return None, "unexpected_output"
    return result.stdout.decode("utf-8", errors="replace"), None


def devices(output):
    """Serials stay internal; never include raw ADB output in the report."""
    found = []
    for line in output.splitlines():
        match = re.match(r"^(\S+)\s+(device|unauthorized|offline|no permissions)\b", line)
        if match:
            serial, state = match.groups()
            connection = ("usb" if re.search(r"\busb:\S+", line) else
                          "emulator" if re.fullmatch(r"emulator-[0-9]+", serial) else "other")
            model = re.search(r"\bmodel:(\S+)", line)
            found.append({"serial": serial, "state": state.replace(" ", "_"),
                          "connection": connection,
                          "model": model.group(1).replace("_", " ")[:100] if model else None})
    return found


def toolchain():
    names = ("adb", "java", "javac", "gradle", "sdkmanager")
    tools = {name: shutil.which(name) is not None for name in names}
    roots = [Path(value).expanduser() for key in ("ANDROID_HOME", "ANDROID_SDK_ROOT")
             if (value := os.environ.get(key))]
    roots.extend((Path.home() / "Android/Sdk", Path("/usr/lib/android-sdk"),
                  Path("/opt/android-sdk"), session_path(PROJECT) / 'mobile/toolchain/sdk'))
    bundled_jdk = session_path(PROJECT) / 'mobile/toolchain/jdk/bin'
    local_jdk = all((bundled_jdk / name).is_file() for name in ('java', 'javac'))
    platforms, build_tools = set(), set()
    for root in roots:
        try:
            for platform in (root / "platforms").glob("android-*/android.jar"):
                match = re.fullmatch(r"android-([0-9]+)", platform.parent.name)
                if match and platform.is_file():
                    platforms.add(int(match[1]))
            for folder in (root / "build-tools").glob("*"):
                if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", folder.name):
                    if all((folder / name).is_file() for name in ("aapt2", "d8", "apksigner", "zipalign")):
                        build_tools.add(folder.name)
        except OSError:
            continue
    return {"tools_on_path": tools, "platform_apis": sorted(platforms),
            "build_tools_versions": sorted(build_tools),
            "local_standalone_jdk": local_jdk,
            "jdk_and_sdk_detected": (local_jdk or (tools["java"] and tools["javac"]))
                and bool(platforms) and bool(build_tools)}


def usb_phone_observations(sysfs=Path('/sys/bus/usb/devices')):
    """Recognize observed Google phones without reading serials or changing USB.

    This is a Linux-only diagnostic, not a universal phone inventory. A phone
    without an ADB interface cannot be unlocked/configured through ADB.
    """
    observations=[]
    try:
        nodes=sorted(sysfs.iterdir())
    except OSError:
        return observations
    for node in nodes:
        try:
            if (node/'idVendor').read_text().strip().lower()!='18d1':continue
            product=(node/'product').read_text().strip()
            if not re.match(r'^(?:Pixel|Nexus)\b',product):continue
            exposed=False
            for interface in node.glob(node.name+':*'):
                values=tuple((interface/name).read_text().strip().lower() for name in
                    ('bInterfaceClass','bInterfaceSubClass','bInterfaceProtocol'))
                if values==('ff','42','01'):exposed=True
            observations.append({'model':''.join(c for c in product if c.isprintable())[:100],
                                 'adb_interface_exposed':exposed})
        except OSError:
            continue
    return observations[:16]


def inspect(device_number=None):
    report = {"schema_version": 2,
              "checked_at": datetime.now(timezone.utc).isoformat(),
              "toolchain": toolchain(), "device_count": 0, "device_states": [],
              "device_candidates": [], "selected_device_number": None,
              "usb_phone_observations": usb_phone_observations(),
              "device": None, "status": "device_not_ready", "reason": None}
    adb = shutil.which("adb")
    if adb is None:
        report["reason"] = "adb_not_installed"
        return report
    output, error = run([adb, "devices", "-l"], timeout=20)
    if error:
        report["reason"] = "adb_" + error
        return report
    attached = devices(output)
    report.update(device_count=len(attached), device_states=[item["state"] for item in attached],
                  device_candidates=[{"number": number, **{key: item[key] for key in
                      ("state", "connection", "model")}} for number, item in enumerate(attached, 1)])
    if (device_number is None and any(not phone['adb_interface_exposed']
            for phone in report['usb_phone_observations'])
            and all(item['connection']=='emulator' for item in attached)):
        report['reason']='usb_debugging_unavailable'
        return report
    if device_number is not None:
        if type(device_number) is not int or not 1 <= device_number <= len(attached):
            report["reason"] = "invalid_device_selection"
            return report
        chosen = device_number - 1
    elif len(attached) == 1:
        chosen = 0
    else:
        # A plugged-in test phone may also be visible over wireless debugging.
        # Prefer the single explicit USB transport; never infer from serials/IPs.
        wired = [index for index, item in enumerate(attached) if item["connection"] == "usb"]
        if len(wired) != 1:
            report["reason"] = "no_device" if not attached else "multiple_devices"
            return report
        chosen = wired[0]
    report["selected_device_number"] = chosen + 1
    selected = attached[chosen]
    serial, state = selected["serial"], selected["state"]
    if state != "device":
        report["reason"] = state
        return report
    properties = {}
    for key, prop in (("model", "ro.product.model"), ("android_version", "ro.build.version.release"),
                      ("api", "ro.build.version.sdk")):
        value, error = run([adb, "-s", serial, "shell", "getprop", prop])
        if error:
            report["reason"] = "device_property_" + error
            return report
        value = value.strip()
        if key == "api":
            if not re.fullmatch(r"[1-9][0-9]{0,2}", value):
                report["reason"] = "unexpected_android_api"
                return report
            properties[key] = int(value)
        else:
            properties[key] = "".join(c for c in value if c.isprintable())[:100]
    report.update(device=properties, reason=None, status=("ready_for_build_setup"
        if report["toolchain"]["jdk_and_sdk_detected"] else "device_ready_build_tools_missing"))
    return report


def save(report, project=PROJECT):
    """Publish one complete report in the existing ignored session directory."""
    folder = Path(project)
    for part in (session_path(folder), session_path(folder) / 'mobile'):
        folder = part
        if folder.is_symlink():
            raise OSError("Symlink in report directory")
        folder.mkdir(mode=0o700, exist_ok=True)
    destination = folder / "preflight.json"
    if destination.is_symlink():
        raise OSError("Symlink in report destination")
    descriptor, temporary = tempfile.mkstemp(prefix=".preflight-", suffix=".json", dir=folder)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            json.dump(report, file, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", type=int, metavar="NUMBER",
                        help="select a numbered candidate from the current ADB list")
    args = parser.parse_args(argv)
    report = inspect(args.device)
    try:
        save(report)
    except OSError:
        print("Cannot save the Android preflight report; check project permissions.")
        return 1
    print(json.dumps(report, indent=2))
    print("Saved " + str(session_path(PROJECT) / 'mobile/preflight.json'))
    reason = report["reason"]
    if reason == "unauthorized":
        print("Unlock the phone and accept its USB debugging prompt, then run this command again.")
    elif reason == "no_device":
        print("Enable Developer options → USB debugging on the phone and check the USB cable, then retry.")
    elif reason == 'usb_debugging_unavailable':
        print('A physical phone is connected over USB but exposes no ADB debugging interface.')
        print('Unlock it, enable Developer options → USB debugging and accept the computer authorization prompt.')
        print('No emulator was selected or queried. An explicit --device NUMBER can select an intended ADB target.')
    elif reason == "multiple_devices":
        print("Multiple ADB targets detected. Nothing was disconnected or reserved.")
        print("Choose the test phone's number above, then rerun with --device NUMBER.")
    elif reason == "invalid_device_selection":
        print("Choose a device number from the current candidate list above, then retry.")
    elif reason == "no_permissions":
        print("ADB lacks Linux USB access. Check the Android device's udev setup in your normal terminal.")
    elif reason:
        print("ADB could not complete the check. Run this helper in your normal terminal, outside the managed sandbox.")
    elif report["status"] == "device_ready_build_tools_missing":
        print("The selected Android device is ready. Build tools are still needed before an APK can be built.")
    else:
        print("Selected device and build tools detected. This read-only preflight does not check APK installation or Tapas login.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
