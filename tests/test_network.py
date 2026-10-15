import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from support import PRIVATE_FILE
from comic_archive.models import ArchiveError
from comic_archive.network import NetworkGuard, NetworkPolicyHold, configure, policy, probe, status, wait_process
from comic_archive.transport import Transport


class NetworkTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / 'Library'

    def test_default_wifi_only_is_readonly_and_unknown_connection_is_held(self):
        with patch('comic_archive.network.probe', return_value={'connection': 'unknown', 'metered': None}):
            self.assertTrue(policy(self.root)['wifi_only'])
            self.assertFalse(status(self.root)['allowed'])
            with self.assertRaises(NetworkPolicyHold): NetworkGuard(self.root).check()
        self.assertFalse(self.root.exists())

    def test_allowed_wifi_wired_and_blocked_cellular_metered_and_unknown(self):
        for kind, metered, allowed in [('wifi', False, True), ('wired', False, True),
                ('cellular', True, False), ('wifi', True, False), ('wired', True, False), ('unknown', None, False)]:
            with self.subTest(kind=kind, metered=metered), patch('comic_archive.network.probe', return_value={'connection':kind,'metered':metered}):
                self.assertEqual(status(self.root)['allowed'], allowed)
                if allowed: NetworkGuard(self.root).check()
                else:
                    with self.assertRaises(NetworkPolicyHold): NetworkGuard(self.root).check()

    def test_explicit_opt_out_is_private_strict_and_live_opt_in_is_observed(self):
        with patch('comic_archive.network.probe', return_value={'connection':'cellular','metered':True}):
            configure(self.root, False)
            guard = NetworkGuard(self.root); guard.check()
            configure(self.root, True)
            with self.assertRaises(NetworkPolicyHold): guard.check()
        path = self.root / '.archive-state/network-policy.json'
        self.assertEqual(path.stat().st_mode & 0o777, PRIVATE_FILE)
        for value in (None, 1, 'false'):
            with self.assertRaises(ArchiveError): configure(self.root, value)
        for value in ({'schema_version':1,'wifi_only':0}, {'schema_version':True,'wifi_only':True},
                      {'schema_version':1,'wifi_only':True,'unknown':'credential'}, []):
            path.write_text(json.dumps(value))
            with self.assertRaises(ArchiveError): policy(self.root)
        path.unlink(); path.symlink_to(self.root/'missing')
        with self.assertRaises(ArchiveError): policy(self.root)

    def test_guard_rechecks_route_and_does_not_expose_connection_identifiers(self):
        with patch('comic_archive.network.probe', side_effect=[{'connection':'wifi','metered':False}, {'connection':'cellular','metered':True}]) as observed:
            guard = NetworkGuard(self.root); guard.check(); guard.check()
            self.assertEqual(observed.call_count, 1)
            guard._checked -= 2
            with self.assertRaises(NetworkPolicyHold) as error: guard.check()
            self.assertNotIn('SSID', str(error.exception))

    def test_the_log_is_told_when_the_connection_rule_starts_and_stops_holding_and_not_at_every_check(self):
        answers = [{'connection':'wired','metered':False}, {'connection':'unknown','metered':None},
                   {'connection':'unknown','metered':None}, {'connection':'wired','metered':False}]
        with patch('comic_archive.network.probe', side_effect=answers), patch('builtins.print') as said:
            guard = NetworkGuard(self.root)
            for held in (False, True, True, False):
                guard._checked -= 2
                if held:
                    with self.assertRaises(NetworkPolicyHold): guard.check()
                else: guard.check()
        self.assertEqual([call.args[0] for call in said.call_args_list],
                         ['Connection check: unknown, metered None; transfers held.',
                          'Connection check: wired, metered False; transfers allowed again.'])

    def test_linux_detection_includes_ipv6_defaults_and_metered_hotspots(self):
        with patch('comic_archive.network.sys.platform', 'linux'):
            for kind, meter, expected in [('wifi','no',False),('ethernet','guess-no',False),('ethernet','no (guessed)',False),('wifi','yes (guessed)',True),('wifi','yes',True),('gsm','no',True)]:
                output = ['[]', '[{"dev":"wlan0"}]', f'{kind}\n100 (connected)\n{meter}']
                with self.subTest(kind=kind,meter=meter), patch('comic_archive.network._command', side_effect=output):
                    self.assertEqual(probe()['metered'], expected)
            with patch('comic_archive.network._command', side_effect=['[{"dev":"eth0"}]','[{"dev":"wwan0"}]','ethernet\n100 (connected)\nno','gsm\n100 (connected)\nno']):
                self.assertEqual(probe()['connection'], 'cellular')

    def test_linux_detection_holds_vpn_unknown_metering_invalid_and_unavailable_commands(self):
        with patch('comic_archive.network.sys.platform', 'linux'):
            for output in (['[]','[]'], ['{}','[]'], ['[{"dev":"--evil"}]','[]'],
                    ['[{"dev":"tun0"}]','[]','tun\n100 (connected)\nno'],
                    ['[{"dev":"wlan0"}]','[]','wifi\n100 (connected)\nunknown'],
                    ['[{"dev":"wlan0"}]','[]','wifi\n30 (disconnected)\nno']):
                with patch('comic_archive.network._command', side_effect=output):
                    self.assertEqual(probe(), {'connection':'unknown','metered':None})
            for error in (FileNotFoundError(), subprocess.TimeoutExpired('nmcli',2), ValueError()):
                with patch('comic_archive.network._command', side_effect=error):
                    self.assertEqual(probe()['connection'], 'unknown')
        with patch('comic_archive.network.sys.platform', 'darwin'):
            self.assertEqual(probe()['connection'], 'unknown')

    def test_windows_is_asked_through_its_own_check_and_a_failure_there_holds_instead_of_stopping_the_download(self):
        with patch('comic_archive.network.sys.platform', 'win32'):
            with patch('comic_archive.network_windows.probe', return_value={'connection': 'wifi', 'metered': False}):
                self.assertEqual(probe(), {'connection': 'wifi', 'metered': False})
            # Run here, the real check reaches for Windows libraries this computer does not have.
            for failure in (OSError('no such library'), AttributeError('WinDLL'), ValueError()):
                with patch('comic_archive.network_windows.probe', side_effect=failure):
                    self.assertEqual(probe(), {'connection': 'unknown', 'metered': None})

    def test_windows_adapter_kinds_and_costs_give_the_answers_the_connection_rule_works_with(self):
        from comic_archive.network_windows import classify
        unrestricted, fixed, variable, roaming = 0x1, 0x2, 0x4, 0x40000
        unknown = {'connection': 'unknown', 'metered': None}
        for kind, cost, expected in [
                (71, unrestricted, {'connection': 'wifi', 'metered': False}),
                (6, unrestricted, {'connection': 'wired', 'metered': False}),
                (71, fixed, {'connection': 'wifi', 'metered': True}),          # A phone's hotspot, marked as metered.
                (6, variable | roaming, {'connection': 'wired', 'metered': True}),
                (243, unrestricted, {'connection': 'cellular', 'metered': True}),
                (244, None, {'connection': 'cellular', 'metered': True}),
                (71, 0, unknown), (71, None, unknown), (6, roaming, unknown),   # Windows cannot tell the cost.
                (53, unrestricted, unknown), (131, unrestricted, unknown),      # A VPN or a tunnel.
                (None, unrestricted, unknown)]:                                 # No way out to the internet.
            with self.subTest(kind=kind, cost=cost):
                self.assertEqual(classify(kind, cost), expected)

    def test_policy_hold_stops_owned_upload_process(self):
        process = Mock(); process.poll.return_value = None
        guard = Mock(); guard.check.side_effect = NetworkPolicyHold('held')
        with self.assertRaises(NetworkPolicyHold): wait_process(process, guard)
        process.terminate.assert_called_once(); process.wait.assert_called_once_with(timeout=3)

    def test_settings_file_over_4096_bytes_is_invalid_even_when_its_content_is_valid(self):
        with patch('comic_archive.network.probe', return_value={'connection': 'wifi', 'metered': False}):
            configure(self.root, False)
        path = self.root / '.archive-state/network-policy.json'
        path.write_text(json.dumps({'schema_version': 1, 'wifi_only': False}) + ' ' * 5000)
        with self.assertRaises(ArchiveError): policy(self.root)
        with self.assertRaises(ArchiveError): NetworkGuard(self.root).check()

    def test_probe_commands_return_trimmed_text_and_failures_make_the_route_unknown(self):
        from comic_archive.network import _command
        with patch('comic_archive.network.subprocess.run', return_value=Mock(returncode=0, stdout=b' wifi\n')):
            self.assertEqual(_command('nmcli'), 'wifi')
        for result in (Mock(returncode=1, stdout=b'[]'), Mock(returncode=0, stdout=b'x' * 65537)):
            with patch('comic_archive.network.subprocess.run', return_value=result):
                with self.assertRaises(ValueError): _command('ip')

    def test_a_failed_or_oversized_nmcli_answer_makes_the_route_unknown_instead_of_unmetered_wifi(self):
        healthy = b'wifi\n100 (connected)\nno'

        def route_with(nmcli_returncode, nmcli_output):
            def run(args, **options):
                if args[0] == 'ip':
                    return Mock(returncode=0, stdout=b'[]' if '-6' in args else b'[{"dev":"wlan0"}]')
                return Mock(returncode=nmcli_returncode, stdout=nmcli_output)
            return run

        with patch('comic_archive.network.sys.platform', 'linux'):
            with patch('comic_archive.network.subprocess.run', route_with(0, healthy)):
                self.assertEqual(probe(), {'connection': 'wifi', 'metered': False})
            for returncode, output in ((1, healthy), (0, healthy + b'\n' * 70000)):
                with self.subTest(returncode=returncode, size=len(output)):
                    with patch('comic_archive.network.subprocess.run', route_with(returncode, output)):
                        self.assertEqual(probe(), {'connection': 'unknown', 'metered': None})

    def test_wait_process_returns_the_exit_status_and_only_stops_a_child_that_is_still_running(self):
        process = Mock(returncode=7); process.poll.side_effect = [None, 7]
        with patch('comic_archive.network.time.sleep'):
            self.assertEqual(wait_process(process, Mock()), 7)
        process.terminate.assert_not_called()
        exited = Mock(); exited.poll.return_value = 0
        guard = Mock(); guard.check.side_effect = NetworkPolicyHold('held')
        with self.assertRaises(NetworkPolicyHold): wait_process(exited, guard)
        exited.terminate.assert_not_called()

    def test_wait_process_stops_a_transfer_that_overruns_its_time_and_kills_one_that_ignores_terminate(self):
        process = Mock(); process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired('transfer', 3), None]
        with self.assertRaises(subprocess.TimeoutExpired): wait_process(process, Mock(), timeout=0)
        process.terminate.assert_called_once(); process.kill.assert_called_once()
        self.assertEqual(process.wait.call_count, 2)

    def test_source_download_checks_policy_before_request_and_during_body(self):
        from test_archive import png
        guard = Mock(); guard.check.side_effect = NetworkPolicyHold('held')
        client = Mock()
        transport = Transport(client=client, interval=.25, network_guard=guard)
        with self.assertRaises(NetworkPolicyHold): transport.get('https://cdn.tapas.io/a.png')
        client.stream.assert_not_called()
        changed = False
        def check():
            if changed: raise NetworkPolicyHold('changed')
        guard.check.side_effect = check
        response = Mock(status_code=200, headers={'Content-Type':'image/png'})
        def chunks():
            nonlocal changed
            yield png(1)
            changed = True
            yield png(2)
        response.iter_bytes.side_effect = chunks
        client.stream.return_value.__enter__ = Mock(return_value=response)
        client.stream.return_value.__exit__ = Mock(return_value=False)
        with self.assertRaises(NetworkPolicyHold): transport.get('https://cdn.tapas.io/a.png')

    def test_selected_library_policy_applies_before_metadata_or_single_episode_capture(self):
        from comic_archive.cli import parser,run
        from comic_archive.importer import archive_series
        from comic_archive.metadata import enrich_series
        from comic_archive.models import Series
        from test_jobs import AccountAdapter
        selected=self.root/'Chosen'
        configure(selected,False)
        wrong=self.root/'Other'
        with patch('comic_archive.network.probe',return_value={'connection':'cellular','metered':True}),patch('builtins.print'):
            adapter=Mock();adapter.network_guard=NetworkGuard(wrong)
            def inspect(*args,**kwargs):
                adapter.network_guard.check()
                return {'complete':True,'authenticated':True,'episodes':[],'source_series_id':'42','title':'Synthetic','episode_count':0}
            adapter.inspect_series.side_effect=inspect
            self.assertEqual(archive_series(adapter,'42',selected)['imported_episode_ids'],[])
            adapter.network_guard.root=wrong
            def identity():adapter.network_guard.check();return '11'
            adapter.account_identity.side_effect=identity
            adapter.series_metadata.return_value=Series('42','Synthetic',source='tapas',platform_id='42')
            self.assertFalse(enrich_series(adapter,'42',selected)['reader_or_comic_pages_requested'])
            source=AccountAdapter();source.network_guard=NetworkGuard(wrong)
            capture=source.capture_automatic
            def guarded(url,*args):source.network_guard.check();return capture(url)
            source.capture_automatic=guarded
            context=Mock();context.__enter__=Mock(return_value=source);context.__exit__=Mock(return_value=False)
            with patch('comic_archive.adapters.tapas.TapasAdapter',return_value=context):
                self.assertEqual(run(parser().parse_args(['archive-episode','https://tapas.io/episode/81','--output',str(selected)])),0)
            self.assertEqual(source.network_guard.root,selected.absolute())
