"""Darwin adapter contracts; native process behaviour is also checked on macOS."""
from pathlib import Path
import json
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import psutil

from comic_archive import macos, network, osdep


class MacTests(unittest.TestCase):
    def test_native_connection_cost_flows_through_the_existing_policy(self):
        with patch.object(network.sys, 'platform', 'darwin'), patch.object(macos.subprocess, 'run') as run:
            for kind, metered in (('wired', False), ('wifi', False), ('wifi', True), ('cellular', True), ('unknown', None)):
                expected = {'connection':kind, 'metered':metered}
                run.return_value.stdout = json.dumps(expected)
                self.assertEqual(network.probe(), expected)
            for invalid in ('{}', 'null', '[]', 'x', '{"connection":"wifi","metered":0}', 'x'*1025):
                run.return_value.stdout = invalid
                self.assertEqual(network.probe(), {'connection':'unknown', 'metered':None})
            for error in (OSError('missing'), subprocess.TimeoutExpired('probe', 2),
                          subprocess.CalledProcessError(1, 'probe')):
                run.side_effect = error
                self.assertEqual(network.probe(), {'connection':'unknown', 'metered':None})

    def test_sleep_assertion_is_held_once_and_released(self):
        with patch.object(macos, '_awake', None), patch.object(macos.subprocess, 'Popen') as launch:
            launch.return_value.poll.return_value = None
            macos.keep_awake(True)
            macos.keep_awake(True)
            launch.assert_called_once()
            self.assertEqual(launch.call_args.args[0], ['/usr/bin/caffeinate', '-i', '-w', str(macos.os.getpid())])
            macos.keep_awake(False)
            launch.return_value.terminate.assert_called_once()
            launch.return_value.wait.assert_called_once_with(timeout=5)
            self.assertIsNone(macos._awake)

    def test_only_exact_arguments_identify_a_worker(self):
        with patch.object(macos.psutil, 'Process') as process:
            process.return_value.cmdline.return_value = ['python', '-m', 'comic_archive', 'worker', '/library with spaces']
            self.assertTrue(macos.runs_command(123, ['comic_archive', 'worker', '/library with spaces']))
            self.assertFalse(macos.runs_command(123, ['archive', 'worker']))
            self.assertFalse(macos.runs_command(123, ['/library']))
            self.assertFalse(macos.runs_command(-1, ['worker']))
            self.assertFalse(macos.runs_command('123', ['worker']))
            process.return_value.cmdline.return_value = []
            self.assertFalse(macos.runs_command(123, []))
            for error in (psutil.NoSuchProcess(123), psutil.AccessDenied(123), OSError('unavailable')):
                process.side_effect = error
                self.assertFalse(macos.runs_command(123, ['worker']))

    def test_darwin_uses_the_native_adapter_instead_of_proc(self):
        with patch.object(osdep, 'WINDOWS', False), patch.object(osdep.sys, 'platform', 'darwin'), \
                patch.object(macos, 'runs_command', return_value=True) as native:
            self.assertTrue(osdep.runs_command(123, ['worker']))
            native.assert_called_once_with(123, ['worker'])

    def test_deepest_volume_is_classified_and_missing_mounts_are_unknown(self):
        root = Path('/').resolve()
        entries = [SimpleNamespace(mountpoint=str(root), fstype='apfs'),
                   SimpleNamespace(mountpoint=str(root/'Volumes/shared'), fstype='smbfs')]
        with patch.object(macos.psutil, 'disk_partitions', return_value=entries):
            self.assertEqual(macos.drive(root/'Volumes/shared/Library'), ('network', 'smbfs'))
            self.assertEqual(macos.drive(root/'Users/test'), ('local', 'apfs'))
            entries[-1].fstype = 'udf'
            self.assertEqual(macos.drive(root/'Volumes/shared'), ('disc', 'udf'))
            entries.clear()
            self.assertEqual(macos.drive(root), ('unknown', 'unknown'))
        with patch.object(macos.psutil, 'disk_partitions', side_effect=psutil.AccessDenied()):
            self.assertEqual(macos.drive(root), ('unknown', 'unknown'))
