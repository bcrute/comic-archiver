"""What the dashboard and the Downloads settings say and allow, decided without a window."""
import unittest

from comic_archive import dashboard_model as model
from comic_archive.dashboard_model import Message, State

NOW = 1_000_000.0
TAPAS = {'key': 'tapas', 'name': 'Tapas', 'available': True, 'current': True}
TAPPYTOON = {'key': 'tappytoon', 'name': 'Tappytoon', 'available': True, 'current': False}


def state(**changes):
    """A library read a moment ago: signed in, the downloader ready, the download paused."""
    values = dict(sites=[TAPAS], library_folder='/library', loaded=True, refreshed_at=NOW,
                  status={'session_configured': True, 'worker': {'reported_state': 'ready'}, 'episodes_by_status': {'archived': 3}},
                  account={'status': 'stopped', 'configured': True}, site_auth={'status': 'idle'},
                  network={'wifi_only': True, 'allowed': True})
    values.update(changes)
    return State(**values)


def account(**changes):
    return {'status': 'running', 'configured': True, 'phase': 'archive'} | changes


def view(**changes):
    return model.view(state(**changes), NOW)


class WordingTests(unittest.TestCase):
    def test_a_refusal_is_worded_for_its_code_and_names_the_open_site(self):
        self.assertEqual(model.error_text('login_required', 'Webtoon'), 'Your Webtoon session needs a fresh login.')
        self.assertIn('Tapas refused a request', model.error_text('source_refused'))
        self.assertEqual(model.error_text('library_busy'), 'An import is active. Try again after this batch finishes.')
        self.assertEqual(model.error_text('cloud_cap'), model.CLOUD_HINTS['cloud_cap'] + ' Local episodes are retained.')
        self.assertEqual(model.error_text('anything else'), 'The action could not be completed.')

    def test_counts_times_and_sizes_are_said_the_way_a_person_would(self):
        for value, shown in ((5, 5), (-1, 0), (True, 0), (2.0, 0), ('3', 0), (None, 0)):
            self.assertEqual(model.number(value), shown)
        self.assertEqual(model.display(1234567), '1,234,567')
        for seconds, said in ((59, '0m 59s'), (61, '1m 1s'), (3600, '1h 0m'), (7322.5, '2h 2m'),
                              (-1, None), (float('inf'), None), ('5', None)):
            self.assertEqual(model.duration(seconds), said)
        for seconds, said in ((60, '5 minutes'), (1500, '25 minutes'), (4000, 'an hour'), (7200, '2 hours'),
                              (3 * 86400, '3 days'), (21 * 86400, '3 weeks'), (90 * 86400, '3 months')):
            self.assertEqual(model.span(seconds), said)
        for size, said in ((2e12, '2.0 TB'), (25e9, '25 GB'), (2.5e9, '2.5 GB'), (300e6, '300 MB'), (1, '1 MB')):
            self.assertEqual(model.room(size), said)
        for seconds, said in ((10, 'a few seconds'), (60, 'about a minute'), (600, 'about 10 minutes')):
            self.assertEqual(model.wait_text(seconds), said)

    def test_time_left_waits_for_an_estimate_and_says_what_is_still_to_check(self):
        self.assertEqual(model.time_left(None), '')
        self.assertEqual(model.time_left({'waiting_episodes': 0, 'unchecked_series': 0, 'seconds': 60}), '')
        self.assertTrue(model.time_left({'waiting_episodes': 4}).startswith('Working out how long'))
        self.assertEqual(model.time_left({'waiting_episodes': 1, 'seconds': 7200}),
                         'About 2 hours of downloading left at the pace of the last five minutes. 1 episode is waiting. '
                         'It only continues while this app is open.')
        rough = model.time_left({'waiting_episodes': 2, 'unchecked_series': 1, 'seconds': 7200})
        self.assertIn('2 episodes are waiting and 1 series has not been checked yet, so this is a rough guess.', rough)
        self.assertIn('3 series have not', model.time_left({'unchecked_series': 3, 'seconds': 60}))

    def test_space_needed_says_the_whole_library_and_whether_the_disk_holds_it(self):
        self.assertEqual(model.space_needed(None), '')
        self.assertEqual(model.space_needed({'waiting_episodes': 0}), '')
        self.assertEqual(model.space_needed({'waiting_episodes': 1}), '')
        self.assertEqual(model.space_needed({'waiting_episodes': 1, 'left_bytes': 2_000_000_000}),
                         'Your whole library should take about 2.0 GB of space.')
        said = model.space_needed({'waiting_episodes': 1, 'unchecked_series': 2, 'saved_bytes': 1_000_000_000,
                                   'left_bytes': 2_000_000_000, 'free_bytes': 500_000_000})
        self.assertEqual(said, 'Your whole library should take about 3.0 GB of space: 1.0 GB is saved and about 2.0 GB is '
                               'still to come. This is a rough guess until every series has been checked. This disk has only '
                               '500 MB free, which is not enough for the rest.')
        self.assertTrue(model.space_needed({'waiting_episodes': 1, 'left_bytes': 10, 'free_bytes': 50_000_000_000})
                        .endswith('This disk has 50 GB free.'))

    def test_a_held_download_says_why_it_stopped_and_what_to_do(self):
        cases = {('archive_incomplete', 0, 1): 'Finished, but 1 episode could not be saved.',
                 ('archive_incomplete', 0, 3): 'Finished, but 3 episodes could not be saved.',
                 ('archive_incomplete', 0, 0): 'Finished, but some saved files need another check.',
                 ('source_refused', 120, 0): 'Go works again in about 2 minutes.',
                 ('source_recovery_limit', 0, 0): 'choose a slower speed in Settings.',
                 ('source_access_changed', 0, 0): 'Check that you can read it on Tapas',
                 ('network_policy', 0, 0): 'Paused while the connection was not Wi-Fi or wired.',
                 ('network_unavailable', 0, 0): 'Tapas could not be reached several times in a row.',
                 ('network_recovery_limit', 0, 0): 'Check your internet connection',
                 ('no_progress', 0, 0): 'the last few tries saved nothing new',
                 ('scan_no_progress', 0, 0): 'Tapas may be having trouble',
                 ('cancelled', 0, 0): 'Paused. Hit Go to continue from saved progress.',
                 ('cloud_space', 0, 0): 'an upload to cloud storage stopped. ' + model.CLOUD_HINTS['cloud_space'],
                 ('mystery', 0, 0): 'the download stopped unexpectedly'}
        for (code, wait, missing), part in cases.items():
            with self.subTest(code=code, missing=missing):
                self.assertIn(part, model.held_message(code, 'Tapas', wait, missing))

    def test_the_step_in_progress_is_counted_only_while_it_runs(self):
        self.assertEqual(model.step_progress(None), '')
        self.assertEqual(model.step_progress({'status': 'running', 'phase': 'archive', 'cancellation_requested': True}), '')
        self.assertEqual(model.step_progress({'status': 'done', 'phase': 'archive'}), '')
        self.assertEqual(model.step_progress({'status': 'running', 'phase': 'lists'}), '')
        self.assertEqual(model.step_progress({'status': 'running', 'phase': 'scan', 'series_checks_completed': 2,
                                              'series_check_target_count': 9}), 'Checked 2 of 9 series in this step.')
        self.assertEqual(model.step_progress({'status': 'running', 'phase': 'scan'}), '')
        self.assertEqual(model.step_progress({'status': 'running', 'phase': 'archive', 'completed': 4, 'downloaded_pages': 1}),
                         '4 saved in this step · 1 image.')
        self.assertEqual(model.step_progress({'status': 'running', 'phase': 'archive', 'completed': 1, 'downloaded_pages': 12,
                                              'downloaded_chapters': 1}), '1 saved in this step · 11 images · 1 novel chapter.')
        self.assertIn('2 novel chapters', model.step_progress({'status': 'running', 'phase': 'archive', 'downloaded_chapters': 2}))


