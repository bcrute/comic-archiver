"""Read-only SDK verification and owner-attested Android release requirements.

The trusted certificate comes from release-owner configuration, never the draft.
Runtime/device results remain owner attestations bound to the exact APK hash;
SDK inspection cannot manufacture missing acceptance evidence.
"""
import re
import subprocess


CHECKS = {'build', 'jvm', 'native_integration', 'real_comic_novel', 'saf_recovery',
          'lifecycle_network', 'retained_signer_update', 'release_hygiene',
          'dependency_audit', 'license_audit', 'zip_alignment', 'page_size_16k'}
PLATFORMS = {'desktop', 'android'}
# Sign-in needs WebView features (isolated script worlds, profiles) that only WebViews far
# newer than Chrome 139 have, and Chrome 139 onward requires Android 10. An older phone
# could install the app but never sign in, so it is not supported.
MIN_SDK = 29


def identity_from_source(project):
    """Require the scaffold's literal identity; never execute Gradle to publish."""
    source = (project / 'android/app/build.gradle.kts').read_text('utf-8')
    identity = {}
    for field, key, quoted in (('applicationId', 'package_id', True),
                               ('versionName', 'version_name', True),
                               ('versionCode', 'version_code', False),
                               ('minSdk', 'min_sdk', False), ('targetSdk', 'target_sdk', False)):
        pattern = r'"([^"\n]+)"' if quoted else r'([0-9]+)'
        matches = re.findall(r'^\s*' + field + r'\s*=\s*' + pattern + r'\s*$', source, re.M)
        if len(matches) != 1:
            raise ValueError('Android release identity must be explicit in the tagged source.')
        identity[key] = matches[0] if quoted else int(matches[0])
    matches = re.findall(r'^\s*abiFilters\s*\+=\s*listOf\(([^)\n]+)\)\s*$', source, re.M)
    if len(matches) != 1 or not re.fullmatch(r'"[a-z0-9_-]+"(?:\s*,\s*"[a-z0-9_-]+")*', matches[0]):
        raise ValueError('Android release ABIs must be explicit in the tagged source.')
    identity['abis'] = sorted(re.findall(r'"([a-z0-9_-]+)"', matches[0]))
    return identity


def tool_output(argv):
    try:
        result = subprocess.run([str(a) for a in argv], capture_output=True, text=True,
                                check=False, timeout=90)
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError('Android SDK release verification is unavailable.') from error
    if result.returncode:
        raise ValueError('Android SDK rejected a staged APK.')
    return result.stdout


def inspect_apk(path, build_tools):
    """Verify actual signature/alignment, then read compiled manifest/ABI identity."""
    signature = tool_output([build_tools / 'apksigner', 'verify', '--verbose', '--print-certs', path])
    certificates = re.findall(r'^(Signer #\d+|V[123] Signer:) certificate SHA-256 digest: ([a-f0-9]{64})\s*$',
                              signature, re.M)
    labels = [label for label, _ in certificates]
    counts = re.findall(r'^Number of signers: ([0-9]+)\s*$', signature, re.M)
    if (not certificates or len({cert for _, cert in certificates}) != 1 or len(set(labels)) != len(labels)
            or any(label.startswith('Signer #') and label != 'Signer #1' for label in labels)
            or (counts and counts != ['1'])):
        raise ValueError('Android release must have exactly the original approved signer.')
    badging = tool_output([build_tools / 'aapt2', 'dump', 'badging', path])
    lines = badging.splitlines()
    packages = [line for line in lines if line.startswith('package: ')]
    if len(packages) != 1:
        raise ValueError('The APK has no unambiguous application identity.')
    identity = {}
    for field, key in (('name', 'package_id'), ('versionName', 'version_name'), ('versionCode', 'version_code')):
        matches = re.findall(r"(?:^| )" + field + r"='([^']*)'", packages[0])
        if len(matches) != 1 or (key == 'version_code' and not matches[0].isdigit()):
            raise ValueError('The APK has an invalid version or package identity.')
        identity[key] = int(matches[0]) if key == 'version_code' else matches[0]
    for prefix, key in (('(?:minSdkVersion|sdkVersion)', 'min_sdk'), ('targetSdkVersion', 'target_sdk')):
        api_lines = [line for line in lines if re.match(r'^' + prefix + ':', line)]
        match = re.fullmatch(prefix + r":'([0-9]+)'", api_lines[0]) if len(api_lines) == 1 else None
        if match is None:
            raise ValueError('The APK must declare numeric supported API levels.')
        identity[key] = int(match[1])
    native = [line for line in lines if line.startswith('native-code:')]
    if len(native) != 1 or not re.fullmatch(r"native-code:(?: '[a-z0-9_-]+')+\s*", native[0]):
        raise ValueError('The universal APK must declare its native ABIs.')
    identity['abis'] = sorted(re.findall(r"'([^']+)'", native[0]))
    if ('application-debuggable' in lines or any(line.startswith(('testOnly=', 'instrumentation:')) for line in lines)
            or re.search(r"versionCodeMajor='(?!0')[^']+'", packages[0])):
        raise ValueError('Debug, test and unexpected version-code builds cannot be published.')
    tool_output([build_tools / 'zipalign', '-c', '-P', '16', '-v', '4', path])
    return identity | {'certificate_sha256': certificates[0][1]}


