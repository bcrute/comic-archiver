import io
import json
import runpy
import struct
import tempfile
import unittest
import zlib
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch
from comic_archive.archive import verify
from comic_archive.cli import main, parser, run
from comic_archive.errors import LibraryBusy
from comic_archive.inventory import Inventory
from comic_archive.models import ArchiveError

TASK = 'job-' + 'a' * 24


def png(color):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes([0, color, 0, 0]))) + chunk(b"IEND", b""))


def invoke(*argv):
    """Run one command line; return (exit code, stdout, stderr). Errors propagate."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = run(parser().parse_args(list(argv)))
    return code, out.getvalue(), err.getvalue()


class CLITests(unittest.TestCase):
    @patch('builtins.print')
    @patch('comic_archive.dropbox_client.install')
    @patch('comic_archive.cloud.connect', return_value={'connected': True})
    def test_dropbox_maintainer_registration_connects_and_stream_pilot_is_bounded(self, connect, install, output):
        args = parser().parse_args(['cloud-connect', 'dropbox', '--client-json', '/private/client.json'])
        self.assertEqual(run(args), 0)
        install.assert_called_once_with(args.output, args.client_json)
        connect.assert_called_once_with(args.output, 'dropbox')
        args = parser().parse_args(['cloud-test', 'dropbox', '--stream', '--episodes', '2'])
        with patch('comic_archive.cloud.smoke_test', return_value={'streamed_episodes': 2}) as smoke:
            self.assertEqual(run(args), 0)
        smoke.assert_called_once_with(args.output, 'dropbox', episodes=2, stream=True)

    @patch('builtins.print')
    @patch('comic_archive.cloud.smoke_test', return_value={'streamed_episodes': 2})
    def test_google_stream_test_uses_bounded_reusable_upload_path(self, smoke, output):
        args = parser().parse_args(['cloud-test', 'google', '--stream', '--episodes', '2'])
        self.assertEqual(run(args), 0)
        smoke.assert_called_once_with(args.output, 'google', episodes=2, stream=True)

    @patch('builtins.print')
    @patch('comic_archive.google_client.install')
    @patch('comic_archive.cloud.connect', return_value={'connected':True})
    def test_google_client_setup_imports_only_private_file_then_connects(self, connect, install, output):
        args = parser().parse_args(['cloud-connect', 'google', '--client-json', '/private/client.json'])
        self.assertEqual(run(args), 0)
        install.assert_called_once_with(args.output, args.client_json)
        connect.assert_called_once_with(args.output, 'google')
        for extra in (['proton', '--client-json', '/private/client.json'], ['google', '--client-json', '/private/client.json', '--config', '/private/rclone.conf', '--remote', 'google']):
            with self.assertRaises(ArchiveError): run(parser().parse_args(['cloud-connect', *extra]))

    @patch('builtins.print')
    @patch('comic_archive.cli.verify', return_value=[])
    @patch('comic_archive.adapters.tapas.TapasAdapter')
    def test_optional_pixel_verification_uses_only_local_files(self, browser, verify, output):
        from pathlib import Path
        self.assertEqual(run(parser().parse_args(['verify', 'Library', '--decode-images'])), 0)
        verify.assert_called_once_with(Path('Library'), decode_images=True)
        browser.assert_not_called()
        self.assertFalse(parser().parse_args(['verify', 'Library']).decode_images)

    @patch('builtins.print')
    @patch('comic_archive.queue_client.submit_inspection',return_value={'task_id':'job-'+'a'*24})
    @patch('comic_archive.adapters.tapas.TapasAdapter')
    def test_session_health_check_via_worker_uses_no_credentials_or_new_browser(self,browser,submit,output):
        args=parser().parse_args(['check-session','--via-worker'])
        self.assertEqual(run(args),0)
        submit.assert_called_once_with(args.session_dir,'check_session')
        browser.assert_not_called()
        with self.assertRaises(ArchiveError):run(parser().parse_args(['check-session','--via-worker','--output','/PRIVATE']))

    @patch('builtins.print')
    @patch('comic_archive.queue_client.submit_job',return_value={'task_id':'job-'+'a'*24})
    @patch('comic_archive.adapters.tapas.TapasAdapter')
    def test_watched_only_queues_bounded_sync_without_starting_a_second_browser(self,browser,submit,output):
        args=parser().parse_args(['sync','--watched-only','--via-worker','--scan-limit','5','--max-seconds','120'])
        self.assertEqual(run(args),0)
        self.assertEqual(submit.call_args.args[1],'sync')
        self.assertFalse(submit.call_args.args[2]['refresh_lists'])
        self.assertEqual(submit.call_args.args[2]['max_seconds'],120)
        browser.assert_not_called()
        for extra in (['--category','subscribed'],['--include-history']):
            with self.assertRaises(ArchiveError):run(parser().parse_args(['sync','--watched-only',*extra]))

    @patch('builtins.print')
    @patch('comic_archive.task_cancellation.request_queued_stop',return_value={'status':'cancellation_requested'})
    @patch('comic_archive.adapters.tapas.TapasAdapter')
    def test_queued_stop_uses_private_fixed_control_without_opening_a_browser(self,adapter_type,stop,output):
        task='job-'+'a'*24
        args=parser().parse_args(['stop-task',task])
        self.assertEqual(run(args),0);stop.assert_called_once_with(args.session_dir,task)
        adapter_type.assert_not_called()

    @patch('builtins.print')
    @patch('comic_archive.queue_client.submit_job',return_value={'task_id':'job-'+'a'*24})
    @patch('comic_archive.adapters.tapas.TapasAdapter')
    def test_scan_only_queues_bounded_inventory_refresh_without_starting_a_browser(self,browser,submit,output):
        args=parser().parse_args(['sync','--scan-only','--watched-only','--via-worker','--scan-limit','50','--max-seconds','900'])
        self.assertEqual(run(args),0)
        self.assertTrue(submit.call_args.args[2]['scan_only'])
        self.assertFalse(submit.call_args.args[2]['refresh_lists'])
        self.assertEqual(submit.call_args.args[2]['max_seconds'],900)
        browser.assert_not_called()

    @patch('builtins.print')
    @patch('comic_archive.queue_client.submit_inspection')
    @patch('comic_archive.adapters.tapas.TapasAdapter')
    def test_page_size_probe_is_a_bounded_metadata_only_worker_operation(self,adapter_type,submit,output):
        submit.return_value={'task_id':'job-'+'a'*24}
        args=parser().parse_args(['inspect-series','42','--page-size','50','--via-worker'])
        self.assertEqual(run(args),0)
        submit.assert_called_once_with(args.session_dir,'inspect_episode_page_size',{'id':'42','max_limit':50})
        adapter_type.assert_not_called()

    @patch('builtins.print')
    @patch('comic_archive.queue_client.submit_inspection')
    @patch('comic_archive.adapters.tapas.TapasAdapter')
    def test_episode_metadata_worker_operation_never_starts_another_browser(self,adapter_type,submit,output):
        submit.return_value={'task_id':'job-'+'a'*24}
        args=parser().parse_args(['inspect-episode','https://tapas.io/episode/81','--metadata','--via-worker'])
        self.assertEqual(run(args),0)
        submit.assert_called_once_with(args.session_dir,'inspect_episode_access',{'url':'https://tapas.io/episode/81'})
        adapter_type.assert_not_called()

    @patch('builtins.print')
    @patch('comic_archive.queue_client.submit_inspection',return_value={'task_id':'example'})
    def test_library_pagination_uses_only_fixed_category_and_page_budget(self,submit,output):
        args=parser().parse_args(['inspect-library','--pages','60','--via-worker'])
        run(args)
        submit.assert_called_once_with(args.session_dir,'inspect_library_pages',{'category':'subscribed','max_pages':60})
        with self.assertRaises(ArchiveError):run(parser().parse_args(['inspect-library','--pages','0','--via-worker']))

    @patch('builtins.print')
    @patch('comic_archive.queue_client.submit_inspection',return_value={'task_id':'example'})
    def test_daily_configuration_is_a_fixed_worker_request(self,submit,output):
        args=parser().parse_args(['daily-schedule','--run-now','--max-pages','1','--scan-limit','1'])
        run(args)
        submitted=submit.call_args
        self.assertEqual(submitted.args[:2],(args.session_dir,'configure_daily'))
        self.assertTrue(submitted.args[2]['run_now'])
        self.assertEqual(submitted.args[2]['options']['max_pages'],1)
        self.assertEqual(submitted.args[2]['options']['scan_limit'],1)
        self.assertNotIn('output',submitted.args[2])

    def test_the_built_in_engine_gets_the_main_thread_and_every_other_browser_runs_the_command_directly(self):
        from comic_archive import cli
        for browser in ('auto', 'chromium', 'firefox'):
            with patch('comic_archive.cli.run', return_value=7) as command, patch('comic_archive.qt_engine.serve') as serve:
                args = parser().parse_args(['--browser', browser, 'doctor'])
                self.assertEqual(cli.run_beside_engine(args), 7)
                command.assert_called_once_with(args)
                serve.assert_not_called()
        with patch('comic_archive.cli.run', return_value=7) as command, \
                patch('comic_archive.qt_engine.serve', side_effect=lambda work: work()) as serve:
            args = parser().parse_args(['--browser', 'qt', 'doctor'])
            self.assertEqual(cli.run_beside_engine(args), 7)
            serve.assert_called_once()
            command.assert_called_once_with(args)

    @patch('comic_archive.worker.run_worker')
    def test_hosted_worker_can_retain_loaded_code_until_restart(self,worker):
        args=parser().parse_args(['--browser','firefox','--headless','browser-worker','--stable-code'])
        run(args)
        worker.assert_called_once_with(args.session_dir,'firefox',True,1.0,reload_adapter=False)

    @patch('builtins.print')
    @patch('comic_archive.queue_client.submit_inspection')
    @patch('comic_archive.adapters.tapas.TapasAdapter')
    def test_library_type_filter_is_a_fixed_inspection(self,adapter_type,submit,output):
        submit.return_value={'task_id':'example'}
        args=parser().parse_args(['inspect-library','--content-type','comics','--via-worker'])
        self.assertEqual(run(args),0)
        submit.assert_called_once_with(args.session_dir,'inspect_library_filter',{'category':'subscribed','content_type':'comics'})
        adapter_type.assert_not_called()
    @patch('builtins.print')
    @patch('comic_archive.queue_client.submit_inspection')
    @patch('comic_archive.adapters.tapas.TapasAdapter')
    def test_free_access_uses_a_fixed_worker_inspection(self,adapter_type,submit,output):
        submit.return_value={'task_id':'example'}
        args=parser().parse_args(['inspect-free-access','--via-worker'])
        self.assertEqual(run(args),0)
        submit.assert_called_once_with(args.session_dir,'inspect_free_access')
        adapter_type.assert_not_called()
    @patch('builtins.print')
    @patch('comic_archive.queue_client.submit_inspection')
    @patch('comic_archive.adapters.tapas.TapasAdapter')
    def test_series_worker_metadata_is_a_fixed_read_only_operation(self,adapter_type,submit,output):
        submit.return_value={'task_id':'example'}
        args=parser().parse_args(['inspect-series','42','--metadata','--via-worker'])
        self.assertEqual(run(args),0)
        submit.assert_called_once_with(args.session_dir,'inspect_series_metadata',{'id':'42'})
        adapter_type.assert_not_called()

    @patch("builtins.print")
    @patch("comic_archive.cli.Store")
    @patch("comic_archive.adapters.tapas.TapasAdapter")
    def test_episode_url_alone_selects_automatic_capture_without_input(self, adapter_type, store, output):
        args = parser().parse_args(["archive-episode", "https://tapas.io/episode/81"])
        adapter = adapter_type.return_value.__enter__.return_value
        with patch("builtins.input") as prompt:
            self.assertEqual(run(args), 0)
        adapter.capture_automatic.assert_called_once_with("https://tapas.io/episode/81", None, None, None, None)
        adapter.capture.assert_not_called()
        prompt.assert_not_called()
        store.return_value.archive.assert_called_once()
        store.return_value.close.assert_called_once()

    @patch("comic_archive.adapters.tapas.TapasAdapter")
    def test_manual_metadata_and_custom_selectors_are_explicit(self, adapter_type):
        for extras in (["--manual"], ["--page-selector", "img"]):
            args = parser().parse_args(["archive-episode", "https://tapas.io/episode/81"] + extras)
            with self.assertRaises(ArchiveError):
                run(args)
        adapter_type.assert_not_called()

    @patch("builtins.print")
    @patch("comic_archive.adapters.tapas.TapasAdapter")
    def test_inspection_commands_do_not_require_manual_navigation(self, adapter_type, output):
        adapter = adapter_type.return_value.__enter__.return_value
        adapter.inspect_series.return_value = {}
        adapter.inspect_episode.return_value = {}
        adapter.inspect_library.return_value = {}
        with patch("builtins.input") as prompt:
            run(parser().parse_args(["inspect-series", "series-slug"]))
            run(parser().parse_args(["inspect-episode", "https://tapas.io/episode/81"]))
            run(parser().parse_args(["inspect-library"]))
        prompt.assert_not_called()
        adapter.inspect_series.assert_called_once_with("series-slug",strict_order=False)
        adapter.inspect_episode.assert_called_once_with("https://tapas.io/episode/81", interactive=False)
        adapter.inspect_library.assert_called_once()

    @patch("builtins.print")
    @patch("comic_archive.cli.Store")
    @patch("comic_archive.adapters.tapas.TapasAdapter")
    def test_headless_and_request_interval_reach_automatic_adapter(self, adapter_type, store, output):
        args=parser().parse_args(["--browser", "firefox", "--headless", "--request-interval", "0.5",
                                 "archive-episode", "https://tapas.io/episode/81"])
        run(args)
        adapter_type.assert_called_once_with(args.session_dir, "firefox", True, 0.5)

    @patch("builtins.print")
    @patch("comic_archive.discovery.discover_available")
    @patch("comic_archive.adapters.tapas.TapasAdapter")
    def test_discovery_is_metadata_only_with_explicit_resume(self, adapter_type, discover, output):
        discover.return_value = dict(state="finished", complete=False, scanned_series_count=1,
                                     scanned_episode_count=3, free_episode_count=1, unlocked_episode_count=1, cached_series_count=0)
        args = parser().parse_args(["--browser", "firefox", "--headless", "discover-available", "--category", "recent", "--resume"])
        with patch("builtins.input") as prompt:
            self.assertEqual(run(args), 0)
        discover.assert_called_once_with(adapter_type.return_value.__enter__.return_value, args.report, ["recent"], True)
        prompt.assert_not_called()

    @patch("builtins.print")
    @patch("comic_archive.adapters.tapas.TapasAdapter")
    def test_recent_library_category_reaches_adapter(self, adapter_type, output):
        adapter = adapter_type.return_value.__enter__.return_value
        adapter.inspect_library.return_value = {}
        run(parser().parse_args(["inspect-library", "--category", "recent"]))
        adapter.inspect_library.assert_called_once_with(category="recent")

    @patch('builtins.print')
    @patch('comic_archive.queue_client.submit_inspection')
    @patch('comic_archive.adapters.tapas.TapasAdapter')
    def test_history_resume_uses_existing_worker_without_starting_browser(self,adapter_type,submit,output):
        submit.return_value = {'task_id':'example'}
        args = parser().parse_args(['inspect-history','--via-worker','--resume','--max-pages','5'])
        self.assertEqual(run(args),0)
        submit.assert_called_once_with(args.session_dir,'inspect_transaction_history',{'max_pages':5,'resume':True})
        adapter_type.assert_not_called()
        for command in (['inspect-history','--resume'],['sync','--history-resume'],
                        ['sync','--history-resume','--via-worker']):
            with self.assertRaises(ArchiveError):
                run(parser().parse_args(command))

    def test_main_turns_failures_into_exit_codes_and_never_echoes_raw_exceptions(self):
        secret = 'https://cdn.example/page.png?token=SECRET'
        cases = ((ArchiveError('Library is busy.'), 1, 'Error: Library is busy.'),
                 (KeyboardInterrupt(), 130, 'completed pages remain cached'),
                 (EOFError(), 1, 'needs a terminal'),
                 (RuntimeError('GET ' + secret), 1, 'Operation failed'))
        for error, code, text in cases:
            with self.subTest(error=type(error).__name__), patch('sys.argv', ['comic-archive', 'doctor']), \
                    patch('comic_archive.cli.run', side_effect=error), redirect_stderr(io.StringIO()) as err, \
                    self.assertRaises(SystemExit) as raised:
                main()
            self.assertEqual(raised.exception.code, code)
            self.assertIn(text, err.getvalue())
            self.assertNotIn('SECRET', err.getvalue())
        for returned in (0, 1):
            with patch('sys.argv', ['comic-archive', 'doctor']), patch('comic_archive.cli.run', return_value=returned), \
                    self.assertRaises(SystemExit) as raised:
                main()
            self.assertEqual(raised.exception.code, returned)

    def test_python_dash_m_entry_point_exits_nonzero_for_a_library_without_archives(self):
        with tempfile.TemporaryDirectory() as tmp, patch('sys.argv', ['comic-archive', 'verify', tmp]), \
                redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err, \
                self.assertRaises(SystemExit) as raised:
            runpy.run_module('comic_archive', run_name='__main__')
        self.assertEqual(raised.exception.code, 1)
        self.assertIn('No portable episode archives found', err.getvalue())
        self.assertNotIn('verified', out.getvalue())

    def test_verify_failure_lists_each_problem_on_stderr_and_exits_nonzero(self):
        with patch('comic_archive.cli.verify', return_value=['a.cbz: size mismatch', 'b.cbz: hash mismatch']):
            code, out, err = invoke('verify', 'Library')
        self.assertEqual(code, 1)
        self.assertEqual(out, '')
        self.assertEqual(err, 'a.cbz: size mismatch\nb.cbz: hash mismatch\n')

    def test_audit_exit_status_follows_file_integrity_not_missing_episodes(self):
        result = {'verified_inventory_episode_count': 4, 'missing_recorded_accessible_episode_count': 1,
                  'scan_remaining_series_count': 2, 'archive_integrity_passed': True}
        with patch('comic_archive.audit.audit_library', return_value=result) as audit:
            code, out, _ = invoke('audit', '--report', 'r.json')
            audit.assert_called_once_with(Path('Library'), Path('r.json'))
            self.assertEqual(code, 0)
            self.assertIn('Verified 4 inventoried episodes; 1 recorded accessible episodes', out)
            self.assertIn('Portable file integrity: passed', out)
            self.assertIn('Dated reconciliation saved as JSON.', out)
            audit.return_value = dict(result, archive_integrity_passed=False)
            code, out, _ = invoke('audit')
            self.assertEqual(code, 1)
            self.assertIn('requires repair', out)
            self.assertNotIn('saved as JSON', out)
            code, out, _ = invoke('audit', '--json')
            self.assertEqual((code, json.loads(out)), (1, audit.return_value))

    def test_local_site_refuses_ports_above_65535(self):
        with patch('comic_archive.local_web.serve') as serve:
            with self.assertRaises(ArchiveError):
                invoke('serve-local', '--port', '65536')
            serve.assert_not_called()

    def test_cloud_connect_needs_config_and_remote_together_and_warns_for_proton(self):
        with patch('comic_archive.cloud.connect', return_value={'step': 'signed_in'}) as connect, \
                patch('comic_archive.cloud.configure_target', return_value={'step': 'adopted'}) as target:
            for extra in (['--config', 'rclone.conf'], ['--remote', 'mine']):
                with self.subTest(extra=extra), self.assertRaises(ArchiveError):
                    invoke('cloud-connect', 'google', *extra)
            connect.assert_not_called()
            target.assert_not_called()
            code, out, err = invoke('cloud-connect', 'proton', '--config', 'rclone.conf', '--remote', 'mine')
            self.assertEqual((code, json.loads(out)), (0, {'step': 'adopted'}))
            self.assertIn('not officially supported by Proton', err)
            target.assert_called_once_with(Path('Library'), 'proton', config=Path('rclone.conf'), remote='mine')
            code, out, err = invoke('cloud-connect', 'google')
            self.assertEqual((code, json.loads(out), err), (0, {'step': 'signed_in'}, ''))
            connect.assert_called_once_with(Path('Library'), 'google')

    def test_cloud_commands_upload_existing_episodes_only_when_asked(self):
        with patch('comic_archive.cloud.upload_pending', return_value={'queued': 'pending'}) as pending, \
                patch('comic_archive.cloud.upload_existing', return_value={'queued': 'existing'}) as existing, \
                patch('comic_archive.cloud.enable', return_value={}) as enable, \
                patch('comic_archive.cloud.smoke_test', return_value={}) as smoke, \
                patch('comic_archive.cloud.cleanup_test', return_value={}) as cleanup, \
                patch('comic_archive.cloud.status', return_value={'pending_uploads': 2}) as status:
            self.assertEqual(json.loads(invoke('cloud-upload', '--limit', '5')[1]), {'queued': 'pending'})
            pending.assert_called_once_with(Path('Library'), limit=5, provider=None)
            existing.assert_not_called()
            self.assertEqual(json.loads(invoke('cloud-upload', '--existing', '--provider', 'google')[1]), {'queued': 'existing'})
            existing.assert_called_once_with(Path('Library'), limit=3, provider='google')
            invoke('cloud-enable', 'proton', '--disable')
            enable.assert_called_with(Path('Library'), 'proton', False)
            invoke('cloud-enable', 'google')
            enable.assert_called_with(Path('Library'), 'google', True)
            invoke('cloud-test', 'google', '--episodes', '3')
            smoke.assert_called_once_with(Path('Library'), 'google', episodes=3)
            invoke('cloud-cleanup', 'test-1')
            cleanup.assert_called_once_with(Path('Library'), 'test-1')
            calls = (pending.call_count, existing.call_count, enable.call_count)
            code, out, _ = invoke('cloud-status', '--output', 'Other')
            self.assertEqual((code, json.loads(out)), (0, {'pending_uploads': 2}))
            status.assert_called_once_with(Path('Other'))
            self.assertEqual((pending.call_count, existing.call_count, enable.call_count), calls)

    def test_network_policy_changes_only_when_a_choice_is_given(self):
        with patch('comic_archive.network.configure', return_value={'wifi_only': True}) as configure, \
                patch('comic_archive.network.status', return_value={'allowed': False}) as status:
            self.assertEqual(json.loads(invoke('network-policy')[1]), {'allowed': False})
            configure.assert_not_called()
            invoke('network-policy', '--wifi-only')
            configure.assert_called_with(Path('Library'), True)
            invoke('network-policy', '--allow-metered')
            configure.assert_called_with(Path('Library'), False)
            self.assertEqual(configure.call_count, 2)
            self.assertEqual(status.call_count, 1)
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                invoke('network-policy', '--wifi-only', '--allow-metered')
            self.assertEqual(configure.call_count, 2)

    def test_storage_policy_purges_completed_cache_unless_keep_is_requested_and_plan_estimates_the_requested_kind(self):
        with patch('comic_archive.storage.configure', return_value={}) as configure, \
                patch('comic_archive.storage.storage_plan', return_value={'fits': True}) as plan:
            invoke('storage-policy')
            configure.assert_called_with(Path('Library'), image_profile='original', purge_completed_cache=True)
            invoke('storage-policy', '--image-profile', 'small', '--keep-completed-cache')
            configure.assert_called_with(Path('Library'), image_profile='small', purge_completed_cache=False)
            self.assertEqual(json.loads(invoke('storage-plan', '--episodes', '5', '--content-type', 'novel')[1]), {'fits': True})
            plan.assert_called_once_with(Path('Library'), episode_count=5, episode_kind='novel')
            invoke('storage-plan')
            plan.assert_called_with(Path('Library'), episode_count=None, episode_kind='comic')

    def test_purge_and_comic_conversion_default_to_nondestructive_and_report_only_counts(self):
        def fake_purge(root, **options):
            options['progress']({'verified_archives': 3, 'path': '/private/Series/ep.cbz'})
            return {'candidates': []}
        with patch('comic_archive.purge.purge_library', side_effect=fake_purge) as purge:
            code, out, err = invoke('purge')
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(err), {'verified_archives': 3})
            options = purge.call_args.kwargs
            self.assertEqual((purge.call_args.args, options['apply'], options['include_legacy'], options['wait_seconds']),
                             ((Path('Library'),), False, False, 0))
            invoke('purge', '--apply', '--include-legacy', '--wait-for-lock', '2.5')
            options = purge.call_args.kwargs
            self.assertEqual((options['apply'], options['include_legacy'], options['wait_seconds']), (True, True, 2.5))

        def fake_convert(root, **options):
            options['progress']({'converted': 1})
            return {'converted': 1}
        with patch('comic_archive.cbz_conversion.convert_comics', side_effect=fake_convert) as convert:
            code, out, err = invoke('convert-comics')
            self.assertEqual((json.loads(err), json.loads(out)), ({'converted': 1}, {'converted': 1}))
            self.assertEqual({k: v for k, v in convert.call_args.kwargs.items() if k != 'progress'},
                             {'limit': None, 'remove_originals': False, 'min_free_bytes': 256 * 1024 ** 2})
            invoke('convert-comics', '--remove-originals', '--limit', '2', '--reserve-bytes', '10')
            self.assertEqual({k: v for k, v in convert.call_args.kwargs.items() if k != 'progress'},
                             {'limit': 2, 'remove_originals': True, 'min_free_bytes': 10})

    def test_backup_and_export_conversion_pass_free_space_reserves_and_stream_progress(self):
        def fake(root, *rest, **options):
            options['progress']({'step': 1})
            return {'done': True}
        with patch('comic_archive.backup.backup_library', side_effect=fake) as backup:
            code, out, _ = invoke('backup', 'dest', '--wait-for-lock', '4')
            lines = out.splitlines()
            self.assertEqual(code, 0)
            self.assertIn('active writer must finish first', lines[0])
            self.assertEqual(json.loads(lines[1]), {'step': 1})
            self.assertEqual(json.loads('\n'.join(lines[2:])), {'done': True})
            self.assertEqual(backup.call_args.args, (Path('Library'), Path('dest')))
            self.assertEqual((backup.call_args.kwargs['wait_seconds'], backup.call_args.kwargs['reserve_bytes']), (4.0, 1024 ** 3))
        with patch('comic_archive.migration.migrate_exports', side_effect=fake) as migrate:
            code, out, _ = invoke('convert-exports', '--limit', '3')
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out.splitlines()[0]), {'step': 1})
            self.assertEqual((migrate.call_args.args, migrate.call_args.kwargs['limit'], migrate.call_args.kwargs['min_free_bytes']),
                             ((Path('Library'),), 3, 256 * 1024 ** 2))

    def test_queue_priority_is_saved_in_the_library_or_queued_for_the_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            library = Path(tmp) / 'Library'
            code, out, _ = invoke('queue-priority', 'owned_first', '--output', str(library))
            self.assertEqual((code, json.loads(out)), (0, {'queue_priority': 'owned_first'}))
            with Inventory(library) as inventory:
                self.assertEqual(inventory.queue_priority(), 'owned_first')
        with patch('comic_archive.queue_client.submit_inspection', return_value={'task_id': TASK}) as submit:
            with self.assertRaises(ArchiveError):
                invoke('queue-priority', 'owned_first', '--via-worker', '--output', 'Other')
            submit.assert_not_called()
            code, out, _ = invoke('queue-priority', 'expiring_first', '--via-worker')
            self.assertEqual((code, json.loads(out)), (0, {'task_id': TASK}))
            submit.assert_called_once_with(Path('.comic-session'), 'configure_priority', {'priority': 'expiring_first'})

    def test_worker_commands_exit_nonzero_when_start_or_stop_did_not_complete(self):
        with patch('comic_archive.background.start') as start, patch('comic_archive.background.stop') as stop, \
                patch('comic_archive.background.status', return_value={'running': True}) as status:
            for failure in ('start_failed', 'start_deferred', 'backlog_resume_failed'):
                start.return_value = {failure: True}
                self.assertEqual(invoke('start-worker')[0], 1, failure)
            start.return_value = {'running': True}
            code, out, _ = invoke('--browser', 'firefox', '--request-interval', '2', 'start-worker', '--resume-backlog', '--restart')
            self.assertEqual((code, json.loads(out)), (0, {'running': True}))
            start.assert_called_with(Path('.comic-session'), 'firefox', 2.0, resume=True, restart=True)
            stop.return_value = {'stop_wait_expired': True}
            self.assertEqual(invoke('stop-worker', '--wait', '5')[0], 1)
            stop.assert_called_once_with(Path('.comic-session'), wait_seconds=5.0)
            stop.return_value = {'stopped': True}
            self.assertEqual(invoke('stop-worker')[0], 0)
            self.assertEqual(invoke('worker-status')[0], 0)
            status.assert_called_once_with(Path('.comic-session'))

    def test_account_export_fast_speed_raises_batch_bytes_and_held_status_fails(self):
        with patch('comic_archive.account_export.configure', return_value={'status': 'held'}) as configure, \
                patch('comic_archive.account_export.status', return_value={'status': 'running'}) as status, \
                patch('comic_archive.account_export.request_stop', return_value={'status': 'stopping'}) as stop:
            code, out, _ = invoke('extract-account', '--speed', 'fast', '--download-workers', '3', '--resume')
            self.assertEqual((code, json.loads(out)), (1, {'status': 'held'}))
            configure.assert_called_once_with(Path('.comic-session'), resume=True, window_budgets={
                'request_interval': 1.0, 'speed': 'fast', 'download_workers': 3, 'batch_bytes': 8 * 1024 ** 3})
            invoke('extract-account')
            self.assertNotIn('batch_bytes', configure.call_args.kwargs['window_budgets'])
            self.assertEqual(invoke('account-status')[0], 0)
            status.assert_called_once_with(Path('.comic-session'))
            self.assertEqual(invoke('stop-account')[0], 0)
            stop.assert_called_once_with(Path('.comic-session'))

    def test_complete_backlog_hands_overall_limits_and_window_modes_to_the_controller(self):
        with patch('comic_archive.completion.configure', return_value={'status': 'paused'}) as configure, \
                patch('comic_archive.completion.status', return_value={'status': 'running'}) as status, \
                patch('comic_archive.completion.request_stop', return_value={'status': 'stopping'}) as stop:
            code, _, _ = invoke('complete-backlog', '--speed', 'fast', '--download-workers', '2', '--no-source-recovery',
                                '--max-windows', '3', '--until-drained', '--resume')
            self.assertEqual(code, 1)
            self.assertEqual(configure.call_args.args, (Path('.comic-session'),))
            options = configure.call_args.kwargs
            self.assertEqual(options['values'], {'max_windows': 3, 'max_days': 7, 'max_pages': 700000,
                                                 'max_bytes': 448 * 1024 ** 3, 'max_network_recoveries': 4,
                                                 'max_source_recoveries': 8})
            self.assertEqual(options['window_budgets'], {'request_interval': 1.0, 'recover_source': False, 'speed': 'fast',
                                                         'batch_bytes': 8 * 1024 ** 3, 'download_workers': 2})
            self.assertEqual((options['resume'], options['until_drained']), (True, True))
            configure.return_value = {'status': 'running'}
            self.assertEqual(invoke('complete-backlog')[0], 0)
            self.assertEqual(configure.call_args.kwargs['window_budgets'], {'request_interval': 1.0})
            self.assertEqual((invoke('completion-status')[0], invoke('stop-completion')[0]), (0, 0))
            status.assert_called_once_with(Path('.comic-session'))
            stop.assert_called_once_with(Path('.comic-session'))

    def test_consecutive_import_commands_refuse_a_different_session_directory(self):
        with patch('comic_archive.batches.run') as batch_run, patch('comic_archive.batches.status') as status, \
                patch('comic_archive.batches.request_stop') as stop:
            for command in ('run-backlog', 'backlog-status', 'stop-backlog'):
                with self.subTest(command=command), self.assertRaises(ArchiveError):
                    invoke('--session-dir', 'elsewhere', command)
            for mock in (batch_run, status, stop):
                mock.assert_not_called()

    def test_run_backlog_hands_budgets_to_the_controller_and_pauses_with_failure_status(self):
        seen = {}

        def fake_run(session_dir, *, values, resume, progress):
            seen.update(values=values, resume=resume)
            progress('batch 1 submitted')
            return {'status': seen.get('status', 'paused')}
        with patch('comic_archive.batches.run', side_effect=fake_run), \
                patch('comic_archive.batches.status', return_value={'status': 'idle'}) as status, \
                patch('comic_archive.batches.request_stop', return_value={'status': 'stopping'}) as stop:
            code, out, _ = invoke('run-backlog', '--archive-only', '--free-only', '--max-batches', '2',
                                  '--download-workers', '4', '--speed', 'fast', '--resume', '--source-recovery-limit', '1')
            self.assertEqual(code, 1)
            self.assertIn('batch 1 submitted\n', out)
            values = seen['values']
            self.assertEqual((values['max_batches'], values['download_workers'], values['speed'], values['free_only'],
                              values['source_recovery_limit'], values['archive_only'], values['refresh_lists'],
                              values['request_interval']), (2, 4, 'fast', True, 1, True, False, 1.0))
            self.assertTrue(seen['resume'])
            invoke('run-backlog', '--watched-only')
            self.assertEqual((seen['values']['refresh_lists'], seen['values']['archive_only'], seen['resume']), (False, False, False))
            seen['status'] = 'running'
            code, _, _ = invoke('run-backlog')
            self.assertEqual((code, seen['values']['refresh_lists']), (0, True))
            self.assertEqual((invoke('backlog-status')[0], invoke('stop-backlog')[0]), (0, 0))
            status.assert_called_once_with(Path('.comic-session'))
            stop.assert_called_once_with(Path('.comic-session'))

    def test_task_status_exits_nonzero_for_a_failed_task(self):
        with patch('comic_archive.queue_client.task_status') as task_status:
            task_status.return_value = {'status': 'error', 'error': 'Library is busy.'}
            code, out, _ = invoke('task-status', TASK)
            self.assertEqual((code, json.loads(out)['error']), (1, 'Library is busy.'))
            task_status.assert_called_once_with(Path('.comic-session'), TASK)
            task_status.return_value = {'status': 'done'}
            self.assertEqual(invoke('task-status', TASK)[0], 0)

    def test_worker_queued_imports_refuse_overrides_the_worker_would_ignore(self):
        url = 'https://tapas.io/series/x'
        with patch('comic_archive.queue_client.submit_job', return_value={'task_id': TASK}) as submit, \
                patch('comic_archive.adapters.tapas.TapasAdapter') as adapter_type:
            for argv in (['archive-discovered', '--output', 'Other'], ['sync', '--report', 'r.json'],
                         ['import-series', url, '--wait-for-lock', '5'], ['archive-discovered', '--wait-for-lock', '1']):
                with self.subTest(argv=argv), self.assertRaises(ArchiveError):
                    invoke(*argv, '--via-worker')
            submit.assert_not_called()
            code, out, _ = invoke('import-series', url, '--via-worker')
            self.assertEqual(code, 0)
            self.assertIn(f'Queued {TASK}.', out)
            self.assertIn(f'task-status {TASK}', out)
            self.assertEqual(submit.call_args.args[:2], (Path('.comic-session'), 'archive_series_url'))
            self.assertEqual(submit.call_args.args[2], {
                'limit': 1, 'max_pages': 1000, 'max_bytes': 256 * 1024 ** 2, 'max_seconds': 900, 'free_only': False,
                'request_interval': 1.0, 'download_workers': 2, 'url': url})
            invoke('archive-discovered', '--via-worker', '--speed', 'fast', '--limit', '4')
            self.assertEqual(submit.call_args.args[1], 'archive_discovered')
            self.assertEqual((submit.call_args.args[2]['speed'], submit.call_args.args[2]['limit']), ('fast', 4))
            self.assertNotIn('download_workers', submit.call_args.args[2])
            adapter_type.assert_not_called()

    def test_import_series_with_a_discovery_report_is_refused_before_a_browser_opens(self):
        with patch('comic_archive.adapters.tapas.TapasAdapter') as adapter_type, \
                patch('comic_archive.jobs.archive_series_url') as job:
            with self.assertRaises(ArchiveError):
                invoke('import-series', 'https://tapas.io/series/x', '--report', 'r.json')
            adapter_type.return_value.__enter__.assert_not_called()
            job.assert_not_called()

    def test_archive_discovered_uses_the_library_report_and_summarises_novel_chapters(self):
        result = {'archived_episode_ids': ['1', '2'], 'downloaded_pages': 7, 'downloaded_chapters': 2, 'downloaded_covers': 1,
                  'downloaded_bytes': 3 * 1024 ** 2, 'status': 'budget_reached', 'pending_episode_count': 5}
        with tempfile.TemporaryDirectory() as tmp, patch('comic_archive.jobs.archive_discovered', return_value=result) as job, \
                patch('comic_archive.adapters.tapas.TapasAdapter') as adapter_type:
            adapter = adapter_type.return_value.__enter__.return_value
            library = Path(tmp) / 'Library'
            library.mkdir()
            code, out, _ = invoke('archive-discovered', '--output', str(library), '--limit', '2', '--download-workers', '3')
            self.assertEqual(code, 0)
            job.assert_called_once_with(adapter, library, None, limit=2, max_pages=None, max_bytes=None, max_seconds=None,
                                        free_only=False, download_workers=3)
            self.assertIn('Archived 2 episodes; downloaded 5 comic pages and 2 novel chapters and 1 covers (3.00 MiB).', out)
            self.assertIn('Job budget reached; 5 matching episodes remain queued.', out)
            (library / 'discovery.json').write_text('{}')
            invoke('archive-discovered', '--output', str(library))
            self.assertEqual(job.call_args.args[2], library / 'discovery.json')
            invoke('archive-discovered', '--output', str(library), '--report', 'other.json')
            self.assertEqual(job.call_args.args[2], Path('other.json'))
            code, out, _ = invoke('archive-discovered', '--output', str(library), '--json')
            self.assertEqual(json.loads(out), result)

    def test_sync_hands_scan_options_to_the_job_and_pages_only_for_comics(self):
        result = {'archived_episode_ids': [], 'downloaded_pages': 0, 'downloaded_bytes': 0, 'status': 'complete'}
        with tempfile.TemporaryDirectory() as tmp, patch('comic_archive.jobs.sync', return_value=result) as job, \
                patch('comic_archive.adapters.tapas.TapasAdapter') as adapter_type:
            adapter = adapter_type.return_value.__enter__.return_value
            library = Path(tmp)
            code, out, _ = invoke('sync', '--output', str(library), '--category', 'recent', '--include-history',
                                  '--history-pages', '7', '--free-only', '--scan-limit', '4')
            self.assertEqual(code, 0)
            job.assert_called_once_with(adapter, library, None, ['recent'], include_history=True, history_pages=7, scan_limit=4,
                                        refresh_lists=True, scan_only=False, limit=None, max_pages=None, max_bytes=None,
                                        max_seconds=None, free_only=True)
            self.assertIn('downloaded 0 pages and 0 covers', out)
            invoke('sync', '--output', str(library), '--watched-only', '--scan-only')
            self.assertEqual((job.call_args.kwargs['refresh_lists'], job.call_args.kwargs['scan_only']), (False, True))

    def test_fast_speed_uses_the_fast_adapter_with_its_own_worker_pool(self):
        from comic_archive.fast_import import FAST_WORKERS
        result = {'archived_episode_ids': [], 'downloaded_pages': 0, 'downloaded_bytes': 0, 'status': 'complete'}
        with tempfile.TemporaryDirectory() as tmp, patch('comic_archive.jobs.archive_discovered', return_value=result) as job, \
                patch('comic_archive.fast_import.FastImportAdapter') as fast_type, \
                patch('comic_archive.adapters.tapas.TapasAdapter') as adapter_type:
            invoke('--request-interval', '5', 'archive-discovered', '--output', tmp, '--speed', 'fast', '--download-workers', '3')
            adapter_type.assert_not_called()
            fast_type.assert_called_once_with(Path('.comic-session'), 'auto', headless=False)
            self.assertIs(job.call_args.args[0], fast_type.return_value.__enter__.return_value)
            self.assertEqual(job.call_args.kwargs['download_workers'], FAST_WORKERS)
            self.assertNotIn('request_interval', job.call_args.kwargs)

    def test_busy_library_stops_a_local_import_before_the_browser_starts(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch('comic_archive.locking.wait_for_library', side_effect=LibraryBusy('Another job is writing this library.')) as wait, \
                patch('comic_archive.jobs.sync') as job, patch('comic_archive.adapters.tapas.TapasAdapter') as adapter_type:
            with self.assertRaises(LibraryBusy):
                invoke('sync', '--output', tmp, '--wait-for-lock', '30')
            wait.assert_called_once_with(Path(tmp), 30.0)
            adapter_type.return_value.__enter__.assert_not_called()
            job.assert_not_called()

    def test_inspections_refuse_unsafe_or_contradictory_options_before_opening_a_browser(self):
        episode = 'https://tapas.io/episode/81'
        with patch('comic_archive.adapters.tapas.TapasAdapter') as adapter_type:
            for argv in (['inspect-history', '--max-pages', '0'], ['inspect-history', '--max-pages', '501'],
                         ['inspect-episode', episode, '--metadata', '--details'],
                         ['inspect-series', '42', '--metadata', '--page-size', '20'],
                         ['inspect-series', '42', '--metadata', '--page-size', '20', '--via-worker'],
                         ['inspect-series', '../etc', '--metadata'],
                         ['inspect-library', '--pages', '201'], ['inspect-library', '--pages', '5', '--content-type', 'comics']):
                with self.subTest(argv=argv), self.assertRaises(ArchiveError):
                    invoke(*argv)
            adapter_type.assert_not_called()

    def test_local_inspections_hand_their_options_to_the_adapter_and_print_json(self):
        episode = 'https://tapas.io/episode/81'
        with patch('comic_archive.adapters.tapas.TapasAdapter') as adapter_type:
            adapter = adapter_type.return_value.__enter__.return_value
            for method in ('inspect_transaction_history', 'inspect_episode_access', 'inspect_episode', 'inspect_series_metadata',
                           'inspect_episode_page_size', 'inspect_library_filter', 'inspect_library', 'inspect_library_pages',
                           'inspect_free_access'):
                getattr(adapter, method).return_value = {'method': method}
            code, out, _ = invoke('inspect-history', '--max-pages', '7')
            self.assertEqual((code, json.loads(out)), (0, {'method': 'inspect_transaction_history'}))
            adapter.inspect_transaction_history.assert_called_once_with(7)
            invoke('inspect-episode', episode, '--metadata')
            adapter.inspect_episode_access.assert_called_once_with(episode)
            invoke('inspect-episode', episode, '--details')
            adapter.inspect_episode.assert_called_once_with(episode, interactive=False, details=True)
            invoke('inspect-series', '42', '--metadata')
            adapter.inspect_series_metadata.assert_called_once_with('42')
            invoke('inspect-series', '42', '--page-size', '50')
            adapter.inspect_episode_page_size.assert_called_once_with('42', 50)
            invoke('inspect-library', '--content-type', 'novels', '--category', 'recent')
            adapter.inspect_library_filter.assert_called_once_with('recent', 'novels')
            invoke('inspect-library', '--details')
            adapter.inspect_library.assert_called_once_with(details=True, category='subscribed')
            invoke('inspect-library', '--pages', '12', '--category', 'updated')
            adapter.inspect_library_pages.assert_called_once_with('updated', 12)
            invoke('inspect-free-access')
            adapter.inspect_free_access.assert_called_once_with()

    def test_worker_inspections_queue_the_matching_operation_with_only_the_requested_options(self):
        episode = 'https://tapas.io/episode/81'
        with patch('comic_archive.queue_client.submit_inspection', return_value={'task_id': TASK}) as submit, \
                patch('comic_archive.adapters.tapas.TapasAdapter') as adapter_type:
            code, out, _ = invoke('inspect-episode', episode, '--via-worker', '--details')
            self.assertEqual((code, json.loads(out)), (0, {'task_id': TASK}))
            submit.assert_called_with(Path('.comic-session'), 'inspect_episode', {'url': episode, 'details': True})
            invoke('inspect-episode', episode, '--via-worker')
            submit.assert_called_with(Path('.comic-session'), 'inspect_episode', {'url': episode})
            code, out, _ = invoke('inspect-series', '42', '--via-worker')
            submit.assert_called_with(Path('.comic-session'), 'inspect_series', {'id': '42', 'strict_order': False})
            self.assertIn(f'task-status {TASK}', out)
            invoke('inspect-library', '--via-worker', '--details', '--category', 'recent')
            submit.assert_called_with(Path('.comic-session'), 'inspect_library', {'category': 'recent', 'details': True})
            adapter_type.assert_not_called()

    def test_series_enrichment_through_the_worker_refuses_other_library_folders(self):
        with patch('comic_archive.queue_client.submit_job', return_value={'task_id': TASK}) as submit, \
                patch('comic_archive.metadata.enrich_series', return_value={'enriched': True}) as enrich, \
                patch('comic_archive.adapters.tapas.TapasAdapter') as adapter_type:
            with self.assertRaises(ArchiveError):
                invoke('enrich-series', '42', '--via-worker', '--output', 'Other')
            submit.assert_not_called()
            code, out, _ = invoke('enrich-series', '42', '--via-worker', '--refresh')
            self.assertEqual(code, 0)
            self.assertIn(f'Queued {TASK}.', out)
            submit.assert_called_once_with(Path('.comic-session'), 'enrich_series', {'id': '42', 'refresh': True})
            adapter_type.assert_not_called()
            code, out, _ = invoke('enrich-series', '42', '--output', 'Other')
            self.assertEqual((code, json.loads(out)), (0, {'enriched': True}))
            enrich.assert_called_once_with(adapter_type.return_value.__enter__.return_value, '42', Path('Other'), False)

    def test_local_session_check_uses_its_own_browser_and_the_chosen_library_not_the_worker(self):
        with patch('comic_archive.jobs.check_session', return_value={'authenticated': True}) as check, \
                patch('comic_archive.queue_client.submit_inspection') as submit, \
                patch('comic_archive.adapters.tapas.TapasAdapter') as adapter_type:
            code, out, _ = invoke('--browser', 'firefox', 'check-session', '--output', 'Other')
            self.assertEqual((code, json.loads(out)), (0, {'authenticated': True}))
            adapter_type.assert_called_once_with(Path('.comic-session'), 'firefox', False, 1.0)
            check.assert_called_once_with(adapter_type.return_value.__enter__.return_value, Path('Other'))
            submit.assert_not_called()

    def test_login_refuses_headless_password_entry_and_imports_firefox_cookies_only_when_asked(self):
        with patch('comic_archive.session.login') as login, patch('comic_archive.session.import_firefox') as import_firefox, \
                patch('comic_archive.session.doctor') as doctor:
            with self.assertRaises(ArchiveError):
                invoke('--headless', 'login')
            login.assert_not_called()
            import_firefox.assert_not_called()
            self.assertEqual(invoke('--headless', 'login', '--firefox-profile', 'profile')[0], 0)
            import_firefox.assert_called_once_with(Path('profile'), Path('.comic-session'))
            login.assert_not_called()
            self.assertEqual(invoke('--browser', 'chromium', 'login')[0], 0)
            login.assert_called_once_with(Path('.comic-session'), 'chromium')
            self.assertEqual(import_firefox.call_count, 1)
            self.assertEqual(invoke('--browser', 'firefox', '--headless', 'doctor')[0], 0)
            doctor.assert_called_once_with('firefox', True)

    def test_http_only_episode_capture_never_starts_a_browser(self):
        url = 'https://tapas.io/episode/81'
        with patch('builtins.print'), patch('comic_archive.cli.Store') as store, \
                patch('comic_archive.adapters.tapas_http.TapasHTTPAdapter') as http_type, \
                patch('comic_archive.adapters.tapas.TapasAdapter') as browser_type:
            adapter = http_type.return_value.__enter__.return_value
            code = run(parser().parse_args(['--request-interval', '0.5', 'archive-episode', url, '--http-only', '--download-workers', '2']))
            self.assertEqual(code, 0)
            http_type.assert_called_once_with(Path('.comic-session'), interval=0.5)
            browser_type.assert_not_called()
            adapter.capture.assert_called_once_with(url, None, None, None, None)
            adapter.capture_automatic.assert_not_called()
            store.return_value.archive.assert_called_once_with(adapter.capture.return_value, adapter, False, download_workers=2)

    def test_manual_episode_capture_passes_metadata_and_selector_and_store_closes_after_failure(self):
        url = 'https://tapas.io/episode/81'
        with patch('builtins.print'), patch('comic_archive.cli.Store') as store, \
                patch('comic_archive.adapters.tapas.TapasAdapter') as browser_type:
            adapter = browser_type.return_value.__enter__.return_value
            for extra in (['--headless'], ['--http-only']):
                argv = ['archive-episode', url, '--manual', '--series-id', '1', '--series-title', 'S', '--title', 'T', '--order', '3']
                if extra == ['--headless']:
                    argv = extra + argv
                else:
                    argv += extra
                with self.subTest(extra=extra), self.assertRaises(ArchiveError):
                    run(parser().parse_args(argv))
            browser_type.assert_not_called()
            args = parser().parse_args(['archive-episode', url, '--manual', '--series-id', '1', '--series-title', 'S',
                                        '--title', 'T', '--order', '3', '--page-selector', 'img.page'])
            store.return_value.archive.side_effect = ArchiveError('Page is too large.')
            with self.assertRaises(ArchiveError):
                run(args)
            adapter.capture.assert_called_once_with(url, '1', 'S', 'T', 3, 'img.page')
            adapter.capture_automatic.assert_not_called()
            store.return_value.close.assert_called_once()

    def test_local_manifest_import_is_verifiable_and_a_mismatched_series_id_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'p1.png').write_bytes(png(3))
            manifest = root / 'manifest.json'
            manifest.write_text(json.dumps({'series': {'id': '1', 'title': 'S'}, 'episodes': [
                {'id': '2', 'title': 'E', 'order': 1, 'pages': [{'order': 1, 'file': 'p1.png'}]}]}))
            wrong, first, second = root / 'wrong', root / 'first', root / 'second'
            with self.assertRaises(ArchiveError):
                invoke('archive-series', '999', '--manifest', str(manifest), '--output', str(wrong))
            self.assertFalse(wrong.exists())
            code, out, _ = invoke('import-local', str(manifest), '--output', str(first))
            self.assertEqual(code, 0)
            self.assertEqual(verify(first), [])
            code, out, _ = invoke('archive-series', '1', '--manifest', str(manifest), '--output', str(second))
            self.assertEqual(code, 0)
            self.assertEqual(verify(second), [])
            with self.assertRaises(ArchiveError):
                invoke('archive-library', '--output', str(root / 'third'))
            self.assertFalse((root / 'third').exists())

    def test_archive_series_without_a_manifest_reads_the_account_with_limit_and_refresh(self):
        with patch('comic_archive.importer.archive_series', return_value={'archived': 5}) as archive, \
                patch('comic_archive.adapters.tapas.TapasAdapter') as adapter_type:
            code, out, _ = invoke('archive-series', '42', '--limit', '5', '--refresh')
            self.assertEqual((code, json.loads(out)), (0, {'archived': 5}))
            archive.assert_called_once_with(adapter_type.return_value.__enter__.return_value, '42', Path('Library'), 5, True)

    def test_chosen_series_import_runs_with_its_small_default_budget(self):
        result = {'archived_episode_ids': ['9'], 'downloaded_pages': 3, 'downloaded_bytes': 1024 ** 2, 'status': 'complete'}
        url = 'https://tapas.io/series/my-comic'
        with tempfile.TemporaryDirectory() as tmp, patch('comic_archive.jobs.archive_series_url', return_value=result) as job, \
                patch('comic_archive.adapters.tapas.TapasAdapter') as adapter_type:
            code, out, _ = invoke('import-series', url, '--output', tmp)
            self.assertEqual(code, 0)
            job.assert_called_once_with(adapter_type.return_value.__enter__.return_value, url, Path(tmp), limit=1, max_pages=1000,
                                        max_bytes=256 * 1024 ** 2, max_seconds=900, free_only=False, download_workers=2)
            self.assertIn('Archived 1 episodes; downloaded 3 pages', out)

    def test_worker_queued_sync_refreshes_account_lists_unless_watched_only(self):
        with patch('comic_archive.queue_client.submit_job', return_value={'task_id': TASK}) as submit:
            invoke('sync', '--via-worker', '--category', 'recent', '--include-history', '--history-pages', '9')
            options = submit.call_args.args[2]
            self.assertEqual((submit.call_args.args[1], options['categories'], options['include_history'], options['history_pages']),
                             ('sync', ['recent'], True, 9))
            self.assertNotIn('refresh_lists', options)
            self.assertNotIn('scan_only', options)


if __name__ == "__main__":
    unittest.main()
