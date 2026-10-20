"""The window's pages drawn by Qt off-screen and clicked through, against a stand-in for the library.

Nothing here opens a library, signs in anywhere or starts a downloader: the library's calls are answered by FakeActions,
and the window's own steps (sites, folders, the theme, the browser) are recorded by Shell.
"""
import copy
import os
from types import SimpleNamespace
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6 import QtCore, QtGui, QtWidgets  # noqa: E402

from comic_archive import app_actions, library_model, storage_model, window_pages, window_style  # noqa: E402

Q = SimpleNamespace(QtCore=QtCore, QtGui=QtGui, QtWidgets=QtWidgets)
NOW = 1_000_000.0
TASK = 'job-' + 'a' * 24
FOLDER = '/home/someone/Comic Archiver/tapas/Library'
FILES = [{'id': 'a' * 64, 'series': 'Long Series Name', 'title': 'Episode 1', 'filename': 'Episode 001.cbz', 'kind': 'cbz',
          'size': 12_345_678, 'saved_at': '2026-10-01T10:00:00+00:00', 'location': 'device'},
         {'id': 'b' * 64, 'series': 'Long Series Name', 'title': 'Episode 2', 'filename': 'Episode 002.cbz', 'kind': 'cbz',
          'size': 2_345_678, 'saved_at': '2026-10-02T10:00:00+00:00', 'location': 'b2', 'date_source': 'file_modified'}]
LOCATIONS = [{'id': 'device', 'label': 'This device', 'files': 120, 'episodes': 60, 'bytes': 3_000_000_000, 'connected': True},
             {'id': 'b2', 'label': 'Backblaze B2', 'files': 20, 'episodes': 10, 'bytes': 500_000_000, 'connected': True}]
APP = None


def setUpModule():
    global APP
    APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def settle():
    """Let timers set to fire at once (a refresh soon after an action) run."""
    for _ in range(3):
        APP.processEvents()


def pick(combo, data):
    """Choose in a drop-down as a person does: the choice changes, then it is reported."""
    index = combo.findData(data)
    combo.setCurrentIndex(index)
    combo.activated.emit(index)


def type_into(field, text):
    field.setText(text)
    field.textEdited.emit(text)


class FakeActions:
    """The library's calls, answered from plain data; each call is recorded, and any can be made to be refused."""

    def __init__(self):
        self.calls, self.refused, self.answers = [], {}, []
        self.cloud_offered, self.summary_fails, self.task_result, self.total = True, False, None, 2
        self.site_settings = [{'key': 'tapas', 'name': 'Tapas', 'current': True, 'speed': 'fast'},
                              {'key': 'tappytoon', 'name': 'Tappytoon', 'current': False, 'speed': 'slow'}]
        self.reading = {
            'status': {'session_configured': True, 'worker': {'reported_state': 'ready'}, 'episodes_by_status': {'archived': 60}},
            'account': {'status': 'stopped', 'configured': True, 'speed': 'fast'},
            'site_auth': {'status': 'idle'},
            'cloud': {'storage': 'shared', 'pending_test_cleanup': [],
                      'targets': [{'provider': 'b2', 'connected': True, 'tested': True, 'enabled': False,
                                   'bucket': 'comic-library-0123', 'verified': 9, 'pending': 1, 'failed': 0}]},
            'cloud_auth': {'status': 'idle'},
            'network': {'wifi_only': True, 'allowed': True}}

    def record(self, name, *args, **options):
        self.calls.append((name, args, options))
        if name in self.refused:
            raise app_actions.Refused('request_unavailable', self.refused[name])

    def named(self, name):
        return [call for call in self.calls if call[0] == name]

    def context(self):
        self.record('context')
        return {'sites': [{'key': 'tapas', 'name': 'Tapas', 'available': True, 'current': True},
                          {'key': 'tappytoon', 'name': 'Tappytoon', 'available': True, 'current': False},
                          {'key': 'webtoon', 'name': 'Webtoon', 'available': True, 'current': False},
                          {'key': 'later', 'name': 'Elsewhere', 'available': False, 'current': False}],
                'site_settings': copy.deepcopy(self.site_settings), 'library_folder': FOLDER,
                'cloud_offered': self.cloud_offered, 'workspace_id': '0123456789abcdef0123456789abcdef'}

    def poll(self, task=None, network=True, cloud=True, library=False, refresh=False):
        self.record('poll', task=task, network=network, cloud=cloud, library=library, refresh=refresh)
        value = copy.deepcopy(self.reading)
        if not cloud:
            del value['cloud'], value['cloud_auth']
        if not network:
            del value['network']
        if library:
            value['summary'] = False if self.summary_fails else {'episode_count': 70, 'file_count': 140, 'locations': LOCATIONS}
        if task is not None and self.task_result is not None:
            value['task'] = self.task_result
        return value

    def account(self, **changes):
        self.reading['account'].update(changes)
        return copy.deepcopy(self.reading['account'])

    def go(self):
        self.record('go')
        self.reading['status']['worker'] = {'reported_state': 'busy'}
        return self.account(status='running', phase='archive')

    def pause(self):
        self.record('pause')
        return self.account(status='stopped')

    def signin(self):
        self.record('signin')
        self.reading['site_auth'] = {'status': 'running'}
        return {'status': 'running'}

    def sign_out(self):
        self.record('sign_out')
        self.reading['status']['session_configured'] = False
        return {'status': 'signed_out'}

    def speed(self, value):
        self.record('speed', value)
        return self.account(speed=value)

    def site_speed(self, key, value):
        self.record('site_speed', key, value)
        for item in self.site_settings:
            if item['key'] == key:
                item['speed'] = value
        return copy.deepcopy(self.site_settings)

    def wifi_only(self, value):
        self.record('wifi_only', value)
        self.reading['network'] = {'wifi_only': value, 'allowed': True}

    def choose_storage(self, chosen, targets):
        self.record('choose_storage', chosen, targets)
        for target in self.reading['cloud']['targets']:
            target['enabled'] = target['provider'] == chosen
        return copy.deepcopy(self.reading['cloud'])

    def cloud_storage(self, mode):
        self.record('cloud_storage', mode)
        self.reading['cloud']['storage'] = mode
        return copy.deepcopy(self.reading['cloud'])

    def cloud_signin(self, provider, credentials):
        self.record('cloud_signin', provider, credentials)
        self.reading['cloud_auth'] = {'status': 'connected', 'provider': provider}
        return {'status': 'running', 'provider': provider}

    def cloud_cancel(self):
        self.record('cloud_cancel')
        self.reading['cloud_auth'] = {'status': 'cancelled'}
        return {'status': 'cancelled'}

    def cloud_test(self):
        self.record('cloud_test')
        return {'task_id': TASK}

    def cloud_upload(self):
        self.record('cloud_upload')
        return {'task_id': TASK}

    def cloud_cleanup(self, test_id):
        self.record('cloud_cleanup', test_id)
        return {'task_id': TASK}

    def saved_files(self, **options):
        self.record('saved_files', **options)
        if self.answers:
            return self.answers.pop(0)
        return {'files': copy.deepcopy(FILES), 'total': self.total, 'offset': options['offset'], 'limit': options['limit'],
                'file_count': 140, 'episode_count': 70, 'locations': LOCATIONS}

    def open_file(self, export_id):
        self.record('open_file', export_id)
        return {}


