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