def check_evidence(value, commit, version, identity, certificate):
    """Validate required Android evidence before consulting any staged SDK tools."""
    platforms = value.get('platforms')
    if (not isinstance(platforms, list) or len(platforms) != 2
            or any(not isinstance(p, str) for p in platforms) or set(platforms) != PLATFORMS):
        raise ValueError('Schema 2 requires exactly the desktop and Android platforms.')
    android = value.get('android')
    if not isinstance(android, dict) or not isinstance(identity, dict):
        raise ValueError('Android evidence and tagged-source identity are required.')
    if not isinstance(certificate, str) or not re.fullmatch(r'[a-f0-9]{64}', certificate):
        raise ValueError('The owner must configure the original Android release certificate.')
    if (android.get('commit') != commit or android.get('certificate_sha256') != certificate
            or any(type(android.get(k)) is not type(v) or android[k] != v for k, v in identity.items())):
        raise ValueError('Android evidence differs from the tagged source or approved signer.')
    if (identity.get('version_name') != version
            or type(identity.get('version_code')) is not int or identity['version_code'] < 1
            or type(identity.get('min_sdk')) is not int or identity['min_sdk'] != MIN_SDK
            or type(identity.get('target_sdk')) is not int or identity['target_sdk'] < 35
            or identity.get('abis') != ['arm64-v8a', 'x86_64']):
        raise ValueError('Android source version/API/ABI identity is not a supported release.')
    checks = android.get('checks')
    if not isinstance(checks, dict) or set(checks) != CHECKS or any(checks[k] is not True for k in CHECKS):
        raise ValueError('Every required Android fleet/device/audit check must pass.')
    tests = android.get('tests')
    if (not isinstance(tests, dict) or type(tests.get('tests_run')) is not int or tests['tests_run'] < 1
            or type(tests.get('failures')) is not int or tests['failures'] != 0
            or type(tests.get('errors')) is not int or tests['errors'] != 0 or tests.get('skipped') != []):
        raise ValueError('Required Android suites must pass without skips.')
    matrix = android.get('device_matrix')
    if not isinstance(matrix, list) or not matrix:
        raise ValueError('The Android device matrix is missing.')
    identities = set()
    for row in matrix:
        if (not isinstance(row, dict) or set(row) != {'api', 'abi', 'page_size', 'passed'}
                or type(row['api']) is not int or row['api'] < MIN_SDK
                or row['abi'] not in identity['abis'] or type(row['page_size']) is not int
                or row['page_size'] not in (4096, 16384) or row['passed'] is not True):
            raise ValueError('The Android matrix includes an invalid or unpassed environment.')
        key = (row['api'], row['abi'], row['page_size'])
        if key in identities:
            raise ValueError('Duplicate Android environments do not add matrix coverage.')
        identities.add(key)
    if (not all(any(row['abi'] == abi for row in matrix) for abi in identity['abis'])
            or not any(row['api'] == MIN_SDK for row in matrix)
            or not any(row['api'] >= 35 for row in matrix)
            or not any(row['api'] == identity['target_sdk'] for row in matrix)
            or not any(row['api'] >= 35 and row['page_size'] == 16384 for row in matrix)):
        raise ValueError('Required minimum/modern/target API, ABI or 16KB runtime coverage is missing.')
    asset = f'ComicArchiver-{version}-android-universal.apk'
    if android.get('artifact') != asset:
        raise ValueError('Android evidence must name the exact universal release APK.')
    return asset