class Shell:
    """The window around the pages: runs work at once (or holds it, to look at the pages while it runs) and records the
    window's own steps."""

    def __init__(self):
        self.calls, self.held, self.is_dark, self.answer = [], None, False, True

    def run(self, work, done):
        if self.held is not None:
            self.held.append((work, done))
        else:
            self.finish(work, done)

    @staticmethod
    def finish(work, done):
        try:
            value = work()
        except app_actions.Refused as refused:
            done(None, str(refused))
            return
        done(value, None)

    def release(self, order=None):
        held, self.held = self.held, None
        for index in order or range(len(held)):
            self.finish(*held[index])

    def dark(self):
        return self.is_dark

    def toggle_theme(self):
        self.calls.append(('toggle_theme',))
        self.is_dark = not self.is_dark

    def open_url(self, url):
        self.calls.append(('open_url', url))

    def open_folder(self):
        self.calls.append(('open_folder',))

    def choose_site(self, key):
        self.calls.append(('choose_site', key))

    def choose_location(self):
        self.calls.append(('choose_location',))

    def ask(self, text):
        self.calls.append(('ask', text))
        return self.answer


class PagesCase(unittest.TestCase):
    def setUp(self):
        self.actions, self.shell, self.clock = FakeActions(), Shell(), [NOW]
        self.window = QtWidgets.QMainWindow()
        self.pages = window_pages.Pages(Q, self.shell, self.actions, now=lambda: self.clock[0])
        self.window.setCentralWidget(self.pages.widget)
        self.window.resize(1180, 900)
        self.window.show()
        self.addCleanup(self.close)

    def close(self):
        self.window.close()
        self.window.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)

    def open(self, route=None):
        self.pages.reopened()
        if route is not None:
            self.pages.go_to(route)

    @property
    def dashboard(self):
        return self.pages.dashboard

    @property
    def settings(self):
        return self.pages.settings

    @property
    def library(self):
        return self.pages.library


class HelperTests(unittest.TestCase):
    def test_routes_lead_to_a_page_that_exists_and_a_tab_that_is_offered(self):
        self.assertEqual(window_pages.route_of('settings', True), 'settings/downloads')
        self.assertEqual(window_pages.route_of('settings/b2', True), 'settings/b2')
        self.assertEqual(window_pages.route_of('settings/b2', False), 'settings/downloads')
        self.assertEqual(window_pages.route_of('library', False), 'library')
        self.assertEqual(window_pages.route_of('https://example.com', True), 'dashboard')

    def test_lists_become_lines_and_keys_lose_stray_spaces(self):
        self.assertEqual(window_pages.flat('<ul><li>One</li><li>Two</li></ul>After'),
                         '<br>&nbsp;&nbsp;•&nbsp;One<br>&nbsp;&nbsp;•&nbsp;Two<br>After')
        self.assertEqual(window_pages.clean_key(' K001 abc\n\tdef '), 'K001abcdef')

    def test_rich_text_has_its_lines_spaced_for_reading(self):
        self.assertEqual(window_pages.prose('<ul><li>One</li></ul>'),
                         f'<div style="line-height: {window_style.LINE_HEIGHT}%"><br>&nbsp;&nbsp;•&nbsp;One<br></div>')

    def test_painting_sets_the_style_sheet_and_the_colour_of_links(self):
        setUpModule()
        window = QtWidgets.QWidget()
        self.addCleanup(window.deleteLater)
        for dark in (True, False):
            window_pages.paint(Q, window, dark)
            self.assertEqual(window.styleSheet(), window_style.stylesheet(dark))
            # On the application's palette, which the style sheet builds every widget's own from.
            for role in (QtGui.QPalette.ColorRole.Link, QtGui.QPalette.ColorRole.LinkVisited):
                self.assertEqual(APP.palette().color(role).name(), window_style.colours(dark)['accent'])