class StateTests(unittest.TestCase):
    def test_the_open_site_is_named_and_tapas_until_the_sites_are_known(self):
        self.assertEqual(State().site(), 'Tapas')
        self.assertEqual(State(sites=[dict(TAPAS, current=False), dict(TAPPYTOON, current=True)]).site(), 'Tappytoon')

    def test_a_sign_in_is_timed_from_when_it_first_connected(self):
        s = State()
        s.received_site_auth({'status': 'connected'}, NOW)
        s.received_site_auth({'status': 'connected'}, NOW + 50)
        self.assertEqual(s.connected_at, NOW)
        s.received_site_auth(None, NOW + 60)
        self.assertIsNone(s.site_auth)
        self.assertEqual(s.since_account(NOW), 0)
        s.received_account({'status': 'running'}, NOW)
        self.assertEqual((s.since_account(NOW + 7.9), s.refreshed_at), (7, NOW))

    def test_a_good_refresh_clears_a_refresh_failure_and_an_old_failure_but_not_a_new_one(self):
        s = State()
        s.refresh_failed()
        self.assertTrue(s.offline and s.message.error and s.message.polling)
        s.refreshed(NOW)
        self.assertIsNone(s.message)
        self.assertFalse(s.offline)
        s.failed('Refused.', NOW)
        s.refreshed(NOW + model.MESSAGE_SECONDS)
        self.assertEqual(s.message, Message('Refused.', True, NOW + model.MESSAGE_SECONDS))
        s.refreshed(NOW + model.MESSAGE_SECONDS + 1)
        self.assertIsNone(s.message)
        s.message = Message('Doing something.')
        s.refreshed(NOW)
        self.assertIsNotNone(s.message)

    def test_cloud_targets_are_read_safely_before_the_cloud_is_known(self):
        self.assertEqual(State().cloud_targets(), [])
        self.assertIsNone(State().target())
        s = State(cloud={'targets': [{'provider': 'other'}, {'provider': 'b2', 'bucket': 'x'}]})
        self.assertEqual(s.target()['bucket'], 'x')

    def test_a_downloader_that_has_not_reported_is_starting_for_two_minutes_after_a_sign_in(self):
        s = state(status={'worker': {'reported_state': 'offline'}}, site_auth={'status': 'connected'}, connected_at=NOW)
        self.assertEqual(model.worker_state(s, NOW + model.STARTING_SECONDS - 1), 'starting')
        self.assertEqual(model.worker_state(s, NOW + model.STARTING_SECONDS), 'offline')
        s.site_auth = {'status': 'idle'}
        self.assertEqual(model.worker_state(s, NOW), 'offline')

    def test_new_episodes_are_said_to_go_to_the_chosen_cloud_or_the_folder(self):
        b2 = {'targets': [{'provider': 'b2', 'connected': True, 'enabled': True}]}
        other = {'targets': [{'provider': 'other', 'connected': True, 'enabled': True}]}
        self.assertEqual(model.save_place(state(cloud=b2)), 'Backblaze B2')
        self.assertEqual(model.save_place(state(cloud=other)), 'Cloud storage')
        self.assertEqual(model.save_place(state(cloud=b2, cloud_offered=False)), '/library')
        self.assertEqual(model.save_place(state(cloud={'targets': [{'provider': 'b2', 'connected': True}]})), '/library')
        self.assertEqual(model.save_place(State()), '')


