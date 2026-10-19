"""What the Storage and Backblaze B2 settings and the dashboard's library card say and allow, decided without a window."""
import unittest

from comic_archive import storage_model as model
from comic_archive.dashboard_model import CLOUD_HINTS, State

READY = {'session_configured': True, 'worker': {'reported_state': 'ready'}}


def b2(**changes):
    return {'provider': 'b2', 'connected': True, 'tested': True, 'enabled': True, 'bucket': 'my-library'} | changes


def state(*targets, **changes):
    """Signed in, the downloader ready and idle, with these cloud targets."""
    values = dict(sites=[{'key': 'tapas', 'name': 'Tapas', 'current': True}], status=dict(READY),
                  account={'status': 'stopped'}, cloud={'storage': 'shared', 'targets': list(targets)})
    values.update(changes)
    return State(**values)


class CostTests(unittest.TestCase):
    def test_money_is_said_in_cents_until_it_is_a_dollar(self):
        for value, said in ((0.004, 'less than 1 cent'), (0.01, 'about 1 cent'), (0.63, 'about 63 cents'), (1.6, 'about $1.60')):
            self.assertEqual(model.dollars(value), said)

    def test_the_cost_is_free_within_ten_gigabytes_and_counted_by_the_day_and_month_past_it(self):
        self.assertEqual(model.b2_cost(None), '')
        self.assertEqual(model.b2_cost({'saved_bytes': 5_000_000_000}),
                         'Your library should take about 5.0 GB, which fits in Backblaze’s free 10 GB.')
        self.assertEqual(model.b2_cost({'saved_bytes': 10_000_000_000, 'left_bytes': 100_000_000_000, 'unchecked_series': 1}),
                         'Your library should take about 110 GB. Once all of it is stored, Backblaze would charge about 2 cents '
                         'a day, about 70 cents a month. This is a rough guess until every series has been checked.')
        self.assertTrue(model.b2_cost_short(None).startswith('free up to 10 GB, then about 2 cents a day'))
        self.assertEqual(model.b2_cost_short({'left_bytes': 5_000_000_000}), 'free, your library fits in the free 10 GB')
        self.assertEqual(model.b2_cost_short({'saved_bytes': 110_000_000_000}), 'about 2 cents a day, about 70 cents a month')


class SummaryTests(unittest.TestCase):
    def test_the_b2_state_says_what_is_left_to_set_up(self):
        cases = [(state(cloud_auth={'status': 'running', 'provider': 'b2'}), ('Checking your key', 'working')),
                 (state(), ('Not set up', 'neutral')),
                 (state(b2(tested=False)), ('Connected, not checked yet', 'warning')),
                 (state(b2(enabled=False)), ('Set up', 'success')),
                 (state(b2()), ('Uploading to B2', 'success')),
                 (state(b2(failed=1)), ('Uploads need attention', 'warning'))]
        for s, expected in cases:
            with self.subTest(expected[0]):
                self.assertEqual(model.b2_summary(s)[:2], expected)
        self.assertEqual(model.b2_summary(state(b2(tested=False)))[2],
                         'Connected to your bucket my-library. Check the connection (step 5) before uploads can start.')
        self.assertIn('your bucket.', model.b2_summary(state(b2(bucket=None)))[2])
        self.assertTrue(model.b2_summary(state(b2(failed=1)))[2].endswith(
            ' 1 upload needs retrying: click Retry unfinished uploads below.'))
        self.assertIn('2 uploads need retrying', model.b2_summary(state(b2(failed=2)))[2])

    def test_a_cloud_task_starts_only_when_signed_in_ready_and_idle(self):
        self.assertTrue(model.can_import(state()))
        for name, changes in {'signed out': {'status': {'worker': {'reported_state': 'ready'}}},
                              'worker busy': {'status': {'session_configured': True, 'worker': {'reported_state': 'busy'}}},
                              'step running': {'status': dict(READY, progress={'status': 'running'})},
                              'downloading': {'account': {'status': 'running'}}, 'busy': {'busy': True},
                              'task': {'task': 'job-1'}, 'signing in': {'cloud_auth': {'status': 'running'}},
                              'nothing read': {'status': None}}.items():
            with self.subTest(name):
                self.assertFalse(model.can_import(state(**changes)))

    def test_the_key_check_is_described_as_it_goes(self):
        self.assertEqual(model.auth_text(None), '')
        self.assertTrue(model.auth_text({'status': 'running', 'stage': 'preparing'}).startswith('Preparing cloud sign-in.'))
        self.assertEqual(model.auth_text({'status': 'running'}), 'Checking your B2 key and private library bucket…')
        self.assertEqual(model.auth_text({'status': 'connected'}), 'Signed in. Next, click Test cloud connection.')
        self.assertEqual(model.auth_text({'status': 'cancelled'}), 'Sign-in cancelled. Your saved files are safe.')
        self.assertTrue(model.auth_text({'status': 'error', 'code': 'signin_network_held'}).startswith('Mobile data is off.'))
        self.assertEqual(model.auth_text({'status': 'error', 'code': 'b2_private_bucket'}), CLOUD_HINTS['b2_private_bucket'])
        self.assertTrue(model.auth_text({'status': 'error', 'code': 'x'}).startswith('Sign-in did not finish.'))

    def test_a_finished_cloud_task_says_what_it_did_or_why_it_stopped(self):
        self.assertEqual(model.task_text('cloud', {'status': 'error', 'code': 'cloud_space'}, 'Tapas'),
                         (CLOUD_HINTS['cloud_space'] + ' Local episodes are retained.', True))
        self.assertEqual(model.task_text('cloud', {'status': 'error'}),
                         ('The task stopped. Completed archives and cached images are retained.', True))
        self.assertEqual(model.task_text('cloud', {'status': 'ok', 'result': {'uploaded_episodes': 3, 'local_archives_deleted': 2}}),
                         ('3 uploads verified; 2 local archives removed.', False))
        self.assertEqual(model.task_text('cloud-test', {'status': 'ok', 'result': {'verified_episodes': 2}}),
                         ('2 episodes verified; test copies deleted. Local originals retained.', False))
        self.assertEqual(model.task_text('cloud-cleanup', {'status': 'ok'}),
                         ('Recorded test copies deleted. Local originals retained.', False))