class DashboardTests(PagesCase):
    def test_the_dashboard_shows_the_library_once_it_is_read_and_says_it_is_drawn(self):
        self.assertFalse(self.pages.drawn())
        answers = []
        self.pages.reopened(answers.append)
        self.assertEqual(answers, [True])
        self.assertTrue(self.pages.drawn())
        self.assertEqual([call[0] for call in self.actions.calls], ['context', 'poll'])
        self.assertTrue(self.actions.named('poll')[0][2]['library'])
        d = self.dashboard
        self.assertEqual((d.title.text(), d.pill.text()), ('Your download is paused', 'Paused'))
        self.assertEqual(d.pill.property('tone'), 'warning')
        self.assertEqual(d.status.text(), 'Paused. Hit Go to continue from saved progress.')
        self.assertEqual((d.session.text(), d.worker.text()), ('Tapas · Session saved', 'Downloader · Ready'))
        self.assertTrue(d.go.isEnabled() and d.go.isVisible())
        self.assertEqual(d.go.text(), 'Go — continue')
        self.assertFalse(d.pause.isVisible() or d.signin.isVisible() or d.allow.isVisible() or d.live.isVisible())
        self.assertIn(FOLDER, d.save.text())
        self.assertEqual(d.updated.text(), 'Updated just now')
        self.assertEqual([cell[0] for cell in d.metric_cells],
                         ['Saved episodes', 'Saved files', 'Episodes on this device', 'Cloud episode copies'])
        self.assertEqual([number.text() for _, number in d.metric_cells], ['70', '140', '60', '10'])
        self.assertEqual(d.library_summary.text(), '70 saved episodes. View all files, or choose a storage location below.')
        self.assertEqual([b.text() for b in d.place_buttons], ['This device\n60 episodes · 120 files', 'Backblaze B2\n10 episodes · 20 files'])

    def test_go_pause_and_sign_in_ask_the_library_and_show_its_answer(self):
        self.open()
        d = self.dashboard
        d.go.click()
        self.assertEqual(len(self.actions.named('go')), 1)
        self.assertTrue(d.pause.isVisible() and d.live.isVisible())
        self.assertEqual((d.go.text(), d.go.isEnabled()), ('Working…', False))
        self.assertEqual(d.status.text(), 'Downloading your episodes…')
        polls = len(self.actions.named('poll'))
        d.pause.click()
        self.assertEqual(len(self.actions.named('pause')), 1)
        self.assertFalse(d.pause.isVisible())
        settle()
        self.assertEqual(len(self.actions.named('poll')), polls + 1)

    def test_signing_in_is_offered_when_needed_and_says_where_to_finish(self):
        self.actions.reading['status']['session_configured'] = False
        self.open()
        d = self.dashboard
        self.assertTrue(d.signin.isVisible() and d.signin.isEnabled())
        self.assertEqual((d.signin.text(), d.status.text()), ('Sign in to Tapas', 'Sign in to Tapas to get started.'))
        self.assertFalse(d.go.isEnabled())
        d.signin.click()
        self.assertEqual(len(self.actions.named('signin')), 1)
        self.assertEqual((d.signin.text(), d.signin.isEnabled()), ('Signing in…', False))
        self.assertEqual(d.status.text(), 'Finish signing in to Tapas in the window that opened.')

    def test_a_refusal_is_said_on_the_dashboard_and_in_settings_until_the_next_action(self):
        self.open()
        self.actions.refused['go'] = 'An import is active. Try again after this batch finishes.'
        self.dashboard.go.click()
        status = self.dashboard.signin_status
        self.assertEqual((status.text(), status.property('error')), ('An import is active. Try again after this batch finishes.', 'true'))
        self.assertTrue(self.dashboard.signin.isVisible())
        self.pages.go_to('settings')
        self.assertEqual(self.settings.status.text(), 'An import is active. Try again after this batch finishes.')
        del self.actions.refused['go']
        self.pages.go()
        self.assertIsNone(self.pages.state.message)
        self.assertFalse(self.settings.status.isVisible())

    def test_while_an_action_runs_the_buttons_wait_and_nothing_else_starts(self):
        self.open()
        self.shell.held = []
        self.dashboard.go.click()
        self.assertFalse(self.dashboard.go.isEnabled() or self.dashboard.files.isEnabled())
        self.dashboard.go.click()
        self.pages.go()
        self.pages.poll()
        self.pages.cloud_test()
        self.assertEqual(len(self.shell.held), 1)
        self.shell.release()
        self.assertEqual(len(self.actions.named('go')), 1)
        self.assertTrue(self.dashboard.files.isEnabled())

    def test_a_library_whose_sites_cannot_be_read_is_still_refreshed(self):
        self.actions.refused['context'] = 'The service is temporarily unavailable. Try again shortly.'
        answers = []
        self.pages.reopened(answers.append)
        self.assertEqual((answers, [call[0] for call in self.actions.calls]), ([True], ['context', 'poll']))
        self.assertFalse(self.dashboard.site_row.isVisible())
        self.assertEqual(self.dashboard.title.text(), 'Your download is paused')

    def test_a_failed_refresh_says_it_will_retry_and_the_next_good_one_clears_it(self):
        self.open()
        self.actions.refused['poll'] = 'The service is temporarily unavailable. Try again shortly.'
        answers = []
        self.pages.poll(answers.append)
        self.assertEqual(answers, [False])
        self.assertTrue(self.dashboard.signin_status.text().startswith('Could not refresh progress.'))
        self.assertEqual(self.dashboard.updated.text(), 'Updates delayed · trying to reconnect')
        self.assertEqual(self.dashboard.pill.text(), 'Reconnecting')
        del self.actions.refused['poll']
        self.clock[0] += 5
        self.pages.poll()
        self.assertFalse(self.dashboard.signin_status.isVisible())
        self.assertEqual(self.dashboard.pill.text(), 'Paused')

    def test_a_refresh_for_a_library_that_was_replaced_meanwhile_is_dropped(self):
        self.open()
        first = self.pages.state
        self.shell.held = []
        self.pages.poll()
        self.pages.reopened()
        self.clock[0] += 10
        self.shell.release()
        self.assertEqual(first.refreshed_at, NOW)
        self.assertIsNot(self.pages.state, first)
        self.assertTrue(self.pages.state.loaded and self.pages.drawn())
        self.assertEqual(self.pages.state.refreshed_at, NOW + 10)

    def test_the_connection_can_be_allowed_from_the_dashboard_when_saving_waits_for_it(self):
        self.actions.reading['network'] = {'wifi_only': True, 'allowed': False, 'reason': 'metered_connection'}
        self.open()
        d = self.dashboard
        self.assertTrue(d.allow.isVisible() and d.allow.isEnabled())
        self.assertTrue(d.status.text().startswith('Saving would wait: this connection is metered'))
        d.allow.click()
        self.assertEqual(self.actions.named('wifi_only')[0][1], (False,))
        self.assertFalse(d.allow.isVisible())
        self.assertEqual(self.pages.state.network, {'wifi_only': False, 'allowed': True})

    def test_the_site_list_asks_the_window_to_switch_and_waits_while_saving(self):
        self.open()
        site = self.dashboard.site
        self.assertEqual([site.itemText(i) for i in range(site.count())], ['Tapas', 'Tappytoon', 'Webtoon', 'Elsewhere — coming later'])
        self.assertTrue(site.model().item(2).isEnabled()); self.assertFalse(site.model().item(3).isEnabled())
        site.activated.emit(1)
        self.assertEqual(self.shell.calls, [('choose_site', 'tappytoon')])
        self.assertEqual(site.currentData(), 'tapas')
        self.dashboard.go.click()
        self.assertFalse(site.isEnabled())
        self.assertEqual(site.toolTip(), 'Pause saving to change the site.')

    def test_the_library_card_leads_to_the_saved_files_and_says_when_it_cannot_count(self):
        self.open()
        cells = [number for _, number in self.dashboard.metric_cells]
        places = list(self.dashboard.place_buttons)
        self.clock[0] += 31
        self.pages.poll()
        self.assertTrue(self.actions.named('poll')[-1][2]['library'])
        self.assertEqual([number for _, number in self.dashboard.metric_cells], cells)
        self.assertEqual(self.dashboard.place_buttons, places)
        places[1].click()
        self.assertEqual(self.pages.route, 'library')
        self.assertEqual(self.actions.named('saved_files')[-1][2]['location'], 'b2')
        self.pages.go_to('dashboard')
        self.dashboard.files.click()
        self.assertEqual(self.actions.named('saved_files')[-1][2]['location'], 'all')
        self.actions.summary_fails = True
        self.pages.go_to('dashboard')
        self.clock[0] += 31
        self.pages.poll()
        self.assertEqual(self.dashboard.library_summary.text(), storage_model.LIBRARY_UNAVAILABLE)

    def test_links_open_only_backblazes_own_pages_in_the_browser(self):
        self.open()
        self.pages.link('billing')
        self.assertEqual(self.shell.calls, [('open_url', window_pages.B2_PAGES['billing'])])
        self.dashboard.cloud_entry.linkActivated.emit('settings/b2')
        self.assertEqual(self.pages.route, 'settings/b2')
        self.pages.link('https://example.com/elsewhere')
        self.assertEqual(self.pages.route, 'dashboard')
        self.assertEqual(len(self.shell.calls), 1)
        self.dashboard.save.linkActivated.emit('settings/storage')
        self.assertEqual(self.pages.route, 'settings/storage')
        self.settings.b2_open.click()
        self.assertEqual(self.shell.calls[-1], ('open_url', window_pages.B2_PAGES['browse']))

    def test_the_theme_switch_shows_where_it_leads(self):
        self.open()
        theme = self.pages.header.theme
        self.assertEqual((theme.text(), theme.toolTip()), ('☾', 'Switch to dark mode'))
        theme.click()
        self.assertEqual(self.shell.calls, [('toggle_theme',)])
        self.assertEqual((theme.text(), theme.accessibleName()), ('☀', 'Switch to light mode'))


