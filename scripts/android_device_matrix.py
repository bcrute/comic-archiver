#!/usr/bin/env python3
"""Run the release APK on the release device matrix's emulators.

Run as `python3 scripts/android_feasibility.py --phase device-matrix --apk <apk>` in a
fleet job of project comic-archiver/android, so the job gets a disposable guest.

Each environment is a fresh job-private emulator from the fleet's vendor SDK manager:
API 24 (the minimum) and API 36 with 16 KB memory pages (modern, target and 16 KB).
The exact release APK, after the publisher's own inspection, is installed and
started; the row passes only if it launches, stays running in front with embedded
Python loaded, and nothing of the app's crashes. The arm64 row comes from a phone.
See docs/android-release-contract.md for the evidence this feeds.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import time
import xml.etree.ElementTree as ET

try:
    from scripts import android_feasibility as build, android_release
except ImportError:  # Run as scripts/android_device_matrix.py, with scripts/ on the path.
    import android_feasibility as build
    import android_release

ROOT = build.REPO
SDK = Path('/opt/android-sdk')
JAVA = Path('/usr/lib/jvm/java-17-openjdk-amd64/bin/java')
PACKAGE = build.APP_ACTIVITY.split('/')[0]
MATRIX = (
    {'image': 'system-images;android-24;google_apis;x86_64', 'api': 24, 'page_size': 4096},
    {'image': 'system-images;android-36;google_apis_ps16k;x86_64', 'api': 36, 'page_size': 16384},
)


def page_size(smaps):
    """The kernel page size from a process's smaps ("KernelPageSize:  16 kB"), in bytes."""
    match = re.search(r'^KernelPageSize:\s+(\d+) kB\s*$', smaps, re.M)
    return int(match[1]) * 1024 if match else None


def screen_text(hierarchy):
    """The visible text and descriptions of a uiautomator dump, in order, without repeats."""
    try:
        root = ET.fromstring(hierarchy)  # noqa: S314 - the job's own emulator's dump
    except ET.ParseError:
        return []
    found = (node.get(key, '').strip() for node in root.iter('node') for key in ('text', 'content-desc'))
    return list(dict.fromkeys(text for text in found if text))


def coverage(rows, identity):
    """The release contract's matrix conditions these passed rows still leave open."""
    passed = [row for row in rows if row['passed']]
    wanted = {'api_24': any(row['api'] == 24 for row in passed),
              'api_35_plus': any(row['api'] >= 35 for row in passed),
              'target_api': any(row['api'] == identity['target_sdk'] for row in passed),
              'page_size_16k_api_35_plus': any(row['api'] >= 35 and row['page_size'] == 16384 for row in passed)}
    wanted.update({f'abi_{abi}': any(row['abi'] == abi for row in passed) for abi in identity['abis']})
    return sorted(name for name, met in wanted.items() if not met)


def observe(adb, serial, apk, expected, folder, runner=build.run_tool):
    """Install and start the APK on one booted emulator; return its matrix row."""
    prefix = [str(adb), '-s', serial]

    def shell(*argv, timeout=60, keep_bytes=4000):
        return runner([*prefix, 'shell', *argv], timeout=timeout, keep_bytes=keep_bytes)

    checks, facts = {}, {}
    # The image's own userdebug build lets this job read the app process's memory map.
    checks['userdebug'] = shell('getprop', 'ro.build.type')['tail'].strip() in {'userdebug', 'eng'}
    rooted = runner([*prefix, 'root'], timeout=60)
    runner([*prefix, 'wait-for-device'], timeout=300)
    checks['root'] = rooted['returncode'] == 0
    api = shell('getprop', 'ro.build.version.sdk')['tail'].strip()
    facts['api'] = int(api) if api.isdigit() else None
    facts['abi'] = shell('getprop', 'ro.product.cpu.abi')['tail'].strip()
    facts['page_size'] = page_size(shell('cat', '/proc/self/smaps', keep_bytes=200000)['tail'])
    checks['api'] = facts['api'] == expected['api']
    checks['page_size'] = facts['page_size'] == expected['page_size']
    checks['abi'] = facts['abi'] in build.ABI_FILTERS

    install = runner([*prefix, 'install', str(apk)], timeout=600)
    checks['installed'] = install['returncode'] == 0 and 'Success' in install['tail'].splitlines()
    checks['log_cleared'] = runner([*prefix, 'logcat', '-b', 'all', '-c'], timeout=60)['returncode'] == 0
    launched = shell('am', 'start', '-W', '-n', build.APP_ACTIVITY, timeout=120)
    checks['launched'] = launched['returncode'] == 0 and 'Status: ok' in launched['tail'].splitlines()
    time.sleep(30)

    pid = shell('pidof', PACKAGE)['tail'].strip()
    checks['running'] = pid.isdigit()
    maps = shell('cat', f'/proc/{pid}/maps', keep_bytes=400000)['tail'] if checks['running'] else ''
    checks['python_loaded'] = 'libpython3.13.so' in maps
    windows = shell('dumpsys', 'window', keep_bytes=200000)['tail']
    focus = [line.strip() for line in windows.splitlines() if 'mCurrentFocus' in line]
    facts['focus'] = focus[:2]
    checks['in_front'] = bool(focus) and PACKAGE in focus[0]
    crashes = runner([*prefix, 'logcat', '-d', '-b', 'crash'], timeout=60, keep_bytes=20000)
    checks['crash_log_read'] = crashes['returncode'] == 0
    checks['no_crash'] = PACKAGE not in crashes['tail']
    facts['crash_log'] = crashes['tail'][-4000:] if not checks['no_crash'] else ''

    shell('uiautomator', 'dump', '/sdcard/matrix-ui.xml', timeout=120)
    hierarchy = shell('cat', '/sdcard/matrix-ui.xml', keep_bytes=400000)['tail']
    facts['screen_text'] = screen_text(hierarchy)[:60]
    checks['screen_has_text'] = bool(facts['screen_text'])
    shell('screencap', '-p', '/sdcard/matrix.png')
    runner([*prefix, 'pull', '/sdcard/matrix.png', str(folder / f"api{expected['api']}.png")], timeout=120)
    logs = runner([*prefix, 'logcat', '-d', '-t', '800'], timeout=60, keep_bytes=120000)
    (folder / f"api{expected['api']}-logcat.txt").write_text(logs['tail'])
    return {**facts, 'image': expected['image'], 'checks': checks, 'passed': all(checks.values())}


