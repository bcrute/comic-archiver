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


def fields(v):
    """A card's rows as {field: (value, warning)}."""
    return {row['name']: (row['value'], row['warning']) for row in v['rows']}


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

    def test_android_estimate_keeps_desktop_numbers_without_the_window_lifetime_rule(self):
        estimate = {'waiting_episodes': 2, 'unchecked_series': 1, 'seconds': 7200}
        desktop = model.time_left(estimate)
        android = model.time_left(estimate, background=True)
        self.assertEqual(desktop.split(' It only continues')[0], android.split(' Android may limit')[0])
        self.assertIn('Reopen the app and press Go if saving stops.', android)
        self.assertNotIn('only continues while this app is open', android)
        self.assertEqual(model.time_left(None, background=True), '')
        self.assertEqual(model.time_left({'waiting_episodes': 0}, background=True), '')
        self.assertEqual(model.time_left({'waiting_episodes': 2}, background=True),
                         model.time_left({'waiting_episodes': 2}))

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

    def test_a_held_download_says_why_it_stopped_in_a_few_words_and_leaves_what_to_do_to_the_buttons(self):
        cases = {('archive_incomplete', 1): '1 episode could not be saved',
                 ('archive_incomplete', 3): '3 episodes could not be saved',
                 ('archive_incomplete', 0): 'Some saved files need another check',
                 ('source_refused', 0): 'Tapas asked the app to slow down',
                 ('source_recovery_limit', 0): 'Tapas asked the app to slow down',
                 ('source_access_changed', 0): 'Tapas still would not send an episode after waiting',
                 ('network_policy', 0): 'The connection was not Wi-Fi or wired',
                 ('network_unavailable', 0): 'Tapas could not be reached several times in a row',
                 ('network_recovery_limit', 0): 'Tapas could not be reached several times in a row',
                 ('no_progress', 0): 'The last few tries saved nothing new',
                 ('scan_no_progress', 0): 'The last few tries saved nothing new',
                 ('cancelled', 0): '',
                 ('cloud_space', 0): 'An upload to cloud storage stopped. ' + model.CLOUD_HINTS['cloud_space'],
                 ('mystery', 0): 'It stopped unexpectedly; check that this disk has free space'}
        for (code, missing), said in cases.items():
            with self.subTest(code=code, missing=missing):
                self.assertEqual(model.held_reason(code, 'Tapas', missing), said)
                self.assertNotIn('Go', said)

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
        self.assertEqual(fields(relogin)['Stopped'], ('Tapas signed you out', True))

    def test_a_saved_sign_in_the_site_no_longer_knows_asks_for_a_sign_in_before_go(self):
        expired = view(site_auth={'status': 'expired'})
        self.assertFalse(expired['go_enabled']); self.assertTrue(expired['signin_visible'] and expired['signin_enabled'])
        self.assertEqual((expired['title'], expired['state'], expired['session']), ('Sign in again', 'Sign-in needed', 'Tapas · Sign-in needed'))
        self.assertEqual(fields(expired)['Account'], ('Sign-in needed', True))
        # One the site still names is connected, whatever the last run found; an unanswered check changes nothing.
        verified = view(site_auth={'status': 'verified'}, account={'status': 'held', 'code': 'login_required'})
        self.assertTrue(verified['go_enabled']); self.assertEqual(verified['session'], 'Tapas · Connected')
        self.assertTrue(view()['go_enabled']); self.assertEqual(view()['session'], 'Tapas · Session saved')

    def test_a_later_login_failure_outweighs_the_check_before_go_even_after_refreshes(self):
        for auth in ('verified', 'connected'):
            with self.subTest(auth=auth):
                s = state(site_auth={'status': auth})
                s.received_account(account(), NOW + 1)
                s.received_account({'status': 'held', 'code': 'login_required'}, NOW + 2)
                s.received_site_auth({'status': auth}, NOW + 2)
                failed = model.view(s, NOW + 2)
                self.assertFalse(failed['go_enabled'])
                self.assertTrue(failed['signin_visible'] and failed['signin_enabled'])
                self.assertEqual(failed['session'], 'Tapas · Sign-in needed')
                self.assertEqual(fields(failed)['Stopped'], ('Tapas signed you out', True))
                # Signing in again clears the old failure; repeating its result does not.
                s.received_site_auth({'status': 'running'}, NOW + 3)
                s.received_site_auth({'status': 'connected'}, NOW + 4)
                self.assertTrue(model.view(s, NOW + 4)['go_enabled'])

    def test_a_late_startup_check_cannot_hide_a_failure_after_go_and_pause_keeps_a_valid_signin(self):
        s = state()
        s.download_started()
        s.received_account({'status': 'held', 'code': 'login_required'}, NOW + 1)
        s.received_site_auth({'status': 'verified'}, NOW + 2)
        self.assertFalse(model.view(s, NOW + 2)['go_enabled'])
        self.assertTrue(model.view(s, NOW + 2)['signin_visible'])
        s.received_account({'status': 'stopped'}, NOW + 3)
        self.assertTrue(model.view(s, NOW + 3)['go_enabled'])
        self.assertEqual(model.view(s, NOW + 3)['session'], 'Tapas · Connected')

    def test_pause_allow_and_the_folder_follow_a_running_download_and_another_site_can_be_chosen_meanwhile(self):
        running = view(account=account())
        self.assertTrue(running['pause_visible'] and running['pause_enabled'])
        self.assertFalse(view(account=account(), busy=True)['pause_enabled'])
        # Choosing another site in Settings stops nothing; a new folder takes every site with it.
        self.assertTrue(running['site_enabled'])
        self.assertFalse(running['folder_enabled'])
        self.assertNotIn('site_tooltip', running)
        self.assertIn('Press Pause on the dashboard first.', running['folder_lock'])
        self.assertFalse(view(site_auth={'status': 'running'})['site_enabled'])
        self.assertFalse(view(busy=True)['site_enabled'])
        idle = view()
        self.assertTrue(idle['site_enabled'] and idle['folder_enabled'] and not idle['pause_visible'])
        self.assertEqual((idle['folder_tooltip'], idle['folder_lock']), ('', ''))
        held = view(network={'wifi_only': True, 'allowed': False})
        self.assertTrue(held['allow_visible'] and held['allow_enabled'])
        self.assertFalse(view(network={'wifi_only': False, 'allowed': False})['allow_visible'])

    def test_only_the_open_sites_speed_is_offered(self):
        alone = view()
        self.assertEqual((alone['speed_label'], alone['speed']), ('Speed on Tapas', 'fast'))
        self.assertNotIn('other_speeds', alone)
        self.assertTrue(alone['speed_visible'] and alone['speed_enabled'] and not alone['signin_note_visible'])
        self.assertIn('If Tapas keeps pausing', alone['speed_help'])
        both = view(sites=[TAPAS, TAPPYTOON], account={'status': 'stopped', 'speed': 'medium'})
        self.assertEqual((both['speed_label'], both['speed'], both['speed_help']), ('Speed on Tapas', 'medium', 'About four images a second.'))
        self.assertEqual(view(account=account(speed='bogus'))['speed_help'], 'Pause to change the speed.')
        self.assertFalse(view(account=account())['speed_enabled'])
        signed_out = view(status={'worker': {'reported_state': 'ready'}})
        self.assertTrue(signed_out['signin_note_visible'] and not signed_out['speed_visible'])
        self.assertEqual(signed_out['signin_note'], 'Sign in to Tapas to choose how fast it saves.')

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
    def test_a_card_says_what_is_so_a_field_and_its_value_each_and_no_instructions(self):
        self.assertEqual(view(loaded=False)['rows'], [{'name': 'Library', 'value': 'Opening…', 'warning': False}])
        self.assertEqual([(row['name'], row['value'], row['warning']) for row in view()['rows']],
                         [('Account', 'Session saved', False), ('Downloader', 'Ready', False), ('Saved', '3 episodes', False),
                          ('Speed', 'Fast', False)])
        cases = [({'account': {'status': 'held', 'code': 'cancelled'}}, {}),
                 ({'account': {'status': 'held', 'code': 'cancelled'}, 'network': {'wifi_only': True, 'reason': 'metered_connection'}},
                  {'Connection': ('This connection is metered; saving waits for Wi-Fi or a wired connection', True)}),
                 ({'network': {'wifi_only': True}},
                  {'Connection': ('This computer cannot tell whether its connection is metered (a VPN can cause this); '
                                  'saving waits for Wi-Fi or a wired connection', True)}),
                 ({'account': {'status': 'held', 'code': 'source_refused'}},
                  {'Stopped': ('Tapas asked the app to slow down', True), 'Speed': ('Fast (a slower speed may help)', True)}),
                 ({'account': {'status': 'stopped', 'go_after_seconds': 600}, 'account_at': NOW},
                  {'Go again in': ('about 10 minutes', False)}),
                 ({'account': {'status': 'finished'}},
                  {'Finished': ('Every episode your account can read is saved', False),
                   'Note': ('Tapas’s account lists may not include every older purchase', False)}),
                 ({'account': {'status': 'finished'}, 'status': {'session_configured': True, 'worker': {'reported_state': 'ready'}}},
                  {'Finished': ('No episodes your Tapas account can save were found', False), 'Saved': ('0 episodes', False)}),
                 ({'account': {'status': 'held', 'code': 'archive_incomplete',
                               'audit': {'missing_recorded_accessible_episode_count': 2}}},
                  {'Finished': ('2 episodes could not be saved', True)}),
                 ({'status': {'session_configured': True, 'worker': {'reported_state': 'busy'}}},
                  {'Step': ('Finishing the current step', False)}),
                 ({'status': {'session_configured': True, 'worker': {'reported_state': 'ready'},
                              'progress': {'status': 'running', 'cancellation_requested': True}}},
                  {'Step': ('Pausing after the image being saved', False)}),
                 ({'account': account(phase='verify')}, {'Step': ('Checking saved files', False)}),
                 ({'account': account(phase='new')}, {'Step': ('Downloading', False)}),
                 ({'account': account(), 'status': {'session_configured': True, 'worker': {'reported_state': 'busy'},
                                                    'progress': {'status': 'running', 'phase': 'archive', 'completed': 2,
                                                                 'downloaded_pages': 5}}},
                  {'Step': ('Downloading episodes · 2 saved in this step · 5 images', False)})]
        for changes, expected in cases:
            with self.subTest(changes):
                said = fields(view(**changes))
                for name, value in expected.items():
                    self.assertEqual(said.get(name), value, said)
                for value, _ in said.values():
                    self.assertNotIn('Go', value)
                    self.assertNotIn('Hit ', value)
        self.assertNotIn('Stopped', fields(view(account={'status': 'held', 'code': 'cancelled'})))

    def test_finding_series_names_the_list_and_how_far_through_it_is(self):
        lists = {'current': 'subscribed', 'checked': 1, 'total': 4}
        reading = {'status': 'running', 'phase': 'account_list', 'category': 'subscribed', 'reading_list_page_count': 3,
                   'observed_library_series_count': 40}
        v = fields(view(account=account(phase='lists', lists=lists),
                        status={'session_configured': True, 'worker': {'reported_state': 'busy'}, 'progress': reading,
                                'episodes_by_status': {'archived': 3}}))
        self.assertEqual(v['Step'], ('Finding your series', False))
        self.assertEqual(v['Lists'], ('1 of 4 read · 3 pages of Subscribed · 40 series', False))
        self.assertEqual(v['Saved'], ('3 episodes', False))
        scan = fields(view(account=account(phase='lists', lists=lists), status={'progress': {'status': 'running', 'phase': 'scan'}}))
        self.assertEqual(scan['Lists'][0], '1 of 4 read · now Subscribed')
        self.assertEqual(fields(view(account=account(phase='lists', lists={'checked': 0})))['Lists'][0], '0 of 4 read')
        series = fields(view(account=account(phase='series'), status={'scan_remaining_series_count': 12}))
        self.assertEqual(series['Step'][0], 'Checking series and downloading')
        self.assertEqual(series['Series left'][0], '12 to check')
        self.assertNotIn('Lists', series)

    def test_download_settings_tables_why_checked_episodes_were_not_downloaded(self):
        # The first outside report: a folder stopped at 100 of 144 with nothing on screen to say why.
        reported = view(status={'session_configured': True, 'episodes_by_status': {'archived': 100, 'unavailable': 44}})
        self.assertEqual(fields(reported)['Saved'][0], '100 episodes')
        self.assertNotIn('not_saved', reported)
        self.assertTrue(reported['not_downloaded_visible'])
        self.assertEqual([(row['reason'], row['count']) for row in reported['not_downloaded']], [
            ('Locked on your Tapas account. Buy or unlock them on Tapas, then hit Go', '44'),
            ('Not released yet', '0'),
            ('Time-limited, with access the app couldn’t confirm is still current', '0'),
            ('Novels this site’s download doesn’t support', '0'),
            ('In a form the app can’t save yet', '0'),
            ('Stopped by download errors', '0')])
        every = view(sites=[dict(TAPAS, name='Webtoon')], status={
            'episodes_by_status': {'archived': 5, 'queued': 7, 'unavailable': 1200, 'unsupported': 9, 'failed': 2},
            'unsupported_by_reason': {'scheduled': 3, 'temporary_access': 2, 'novel': 1, 'reader_manifest_incomplete': 3}})
        self.assertTrue(every['not_downloaded_visible'])
        self.assertEqual([row['count'] for row in every['not_downloaded']], ['1,200', '3', '2', '1', '3', '2'])
        self.assertTrue(every['not_downloaded'][0]['reason'].startswith('Locked on your Webtoon account.'))

    def test_the_not_downloaded_table_waits_for_the_library_and_never_invents_or_loses_episodes(self):
        unread = view(status=None)
        self.assertEqual((unread['not_downloaded_visible'], unread['not_downloaded']), (False, []))
        # Signed in with nothing skipped: the table shows, all zero. Signed out with nothing skipped: no table.
        self.assertEqual([row['count'] for row in view()['not_downloaded']], ['0'] * 6)
        self.assertFalse(view(status={'episodes_by_status': {'archived': 3}})['not_downloaded_visible'])
        self.assertTrue(view(status={'episodes_by_status': {'failed': 1}})['not_downloaded_visible'])
        odd = view(status={'session_configured': True,
                           'episodes_by_status': {'unavailable': -1, 'failed': '2', 'unsupported': True}})
        self.assertEqual([row['count'] for row in odd['not_downloaded']], ['0'] * 6)
        for reasons in (None, ['scheduled'], {'scheduled': 0}):
            with self.subTest(reasons=reasons):
                rows = view(status={'episodes_by_status': {'unsupported': 2}, 'unsupported_by_reason': reasons})['not_downloaded']
                self.assertEqual([row['count'] for row in rows], ['0', '0', '0', '0', '2', '0'])

    def test_the_saved_count_time_left_and_space_follow_what_was_read(self):
        self.assertEqual([row['name'] for row in view(status=None)['rows']], ['Account', 'Downloader'])
        self.assertEqual(fields(view(status={'episodes_by_status': {'archived': 1}}))['Saved'][0], '1 episode')
        self.assertNotIn('Note', fields(view(account={'status': 'finished'}, sites=[dict(TAPAS, name='Webtoon')])))
        planned = fields(view(status={'estimate': {'waiting_episodes': 3, 'seconds': 600, 'left_bytes': 1_000_000_000}}))
        self.assertEqual(planned['Time left'], ('About 10 minutes · 3 episodes waiting', False))
        self.assertEqual(planned['Space'], ('About 1.0 GB in all', False))
        rough = fields(view(status={'estimate': {'waiting_episodes': 1, 'unchecked_series': 1, 'seconds': 7200,
                                                  'saved_bytes': 2_000_000_000, 'left_bytes': 1_000_000_000,
                                                  'free_bytes': 50_000_000_000}}))
        self.assertEqual(rough['Time left'][0], 'About 2 hours · 1 episode waiting, 1 series not checked yet, so a rough guess')
        self.assertEqual(rough['Space'], ('About 3.0 GB in all, 1.0 GB still to come (rough) · 50 GB free', False))
        short = fields(view(status={'estimate': {'waiting_episodes': 1, 'left_bytes': 1_000_000_000, 'free_bytes': 10}}))
        self.assertEqual(short['Space'], ('About 1.0 GB in all · 1 MB free, not enough', True))
        early = fields(view(status={'estimate': {'waiting_episodes': 4}}))
        self.assertEqual(early['Time left'][0], 'Estimating; known after about five minutes of downloading')
        self.assertNotIn('Space', early)
        estimate = {'waiting_episodes': 3, 'seconds': 600, 'left_bytes': 1_000_000_000}
        finished = fields(view(status={'estimate': estimate}, account={'status': 'finished', 'configured': True}))
        self.assertNotIn('Time left', finished)
        self.assertNotIn('Space', fields(view(status={'estimate': estimate}, account={'status': 'stopped'})))
        self.assertEqual((model.time_row(None), model.space_row(None)), ('', ('', False)))
        self.assertEqual(model.time_row({'waiting_episodes': 0, 'unchecked_series': 0, 'seconds': 60}), '')

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
    def test_running_for_counts_on_from_what_the_app_last_said(self):
        v = model.view(state(account=account(elapsed_seconds=100, progress_age_seconds=5), account_at=NOW - 10, refreshed_at=NOW - 3), NOW)
        self.assertEqual(fields(v)['Running for'], ('1m 50s · last saved 0m 15s ago', False))
        self.assertNotIn('Running for', fields(view(account=account())))
        self.assertNotIn('Running for', fields(view(account={'status': 'stopped', 'elapsed_seconds': 100})))

    def test_the_card_says_whether_updates_arrive_and_what_the_download_waits_for(self):
        self.assertEqual(fields(view(account=account(), offline=True))['Updates'], ('Delayed: reaching the downloader again', True))
        self.assertIn('Updates', fields(view(account=account(), refreshed_at=NOW - model.DELAYED_SECONDS)))
        self.assertNotIn('Step', fields(view(account=account(), offline=True)))
        for reason, said in (('source', 'Tapas asked the app to wait; carries on in about a minute'),
                             ('images', 'Some images did not arrive; carries on in about a minute'),
                             ('idle', 'The last step saved nothing new; carries on in about a minute'),
                             ('network', 'The connection to Tapas was interrupted; carries on in about a minute')):
            with self.subTest(reason):
                self.assertEqual(fields(view(account=account(retry={'reason': reason, 'seconds': 60})))['Waiting'], (said, True))
        self.assertEqual(fields(view(account=account(retry={'reason': 'network', 'seconds': 60}),
                                     network={'wifi_only': True, 'allowed': False}))['Waiting'],
                         ('For Wi-Fi or a wired connection', True))
        for timing in ({'progress_age_seconds': 60}, {'between_steps_seconds': 60}, {'elapsed_seconds': 60}):
            with self.subTest(timing):
                v = view(account=account(**timing))
                self.assertEqual(fields(v)['Waiting'], ('No progress for over a minute; Tapas may be slow', True))
                self.assertTrue(v['live_waiting'])
                self.assertEqual((v['title'], v['state'], v['tone']), ('Waiting for a progress update', 'Waiting', 'warning'))
        self.assertFalse(view(account=account(progress_age_seconds=10, elapsed_seconds=600))['live_waiting'])
        self.assertNotIn('Waiting', fields(view(account=account(progress_age_seconds=10, elapsed_seconds=600))))

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
            self.assertEqual((fields(v)['Account'][0], fields(v)['Downloader'][0]), (v['session_word'], v['worker_word']))
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


if __name__ == '__main__':
    unittest.main()