class NavigationTests(PagesCase):
    def test_the_header_and_the_tabs_lead_to_each_page(self):
        self.open()
        nav = self.pages.header.nav
        nav['library'].click()
        self.assertEqual(self.pages.stack.currentWidget(), self.library.widget)
        self.assertTrue(nav['library'].isChecked())
        nav['settings'].click()
        self.assertEqual(self.pages.route, 'settings/downloads')
        self.assertEqual(self.pages.stack.currentWidget(), self.settings.widget)
        self.assertTrue(self.settings.tabs['downloads'].isChecked() and nav['settings'].isChecked())
        self.assertTrue(self.settings.panels['downloads'].isVisible())
        self.assertFalse(self.settings.panels['b2'].isVisible())
        self.settings.tabs['b2'].click()
        self.assertEqual(self.pages.route, 'settings/b2')
        self.assertTrue(self.settings.panels['b2'].isVisible() and not self.settings.panels['downloads'].isVisible())
        nav['dashboard'].click()
        self.assertEqual(self.pages.stack.currentWidget(), self.dashboard.widget)

    def test_without_cloud_storage_there_is_no_backblaze_tab_card_or_offer(self):
        self.actions.cloud_offered = False
        self.open('settings/b2')
        self.assertEqual(self.pages.route, 'settings/downloads')
        self.assertFalse(self.settings.tabs['b2'].isVisible())
        self.assertFalse(self.actions.named('poll')[0][2]['cloud'])
        self.pages.go_to('settings/storage')
        self.assertFalse(self.settings.b2_card.isVisible() or self.settings.choice.isVisible())
        self.pages.go_to('dashboard')
        self.assertFalse(self.dashboard.cloud_entry.isVisible())
        self.assertEqual([cell[0] for cell in self.dashboard.metric_cells], ['Saved episodes', 'Saved files'])
        self.assertIn(FOLDER, self.dashboard.save.text())

    def test_cloud_storage_is_offered_on_the_dashboard_until_one_is_set_up(self):
        self.actions.reading['cloud']['targets'] = []
        self.open()
        self.assertTrue(self.dashboard.cloud_entry.isVisible())


