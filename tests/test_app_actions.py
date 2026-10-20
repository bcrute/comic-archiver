import dataclasses
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch

from comic_archive import app_actions, sites
from comic_archive.app_actions import Actions, Refused, due, received
from comic_archive.dashboard_model import LIBRARY_SECONDS, NETWORK_SECONDS, Message, State, error_text
from comic_archive.errors import DeviceFileOpenUnavailable, LibraryBusy
from comic_archive.library_model import NO_OPENER, OPEN_FAILED, UNAVAILABLE
from comic_archive.local_library import LOCAL_OWNER
from comic_archive.models import ArchiveError

LIBRARY = 'a' * 32
TASK = 'job-' + '0' * 24
LATER = 'job-' + '1' * 24


class ActionsTests(unittest.TestCase):

    def setUp(self):
        self.registry = Mock(workspace_id=LIBRARY)
        self.settings = Mock()
        self.settings.list.return_value = [{'key': 'tapas', 'name': 'Tapas', 'current': True, 'speed': 'fast'}]
        self.runtime = SimpleNamespace(registry=self.registry, site=sites.DEFAULT, folder='/library folder',
                                       site_settings=self.settings)
        self.actions = Actions(lambda: self.runtime)

    def refused(self, work, *args, **options):
        with self.assertRaises(Refused) as refused:
            work(*args, **options)
        return refused.exception.code, str(refused.exception)

    def test_each_call_goes_to_the_open_library_as_its_local_owner(self):
        for name, method, args in (('go', 'start_account', ()), ('pause', 'pause_account', ()), ('signin', 'start_site_signin', ()),
                                   ('sign_out', 'sign_out_site', ()), ('speed', 'choose_speed', ('slow',)),
                                   ('cloud_enable', 'configure_cloud', ('b2', True)),
                                   ('cloud_cancel', 'cancel_cloud_signin', ()), ('cloud_upload', 'upload_cloud', ()),
                                   ('wifi_only', 'configure_network', (False,)), ('cloud_storage', 'choose_cloud_storage', ('separate',)),
                                   ('cloud_test', 'test_cloud', ('b2',)), ('task', 'task', (TASK,))):
            answer = getattr(self.registry, method).return_value = {'answer': name}
            self.assertEqual(getattr(self.actions, name)(*args), answer, name)
            getattr(self.registry, method).assert_called_once_with(LOCAL_OWNER, LIBRARY, *args)

    def test_a_site_or_folder_change_is_followed_and_no_library_is_said_plainly(self):
        self.actions.go()
        self.runtime = SimpleNamespace(registry=Mock(workspace_id='b' * 32), site=sites.DEFAULT)
        self.actions.go()
        self.runtime.registry.start_account.assert_called_once_with(LOCAL_OWNER, 'b' * 32)
        self.registry.start_account.assert_called_once_with(LOCAL_OWNER, LIBRARY)
        self.runtime.registry = None
        self.assertEqual(self.refused(self.actions.go), ('service_unavailable', error_text('service_unavailable')))
        self.runtime = None
        self.assertEqual(self.refused(self.actions.poll), ('service_unavailable', error_text('service_unavailable')))

    def test_a_refusal_is_told_in_fixed_words_and_an_unexpected_failure_is_logged_by_kind_only(self):
        tappytoon = sites.find('tappytoon')
        self.runtime.site = tappytoon
        cases = ((LibraryBusy('PRIVATE busy'), ('library_busy', error_text('library_busy', 'Tappytoon'))),
                 (DeviceFileOpenUnavailable('PRIVATE opener'), ('device_file_open_unavailable', NO_OPENER)),
                 (ArchiveError('PRIVATE refused'), ('request_unavailable', error_text('request_unavailable', 'Tappytoon'))))
        for error, expected in cases:
            self.registry.start_account.side_effect = error
            self.assertEqual(self.refused(self.actions.go), expected)
        self.registry.start_account.side_effect = RuntimeError('PRIVATE /home/someone/cookies')
        with self.assertLogs('comic_archive.desktop', 'ERROR') as logged:
            self.assertEqual(self.refused(self.actions.go), ('service_unavailable', error_text('service_unavailable', 'Tappytoon')))
        self.assertIn('start_account: RuntimeError', logged.output[0])
        self.assertNotIn('PRIVATE', '\n'.join(logged.output))
        # A call with its own words for a failure says those instead.
        self.registry.open_file_on_device.side_effect = ArchiveError('PRIVATE')
        self.assertEqual(self.refused(self.actions.open_file, 'f' * 64), ('request_unavailable', OPEN_FAILED))
        self.registry.saved_files.side_effect = OSError('PRIVATE')
        with self.assertLogs('comic_archive.desktop', 'ERROR'):
            self.assertEqual(self.refused(self.actions.saved_files), ('service_unavailable', UNAVAILABLE))

    def test_the_context_names_the_sites_their_settings_and_the_folder(self):
        with patch('comic_archive.cloud_tools.offered', return_value=False):
            context = self.actions.context()
        self.assertEqual(context, {'sites': sites.choices(sites.DEFAULT), 'site_settings': self.settings.list.return_value,
                                   'library_folder': '/library folder', 'cloud_offered': False, 'workspace_id': LIBRARY})
        self.assertEqual([(site['key'], site['available'], site['current']) for site in context['sites']],
                         [('tapas', True, True), ('webtoon', True, False), ('tappytoon', True, False)])
        # Only a site that works can be chosen.
        later = dataclasses.replace(sites.named('webtoon'), key='later', available=False)
        with patch.object(sites, 'SITES', sites.SITES + (later,)):
            self.assertIsNone(sites.find('later')); self.assertIsNone(sites.find('nowhere'))
            self.assertIs(sites.find('tapas'), sites.DEFAULT); self.assertIs(sites.find('webtoon'), sites.named('webtoon'))
        self.settings.list.side_effect = OSError('PRIVATE settings')
        with self.assertLogs('comic_archive.desktop', 'WARNING') as logged:
            self.assertEqual(self.actions.context()['site_settings'], [])
        self.assertNotIn('PRIVATE', '\n'.join(logged.output))

    def test_a_refresh_reads_what_it_is_asked_for_and_a_failed_count_leaves_the_rest_standing(self):
        reading = self.actions.poll(network=False, cloud=False)
        self.assertEqual(set(reading), {'status', 'account', 'site_auth'})
        self.registry.cloud_status.assert_not_called(); self.registry.network_status.assert_not_called()
        self.registry.saved_files.return_value = {'file_count': 3}
        reading = self.actions.poll(task=TASK, library=True, refresh=True)
        self.assertEqual(set(reading), {'status', 'account', 'site_auth', 'cloud', 'cloud_auth', 'task', 'network', 'summary'})
        self.assertEqual(reading['summary'], {'file_count': 3})
        self.registry.task.assert_called_once_with(LOCAL_OWNER, LIBRARY, TASK)
        self.registry.saved_files.assert_called_once_with(LOCAL_OWNER, LIBRARY, location='all', query='', kind='all',
                                                          sort='saved_desc', offset=0, limit=50, summary=True, refresh=True)
        self.registry.saved_files.side_effect = ArchiveError('PRIVATE')
        self.assertIs(self.actions.poll(library=True)['summary'], False)
        # Any other read failing fails the whole refresh.
        self.registry.status.side_effect = ArchiveError('PRIVATE')
        self.assertEqual(self.refused(self.actions.poll)[0], 'request_unavailable')

    def test_storage_is_one_place_at_a_time_and_the_old_one_is_turned_off_first(self):
        self.registry.cloud_status.return_value = {'targets': []}
        targets = [{'provider': 'other', 'enabled': True}, {'provider': 'b2', 'enabled': False}]
        self.assertEqual(self.actions.choose_storage('b2', targets), {'targets': []})
        self.assertEqual([c for c in self.registry.mock_calls if c[0] == 'configure_cloud'],
                         [call.configure_cloud(LOCAL_OWNER, LIBRARY, 'other', False), call.configure_cloud(LOCAL_OWNER, LIBRARY, 'b2', True)])
        self.registry.reset_mock()
        self.actions.choose_storage('device', [{'provider': 'b2', 'enabled': True}])
        self.registry.configure_cloud.assert_called_once_with(LOCAL_OWNER, LIBRARY, 'b2', False)
        self.registry.reset_mock()
        # Choosing the place already chosen turns nothing off.
        self.actions.choose_storage('b2', [{'provider': 'b2', 'enabled': True}])
        self.registry.configure_cloud.assert_called_once_with(LOCAL_OWNER, LIBRARY, 'b2', True)

    def test_a_value_the_window_could_not_have_offered_is_refused_before_the_library_is_asked(self):
        refusals = ((self.actions.cloud_storage, 'both'), (self.actions.cloud_storage, None), (self.actions.cloud_test, 'google'),
                    (self.actions.cloud_cleanup, '../outside'), (self.actions.task, 'job-xyz'), (self.actions.task, None),
                    (self.actions.task, TASK + '0'), (self.actions.wifi_only, 'false'), (self.actions.wifi_only, 0),
                    (self.actions.cloud_signin, 'b2', {'password': 'PRIVATE'}), (self.actions.cloud_signin, 'google', {}))
        for work, *args in refusals:
            self.assertEqual(self.refused(work, *args), ('request_unavailable', error_text('request_unavailable')), args)
        for export in ('F' * 64, 'f' * 63, None, '../' + 'f' * 61):
            self.assertEqual(self.refused(self.actions.open_file, export), ('request_unavailable', OPEN_FAILED))
        for options in ({'kind': 'everything'}, {'limit': 0}, {'path': '/PRIVATE'}, {'query': 'x' * 201}):
            self.assertEqual(self.refused(self.actions.saved_files, **options)[0], 'request_unavailable', options)
        self.assertEqual(self.registry.mock_calls, [])
        test_id = 'f257c685-2a39-4e16-8dc0-353338e2f7d7'
        self.actions.cloud_cleanup(test_id)
        self.registry.cleanup_cloud_test.assert_called_once_with(LOCAL_OWNER, LIBRARY, test_id)
        credentials = {'key_id': 'K' * 12, 'application_key': 'A' * 31}
        self.actions.cloud_signin('b2', credentials)
        self.registry.start_cloud_signin.assert_called_once_with(LOCAL_OWNER, LIBRARY, 'b2', credentials)
        self.actions.open_file('f' * 64)
        self.registry.open_file_on_device.assert_called_once_with(LOCAL_OWNER, LIBRARY, 'f' * 64)

    def test_saved_files_options_are_checked_as_the_catalog_takes_them(self):
        self.actions.saved_files(summary=True, refresh=False, query='long series', kind='episodes', sort='series',
                                 offset=50, limit=25, location='device')
        self.registry.saved_files.assert_called_once_with(LOCAL_OWNER, LIBRARY, location='device', query='long series',
                                                          kind='episodes', sort='series', offset=50, limit=25,
                                                          summary=True, refresh=False)

    def test_calls_that_change_the_library_take_turns_and_reading_saved_files_does_not_wait_for_them(self):
        held, release = threading.Event(), threading.Event()

        def hold():
            with self.actions._turn:
                held.set(); release.wait(10)
        holder = threading.Thread(target=hold); holder.start()
        self.assertTrue(held.wait(5))
        try:
            done = threading.Event()
            waiting = threading.Thread(target=lambda: (self.actions.pause(), done.set())); waiting.start()
            self.registry.saved_files.return_value = {'files': []}
            self.assertEqual(self.actions.saved_files(), {'files': []})
            self.assertFalse(done.wait(.3))
            self.registry.pause_account.assert_not_called()
        finally:
            release.set()
        holder.join(5); waiting.join(5)
        self.assertTrue(done.is_set())
        self.registry.pause_account.assert_called_once_with(LOCAL_OWNER, LIBRARY)

    def test_another_site_s_speed_goes_through_the_sites_settings(self):
        self.settings.choose_speed.return_value = ['chosen']
        self.assertEqual(self.actions.site_speed('tappytoon', 'slow'), ['chosen'])
        self.settings.choose_speed.assert_called_once_with('tappytoon', 'slow')
        self.settings.choose_speed.side_effect = ArchiveError('PRIVATE the open site')
        self.assertEqual(self.refused(self.actions.site_speed, 'tapas', 'slow'),
                         ('request_unavailable', error_text('request_unavailable')))
        self.assertEqual(self.registry.mock_calls, [])