class ButtonTests(unittest.TestCase):
    def test_go_is_offered_only_when_the_downloader_is_ready_and_nothing_else_runs(self):
        self.assertTrue(view()['go_enabled'])
        self.assertEqual(view()['go_text'], 'Go — continue')
        self.assertEqual(view(account={'status': 'finished', 'configured': True})['go_text'], 'Go')
        for name, changes in {'busy': {'busy': True}, 'task': {'task': 'job-1'}, 'cloud sign-in': {'cloud_auth': {'status': 'running'}},
                              'no account': {'account': None}, 'signed out': {'status': {'worker': {'reported_state': 'ready'}}},
                              'signing in': {'site_auth': {'status': 'running'}}, 'running': {'account': account()},
                              'worker busy': {'status': {'session_configured': True, 'worker': {'reported_state': 'busy'}}},
                              'wait': {'account': {'status': 'stopped', 'go_after_seconds': 30}, 'account_at': NOW}}.items():
            with self.subTest(name):
                self.assertFalse(view(**changes)['go_enabled'])
        self.assertTrue(view(account={'status': 'stopped', 'go_after_seconds': 30}, account_at=NOW - 31)['go_enabled'])
        self.assertEqual(view(account=account())['go_text'], 'Working…')

    def test_sign_in_shows_when_it_would_help_and_waits_while_saving(self):
        self.assertFalse(view()['signin_visible'])
        self.assertTrue(view(status={'worker': {'reported_state': 'ready'}})['signin_visible'])
        self.assertTrue(view(status={'session_configured': True, 'worker': {'reported_state': 'offline'}})['signin_visible'])
        self.assertTrue(view(message=Message('Refused.', True))['signin_visible'])
        signing = view(site_auth={'status': 'running'})
        self.assertEqual((signing['signin_text'], signing['signin_enabled']), ('Signing in…', False))
        self.assertEqual(view()['signin_text'], 'Sign in to Tapas')
        self.assertFalse(view(account=account())['signin_enabled'])
        self.assertFalse(view(busy=True)['signin_enabled'])
        relogin = view(account={'status': 'held', 'code': 'login_required'})
        self.assertTrue(relogin['signin_visible'])
        self.assertTrue(relogin['status'].startswith('Paused: Tapas signed you out.'))

    def test_pause_allow_site_and_folder_follow_a_running_download(self):
        running = view(account=account())
        self.assertTrue(running['pause_visible'] and running['pause_enabled'])
        self.assertFalse(view(account=account(), busy=True)['pause_enabled'])
        self.assertFalse(running['site_enabled'] or running['folder_enabled'])
        self.assertEqual(running['site_tooltip'], 'Pause saving to change the site.')
        self.assertIn('Press Pause on the dashboard first.', running['folder_lock'])
        idle = view()
        self.assertTrue(idle['site_enabled'] and idle['folder_enabled'] and not idle['pause_visible'])
        self.assertEqual((idle['site_tooltip'], idle['folder_tooltip'], idle['folder_lock']), ('', '', ''))
        held = view(network={'wifi_only': True, 'allowed': False})
        self.assertTrue(held['allow_visible'] and held['allow_enabled'])
        self.assertFalse(view(network={'wifi_only': False, 'allowed': False})['allow_visible'])

    def test_speeds_are_offered_for_the_open_site_and_each_other_one(self):
        alone = view()
        self.assertEqual((alone['speed_label'], alone['speed'], alone['other_speeds']), ('Speed', 'fast', []))
        self.assertTrue(alone['speed_visible'] and alone['speed_enabled'] and not alone['signin_note_visible'])
        self.assertIn('If Tapas keeps pausing', alone['speed_help'])
        both = view(sites=[TAPAS, TAPPYTOON], site_settings=[dict(TAPAS, speed='fast'), dict(TAPPYTOON, speed='slow')],
                    account={'status': 'stopped', 'speed': 'medium'})
        self.assertEqual((both['speed_label'], both['speed'], both['speed_help']), ('Speed on Tapas', 'medium', 'About four images a second.'))
        self.assertEqual(both['other_speeds'], [{'key': 'tappytoon', 'label': 'Speed on Tappytoon', 'speed': 'slow',
                                                 'help': 'Used the next time you hit Go on Tappytoon.', 'enabled': True}])
        self.assertIsNone(view(site_settings=[dict(TAPPYTOON, speed='warp')])['other_speeds'][0]['speed'])
        self.assertEqual(view(account=account(speed='bogus'))['speed_help'], 'Pause to change the speed.')
        self.assertFalse(view(account=account())['speed_enabled'])
        signed_out = view(status={'worker': {'reported_state': 'ready'}})
        self.assertTrue(signed_out['signin_note_visible'] and not signed_out['speed_visible'])

    def test_sign_out_is_offered_once_signed_in_and_waits_for_a_download_or_sign_in(self):
        idle = view()
        self.assertTrue(idle['signout_visible'] and idle['signout_enabled'])
        self.assertEqual(idle['signout_text'], 'Sign out of Tapas')
        self.assertEqual(idle['signout_help'], 'Comic Archiver keeps your Tapas sign-in on this computer so it can download. '
                                               'Signing out removes it from Comic Archiver only. Your Tapas account, '
                                               'and anywhere else you use it, aren\'t affected. Your saved episodes '
                                               'stay.')
        for waiting in (view(account=account()),
                        view(status={'session_configured': True, 'worker': {'reported_state': 'busy'}})):
            self.assertFalse(waiting['signout_enabled'])
            self.assertEqual(waiting['signout_help'], 'Pause the download to sign out.')
        signing_in = view(site_auth={'status': 'running'})
        self.assertFalse(signing_in['signout_enabled'])
        self.assertIn('Signing out removes it from Comic Archiver only.', signing_in['signout_help'])
        self.assertFalse(view(busy=True)['signout_enabled'])
        self.assertFalse(view(status={'worker': {'reported_state': 'ready'}})['signout_visible'])

    def test_the_connection_choice_says_what_saving_does_on_each_kind_of_connection(self):
        self.assertFalse(view(network=None)['network_visible'] or view(network=None)['network_enabled'])
        anything = view(network={'wifi_only': False, 'allowed': True})
        self.assertTrue(anything['network_checked'])
        self.assertTrue(anything['network_help'].startswith('Saving uses any connection'))
        self.assertTrue(view()['network_help'].startswith('Off: saving uses Wi-Fi'))
        self.assertTrue(view(network={'wifi_only': True, 'reason': 'metered_connection'})['network_help'].startswith('This connection is metered'))
        self.assertTrue(view(network={'wifi_only': True})['network_help'].startswith('This computer cannot tell'))
        self.assertFalse(view(busy=True)['network_enabled'])