class DownloadSettingsTests(PagesCase):
    def test_speeds_are_chosen_for_the_open_site_and_each_other_one(self):
        self.open('settings/downloads')
        s = self.settings
        self.assertEqual((s.speed_label.text(), s.speed.currentData()), ('Speed on Tapas', 'fast'))
        self.assertFalse(s.signin_note.isVisible())
        pick(s.speed, 'medium')
        self.assertEqual(self.actions.named('speed')[0][1], ('medium',))
        self.assertEqual((s.speed.currentData(), s.speed_help.text()), ('medium', 'About four images a second.'))
        _, label, combo, help_text = s.speed_rows['tappytoon']
        self.assertEqual((label.text(), combo.currentData(), help_text.text()),
                         ('Speed on Tappytoon', 'slow', 'Used the next time you hit Go on Tappytoon.'))
        pick(combo, 'fast')
        self.assertEqual(self.actions.named('site_speed')[0][1], ('tappytoon', 'fast'))
        self.assertEqual(self.pages.state.site_settings[1]['speed'], 'fast')
        self.actions.site_settings = self.actions.site_settings[:1]
        self.pages.reopened()
        self.assertEqual(list(s.speed_rows), [])
        self.assertEqual(s.speed_label.text(), 'Speed')

    def test_sign_out_asks_first_then_forgets_the_sign_in_and_offers_sign_in_again(self):
        self.open('settings/downloads')
        s = self.settings
        self.assertTrue(s.signout_row.isVisible() and s.signout.isEnabled())
        self.assertEqual(s.signout.text(), 'Sign out of Tapas')
        self.assertIn('Signing out removes it from Comic Archiver only.', s.signout_help.text())
        question = ('ask', window_pages.SIGN_OUT.format(site='Tapas'))
        self.assertIn('This signs out only Comic Archiver, on this computer.', question[1])
        self.shell.answer = False
        s.signout.click()
        self.assertEqual((self.shell.calls, self.actions.named('sign_out')), ([question], []))
        self.shell.answer = True
        s.signout.click()
        self.assertEqual(self.shell.calls, [question, question])
        self.assertEqual(len(self.actions.named('sign_out')), 1)
        # Said at once, before the library is read again.
        self.assertFalse(self.pages.state.status['session_configured'])
        self.assertEqual(self.pages.state.site_auth, {'status': 'idle'})
        self.assertFalse(s.signout_row.isVisible())
        polls = len(self.actions.named('poll'))
        settle()
        self.assertGreater(len(self.actions.named('poll')), polls)
        self.assertFalse(s.signout_row.isVisible())
        self.pages.go_to('dashboard')
        self.assertTrue(self.dashboard.signin.isVisible() and self.dashboard.signin.isEnabled())

    def test_sign_out_waits_for_a_download_and_a_refusal_is_said(self):
        self.actions.reading['account'] = {'status': 'running', 'configured': True, 'phase': 'archive', 'speed': 'fast'}
        self.open('settings/downloads')
        s = self.settings
        self.assertFalse(s.signout.isEnabled())
        self.assertEqual(s.signout_help.text(), 'Pause the download to sign out.')
        self.actions.reading['account']['status'] = 'stopped'
        self.actions.refused['sign_out'] = 'Pause the download first.'
        self.open('settings/downloads')
        s.signout.click()
        self.assertEqual(len(self.actions.named('sign_out')), 1)
        self.assertTrue(self.pages.state.status['session_configured'])
        self.assertTrue(s.signout_row.isVisible())
        self.assertIn('Pause the download first.', s.status.text())
        # While another change runs, nothing is asked.
        self.pages.state.busy = True
        self.shell.calls.clear()
        self.pages.sign_out()
        self.assertEqual(self.shell.calls, [])

    def test_another_site_with_no_speed_saved_shows_the_first_one(self):
        self.actions.site_settings[1]['speed'] = None
        self.open('settings/downloads')
        _, _, combo, _ = self.settings.speed_rows['tappytoon']
        self.assertEqual(combo.currentData(), combo.itemData(0))

    def test_the_speed_waits_while_saving_and_a_signed_out_site_has_none(self):
        self.actions.reading['account']['status'] = 'running'
        self.open('settings/downloads')
        self.assertFalse(self.settings.speed.isEnabled())
        self.assertEqual(self.settings.speed_help.text(), 'Pause to change the speed.')
        self.actions.reading['status']['session_configured'] = False
        self.actions.site_settings = self.actions.site_settings[:1]
        self.pages.reopened()
        self.assertTrue(self.settings.signin_note.isVisible())
        self.assertFalse(self.settings.speed_row.isVisible())

    def test_the_connection_choice_is_saved_and_then_shown_as_saved(self):
        self.open('settings/downloads')
        network = self.settings.network
        self.assertFalse(network.isChecked())
        self.assertTrue(self.settings.network_help.text().startswith('Off: saving uses Wi-Fi'))
        network.click()
        self.assertEqual(self.actions.named('wifi_only')[0][1], (False,))
        self.assertEqual(self.actions.named('poll')[-1][2], {'task': None, 'network': True, 'cloud': False, 'library': False,
                                                            'refresh': False})
        self.assertTrue(network.isChecked())
        self.assertFalse(self.pages.state.network_saving)
        self.assertTrue(self.settings.network_help.text().startswith('Saving uses any connection'))

    def test_a_connection_choice_made_while_another_action_runs_is_put_back(self):
        self.open('settings/downloads')
        self.shell.held = []
        self.pages.go()
        self.settings.network.setChecked(True)
        self.assertFalse(self.settings.network.isChecked())
        self.assertEqual(self.actions.named('wifi_only'), [])

    def test_a_refused_connection_choice_shows_the_saved_one_again(self):
        self.open('settings/downloads')
        self.actions.refused['wifi_only'] = 'This action is unavailable. Check the connection and selected library.'
        self.settings.network.click()
        self.assertFalse(self.settings.network.isChecked())
        self.assertEqual(self.settings.status.text(), 'This action is unavailable. Check the connection and selected library.')