class StorageTests(unittest.TestCase):
    def test_the_storage_choice_appears_once_a_cloud_storage_is_connected(self):
        self.assertEqual(model.storage_choice(state(cloud=None)),
                         {'visible': False, 'options': [], 'chosen': 'device', 'lock': '',
                          'help': 'New episodes stay in the save folder.', 'entry_visible': False})
        self.assertTrue(model.storage_choice(state())['entry_visible'])
        self.assertFalse(model.storage_choice(state(b2(), cloud_offered=False))['visible'])
        self.assertFalse(model.storage_choice(state(cloud_offered=False))['entry_visible'])
        choice = model.storage_choice(state(b2(enabled=False)))
        self.assertTrue(choice['visible'] and not choice['entry_visible'])
        self.assertEqual(choice['options'], [{'key': 'device', 'label': 'This device', 'ready': True, 'enabled': True, 'checked': True},
                                             {'key': 'b2', 'label': 'Backblaze B2', 'ready': True, 'enabled': True, 'checked': False}])
        self.assertEqual(choice['help'], 'New episodes stay in the save folder.')

    def test_storage_that_failed_its_check_cannot_be_chosen_and_the_chosen_one_is_named(self):
        unchecked = model.storage_choice(state(b2(enabled=False, tested=False)))
        self.assertFalse(unchecked['options'][1]['enabled'])
        self.assertEqual(unchecked['help'], 'Backblaze B2 can be chosen once its connection check passes, on its own tab.')
        chosen = model.storage_choice(state(b2()))
        self.assertEqual(chosen['chosen'], 'b2')
        self.assertTrue(chosen['options'][1]['checked'])
        self.assertTrue(chosen['help'].startswith('Each new episode is uploaded to Backblaze B2 as it downloads.'))
        self.assertEqual(model.label('other'), 'Cloud storage')

    def test_the_storage_choice_is_locked_and_says_why_while_something_runs(self):
        for changes, why in (({'account': {'status': 'running'}}, 'Press Pause on the dashboard first.'),
                             ({'cloud_auth': {'status': 'running'}}, 'while you sign in to cloud storage.'),
                             ({'task': 'job-1'}, 'until the cloud task finishes.'), ({'busy': True}, '')):
            with self.subTest(why):
                choice = model.storage_choice(state(b2(), **changes))
                self.assertFalse(any(option['enabled'] for option in choice['options']))
                self.assertTrue(choice['lock'].endswith(why))

    def test_each_storage_place_says_what_it_holds(self):
        empty = model.storage(state())
        self.assertEqual(empty, {'device_stored': 'Counting…', 'b2_pill': 'Not set up', 'b2_tone': 'neutral',
                                 'b2_text': 'Your own Backblaze account. Backblaze bills you itself; the first 10 GB is free.',
                                 'b2_stored': '', 'b2_links_visible': False, 'b2_setup': 'Set up'})
        locations = [{'id': 'device', 'episodes': 1, 'bytes': 2_000_000_000}, {'id': 'b2', 'episodes': 5, 'bytes': 30_000_000_000}]
        held = model.storage(state(b2(), locations=locations))
        self.assertEqual((held['device_stored'], held['b2_stored']), ('1 episode stored · 2.0 GB', '5 episodes stored · 30 GB'))
        self.assertEqual((held['b2_text'], held['b2_links_visible'], held['b2_setup']),
                         ('Your own Backblaze account, bucket my-library.', True, 'B2 settings'))
        self.assertEqual(model.storage(state(b2(bucket=None)))['b2_text'], 'Your own Backblaze account.')