class StatusTests(unittest.TestCase):
    def test_each_state_of_the_download_has_its_own_sentence(self):
        cases = [({'loaded': False}, 'Connecting to your library…'),
                 ({'site_auth': {'status': 'running'}}, 'Finish signing in to Tapas in the window that opened.'),
                 ({'status': {'worker': {'reported_state': 'ready'}}}, 'Sign in to Tapas to get started.'),
                 ({'account': account(phase='verify')}, 'Checking the completed library…'),
                 ({'account': account(phase='new')}, 'Downloading your library…'),
                 ({'account': {'status': 'held', 'code': 'cancelled'}, 'network': {'wifi_only': True, 'reason': 'metered_connection'}},
                  'Paused: this connection is metered, and the app saves only on Wi-Fi'),
                 ({'account': {'status': 'held', 'code': 'cancelled'}}, 'Paused. Hit Go to continue from saved progress.'),
                 ({'status': {'session_configured': True, 'worker': {'reported_state': 'busy'}}},
                  'Finishing the current step. Your saved files are safe.'),
                 ({'status': {'session_configured': True, 'worker': {'reported_state': 'ready'},
                              'progress': {'status': 'running', 'cancellation_requested': True}}}, 'Pausing after the image being saved.'),
                 ({'status': {'session_configured': True, 'worker': {}}, 'site_auth': {'status': 'connected'}, 'connected_at': NOW},
                  'Starting the downloader…'),
                 ({'status': {'session_configured': True, 'worker': {}}}, 'The downloader is offline. Click Sign in to Tapas to reconnect.'),
                 ({'network': {'wifi_only': True}}, 'Saving would wait: this computer cannot tell whether its connection is metered'),
                 ({'account': {'status': 'stopped', 'go_after_seconds': 600}, 'account_at': NOW},
                  'Paused. Tapas asked the app to wait: Go works again in about 10 minutes.'),
                 ({'account': {'status': 'finished'}}, 'Finished. Every episode your account can read is saved.'),
                 ({'account': {'status': 'finished'}, 'status': {'session_configured': True, 'worker': {'reported_state': 'ready'}}},
                  'Finished. No episodes your Tapas account can save were found.'),
                 ({'account': {'status': 'new'}}, 'Ready. Hit Go to save your Tapas library.')]
        for changes, start in cases:
            with self.subTest(start):
                self.assertTrue(view(**changes)['status'].startswith(start), view(**changes)['status'])

    def test_a_download_held_for_asking_too_fast_leads_to_its_speed_setting(self):
        self.assertTrue(view(account={'status': 'held', 'code': 'source_refused'})['settings_link_visible'])
        self.assertFalse(view(account={'status': 'held', 'code': 'cancelled'})['settings_link_visible'])
        self.assertFalse(view(account={'status': 'held', 'code': 'source_refused'},
                              network={'wifi_only': True, 'allowed': False})['settings_link_visible'])
        self.assertIn('Tapas asked the app to slow down', view(account={'status': 'held', 'code': 'source_refused'})['status'])

    def test_finding_series_names_the_list_and_how_far_through_it_is(self):
        lists = {'current': 'subscribed', 'checked': 1, 'total': 4}
        reading = {'status': 'running', 'phase': 'account_list', 'category': 'subscribed', 'reading_list_page_count': 3,
                   'observed_library_series_count': 40}
        v = view(account=account(phase='lists', lists=lists),
                 status={'session_configured': True, 'worker': {'reported_state': 'busy'}, 'progress': reading,
                         'episodes_by_status': {'archived': 3}})
        self.assertEqual(v['status'], 'Finding series — Subscribed list (2 of 4).')
        self.assertEqual(v['list_count'], '1 of 4 lists checked. 3 pages read · 40 series listed in Subscribed.')
        self.assertTrue(v['meter_visible'] and v['list_help_visible'])
        self.assertEqual((v['meter_value'], v['meter_max']), (1, 4))
        self.assertEqual(v['detail'], '3 episodes saved.')
        scan = view(account=account(phase='lists', lists=lists), status={'progress': {'status': 'running', 'phase': 'scan'}})
        self.assertTrue(scan['list_count'].endswith('Checking series details before moving to the next list.'))
        opening = view(account=account(phase='lists', lists={'checked': 0}))
        self.assertEqual(opening['list_count'], '0 of 4 lists checked. Opening the next list and waiting for Tapas.')
        self.assertEqual(opening['status'], 'Finding series in your Tapas account…')
        series = view(account=account(phase='series'), status={'scan_remaining_series_count': 12})
        self.assertEqual(series['list_count'], '12 known series still awaiting checks.')
        self.assertFalse(series['meter_visible'])

    def test_the_saved_count_estimate_and_space_follow_what_was_read(self):
        self.assertEqual(view(status=None)['detail'], 'Loading saved-episode count…')
        self.assertEqual(view(status={'episodes_by_status': {'archived': 1}})['detail'], '1 episode saved.')
        progress = {'status': 'running', 'phase': 'archive', 'completed': 2, 'downloaded_pages': 5}
        self.assertEqual(view(status={'episodes_by_status': {'archived': 2}, 'progress': progress})['detail'],
                         '2 episodes saved. 2 saved in this step · 5 images.')
        self.assertTrue(view(account={'status': 'finished'})['detail'].endswith('may not include every older purchase.'))
        webtoon = dict(TAPAS, name='Webtoon')
        self.assertEqual(view(account={'status': 'finished'}, sites=[webtoon])['detail'], '3 episodes saved.')
        estimate = {'waiting_episodes': 3, 'seconds': 600, 'left_bytes': 1_000_000_000}
        planned = view(status={'estimate': estimate})
        self.assertTrue(planned['estimate'].startswith('About 10 minutes'))
        self.assertTrue(planned['space'].startswith('Your whole library'))
        self.assertEqual(view(status={'estimate': estimate}, account={'status': 'finished', 'configured': True})['estimate'], '')
        self.assertEqual(view(status={'estimate': estimate}, account={'status': 'stopped'})['space'], '')

    def test_sign_in_messages_say_what_went_wrong_or_what_to_do_next(self):
        self.assertEqual(view(site_auth={'status': 'running'})['signin_status'],
                         'Finish signing in to Tapas. We’ll detect it automatically; then return here and hit Go.')
        for code, part in (('network_held', 'press Allow this connection'), ('account_mismatch', 'does not match this saved library'),
                           ('signin_incomplete', 'Sign-in was not completed'), ('other', 'Sign-in did not finish')):
            v = view(site_auth={'status': 'error', 'code': code})
            self.assertIn(part, v['signin_status'])
            self.assertTrue(v['signin_status_error'])
        failure = view(message=Message('Refused.', True))
        self.assertEqual((failure['signin_status'], failure['settings_status']), ('Refused.', 'Refused.'))
        self.assertTrue(failure['signin_status_error'] and failure['settings_status_error'])
        plain = view(message=Message('Doing it.'))
        self.assertFalse(plain['signin_status_error'] or plain['settings_status_error'])
        self.assertEqual(view()['signin_status'], '')