class StorageSettingsTests(PagesCase):
    def test_storage_is_chosen_among_the_places_that_passed_their_check(self):
        self.open('settings/storage')
        s = self.settings
        self.assertEqual([b.text() for b in s.option_buttons], ['This device', 'Backblaze B2'])
        self.assertTrue(s.option_buttons[0].isChecked() and s.option_buttons[1].isEnabled())
        self.assertEqual(s.choice_help.text(), 'New episodes stay in the save folder.')
        s.option_buttons[1].click()
        chosen, targets = self.actions.named('choose_storage')[0][1]
        self.assertEqual((chosen, [t['provider'] for t in targets]), ('b2', ['b2']))
        self.assertTrue(s.option_buttons[1].isChecked())
        self.assertTrue(s.choice_help.text().startswith('Each new episode is uploaded to Backblaze B2'))
        self.assertIn('Backblaze B2', self.dashboard.save.text())

    def test_a_refused_storage_choice_says_why_and_shows_the_kept_one_again(self):
        self.open('settings/storage')
        s = self.settings
        self.actions.refused['choose_storage'] = 'This action is unavailable. Check the connection and selected library.'
        s.option_buttons[1].click()
        self.assertEqual((s.choice_status.text(), s.choice_status.property('error')),
                         ('This action is unavailable. Check the connection and selected library.', 'true'))
        polls = len(self.actions.named('poll'))
        settle()
        self.assertEqual(len(self.actions.named('poll')), polls + 1)
        self.assertTrue(s.option_buttons[0].isChecked())

    def test_the_storage_choice_is_locked_while_saving_and_says_why(self):
        self.actions.reading['account']['status'] = 'running'
        self.open('settings/storage')
        s = self.settings
        self.assertFalse(any(b.isEnabled() for b in s.option_buttons))
        self.assertTrue(s.choice_lock.text().endswith('Press Pause on the dashboard first.'))
        self.assertFalse(s.change_folder.isEnabled())
        self.assertEqual(s.folder_lock.text(), 'The folder can’t be changed while episodes are downloading. '
                                               'Press Pause on the dashboard first.')

    def test_a_choice_made_while_another_action_runs_waits(self):
        self.open('settings/storage')
        self.shell.held = []
        self.pages.go()
        self.pages.choose_storage('b2')
        self.assertEqual(len(self.shell.held), 1)

    def test_the_folder_is_shown_opened_and_changed_through_the_window(self):
        self.open('settings/storage')
        s = self.settings
        self.assertIn(FOLDER, s.folder.text())
        self.assertEqual(s.device_stored.text(), '60 episodes stored · 3.0 GB')
        self.assertEqual((s.b2_pill.text(), s.b2_stored.text(), s.b2_setup.text()), ('Set up', '10 episodes stored · 500 MB', 'B2 settings'))
        s.open_folder.click()
        s.change_folder.click()
        self.assertEqual(self.shell.calls, [('open_folder',), ('choose_location',)])
        s.b2_setup.click()
        self.assertEqual(self.pages.route, 'settings/b2')


class B2SettingsTests(PagesCase):
    def setUp(self):
        super().setUp()
        self.actions.reading['cloud']['targets'] = []

    def test_a_wide_window_is_filled_and_the_steps_keep_a_width_that_reads_easily(self):
        self.window.resize(1900, 900)
        self.open('settings/b2')
        settle()
        s = self.settings
        self.assertGreater(s.panels['b2'].width(), window_style.READING + 400)
        for part in (s.step3, s.bucket, s.key_id, s.application_key, s.cloud_status):
            self.assertEqual(part.maximumWidth(), window_style.READING)
            self.assertLessEqual(part.width(), window_style.READING)
        # A state box's words sit beside its label and take what the box has.
        words = [label for label in s.b2_state.findChildren(QtWidgets.QLabel) if label.wordWrap()]
        self.assertTrue(words)
        self.assertTrue(all(label.maximumWidth() > window_style.READING for label in words))

    def test_a_mistyped_key_is_named_before_anything_is_sent(self):
        self.open('settings/b2')
        s = self.settings
        self.assertEqual(s.bucket.text(), 'comic-library-0123456789ab')
        self.assertIn('comic-library-0123456789ab', s.step3.text())
        self.assertTrue(s.setup.isVisible() and not s.fold_row.isVisible())
        type_into(s.bucket, 'bad')
        s.connect.click()
        self.assertEqual(s.action_status.text(), 'Type a bucket name of 6 to 63 letters, numbers or hyphens, as in step 2.')
        self.assertEqual(s.action_status.property('error'), 'true')
        self.assertEqual(s.bucket.text(), 'bad')
        type_into(s.bucket, 'my-comics-bucket')
        type_into(s.key_id, 'K0012345abcd')
        type_into(s.application_key, 'short')
        s.connect.click()
        self.assertEqual(s.action_status.text(), 'Paste the keyID and applicationKey exactly as Backblaze shows them.')
        self.assertEqual(self.actions.named('cloud_signin'), [])

    def test_a_key_that_connects_is_sent_once_and_then_forgotten(self):
        self.open('settings/b2')
        s = self.settings
        self.assertEqual(s.application_key.echoMode(), QtWidgets.QLineEdit.EchoMode.Password)
        type_into(s.bucket, 'my-comics-bucket')
        type_into(s.key_id, 'K0012345abcd')
        type_into(s.application_key, 'K001secretsecret')
        s.application_key.returnPressed.emit()
        self.assertEqual(self.actions.named('cloud_signin')[0][1],
                         ('b2', {'key_id': 'K0012345abcd', 'application_key': 'K001secretsecret', 'bucket': 'my-comics-bucket'}))
        self.assertEqual(s.action_status.text(), storage_model.CONNECTING)
        self.assertTrue(s.cancel.isVisible())
        self.assertFalse(s.connect.isEnabled() or s.key_id.isEnabled())
        self.assertEqual(s.key_id.text(), 'K0012345abcd')
        settle()
        self.assertEqual((s.key_id.text(), s.application_key.text()), ('', ''))
        self.assertEqual(s.auth.text(), 'Signed in. Next, click Test cloud connection.')
        self.assertIsNone(self.pages.state.bucket_draft)

    def test_a_refused_key_keeps_the_bucket_as_typed_and_says_why(self):
        self.open('settings/b2')
        s = self.settings
        self.actions.refused['cloud_signin'] = 'This action is unavailable. Check the connection and selected library.'
        type_into(s.bucket, 'my-comics-bucket')
        type_into(s.key_id, 'K0012345abcd')
        type_into(s.application_key, 'K001secretsecret')
        s.connect.click()
        self.assertEqual(s.action_status.text(), 'This action is unavailable. Check the connection and selected library.')
        self.assertEqual(s.bucket.text(), 'my-comics-bucket')

    def test_typed_keys_lose_stray_spaces_and_are_forgotten_on_leaving_the_tab(self):
        self.open('settings/b2')
        s = self.settings
        s.key_id.textEdited.emit(' K001 2345abcd\n')
        self.assertEqual(s.key_id.text(), 'K0012345abcd')
        s.bucket.textEdited.emit('my bucket ')
        self.assertEqual((s.bucket.text(), self.pages.state.bucket_draft), ('mybucket', 'mybucket'))
        self.assertIn('<b>mybucket</b>', s.step3.text())
        type_into(s.application_key, 'K001secretsecret')
        self.pages.go_to('dashboard')
        self.assertEqual((s.key_id.text(), s.application_key.text()), ('', ''))

    def test_connecting_waits_while_saving_even_from_the_enter_key(self):
        self.actions.reading['account']['status'] = 'running'
        self.open('settings/b2')
        s = self.settings
        self.assertFalse(s.connect.isEnabled() or s.application_key.isEnabled())
        s.application_key.returnPressed.emit()
        self.assertEqual(self.actions.named('cloud_signin'), [])
        self.assertFalse(s.action_status.isVisible())

    def test_a_key_check_can_be_cancelled(self):
        self.actions.reading['cloud_auth'] = {'status': 'running', 'provider': 'b2'}
        self.open('settings/b2')
        s = self.settings
        self.assertTrue(s.cancel.isVisible())
        self.assertEqual(s.b2_state_pill.text(), 'Checking your key')
        s.cancel.click()
        self.assertEqual(len(self.actions.named('cloud_cancel')), 1)
        self.assertFalse(s.cancel.isVisible())
        self.assertEqual(s.auth.text(), 'Sign-in cancelled. Your saved files are safe.')

    def test_sharing_one_backblaze_setup_is_switched_on_its_tab(self):
        self.open('settings/b2')
        s = self.settings
        self.assertTrue(s.shared.isChecked())
        s.shared.click()
        self.assertEqual(self.actions.named('cloud_storage')[0][1], ('separate',))
        self.assertFalse(s.shared.isChecked())
        self.assertEqual(s.sharing_help.text(), 'Tapas has a Backblaze setup of its own. Your other sites keep the shared one.')

    def test_the_setup_pages_open_in_the_browser(self):
        self.open('settings/b2')
        for button in self.settings.setup.findChildren(QtWidgets.QPushButton):
            if button.text().startswith(('Create B2', 'Open Caps')):
                button.click()
        self.assertEqual(self.shell.calls, [('open_url', window_pages.B2_PAGES['signup']), ('open_url', window_pages.B2_PAGES['caps'])])