class B2Tests(unittest.TestCase):
    def test_a_typed_key_is_checked_for_its_shape_before_it_is_sent(self):
        good = ('my-library', 'K0012345abcd', 'K001applicationkey')
        self.assertEqual(model.key_problem(*good), '')
        for bucket in ('short', 'with space', 'a' * 64, 'under_score'):
            self.assertIn('bucket name', model.key_problem(bucket, *good[1:]))
        for key_id, application_key in (('short', good[2]), ('with-hyphen', good[2]), (good[1], 'x' * 7), (good[1], 'x' * 257)):
            self.assertEqual(model.key_problem(good[0], key_id, application_key),
                             'Paste the keyID and applicationKey exactly as Backblaze shows them.')

    def test_the_bucket_name_is_what_was_typed_then_what_was_saved_then_a_suggestion(self):
        self.assertEqual(model.bucket_name(None, None, 'abcdef0123456789'), 'comic-library-abcdef012345')
        self.assertEqual(model.bucket_name(None, 'saved-name', 'abcdef0123456789'), 'saved-name')
        self.assertEqual(model.bucket_name('', 'saved-name', 'abcdef0123456789'), '')

    def test_before_the_cloud_is_read_nothing_can_be_set_up(self):
        settings = model.b2(state(cloud=None))
        self.assertEqual(settings['status'], 'Checking cloud connections…')
        self.assertFalse(settings['connect_enabled'] or settings['sharing_visible'] or settings['fold_visible'] or settings['folded'])
        self.assertEqual(model.b2(state())['status'], 'No connection configured for this provider.')

    def test_the_status_line_counts_receipts_once_the_connection_is_checked(self):
        self.assertEqual(model.b2(state(b2(verified=9, pending=1)))['status'],
                         'Automatic uploads enabled. 9 verified upload receipts; 1 pending; 0 need retry. '
                         'Cloud availability has not been rechecked.')
        self.assertTrue(model.b2(state(b2(enabled=False)))['status'].startswith('Uploads disabled.'))
        self.assertTrue(model.b2(state(b2(tested=False)))['status'].startswith('Connection configured.'))

    def test_a_connected_b2_folds_its_setup_steps_away_until_asked(self):
        folded = model.b2(state(b2()))
        self.assertTrue(folded['folded'] and folded['fold_visible'])
        self.assertEqual((folded['fold_text'], folded['connect_text']), ('Show the setup steps', 'Replace B2 connection'))
        opened = model.b2(state(b2()), steps_open=True)
        self.assertFalse(opened['folded'])
        self.assertEqual(opened['fold_text'], 'Hide the setup steps')
        self.assertEqual(model.b2(state())['connect_text'], 'Connect B2')

    def test_sharing_one_setup_between_sites_is_offered_and_explained(self):
        shared = model.b2(state())
        self.assertTrue(shared['sharing_visible'] and shared['shared'] and shared['sharing_enabled'])
        self.assertTrue(shared['sharing_help'].startswith('Every site you save from uses it'))
        separate = model.b2(state(cloud={'storage': 'separate', 'targets': []}))
        self.assertFalse(separate['shared'])
        self.assertEqual(separate['sharing_help'], 'Tapas has a Backblaze setup of its own. Your other sites keep the shared one.')
        self.assertFalse(model.b2(state(cloud={'targets': []}))['sharing_visible'])
        self.assertFalse(model.b2(state(busy=True))['sharing_enabled'])
        self.assertFalse(model.b2(state(cloud_auth={'status': 'running'}))['sharing_enabled'])

    def test_connecting_waits_for_anything_else_that_runs_and_can_be_cancelled_while_it_checks(self):
        self.assertTrue(model.b2(state())['connect_enabled'])
        for name, changes in {'busy': {'busy': True}, 'task': {'task': 'job-1'}, 'downloading': {'account': {'status': 'running'}},
                              'signing in': {'cloud_auth': {'status': 'running'}},
                              'worker busy': {'status': {'worker': {'reported_state': 'busy'}}}}.items():
            with self.subTest(name):
                self.assertFalse(model.b2(state(**changes))['connect_enabled'])
        checking = model.b2(state(cloud_auth={'status': 'running'}))
        self.assertTrue(checking['cancel_visible'] and checking['cancel_enabled'])
        self.assertFalse(model.b2(state(cloud_auth={'status': 'running'}, busy=True))['cancel_enabled'])
        self.assertEqual(checking['auth'], 'Checking your B2 key and private library bucket…')

    def test_failed_uploads_and_unfinished_test_cleanups_say_how_to_repair_them(self):
        pending = [{'test_id': 'a' * 64, 'provider': 'b2', 'error_code': 'cloud_connection'}, {'test_id': 'b' * 64, 'error_code': 3}]
        settings = model.b2(state(b2(failure_reasons={'cloud_cap': 2, 'cloud_space': 0}), cloud={'targets': [
            b2(failure_reasons={'cloud_cap': 2, 'cloud_space': 0})], 'pending_test_cleanup': pending}))
        self.assertEqual(settings['repair'], f"2 failed uploads: {CLOUD_HINTS['cloud_cap']} Local episodes are retained. These are "
                                             'recorded repair hints; cloud availability has not been rechecked. Test cleanup '
                                             f"pending: {CLOUD_HINTS['cloud_connection']} Local originals are retained.")
        self.assertEqual(settings['cleanup'], [{'test_id': 'a' * 64, 'text': 'Retry Backblaze B2 test cleanup', 'enabled': True},
                                               {'test_id': 'b' * 64, 'text': 'Retry Cloud storage test cleanup', 'enabled': True}])
        self.assertFalse(settings['test_enabled'])
        self.assertEqual(model.b2(state(b2()))['repair'], '')

    def test_test_and_retry_are_offered_only_when_a_cloud_task_could_start(self):
        ready = model.b2(state(b2()))
        self.assertTrue(ready['test_enabled'] and ready['retry_enabled'])
        self.assertFalse(model.b2(state(b2(enabled=False)))['retry_enabled'])
        self.assertFalse(model.b2(state())['test_enabled'])
        busy = model.b2(state(b2(), task='job-1'))
        self.assertFalse(busy['test_enabled'] or busy['retry_enabled'])

    def test_the_cost_appears_once_the_library_size_is_known(self):
        settings = model.b2(state(status=dict(READY, estimate={'saved_bytes': 5_000_000_000})))
        self.assertEqual(settings['cost_short'], 'free, your library fits in the free 10 GB')
        self.assertTrue(settings['cost'].startswith('Your library should take about 5.0 GB'))


