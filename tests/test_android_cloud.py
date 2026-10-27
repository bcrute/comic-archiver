"""Native bridge to shared cloud policy and durable queue, using private fixtures only."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from comic_archive import android_session, cloud_runtime, network_android
from comic_archive.models import ArchiveError
from test_app_bridge import app_bridge
from test_b2_native import B2Wire


class AndroidCloudTests(unittest.TestCase):
    def setUp(self):
        self.previous = Path.cwd()
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.addCleanup(self.clean)
        app_bridge._workspace.update(root=None, base=None, site_key='tapas')
        app_bridge._signin_attempts.clear()
        app_bridge._catalog_cache.update(root=None, registry=None)
        app_bridge._cloud_cache.update(root=None, controller=None)
        self.assertTrue(self.call('prepare_workspace')['ok'])
        self.wire = B2Wire()
        real = httpx.Client
        transport = httpx.MockTransport(self.wire.request)
        self.addCleanup(patch.stopall)
        patch('comic_archive.b2_auth.httpx.Client', side_effect=lambda **kw: real(transport=transport, **kw)).start()
        patch('comic_archive.network.probe', return_value={'connection': 'wifi', 'metered': False}).start()
        patch.object(cloud_runtime, '_android', True).start()

    def clean(self):
        controller = app_bridge._cloud_cache['controller']
        if controller:
            controller.registry.cloud_signin.close()
        app_bridge._cloud_cache.update(root=None, controller=None)
        app_bridge._catalog_cache.update(root=None, registry=None)
        network_android.configure(None)
        os.chdir(self.previous)
        self.temporary.cleanup()

    def call(self, operation, **arguments):
        return json.loads(app_bridge.dispatch(json.dumps(
            {'v': 1, 'op': operation, 'args': {'root': str(self.root), **arguments}})))

    def connected(self):
        answer = self.call('cloud_action', action='connect', credentials={
            'bucket': 'fixture-library', 'key_id': 'fixtureKey123', 'application_key': 'FIXTURE_KEY123'})
        self.assertTrue(answer['ok'], answer)
        controller = app_bridge._cloud_cache['controller']
        controller.registry.cloud_signin._thread.join(timeout=4)
        self.assertFalse(controller.registry.cloud_signin._thread.is_alive())
        self.assertEqual(controller.registry.cloud_signin.status()['status'], 'connected')
        return controller

    def test_empty_native_cloud_state_is_safe_offline_and_shares_existing_settings(self):
        result = self.call('cloud_status')
        self.assertTrue(result['ok'], result)
        value = result['result']
        self.assertEqual(value['destination'], 'device')
        self.assertTrue(value['b2']['shared'])
        self.assertFalse(value['b2']['test_enabled'])
        self.assertNotIn(str(self.root), json.dumps(value))
        self.assertEqual(self.wire.operations, [])

    def interrupted_worker(self, *, version=None):
        from comic_archive.inventory import Inventory
        from comic_archive.worker import private_json
        self.call('cloud_status')
        controller = app_bridge._cloud_cache['controller']
        folder = self.root / '.comic-session/worker'
        folder.mkdir(parents=True, exist_ok=True)
        private_json(folder / 'status.json', {'worker_id': 'c' * 32,
            'pid': 99999999, 'state': 'busy', 'task_id': 'job-' + 'd' * 24})
        if version is not None:
            # The current producer stamps its own version. This is deliberately
            # unsupported negative input, not a historical producer fixture.
            from comic_archive.archive import atomic_json
            status = json.loads((folder / 'status.json').read_text())
            atomic_json(folder / 'status.json', status | {'worker_version': version})
            (folder / 'status.json').chmod(0o600)
        with Inventory(self.root / 'Library') as inventory:
            run = inventory.start_run()
        progress = self.root / 'Library/.archive-state/progress.json'
        private_json(progress, {'run_id': run, 'status': 'running', 'phase': 'archive',
            'downloaded_pages': 3})
        return controller, folder, progress

    def test_stopped_native_runtime_does_not_inherit_historical_busy_cloud_admission(self):
        from comic_archive import worker_host
        from comic_archive.local_library import LOCAL_OWNER
        controller, folder, progress = self.interrupted_worker()
        before = {path: path.read_bytes() for path in (folder / 'status.json', progress)}
        raw = controller.registry.status(LOCAL_OWNER, controller.registry.workspace_id)
        self.assertEqual(raw['worker']['reported_state'], 'busy')
        self.assertEqual(raw['progress']['status'], 'running')
        self.assertIs(worker_host.status(controller.workspace.session)['running'], False)
        with patch('comic_archive.android_download.start') as go:
            view = self.call('cloud_status')['result']
        self.assertTrue(view['b2']['connect_enabled'])
        self.assertFalse(view['task_running'] or view['b2']['test_enabled'])
        self.assertIsNone(controller._read().status['worker'])
        self.assertIsNone(controller._read().status['progress'])
        self.assertEqual(before, {path: path.read_bytes() for path in before})
        self.assertEqual(list((folder / 'requests').glob('*.json')), [])
        self.assertEqual(self.wire.operations, [])
        go.assert_not_called()

    def test_actual_ready_embedded_worker_does_not_inherit_an_old_run_progress_fence(self):
        from comic_archive import worker_host
        controller = self.connected()
        android_session.mark_signed_in(self.root / '.comic-session', compatibility_state=True)
        controller, _, progress = self.interrupted_worker()
        before = progress.read_bytes()
        operations = list(self.wire.operations)
        (self.root / 'net.json').write_text(json.dumps({'connection': 'wifi', 'metered': False}))
        try:
            with patch('comic_archive.android_download.start') as go:
                live = worker_host.start_in_process(controller.workspace.session,
                    controller.workspace.library, lambda url: '', None, self.root / 'net.json')
                self.assertTrue(live['running'], live)
                self.assertEqual(live['state'], 'ready')
                value = self.call('cloud_status')['result']
                self.assertTrue(value['b2']['connect_enabled'])
                self.assertTrue(value['b2']['test_enabled'])
                self.assertFalse(value['task_running'])
                self.assertEqual(progress.read_bytes(), before)
                self.assertEqual(self.wire.operations, operations)
                go.assert_not_called()
        finally:
            retired = worker_host.request_stop(controller.workspace.session, wait_seconds=15)
            self.assertIs(retired['running'], False)

    def test_a_starting_worker_is_not_reported_busy_from_the_interrupted_runs_status(self):
        import time
        from comic_archive import worker, worker_host
        self.connected()
        android_session.mark_signed_in(self.root / '.comic-session', compatibility_state=True)
        controller, _, _ = self.interrupted_worker()
        (self.root / 'net.json').write_text(json.dumps({'connection': 'wifi', 'metered': False}))
        original = worker._run_worker

        def slow_first_status(*args):
            time.sleep(0.6)  # The runtime lock is held; status.json is still the old run's "busy".
            return original(*args)
        try:
            with patch.object(worker, '_run_worker', slow_first_status), \
                    patch('comic_archive.android_download.start'):
                live = worker_host.start_in_process(controller.workspace.session,
                    controller.workspace.library, lambda url: '', None, self.root / 'net.json')
            self.assertTrue(live['started'], live)
            self.assertEqual(live['state'], 'ready')
        finally:
            retired = worker_host.request_stop(controller.workspace.session, wait_seconds=15)
            self.assertIs(retired['running'], False)

    def test_held_runtime_lock_still_refuses_cloud_connection_before_http(self):
        from comic_archive import worker_host
        from comic_archive.locking import LibraryLock
        controller, folder, _ = self.interrupted_worker()
        before = (folder / 'status.json').read_bytes()
        with LibraryLock(folder / '.runtime'):
            self.assertIs(worker_host.status(controller.workspace.session)['running'], True)
            self.assertFalse(self.call('cloud_status')['result']['b2']['connect_enabled'])
            with self.assertRaises(ArchiveError):
                controller.act('connect', credentials={'bucket': 'fixture-library',
                    'key_id': 'fixtureKey123', 'application_key': 'FIXTURE_KEY123'})
        self.assertEqual((folder / 'status.json').read_bytes(), before)
        self.assertEqual(self.wire.operations, [])

    def test_unknown_legacy_worker_liveness_is_not_idle_cloud_authority(self):
        from comic_archive import worker_host
        controller, folder, _ = self.interrupted_worker(version=34)
        before = (folder / 'status.json').read_bytes()
        self.assertIsNone(worker_host.status(controller.workspace.session)['running'])
        self.assertFalse(self.call('cloud_status')['result']['b2']['connect_enabled'])
        with self.assertRaises(ArchiveError):
            controller.act('connect', credentials={'bucket': 'fixture-library',
                'key_id': 'fixtureKey123', 'application_key': 'FIXTURE_KEY123'})
        controller._task = 'job-' + 'e' * 24
        with patch('comic_archive.worker_host.start_in_process') as start:
            with self.assertRaises(ArchiveError):
                controller.act('resume', start_worker=start)
            start.assert_not_called()
        self.assertEqual((folder / 'status.json').read_bytes(), before)
        self.assertEqual(self.wire.operations, [])

    def test_real_native_key_setup_uses_core_private_config_without_enabling_or_echoing_it(self):
        controller = self.connected()
        value = self.call('cloud_status')['result']
        self.assertEqual(value['auth_status'], 'connected')
        self.assertEqual(value['destination'], 'device')
        self.assertFalse(value['b2']['test_enabled'])  # No site session.
        self.assertFalse(value['b2']['retry_enabled'])
        for private in ('fixtureKey123', 'FIXTURE_KEY123', 'authorizationToken', str(self.root)):
            self.assertNotIn(private, json.dumps(value))
        target = controller.registry.cloud_status('trusted-local-device', controller.registry.workspace_id)['targets'][0]
        self.assertFalse(target['tested'] or target['enabled'])

    def test_dashboard_summary_matches_the_real_shared_catalog_without_starting_work(self):
        from zipfile import ZipFile
        from comic_archive import storage_model
        from comic_archive.local_library import LOCAL_OWNER
        folder = self.root / 'Library' / 'Fixture Series'
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'series.json').write_text(json.dumps({'schema_version': 2,
            'export_id': '10000000-0000-4000-8000-000000000001', 'title': 'Fixture Series'}))
        (folder / 'chapter.json').write_text(json.dumps({'schema_version': 2,
            'export_id': '10000000-0000-4000-8000-000000000002', 'title': 'Chapter one',
            'format': 'cbz', 'content_type': 'comic', 'page_count': 1}))
        with ZipFile(folder / 'chapter.cbz', 'w') as archive:
            archive.writestr('001.jpg', b'synthetic catalog fixture')
        before = {path.name: path.read_bytes() for path in folder.iterdir()}
        with patch('comic_archive.android_download.start') as go:
            answer = self.call('cloud_status')['result']['overview']
            controller = app_bridge._cloud_cache['controller']
            summary = controller.registry.saved_files(LOCAL_OWNER, controller.registry.workspace_id, summary=True)
            desktop = storage_model.library_overview(summary, True)
            self.assertEqual(answer, json.loads(json.dumps(desktop | {'available': True})))
            self.assertEqual(summary['episode_count'], 1)
            metrics = {label: count for count, label in answer['metrics']}
            self.assertEqual(metrics['Saved episodes'], '1')
            catalog = self.call('file_catalog', options={'kind': 'all'})['result']
            self.assertEqual(metrics['Saved files'], str(catalog['total']))
            self.assertIn(answer['locations'][0]['id'], {place['id'] for place in catalog['locations']})
            go.assert_not_called()
        self.assertEqual(before, {path.name: path.read_bytes() for path in folder.iterdir()})
        self.assertEqual(self.wire.operations, [])

    def test_dashboard_summary_failure_is_unavailable_instead_of_zero_or_private_text(self):
        self.call('cloud_status')
        controller = app_bridge._cloud_cache['controller']
        with patch.object(controller.registry, 'saved_files', side_effect=ArchiveError('PRIVATE catalog path')):
            value = self.call('cloud_status')['result']
        self.assertFalse(value['overview']['available'])
        self.assertIn('unavailable', value['overview']['summary'])
        self.assertTrue(all(count == '—' for count, _ in value['overview']['metrics']))
        self.assertEqual(value['overview']['locations'], [])
        self.assertNotIn('PRIVATE', json.dumps(value))
        self.assertEqual(self.wire.operations, [])

    def test_native_estimates_reuse_shared_numbers_and_are_hidden_for_finished_plans(self):
        from comic_archive import dashboard_model
        self.call('cloud_status')
        controller = app_bridge._cloud_cache['controller']
        original = controller.registry.status
        estimate = {'waiting_episodes': 3, 'unchecked_series': 2, 'seconds': 7200,
                    'saved_bytes': 1_000_000_000, 'left_bytes': 2_000_000_000, 'free_bytes': 1_000_000_000}
        def status(*args):
            return original(*args) | {'estimate': estimate}
        with patch.object(controller.registry, 'status', side_effect=status), \
                patch.object(controller.registry, 'account_status', return_value={'configured': True, 'status': 'stopped'}):
            value = self.call('cloud_status')['result']
        self.assertEqual(value['time_estimate'], dashboard_model.time_left(estimate, background=True))
        self.assertEqual(value['space_estimate'], dashboard_model.space_needed(estimate))
        self.assertIn('not enough', value['space_estimate'])
        with patch.object(controller.registry, 'status', side_effect=status), \
                patch.object(controller.registry, 'account_status', return_value={'configured': True, 'status': 'finished'}):
            finished = self.call('cloud_status')['result']
        self.assertEqual(finished['time_estimate'], '')
        self.assertEqual(finished['space_estimate'], '')
        self.assertEqual(self.wire.operations, [])

    def test_native_bridge_rejects_unknown_actions_and_credential_shapes_before_http(self):
        for arguments in ({'action': 'purge'}, {'action': 'test', 'url': 'https://unrelated.invalid'},
                          {'action': 'connect', 'credentials': {'key_id': 'SECRET_FIXTURE'}},
                          {'action': 'destination', 'value': 7}):
            answer = self.call('cloud_action', **arguments)
            self.assertFalse(answer['ok'])
            self.assertEqual(answer['code'], 'bad_request')
            self.assertNotIn('SECRET_FIXTURE', answer['message'])
        self.assertEqual(self.wire.operations, [])

    def test_connection_must_pass_core_test_before_destination_can_be_enabled(self):
        self.connected()
        refused = self.call('cloud_action', action='destination', value='b2')
        self.assertFalse(refused['ok'])
        self.assertEqual(self.call('cloud_status')['result']['destination'], 'device')

    def test_native_cloud_task_requires_worker_readiness_before_queueing_and_never_calls_go(self):
        controller = self.connected()
        android_session.mark_signed_in(self.root / '.comic-session', compatibility_state=True)
        session = self.root / '.comic-session'
        for folder in ('requests', 'responses'):
            (session / 'worker' / folder).mkdir(parents=True, exist_ok=True)
        old_status = controller.registry.status
        ready = False

        def status(*arguments):
            answer = old_status(*arguments)
            answer['worker'] = {'reported_state': 'ready' if ready else 'offline'}
            return answer

        def start():
            nonlocal ready
            ready = True

        with patch.object(controller.registry, 'status', side_effect=status), \
                patch('comic_archive.worker_host.status', side_effect=lambda session: {
                    'running': ready, 'state': 'ready' if ready else 'stopped'}), \
                patch('comic_archive.android_download.start') as go:
            result = controller.act('test', start_worker=start)
            self.assertTrue(result['task_running'])
            request = json.loads(next((session / 'worker/requests').glob('*.json')).read_text())
            self.assertEqual(request['operation'], 'cloud_test')
            self.assertEqual(request['args'], {'provider': 'b2'})
            refused = self.call('go')
            self.assertFalse(refused['ok'])
            go.assert_not_called()
            # A new process/controller recovers this exact queue identity while held.
            from comic_archive.android_cloud import AndroidCloud
            ready = False
            restored = AndroidCloud(controller.registry)
            self.assertEqual(restored._task, controller._task)
            self.assertTrue(restored.snapshot()['task_held'])
            calls = []
            resumed = restored.act('resume', start_worker=lambda: calls.append('start'))
            self.assertEqual(calls, ['start'])
            self.assertTrue(resumed['task_running'])
            self.assertEqual(len(list((session / 'worker/requests').glob('*.json'))), 1)

    def test_failed_worker_start_never_creates_a_cloud_queue_request(self):
        controller = self.connected()
        android_session.mark_signed_in(self.root / '.comic-session', compatibility_state=True)
        with self.assertRaises(ArchiveError):
            controller.act('test', start_worker=lambda: None)
        self.assertFalse(list((self.root / '.comic-session/worker/requests').glob('*.json')))

    def test_shared_and_separate_connections_switch_without_erasing_prior_setup(self):
        self.connected()
        result = self.call('cloud_action', action='sharing', value='separate')
        self.assertTrue(result['ok'], result)
        self.assertFalse(result['result']['b2']['shared'])
        restored = self.call('cloud_action', action='sharing', value='shared')
        self.assertTrue(restored['ok'], restored)
        self.assertEqual(restored['result']['auth_status'], 'connected')
        self.assertEqual(restored['result']['b2']['bucket'], 'fixture-library')