class ReceivedTests(unittest.TestCase):

    def reading(self, **values):
        return {'status': {'workspace_id': LIBRARY}, 'account': {'status': 'stopped'}, 'site_auth': {'status': 'idle'}} | values

    def test_what_a_refresh_reads_besides_the_dashboard_is_due_by_its_own_clock(self):
        state = State(network_at=1000, library_at=1000)
        self.assertEqual(due(state, 1000 + NETWORK_SECONDS), (False, False, False))
        self.assertEqual(due(state, 1001 + NETWORK_SECONDS), (True, False, False))
        self.assertEqual(due(state, 1001 + LIBRARY_SECONDS), (True, True, False))
        state.library_refresh = True
        self.assertEqual(due(state, 1000), (False, True, True))

    def test_a_refresh_is_put_on_the_state_and_what_it_did_not_read_is_kept(self):
        state = State(cloud={'targets': ['kept']}, message=Message('Could not refresh', error=True, polling=True), offline=True)
        self.assertFalse(received(state, self.reading(network={'allowed': True}), 500))
        self.assertEqual((state.status, state.account, state.site_auth, state.network), ({'workspace_id': LIBRARY},
                         {'status': 'stopped'}, {'status': 'idle'}, {'allowed': True}))
        self.assertEqual((state.cloud, state.network_at, state.account_at, state.refreshed_at), ({'targets': ['kept']}, 500, 500, 500))
        self.assertTrue(state.loaded); self.assertFalse(state.offline); self.assertIsNone(state.message)
        received(state, self.reading(), 600)
        self.assertEqual(state.network_at, 500)

    def test_the_saved_files_counts_come_with_their_places_and_a_failed_count_keeps_the_places(self):
        state = State(library_refresh=True)
        received(state, self.reading(summary={'file_count': 2, 'locations': [{'id': 'device'}]}), 700)
        self.assertEqual((state.summary, state.locations, state.library_at, state.library_refresh),
                         ({'file_count': 2, 'locations': [{'id': 'device'}]}, [{'id': 'device'}], 700, False))
        received(state, self.reading(summary=False), 800)
        self.assertEqual((state.summary, state.locations, state.library_at), (False, [{'id': 'device'}], 800))

    def test_a_finished_cloud_task_is_said_beside_the_cloud_buttons_and_the_files_are_counted_again(self):
        state = State(task=TASK, task_kind='cloud')
        received(state, self.reading(task={'status': 'running'}), 10, task=TASK)
        self.assertEqual((state.task, state.cloud_message, state.library_refresh), (TASK, None, False))
        received(state, self.reading(task={'status': 'ok', 'result': {'uploaded_episodes': 2, 'local_archives_deleted': 1}}), 20, task=TASK)
        self.assertEqual(state.cloud_message, Message('2 uploads verified; 1 local archives removed.', False))
        self.assertEqual((state.task, state.task_kind, state.library_refresh, state.message), (None, None, True, None))
        state = State(task=TASK, task_kind='cloud-test')
        received(state, self.reading(task={'status': 'error', 'code': 'task_failed'}), 30, task=TASK)
        self.assertEqual(state.cloud_message, Message(error_text('task_failed'), True))
        self.assertEqual((state.message.text, state.message.error, state.task), (error_text('task_failed'), True, None))

    def test_a_task_started_since_the_refresh_was_asked_for_is_left_alone(self):
        state = State(task=LATER, task_kind='cloud')
        received(state, self.reading(task={'status': 'ok', 'result': {}}), 10, task=TASK)
        self.assertEqual((state.task, state.cloud_message, state.library_refresh), (LATER, None, False))

    def test_a_download_that_has_just_finished_has_its_files_counted_again(self):
        state = State(account={'status': 'running'})
        received(state, self.reading(account={'status': 'finished'}), 10)
        self.assertTrue(state.library_refresh)
        state.library_refresh = False
        received(state, self.reading(account={'status': 'finished'}), 20)
        self.assertFalse(state.library_refresh)

    def test_a_cloud_key_that_has_just_connected_empties_the_key_fields_once(self):
        state = State(cloud_auth={'status': 'running'}, bucket_draft='my-bucket')
        self.assertTrue(received(state, self.reading(cloud_auth={'status': 'connected'}), 10))
        self.assertEqual((state.bucket_draft, state.library_refresh, state.cloud_auth), (None, True, {'status': 'connected'}))
        state.bucket_draft = 'typed-again'
        self.assertFalse(received(state, self.reading(cloud_auth={'status': 'connected'}), 20))
        self.assertEqual(state.bucket_draft, 'typed-again')

    def test_the_log_names_no_path_or_text_from_an_exception(self):
        try:
            raise OSError('PRIVATE /home/someone')
        except OSError as error:
            text = app_actions.described(error)
        self.assertTrue(text.startswith('OSError at test_app_actions.py:'))
        self.assertNotIn('PRIVATE', text); self.assertNotIn('/home', text)


if __name__ == '__main__':
    unittest.main()