class ConnectedB2Tests(PagesCase):
    def setUp(self):
        super().setUp()
        self.actions.reading['cloud']['targets'][0]['enabled'] = True

    def test_a_connected_b2_folds_its_steps_and_shows_its_costs_on_request(self):
        self.open('settings/b2')
        s = self.settings
        self.assertFalse(s.setup.isVisible())
        self.assertTrue(s.fold_row.isVisible())
        self.assertEqual((s.b2_state_pill.text(), s.connect.text()), ('Uploading to B2', 'Replace B2 connection'))
        s.fold.click()
        self.assertTrue(s.setup.isVisible())
        self.assertEqual(s.fold.text(), 'Hide the setup steps')
        self.assertEqual(s.bucket.text(), 'comic-library-0123')
        self.assertTrue(s.costs_toggle.text().startswith('Estimated Backblaze cost: free up to 10 GB'))
        self.assertFalse(s.costs_body.isVisible())
        s.costs_toggle.click()
        self.assertTrue(s.costs_body.isVisible())
        s.costs_toggle.click()
        self.assertFalse(s.costs_body.isVisible())

    def test_the_connection_test_starts_a_task_and_says_how_it_ended(self):
        self.open('settings/b2')
        s = self.settings
        self.assertTrue(s.test.isEnabled() and s.retry.isEnabled())
        s.test.click()
        self.assertEqual(len(self.actions.named('cloud_test')), 1)
        self.assertEqual(s.action_status.text(), storage_model.TASK_STARTED['cloud-test'])
        self.assertFalse(s.test.isEnabled() or s.retry.isEnabled())
        self.actions.task_result = {'status': 'ok', 'result': {'verified_episodes': 2}}
        self.pages.poll()
        self.assertEqual(self.actions.named('poll')[-1][2]['task'], TASK)
        self.assertEqual(s.action_status.text(), '2 episodes verified; test copies deleted. Local originals retained.')
        self.assertIsNone(self.pages.state.task)
        self.assertTrue(s.test.isEnabled())

    def test_a_cloud_task_that_fails_says_why_on_the_tab_and_the_dashboard(self):
        self.open('settings/b2')
        s = self.settings
        s.retry.click()
        self.assertEqual(s.action_status.text(), storage_model.TASK_STARTED['cloud'])
        self.actions.task_result = {'status': 'error', 'code': 'cloud_space'}
        self.pages.poll()
        self.assertTrue(s.action_status.text().startswith('The provider reported insufficient cloud storage.'))
        self.assertEqual(s.action_status.property('error'), 'true')
        self.actions.refused['cloud_upload'] = 'The service is temporarily unavailable. Try again shortly.'
        s.retry.click()
        self.assertEqual(s.action_status.text(), 'The service is temporarily unavailable. Try again shortly.')
        self.assertEqual(self.settings.status.text(), 'The service is temporarily unavailable. Try again shortly.')

    def test_an_unfinished_test_cleanup_is_retried_from_its_own_button(self):
        self.actions.reading['cloud']['pending_test_cleanup'] = [{'test_id': 'c' * 64, 'provider': 'b2', 'error_code': 'cloud_connection'}]
        self.open('settings/b2')
        s = self.settings
        self.assertFalse(s.test.isEnabled())
        self.assertIn('Test cleanup pending:', s.repair.text())
        button, = s.cleanups
        self.assertEqual(button.text(), 'Retry Backblaze B2 test cleanup')
        button.click()
        self.assertEqual(self.actions.named('cloud_cleanup')[0][1], ('c' * 64,))
        self.assertEqual(s.action_status.text(), storage_model.TASK_STARTED['cloud-cleanup'])
        self.actions.reading['cloud']['pending_test_cleanup'] = []
        self.actions.task_result = {'status': 'ok'}
        self.pages.poll()
        self.assertEqual(s.cleanups, [])