def run(apk):
    if ROOT != Path('/job/repo') or os.environ.get('CI') != 'true' or \
            os.environ.get('FLEET_STORAGE_PROJECT') != 'comic-archiver/android':
        raise ValueError('Use the installed CI fleet helper with project comic-archiver/android')
    artifacts = Path(os.environ['CI_ARTIFACTS_DIR']) / 'device-matrix'
    artifacts.mkdir(parents=True, exist_ok=True)
    result = {'apk': apk.name, 'rows': [], 'passed': False}
    try:
        certificate = os.environ.get(build.RELEASE_CERT_SHA256_ENV, '').strip().replace(':', '').lower()
        if not re.fullmatch(r'[a-f0-9]{64}', certificate):
            raise ValueError('Configure the release certificate fingerprint for this job')
        tools = sorted(path for path in (SDK / 'build-tools').iterdir()
                       if (path / 'apksigner').is_file() and (path / 'aapt2').is_file())
        if not tools:
            raise ValueError('No SDK build tools to inspect the APK')
        os.environ.update(JAVA_HOME=str(JAVA.parent.parent), PATH=f"{JAVA.parent}:{os.environ['PATH']}")
        identity = android_release.inspect_apk(apk, tools[-1])
        expected = dict(android_release.identity_from_source(ROOT), certificate_sha256=certificate)
        if identity != expected:
            raise ValueError('The APK differs from the tagged source or approved signer')
        result.update(identity=identity, apk_sha256=hashlib.sha256(apk.read_bytes()).hexdigest())
        if not build._emulator_capable():
            raise RuntimeError('Device matrix pending: no accessible KVM')
        runtime_env = {'JAVA_HOME': str(JAVA.parent.parent), 'ANDROID_HOME': str(SDK), 'ANDROID_SDK_ROOT': str(SDK)}
        paths = {'sdk': SDK, 'java': str(JAVA)}
        for environment in MATRIX:
            installed = build.run_tool([SDK / 'cmdline-tools/latest/bin/sdkmanager', '--sdk_root=' + str(SDK),
                                        '--install', environment['image']], timeout=1800, keep_bytes=4000,
                                       input_bytes='y\n' * 100, env_extra=runtime_env)
            if installed['returncode'] != 0:
                row = {'image': environment['image'], 'passed': False, 'reason': 'image install failed',
                       'tail': installed['tail'][-1500:]}
            else:
                startup = artifacts / f"api{environment['api']}-emulator.log"
                try:
                    with startup.open('wb') as output, \
                            build.job_emulator(paths, environment['image'], emulator_output=output) as (adb, serial, _):
                        row = observe(adb, serial, apk, environment, artifacts)
                except (OSError, ValueError, RuntimeError) as error:
                    row = {'image': environment['image'], 'passed': False,
                           'reason': f'{type(error).__name__}: {str(error)[:400]}'}
            result['rows'].append(row)
            print(json.dumps({'environment': environment['image'], 'passed': row['passed']}), flush=True)
        complete = [row for row in result['rows'] if 'api' in row]
        result['uncovered'] = coverage(complete, identity)
        result['passed'] = bool(result['rows']) and all(row['passed'] for row in result['rows'])
        return 0 if result['passed'] else 1
    except (OSError, ValueError, RuntimeError) as error:
        result['failure'] = f'{type(error).__name__}: {str(error)[:500]}'
        return 1
    finally:
        (artifacts / 'device-matrix.json').write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps({key: value for key, value in result.items() if key != 'rows'}, indent=2), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apk', type=Path, required=True, help='The inspected release APK')
    options = parser.parse_args()
    raise SystemExit(run(options.apk.resolve(strict=True)))
