"""Required offline validation; run it on the CI hosts.

Install the test extra first. --prepare obtains the Firefox runtime.
No credentials, real libraries or Tapas requests are used.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
OUT = ROOT / '.validation'


class LiveResult(unittest.TextTestResult):
    """Report a failure when it happens, while still running every required test."""
    def addError(self, test, error):
        super().addError(test, error)
        self.stream.writeln(self.errors[-1][1])
        self.stream.flush()

    def addFailure(self, test, error):
        super().addFailure(test, error)
        self.stream.writeln(self.failures[-1][1])
        self.stream.flush()

    def addSubTest(self, test, subtest, error):
        super().addSubTest(test, subtest, error)
        if error is not None:
            self.stream.writeln(self._exc_info_to_string(error, subtest))
            self.stream.flush()


def command(args, *, cwd=ROOT, env=None):
    print('Validation stage: ' + ' '.join(str(a) for a in args), flush=True)
    subprocess.run([str(a) for a in args], cwd=cwd, env=env, check=True, timeout=300)


def prerequisites(prepare):
    import coverage, PIL, playwright.sync_api  # noqa: F401 - fail before collecting tests if a required extra is absent
    if prepare:
        command([sys.executable, '-m', 'playwright', 'install', 'firefox'])


def suites(prepare=False):
    import coverage
    measured = coverage.Coverage(branch=True, source=['comic_archive'], data_file=str(OUT / '.coverage'))
    measured.start()
    try:
        prerequisites(prepare)
        suite = unittest.defaultTestLoader.discover(str(ROOT / 'tests'))
        result = unittest.TextTestRunner(verbosity=2, resultclass=LiveResult).run(suite)
    finally:
        measured.stop()
        measured.save()
        measured.json_report(outfile=str(OUT / 'coverage.json'))
        measured.html_report(directory=str(OUT / 'htmlcov'))
    stats = {'tests_run': result.testsRun, 'skipped': [(str(t), reason) for t, reason in result.skipped],
             'failures': len(result.failures), 'errors': len(result.errors)}
    (OUT / 'tests.json').write_text(json.dumps(stats, indent=2) + '\n')
    if not result.wasSuccessful() or result.skipped:
        raise RuntimeError('Required suite failed or skipped checks; see .validation/tests.json')
    report = json.loads((OUT / 'coverage.json').read_text())
    totals = report['totals']
    line = 100 * totals['covered_lines'] / totals['num_statements']
    branch = 100 * totals['covered_branches'] / totals['num_branches']
    controller = report['files']['comic_archive/account_export.py']['summary']
    controller_line = 100 * controller['covered_lines'] / controller['num_statements']
    controller_branch = 100 * controller['covered_branches'] / controller['num_branches']
    summary = {'line_percent': round(line, 2), 'branch_percent': round(branch, 2),
               'account_export_line_percent': round(controller_line, 2),
               'account_export_branch_percent': round(controller_branch, 2), **stats}
    print(json.dumps(summary), flush=True)
    # Measured baseline floors prevent regressions; scenario gates below remain
    # mandatory. Coverage percentages alone do not prove release readiness.
    if line < 85 or branch < 75 or controller_line < 98 or controller_branch < 90:
        raise RuntimeError('Coverage fell below required floors; inspect .validation/htmlcov')
    return summary


def static_checks():
    """Lint and security rules over the code, then known vulnerabilities in the installed dependencies."""
    command([sys.executable, '-m', 'ruff', 'check', '--no-cache', 'comic_archive', 'scripts', 'tests'])
    frozen = subprocess.run(['uv', 'pip', 'freeze', '--python', sys.executable], cwd=ROOT, check=True,
                            capture_output=True, text=True, timeout=120).stdout
    # This project itself is installed editable and is not on the package index.
    requirements = OUT / 'audit-requirements.txt'
    requirements.write_text(''.join(line + '\n' for line in frozen.splitlines() if '==' in line and not line.startswith('-e')))
    command([sys.executable, '-m', 'pip_audit', '--disable-pip', '--no-deps', '--progress-spinner', 'off', '-r', requirements])


def installed_package():
    command([sys.executable, '-m', 'build', '--installer', 'uv', '--wheel', '--outdir', OUT / 'dist'])
    wheels = list((OUT / 'dist').glob('*.whl'))
    if len(wheels) != 1:
        raise RuntimeError('Expected exactly one newly built distribution')
    with tempfile.TemporaryDirectory(prefix='comic-installed-') as temporary:
        root = Path(temporary)
        environment = root / 'venv'
        command([sys.executable, '-m', 'venv', '--without-pip', environment])
        python = environment / 'bin/python'
        command(['uv', 'pip', 'install', '--python', python, str(wheels[0]) + '[browser,validation]'])
        user = root / 'user'
        user.mkdir()
        clean = os.environ.copy()
        clean.pop('PYTHONPATH', None)
        clean.pop('PYTHONHOME', None)
        command([python, '-I', ROOT / 'scripts/installed_probe.py'], cwd=user, env=clean)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prepare', action='store_true')
    args = parser.parse_args()
    os.chdir(ROOT)
    OUT.mkdir(exist_ok=True)
    # Never reuse a stale distribution as evidence for the current source.
    if (OUT / 'dist').exists():
        shutil.rmtree(OUT / 'dist')
    static_checks()
    summary = suites(args.prepare)
    command([sys.executable, '-m', 'compileall', '-q', 'comic_archive'])
    command([sys.executable, 'scripts/reading_filter_smoke.py'])
    command([sys.executable, 'scripts/cloud_tools_smoke.py'])
    installed_package()
    summary.update(required_gates_passed=True, lint_and_security_rules=True, dependency_audit=True,
                   reading_filter_smoke=True, pinned_cloud_helper_install=True, fresh_wheel_install=True,
                   real_second_account_onboarding_verified=False,
                   windows_verified=False, macos_verified=False)
    (OUT / 'validation.json').write_text(json.dumps(summary, indent=2) + '\n')
    print(json.dumps(summary), flush=True)


if __name__ == '__main__':
    main()
