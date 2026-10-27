#!/usr/bin/env python3
"""Run real Android integration on the fleet, without rebuilding matching APKs.

The disposable runtime uses the fleet's vendor SDK manager and an official pinned
x86_64 WebView. Compile SDK/NDK/JDK/Gradle pins remain independent. Inputs are
public test APKs plus a source/hash manifest; no accounts or phone data are used.
See docs/android-ci-integration.md for the fleet invocation and input contract.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import zipfile
import urllib.request

import android_beta_integration as beta
import android_feasibility as build

ROOT = build.REPO
SDK = Path('/opt/android-sdk')
TARGET = beta.TARGET


def source_inputs(fixture=False):
    """Include additions/deletions as well as changes; exclude generated caches."""
    directories = ['android/app/src/androidTest'] if fixture else ['android/app/src/main', 'comic_archive']
    fixed = [] if fixture else ['android/app/build.gradle.kts', 'android/build.gradle.kts',
        'android/gradle.properties', 'android/gradle/libs.versions.toml',
        'android/settings.gradle.kts', 'packaging/android/toolchain-pins.json']
    paths = [ROOT / name for name in fixed]
    for name in directories:
        paths.extend(path for path in (ROOT / name).rglob('*') if path.is_file()
                     and '__pycache__' not in path.parts and path.suffix != '.pyc')
    result = {}
    for path in sorted(paths):
        if path.is_symlink():
            raise ValueError('APK source input must not be a symlink')
        data = path.read_bytes()
        result[path.relative_to(ROOT).as_posix()] = {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
    return result


def input_record(app, tests, certificate):
    """Call in the build job after both APKs and their common signer are verified."""
    return {'application_files': source_inputs(), 'fixture_files': source_inputs(True),
            'app_sha256': hashlib.sha256(app.read_bytes()).hexdigest(),
            'test_sha256': hashlib.sha256(tests.read_bytes()).hexdigest(), 'cert_sha256': certificate}


def run(app, tests, manifest):
    if ROOT != Path('/job/repo') or os.environ.get('CI') != 'true' or \
            os.environ.get('FLEET_STORAGE_PROJECT') != 'comic-archiver/android':
        raise ValueError('Use the installed CI fleet helper with project comic-archiver/android')
    artifacts = Path(os.environ['CI_ARTIFACTS_DIR']) / 'native-integration'
    artifacts.mkdir(parents=True, exist_ok=True)

    def save(name, value):
        (artifacts / name).write_text(json.dumps(value, indent=2) + '\n')
        print(json.dumps({'record': name, 'status': value.get('status', value.get('returncode'))}), flush=True)

    def checked(name, argv, **kwargs):
        value = build.run_tool(argv, **kwargs)
        save(name, value)
        if value['returncode']:
            raise RuntimeError(name + ' failed; primary output retained')
        return value

    try:
        inputs = json.loads(manifest.read_text())
        for fixture, key in [(False, 'application_files'), (True, 'fixture_files')]:
            if inputs[key] != source_inputs(fixture):
                raise ValueError('Rebuild APKs: ' + key + ' changed since compilation')
        for path, key in [(app, 'app_sha256'), (tests, 'test_sha256')]:
            if hashlib.sha256(path.read_bytes()).hexdigest() != inputs[key]:
                raise ValueError('APK bytes differ from build manifest: ' + path.name)
            certificate, _ = build._apk_signer_cert_sha256(SDK, '/usr/lib/jvm/java-17-openjdk-amd64/bin/java', path, build.run_tool)
            if not certificate or certificate != inputs['cert_sha256']:
                raise ValueError('APK signing identity differs from build manifest')
        save('reuse.json', inputs | {'app_builds': 0, 'test_builds': 0})
        if not build._emulator_capable():
            raise RuntimeError('Native integration pending: no accessible KVM')
        pin = json.loads((ROOT / 'packaging/android/integration-runtime.json').read_text())
        image = pin['system_image']
        # A preceding build may have selected a private JDK whose bundled CA set
        # lacks the fleet mirror CA. The runtime SDK uses the fleet's native JDK
        # and its configured trust store, independently of compilation.
        runtime_env = {'JAVA_HOME': '/usr/lib/jvm/java-17-openjdk-amd64',
                       'ANDROID_HOME': str(SDK), 'ANDROID_SDK_ROOT': str(SDK)}
        checked('sdk-image-install.json', [SDK / 'cmdline-tools/latest/bin/sdkmanager',
            '--sdk_root=' + str(SDK), '--install', image], timeout=900, keep_bytes=16000,
            input_bytes='y\n' * 100, env_extra=runtime_env)
        image_root = SDK.joinpath(*image.split(';'))
        shutil.copyfile(image_root / 'package.xml', artifacts / 'sdk-image-package.xml')
        save('runtime-image.json', {'image': image, 'source_properties': (image_root / 'source.properties').read_text(), 'registered': True})

        web = pin['webview']
        scratch = build.JOB_DIR / 'webview'
        scratch.mkdir(parents=True, exist_ok=True)
        bundle, provider = scratch / 'webview.zip', scratch / 'SystemWebView.apk'
        digest, size = hashlib.sha256(), 0
        with urllib.request.urlopen(build._acquisition_url(web['url']), timeout=180) as source, bundle.open('wb') as output:
            while chunk := source.read(1024 * 1024):
                size += len(chunk)
                if size > web['bytes']:
                    raise ValueError('WebView archive exceeds pinned size')
                digest.update(chunk)
                output.write(chunk)
        if size != web['bytes'] or digest.hexdigest() != web['sha256']:
            raise ValueError('WebView archive identity mismatch')
        with zipfile.ZipFile(bundle) as archive:
            member = archive.getinfo(web['apk_member'])
            if member.file_size != web['apk_bytes'] or member.file_size >= 512 * 1024 * 1024:
                raise ValueError('WebView APK size differs from pin')
            provider.write_bytes(archive.read(member))
        if hashlib.sha256(provider.read_bytes()).hexdigest() != web['apk_sha256']:
            raise ValueError('WebView APK identity mismatch')
        with zipfile.ZipFile(provider) as archive:
            if not any(name.startswith('lib/x86_64/') and name.endswith('.so') for name in archive.namelist()):
                raise ValueError('Expected an x86_64 WebView provider')
        save('webview-input.json', web | {'verified': True})

        def journey(adb, serial, avd_name, test_apk, output, runner):
            # phase_in_app has independently checked this exact AVD's ownership.
            prefix = [str(adb), '-s', serial]
            try:
                kind = checked('emulator-build-type.json', [*prefix, 'shell', 'getprop', 'ro.build.type'], timeout=30)
                if kind['tail'].strip() not in {'userdebug', 'eng'}:
                    raise ValueError('WebView override is only for a disposable development emulator')
                checked('stop-before-provider-update.json', [*prefix, 'shell', 'am', 'force-stop', TARGET], timeout=30)
                checked('webview-install.json', [*prefix, 'install', '-r', '-t', provider], timeout=240)
                selected = checked('webview-select.json', [*prefix, 'shell', 'cmd', 'webviewupdate',
                    'set-webview-implementation', 'com.android.webview'], timeout=60)
                if 'Success' not in selected['tail']:
                    raise RuntimeError('WebView selection failed')
                actual = checked('webview-provider.json', [*prefix, 'shell', 'dumpsys', 'webviewupdate'], timeout=30, keep_bytes=16000)
                if 'Current WebView package (name, version): (com.android.webview,' not in actual['tail']:
                    raise RuntimeError('The selected provider is not active')
                checked('test-install.json', [*prefix, 'install', '-r', test_apk], timeout=240)
                method = TARGET + '.integration.CookieBridgeTest#pythonResponseCookiesReachTheChapterAssetHost'
                result = checked('cookie-bridge-instrumentation.json', [*prefix, 'shell', 'am', 'instrument', '-w', '-r',
                    '-e', 'class', method, '-e', 'disposable_emulator', 'true', '-e', 'run_token', secrets.token_hex(16),
                    TARGET + '.test/' + TARGET + '.integration.BetaTestRunner'], timeout=180, keep_bytes=32000)
                if 'OK (1 test)' not in result['tail'] or 'INSTRUMENTATION_CODE: -1' not in result['tail']:
                    raise RuntimeError('The actual cookie bridge test did not pass')
                cleared = checked('clear-owned-fixture.json', [*prefix, 'shell', 'pm', 'clear', TARGET], timeout=30)
                if 'Success' not in cleared['tail']:
                    raise RuntimeError('Independent fixture reset failed')
                return beta.run(adb, serial, avd_name, test_apk, output, runner)
            finally:
                logs = runner([*prefix, 'logcat', '-d', '-t', '600'], timeout=30, keep_bytes=90000)
                (artifacts / 'logcat.txt').write_text(logs['tail'])

        build.APK.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(app, build.APK)
        build.report['phases']['preflight'] = {'status': 'runtime_only', 'system_images': [image]}
        with (artifacts / 'emulator-startup.log').open('wb') as startup:
            build.phase_in_app({'sdk': SDK, 'java': '/usr/lib/jvm/java-17-openjdk-amd64/bin/java'}, tests,
                               journey=journey, emulator_output=startup)
        passed = build.report['phases'].get('beta_integration', {}).get('status') == 'passed'
        save('terminal.json', {'passed': passed, 'app_builds': 0, 'test_builds': 0, 'full_release_qualification': False})
        return 0 if passed else 1
    except (OSError, ValueError, RuntimeError) as error:
        save('failure.json', {'status': 'failed', 'type': type(error).__name__, 'reason': str(error)[:500]})
        return 1
    finally:
        save('builder-report.json', build.report)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app', type=Path, required=True)
    parser.add_argument('--tests', type=Path, required=True)
    parser.add_argument('--inputs', type=Path, required=True)
    options = parser.parse_args()
    raise SystemExit(run(options.app, options.tests, options.inputs))