class OverviewTests(unittest.TestCase):
    LOCATIONS = [{'id': 'device', 'label': 'This device', 'episodes': 60, 'files': 120, 'connected': True},
                 {'id': 'b2', 'label': 'Backblaze B2', 'episodes': 10, 'files': 0, 'connected': False}]

    def test_the_library_card_counts_dashes_until_the_files_are_counted(self):
        self.assertEqual(model.library_overview(None, True), {'metrics': [('—', 'Saved episodes'), ('—', 'Saved files')],
                                                              'locations': [], 'summary': 'Counting your saved files…'})

    def test_without_cloud_storage_only_this_computer_is_counted(self):
        overview = model.library_overview({'episode_count': 70, 'file_count': 140, 'locations': self.LOCATIONS}, False)
        self.assertEqual(overview['metrics'], [('60', 'Saved episodes'), ('120', 'Saved files')])
        self.assertEqual(overview['locations'], [{'id': 'device', 'label': 'This device', 'counts': '60 episodes · 120 files'}])
        self.assertEqual(overview['summary'], '60 saved episodes on this computer. View saved files for the complete list.')

    def test_with_cloud_storage_every_place_and_the_cloud_copies_are_counted(self):
        overview = model.library_overview({'episode_count': 70, 'file_count': 140, 'locations': self.LOCATIONS}, True)
        self.assertEqual(overview['metrics'], [('70', 'Saved episodes'), ('140', 'Saved files'), ('60', 'Episodes on this device'),
                                               ('10', 'Cloud episode copies')])
        self.assertEqual(overview['locations'][1]['counts'], '10 episodes · 0 files · Not connected')
        self.assertEqual(overview['summary'], '70 saved episodes. View all files, or choose a storage location below.')
        self.assertEqual(model.library_overview({'locations': []}, True)['metrics'][:3],
                         [('—', 'Saved episodes'), ('—', 'Saved files'), ('—', 'Episodes on this device')])


if __name__ == '__main__':
    unittest.main()
