"""Prepare the app-owned desktop image and run existing gates in one fleet job.

The physical hosts are unchanged. Docker runs inside the disposable fleet guest;
its existing acquiring-cache-only egress and resource admission remain in force.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def run(args, **kwargs):
    print('Linux CI stage: '+' '.join(map(str, args)), flush=True)
    return subprocess.run(list(map(str, args)), check=True, **kwargs)


def checks(groups, mutation_ids):
    output = Path(os.environ['CI_ARTIFACTS_DIR'])
    output.mkdir(parents=True, exist_ok=True)
    shutil.copy2('/opt/ci-inputs/os-packages.txt', output/'os-packages.txt')
    # The installer maintains locks/link records even when the browser is already
    # installed. Give it a job-owned cache, keeping the image itself read-only.
    browsers = ROOT/'.validation/playwright'
    shutil.copytree('/opt/playwright', browsers, dirs_exist_ok=True)
    os.environ['PLAYWRIGHT_BROWSERS_PATH'] = str(browsers)
    run([sys.executable, '-m', 'venv', '--without-pip', '--system-site-packages', ROOT/'.venv'])
    python = ROOT/'.venv/bin/python'
    run(['uv', 'pip', 'install', '--python', python, '--no-deps', '--no-build-isolation', '-e', ROOT])
    env = os.environ.copy()
    env['PATH'] = str(python.parent)+os.pathsep+env['PATH']
    stages = [('validate', [python, 'scripts/validate.py', '--prepare'])]
    if mutation_ids:
        inventory = json.loads((ROOT/'tests/mutations.json').read_text())
        selected = [m for m in inventory if m['id'] in mutation_ids]
        if len(selected) != len(set(mutation_ids)):
            raise RuntimeError('A mutation asked for is not in the inventory')
        selection = output/'repair-mutations.json'
        selection.write_text(json.dumps(selected, indent=2)+'\n')
        stages.append(('mutations', [python, 'scripts/mutation_check.py', selection]))
    else:
        stages.append(('mutations', [python, 'scripts/mutation_check.py']))
    stages += [('lint', [python, '-m', 'ruff', 'check', '--no-cache', 'comic_archive', 'scripts', 'tests']),
               ('cloud-helper', [python, 'scripts/cloud_tools_smoke.py']),
               ('installed-package', [python, '-c',
                'import shutil; from scripts import validate; '
                'shutil.rmtree(validate.OUT/"dist", ignore_errors=True); validate.installed_package()']),
               ('engine', [python, 'scripts/qt_engine_smoke.py']),
               ('adapter', [python, 'scripts/qt_adapter_check.py'])]
    results = {}
    for name, command in stages:
        if name not in groups:
            continue
        print('Starting gate: '+name, flush=True)
        with (output/(name+'.log')).open('w') as log:
            done = subprocess.run(list(map(str, command)), cwd=ROOT, env=env,
                                  stdout=log, stderr=subprocess.STDOUT, check=False)
        results[name] = done.returncode
        (output/'gates.json').write_text(json.dumps(results, indent=2)+'\n')
        print(name+': exit '+str(done.returncode), flush=True)
        print((output/(name+'.log')).read_text(errors='replace')[-5000:], flush=True)
    if (ROOT/'.validation').is_dir():
        shutil.copytree(ROOT/'.validation', output/'validation', dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns('playwright'))
    return int(any(results.values()))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inside', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--groups', nargs='+', choices=('validate', 'mutations', 'engine', 'adapter',
                                                      'lint', 'cloud-helper', 'installed-package'),
                        default=['validate', 'mutations', 'engine', 'adapter'])
    parser.add_argument('--mutation-id', action='append', default=[], help='rerun an exact failed inventory entry')
    args = parser.parse_args()
    if args.inside:
        return checks(args.groups, args.mutation_id)
    if os.environ.get('CI') != 'true' or not os.environ.get('CI_ARTIFACTS_DIR'):
        raise RuntimeError('Use the supported fleet helper')
    output = Path(os.environ['CI_ARTIFACTS_DIR'])
    output.mkdir(parents=True, exist_ok=True)
    apt = os.environ['COMIC_ARCHIVER_CI_APT_MIRROR'].rstrip('/')
    package_index = os.environ['PIP_INDEX_URL']
    firefox = os.environ['PLAYWRIGHT_FIREFOX_DOWNLOAD_HOST']
    with tempfile.TemporaryDirectory(prefix='comic-desktop-image-') as temporary:
        context = Path(temporary)
        shutil.copy2(ROOT/'packaging/ci/Dockerfile', context/'Dockerfile')
        shutil.copy2(ROOT/'packaging/linux-requirements.txt', context/'requirements.txt')
        shutil.copy2(ROOT/'pyproject.toml', context/'project.toml')
        shutil.copy2(os.environ['SSL_CERT_FILE'], context/'fleet-ca.crt')
        identity = context/'image.id'
        run(['docker', 'buildx', 'build', '--load', '--iidfile', identity,
             '--build-arg', 'APT_MIRROR='+apt,
             '--build-arg', 'APT_SECURITY_MIRROR='+apt+'-security',
             '--build-arg', 'PACKAGE_INDEX='+package_index,
             '--build-arg', 'FIREFOX_DOWNLOAD_HOST='+firefox, context])
        image = identity.read_text().strip()
        (output/'test-image.json').write_text(json.dumps({'image':image}, indent=2)+'\n')
    command = ['docker', 'run', '--rm', '--pull=never', '--read-only', '--cap-drop', 'ALL',
               '--security-opt', 'no-new-privileges', '--user', '1000:1000',
               '--cpus', '4', '--memory', '8g', '--pids-limit', '512',
               '--tmpfs', '/tmp:rw,nosuid,nodev,size=2g,mode=1777',
               '--tmpfs', '/var/tmp:rw,nosuid,nodev,exec,size=2g,mode=1777',
               '--mount', f'type=bind,src={ROOT},dst=/workspace',
               '--mount', f'type=bind,src={output},dst=/artifacts',
               '--workdir', '/workspace', '--env', 'HOME=/tmp/home',
               '--env', 'TMPDIR=/var/tmp',
               '--env', 'CI=true', '--env', 'CI_ARTIFACTS_DIR=/artifacts',
               '--env', 'PLAYWRIGHT_DOWNLOAD_HOST='+firefox]
    for key in ('PIP_INDEX_URL', 'UV_INDEX_URL', 'PIP_AUDIT_VULNERABILITY_SERVICE', 'PIP_AUDIT_OSV_URL',
                'PLAYWRIGHT_FIREFOX_DOWNLOAD_HOST', 'COMIC_ARCHIVER_CI_HELPER_DOWNLOAD_BASE',
                'QT_QPA_PLATFORM', 'QTWEBENGINE_CHROMIUM_FLAGS', 'QT_QUICK_BACKEND'):
        if key in os.environ:
            command += ['--env', key+'='+os.environ[key]]
    command += [image, 'python', 'scripts/linux_ci.py', '--inside']
    command += ['--groups', *args.groups]
    for identifier in args.mutation_id:
        command += ['--mutation-id', identifier]
    return subprocess.run(command, check=False).returncode


if __name__ == '__main__':
    sys.exit(main())