class LibraryTests(PagesCase):
    def options(self):
        return self.actions.named('saved_files')[-1][2]

    def test_the_library_lists_a_page_of_saved_files_with_its_places_and_range(self):
        self.open('library')
        self.assertEqual(self.options(), {'location': 'all', 'query': '', 'kind': 'episodes', 'sort': 'saved_desc', 'offset': 0,
                                          'limit': 50, 'refresh': False})
        lib, table = self.library, self.library.table
        self.assertEqual(lib.title.text(), 'Your saved Tapas library')
        self.assertEqual((lib.notice.text(), lib.range.text()), ('Saved files loaded.', 'Showing 1–2 of 2 files.'))
        self.assertEqual(lib.summary.text(), '70 saved episodes across 140 files and cloud copies.')
        self.assertEqual([b.text().split('\n')[0] for b in lib.place_buttons], ['All storage', 'This device', 'Backblaze B2'])
        self.assertTrue(lib.place_buttons[0].isChecked())
        self.assertEqual(table.rowCount(), 2)
        self.assertEqual([table.item(0, c).text() for c in range(7)][:5], ['Long Series Name', 'Episode 1', 'Episode 001.cbz', 'Comic', '11.8 MB'])
        self.assertTrue(table.item(1, 5).text().endswith('\nFile time'))
        self.assertEqual(table.item(1, 5).toolTip(), '2026-10-02T10:00:00+00:00')
        self.assertFalse(table.isColumnHidden(6))
        self.assertIsNotNone(table.cellWidget(0, 7))
        self.assertEqual(table.item(1, 7).text(), 'Cloud copy')
        self.assertEqual((lib.page.value(), lib.page_count.text()), (1, 'of 1'))
        self.assertFalse(lib.previous.isEnabled() or lib.next.isEnabled())

    def test_paging_searching_filtering_and_sorting_ask_for_the_right_files(self):
        self.actions.total = 250
        self.open('library')
        lib = self.library
        self.assertTrue(lib.next.isEnabled() and not lib.previous.isEnabled())
        lib.next.click()
        self.assertEqual(self.options()['offset'], 50)
        self.assertTrue(lib.previous.isEnabled())
        lib.previous.click()
        self.assertEqual(self.options()['offset'], 0)
        lib.page.setValue(4)
        lib.jump.click()
        self.assertEqual(self.options()['offset'], 150)
        self.assertEqual(lib.page.value(), 4)
        lib.search.setText('  Episode  ')
        lib.search.returnPressed.emit()
        self.assertEqual((self.options()['query'], self.options()['offset']), ('Episode', 0))
        pick(lib.kind, 'all')
        pick(lib.sort, 'series')
        self.assertEqual((self.options()['kind'], self.options()['sort']), ('all', 'series'))
        lib.clear.click()
        self.assertEqual((self.options()['query'], self.options()['kind'], self.options()['sort']), ('', 'episodes', 'series'))
        pick(lib.size, 100)
        self.assertEqual((self.options()['limit'], self.options()['offset']), (100, 0))
        lib.refresh.click()
        self.assertTrue(self.options()['refresh'])
        lib.place_buttons[2].click()
        self.assertEqual((self.options()['location'], self.options()['offset']), ('b2', 0))
        lib.find.click()
        self.assertEqual(self.options()['location'], 'b2')

    def test_while_files_load_the_controls_wait_and_a_late_answer_is_dropped(self):
        self.open('library')
        lib = self.library
        self.shell.held = []
        lib.load()
        self.assertEqual(lib.notice.text(), library_model.LOADING)
        self.assertFalse(lib.refresh.isEnabled() or lib.search.isEnabled() or lib.place_buttons[0].isEnabled())
        lib.load()
        self.actions.answers = [{'files': FILES[:1], 'total': 1, 'offset': 0, 'limit': 50, 'file_count': 1, 'locations': []},
                                {'files': [], 'total': 0, 'offset': 0, 'limit': 50, 'file_count': 140, 'locations': []}]
        self.shell.release(order=[1, 0])
        self.assertEqual((lib.notice.text(), lib.table.rowCount()), ('Saved files loaded.', 1))
        self.assertTrue(lib.refresh.isEnabled())

    def test_a_library_that_could_not_be_read_says_so(self):
        self.actions.refused['saved_files'] = library_model.UNAVAILABLE
        self.open('library')
        lib = self.library
        self.assertEqual((lib.notice.text(), lib.notice.property('error')), (library_model.UNAVAILABLE, 'true'))
        self.assertEqual((lib.table.rowCount(), lib.range.text()), (0, ''))
        self.assertTrue(lib.refresh.isEnabled())

    def test_opening_a_file_says_it_was_sent_or_why_it_could_not_be(self):
        self.open('library')
        holder = self.library.table.cellWidget(0, 7)
        button, feedback = holder.findChild(QtWidgets.QPushButton), holder.findChild(QtWidgets.QLabel)
        self.assertEqual(button.accessibleName(), 'Open Episode 001.cbz')
        self.shell.held = []
        button.click()
        self.assertEqual((button.text(), button.isEnabled(), feedback.text()), ('Opening…', False, library_model.OPENING))
        self.shell.release()
        self.assertEqual(self.actions.named('open_file')[0][1], ('a' * 64,))
        self.assertEqual((button.text(), button.isEnabled(), feedback.text()), ('Open', True, library_model.OPENED))
        self.assertEqual(feedback.property('error'), 'false')
        self.actions.refused['open_file'] = library_model.NO_OPENER
        button.click()
        self.assertEqual((feedback.text(), feedback.property('error')), (library_model.NO_OPENER, 'true'))

    def test_without_cloud_storage_the_library_shows_this_computer_only(self):
        self.actions.cloud_offered = False
        self.open('library')
        lib = self.library
        self.assertEqual(self.options()['location'], 'device')
        self.assertEqual([b.text().split('\n')[0] for b in lib.place_buttons], ['This device'])
        self.assertTrue(lib.table.isColumnHidden(6))
        self.assertEqual(lib.summary.text(), '60 saved episodes across 120 files on this computer.')
        lib.open_folder.click()
        self.assertEqual(self.shell.calls, [('open_folder',)])

    def test_returning_to_the_library_keeps_its_place_and_reopening_starts_at_all_storage(self):
        self.open('library')
        self.library.place_buttons[2].click()
        self.pages.go_to('dashboard')
        self.pages.go_to('library')
        self.assertEqual(self.options()['location'], 'b2')
        self.pages.reopened()
        self.assertEqual(self.pages.route, 'library')
        self.assertEqual(self.options()['location'], 'all')
        self.assertTrue(self.pages.state.loaded)


if __name__ == '__main__':
    unittest.main()