class LiveTests(unittest.TestCase):
    def test_the_clock_counts_on_from_what_the_app_last_said(self):
        v = model.view(state(account=account(elapsed_seconds=100, progress_age_seconds=5), account_at=NOW - 10, refreshed_at=NOW - 3), NOW)
        self.assertEqual(v['clock'], 'Elapsed: 1m 50s · Last recorded activity: 0m 15s ago · Status checked 0m 3s ago')
        self.assertEqual(model.view(state(refreshed_at=0), NOW)['clock'], '')

    def test_activity_says_whether_updates_arrive_and_what_the_download_waits_for(self):
        def activity(**changes):
            return view(**changes)['activity']
        self.assertEqual(activity(), '')
        self.assertEqual(activity(account=account(), status={'session_configured': True, 'worker': {'reported_state': 'busy'}}),
                         'Working. Progress updates every five seconds.')
        self.assertEqual(activity(account=account(), status={'session_configured': True, 'worker': {'reported_state': 'ready'}}),
                         'Working. Getting the next step ready.')
        self.assertTrue(activity(account=account(), offline=True).startswith('Progress updates are delayed.'))
        self.assertTrue(activity(account=account(), refreshed_at=NOW - model.DELAYED_SECONDS).startswith('Progress updates are delayed.'))
        for reason, start in (('source', 'Tapas asked the app to wait. The download continues by itself in about a minute.'),
                              ('images', 'Some images did not arrive.'), ('network', 'The connection to Tapas was interrupted.')):
            self.assertTrue(activity(account=account(retry={'reason': reason, 'seconds': 60})).startswith(start))
        self.assertTrue(activity(account=account(retry={'reason': 'network', 'seconds': 60}),
                                 network={'wifi_only': True, 'allowed': False}).startswith('Waiting: this computer cannot tell'))
        for timing in ({'progress_age_seconds': 60}, {'between_steps_seconds': 60}, {'elapsed_seconds': 60}):
            with self.subTest(timing):
                v = view(account=account(**timing))
                self.assertTrue(v['activity'].startswith('No new progress for over a minute; Tapas may be slow.'))
                self.assertTrue(v['live_waiting'])
                self.assertEqual((v['title'], v['state'], v['tone']), ('Waiting for a progress update', 'Waiting', 'warning'))
        self.assertFalse(view(account=account(progress_age_seconds=10, elapsed_seconds=600))['live_waiting'])

    def test_the_heading_names_the_state_of_the_download_in_a_word(self):
        cases = [({'loaded': False}, ('Loading your status…', 'Loading', 'neutral')),
                 ({'offline': True}, ('Status updates unavailable', 'Reconnecting', 'warning')),
                 ({'site_auth': {'status': 'running'}}, ('Finish signing in', 'Signing in', 'working')),
                 ({'status': {'worker': {'reported_state': 'ready'}}}, ('Sign in to get started', 'Sign-in needed', 'warning')),
                 ({'account': account(phase='lists')}, ('Finding your series', 'Finding series', 'working')),
                 ({'account': account(phase='other')}, ('Saving your library', 'Working', 'working')),
                 ({'account': {'status': 'held', 'code': 'archive_incomplete'}}, ('Finished, with episodes missing', 'Finished', 'warning')),
                 ({'account': {'status': 'held'}}, ('Your download is paused', 'Paused', 'warning')),
                 ({'account': {'status': 'new'}, 'status': {'session_configured': True, 'worker': {'reported_state': 'busy'}}},
                  ('Finishing the current step', 'Working', 'working')),
                 ({'account': {'status': 'new'}, 'status': {'session_configured': True}, 'site_auth': {'status': 'connected'},
                   'connected_at': NOW}, ('Starting the downloader', 'Starting', 'working')),
                 ({'account': {'status': 'new'}, 'status': {'session_configured': True}}, ('The downloader is offline', 'Offline', 'warning')),
                 ({'account': {'status': 'finished'}}, ('This run is finished', 'Finished', 'success')),
                 ({'account': {'status': 'new'}}, ('Ready to save', 'Ready', 'success'))]
        for changes, expected in cases:
            with self.subTest(expected[0]):
                v = view(**changes)
                self.assertEqual((v['title'], v['state'], v['tone']), expected)

    def test_the_badges_say_how_the_account_and_downloader_are(self):
        def badges(**changes):
            v = view(**changes)
            return v['session'], v['session_tone'], v['worker'], v['worker_tone']
        self.assertEqual(badges(status=None), ('Tapas · Checking…', 'neutral', 'Downloader · Checking…', 'neutral'))
        self.assertEqual(badges(), ('Tapas · Session saved', 'neutral', 'Downloader · Ready', 'neutral'))
        self.assertEqual(badges(site_auth={'status': 'connected'})[0], 'Tapas · Connected')
        self.assertEqual(badges(site_auth={'status': 'running'})[0], 'Tapas · Signing in')
        self.assertEqual(badges(status={'worker': {}}), ('Tapas · Sign-in needed', 'warning', 'Downloader · Starts after sign-in', 'neutral'))
        self.assertEqual(badges(status={'session_configured': True, 'worker': {}})[2:], ('Downloader · Offline', 'warning'))
        self.assertEqual(badges(status={'session_configured': True, 'worker': {'reported_state': 'busy'}})[2], 'Downloader · Working')
        self.assertEqual(badges(offline=True)[2:], ('Downloader · Status unknown', 'warning'))
        self.assertEqual(badges(status={'session_configured': True, 'worker': {}}, site_auth={'status': 'connected'},
                                connected_at=NOW)[2], 'Downloader · Starting')

    def test_the_last_update_is_said_until_updates_are_delayed(self):
        self.assertEqual(view(refreshed_at=0)['updated'], 'Checking current status…')
        self.assertEqual(view(refreshed_at=NOW - 1)['updated'], 'Updated just now')
        self.assertEqual(view(refreshed_at=NOW - 9)['updated'], 'Updated 9s ago')
        self.assertEqual(view(offline=True)['updated'], 'Updates delayed · trying to reconnect')
        self.assertEqual(view()['footnote'], 'Saving runs only while this window is open. Closing it pauses, and Go carries on later.')


if __name__ == '__main__':
    unittest.main()
