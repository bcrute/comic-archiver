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

from comic_archive import app_actions, dashboard_model, storage_model, window_pages, window_style  # noqa: E402

Q = SimpleNamespace(QtCore=QtCore, QtGui=QtGui, QtWidgets=QtWidgets)
NOW = 1_000_000.0
TASK = 'job-' + 'a' * 24
FOLDER = '/home/someone/Comic Archiver/tapas/Library'
LOCATIONS = [{'id': 'device', 'label': 'This device', 'files': 120, 'episodes': 60, 'bytes': 3_000_000_000, 'connected': True},
             {'id': 'b2', 'label': 'Backblaze B2', 'files': 20, 'episodes': 10, 'bytes': 500_000_000, 'connected': True}]
SITES = {'tapas': 'Tapas', 'tappytoon': 'Tappytoon', 'webtoon': 'Webtoon', 'later': 'Elsewhere'}
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

    def __init__(self, site='tapas'):
        self.site = site
        self.calls, self.refused, self.kept = [], {}, []
        self.cloud_offered, self.summary_fails, self.task_result = True, False, None
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
        return {'sites': [{'key': key, 'name': name, 'available': key != 'later', 'current': key == self.site}
                          for key, name in SITES.items()],
                'library_folder': FOLDER,
                'cloud_offered': self.cloud_offered, 'workspace_id': '0123456789abcdef0123456789abcdef'}

    def support_records(self):
        self.record('support_records')
        return (f'=== {SITES[self.site]}: episodes not saved\nseries 11 episode 22: failed job_failed\n'
                f'This step stopped in {FOLDER}/x.cbz', FOLDER.rsplit('/', 2)[0])

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
        return {'status': 'signed_out', 'kept': self.kept}

    def speed(self, value):
        self.record('speed', value)
        return self.account(speed=value)

    def wifi_only(self, value):
        self.record('wifi_only', value)
        self.reading['network'] = {'wifi_only': value, 'allowed': True}

    def choose_storage(self, chosen, targets):
        self.record('choose_storage', chosen, targets)
        for target in self.reading['cloud']['targets']:
            target['enabled'] = target['provider'] == chosen
        return copy.deepcopy(self.reading['cloud'])

    def cloud_confirm(self, provider, use):
        self.record('cloud_confirm', provider, use)
        for target in self.reading['cloud']['targets']:
            target.update(confirmed=True, enabled=target['enabled'] and use)
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
        if options.get('summary'):
            if self.summary_fails:
                raise app_actions.Refused('request_unavailable', 'Counts unavailable')
            return {'episode_count': 70, 'file_count': 140, 'locations': LOCATIONS}
        raise AssertionError('The dashboard requests summary counts only')


class Shell:
    """The window around the pages: runs work at once (or holds it, to look at the pages while it runs) and records the
    window's own steps."""

    def __init__(self):
        self.calls, self.held, self.is_dark, self.answer = [], None, False, True
        self.open, self.chosen, self.opens = ['tapas'], 'tapas', False

    def open_sites(self):
        return list(self.open)

    def chosen_site(self):
        return self.chosen

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

    def choose_site(self, key, then=None):
        """Recorded; with opens set, the site opens and is chosen as the window's does, then then() follows."""
        self.calls.append(('choose_site', key))
        if self.opens:
            self.chosen = key
            if key not in self.open:
                self.open.append(key)
            if then is not None:
                then()

    def choose_location(self):
        self.calls.append(('choose_location',))

    def ask(self, text):
        self.calls.append(('ask', text))
        return self.answer


class PagesCase(unittest.TestCase):
    def setUp(self):
        self.actions, self.shell, self.clock = FakeActions(), Shell(), [NOW]
        self.sites = {'tapas': self.actions, 'tappytoon': FakeActions('tappytoon'), 'webtoon': FakeActions('webtoon')}
        self.window = QtWidgets.QMainWindow()
        self.pages = window_pages.Pages(Q, self.shell, self.sites.__getitem__, now=lambda: self.clock[0])
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
    def card(self):
        """The chosen site's card."""
        return self.pages.dashboard.cards[self.shell.chosen]

    def fields(self, key=None):
        """A site's card as {field: value}."""
        card = self.pages.dashboard.cards[key or self.shell.chosen]
        return {name.text(): value.text() for name, value in card.cells}

    @property
    def settings(self):
        return self.pages.settings



class HelperTests(unittest.TestCase):
    def test_single_line_labels_preserve_full_text_and_spaces_in_paths(self):
        label = window_pages.Kit(Q, lambda target: None).single_line('Folder with  two spaces\nNext line')
        self.addCleanup(label.deleteLater)
        expected = 'Folder with  two spaces Next line'
        self.assertEqual(label.text(), expected)
        self.assertEqual(label.toolTip(), expected)
        self.assertEqual(label.accessibleDescription(), expected)

    def test_routes_lead_to_a_page_that_exists_and_a_tab_that_is_offered(self):
        self.assertEqual(window_pages.route_of('settings', True), 'settings/downloads')
        self.assertEqual(window_pages.route_of('settings/b2', True), 'settings/b2')
        self.assertEqual(window_pages.route_of('settings/b2', False), 'settings/downloads')
        self.assertEqual(window_pages.route_of('library', False), 'dashboard')
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
    def test_hinted_values_expand_when_their_text_changes(self):
        window_pages.paint(Q, self.window, True)
        self.open()
        self.window.resize(1800, 1000)
        texts = ('—', 'an hour', '4.8 GB to go | 1.4 TB free', '15 hours', '409 GB to go | 473 GB free')
        for family in ('DejaVu Sans', 'Noto Sans'):
            font = self.window.font(); font.setFamily(family); self.window.setFont(font)
            for text in (*texts, *(f'{n} hours' for n in range(1, 21))):
                rows = dashboard_model.card_rows({'Time left': text})
                next(row for row in rows if row['name'] == 'Time left')['tip'] = dashboard_model.ESTIMATE_TIP
                self.card._fields(rows)
                settle()
                value = dict((name.text(), value) for name, value in self.card.cells)['Time left']
                hint = self.card.hints[dashboard_model.CARD_FIELDS.index('Time left')]
                needed = value.fontMetrics().horizontalAdvance(text)
                self.assertGreaterEqual(value.contentsRect().width(), needed, (text, needed, value.width()))
                self.assertEqual(value.fontMetrics().elidedText(text, QtCore.Qt.TextElideMode.ElideRight,
                                                               value.contentsRect().width()), text)
                self.assertLessEqual(hint.x() - value.geometry().right(), 12)
        if destination := os.environ.get('COMIC_LAYOUT_ARTIFACTS'):
            from pathlib import Path
            self.assertTrue(self.card.widget.grab().save(str(Path(destination) / 'hint-value-width.png')))

    def test_local_verification_progress_fits_the_existing_rows(self):
        window_pages.paint(Q, self.window, False)
        self.actions.reading['account'].update(status='running', phase='verify', elapsed_seconds=600,
                                              progress_age_seconds=10)
        self.actions.reading['status']['progress'] = {
            'phase':'verify','status':'running','audit_stage':'archives','archives_checked':1200,
            'archives_total':6000,'archive_bytes_checked':45_000_000_000,'audit_elapsed_seconds':600}
        self.open()
        settle()
        self.assertIn('1,200 / 6,000 archives', self.fields()['Progress'])
        self.assertIn('no provider requests', self.fields()['Note'])
        self.assertEqual(tuple(name.text() for name, _ in self.card.cells), dashboard_model.CARD_FIELDS)
        if destination := os.environ.get('COMIC_LAYOUT_ARTIFACTS'):
            from pathlib import Path
            folder = Path(destination); folder.mkdir(parents=True, exist_ok=True)
            self.assertTrue(self.card.widget.grab().save(str(folder / 'local-verification.png')))

    def test_status_changes_keep_the_same_widgets_positions_and_single_line_height(self):
        from pathlib import Path
        window_pages.paint(Q, self.window, False)
        self.open()
        s, card = self.pages.state, self.card
        original = [(id(name), id(value)) for name, value in card.cells]
        for width in (800, 1400):
            self.window.resize(width, 1000)
            settle()
            baseline = None
            for mode in ('loading', 'downloading', 'waiting', 'finished'):
                s.loaded = mode != 'loading'
                s.account = {'status': 'finished' if mode == 'finished' else 'running', 'phase': 'archive',
                             'elapsed_seconds': 3600}
                if mode == 'waiting':
                    s.account['retry'] = {'reason': 'network', 'seconds': 900}
                    s.message = dashboard_model.Message('A long status message ' * 20 + '\nwith a second line', True)
                else:
                    s.message = None
                self.pages.render()
                settle()
                self.assertEqual([(id(name), id(value)) for name, value in card.cells], original)
                self.assertEqual(tuple(name.text() for name, _ in card.cells), dashboard_model.CARD_FIELDS)
                positions = [(name.y(), value.parentWidget().y(), value.height()) for name, value in card.cells]
                geometry = (card.widget.height(), positions, card.action.mapTo(card.widget, QtCore.QPoint(0, 0)).y(),
                            self.dashboard.updated.geometry().getRect(), self.dashboard.go_all.geometry().getRect())
                self.assertLess(self.dashboard.updated.geometry().right(), self.dashboard.go_all.geometry().left())
                if baseline is None:
                    baseline = geometry
                self.assertEqual(geometry, baseline, (width, mode))
                for _, value in card.cells:
                    self.assertGreater(value.width(), value.parentWidget().width() * .8)
                    self.assertFalse(value.wordWrap())
                    self.assertNotIn('\n', value.text())
                    self.assertEqual(value.toolTip(), value.text())
                self.assertFalse(card.message.wordWrap())
                self.assertNotIn('\n', card.message.text())
                if mode == 'waiting':
                    self.assertIn('with a second line', card.message.toolTip())
                    self.assertGreater(card.message.fontMetrics().horizontalAdvance(card.message.text()), card.message.width())
                destination = os.environ.get('COMIC_LAYOUT_ARTIFACTS')
                if destination:
                    folder = Path(destination); folder.mkdir(parents=True, exist_ok=True)
                    self.assertTrue(card.widget.grab().save(str(folder / f'card-{width}-{mode}.png')))

    def test_the_dashboard_shows_the_library_once_it_is_read_and_says_it_is_drawn(self):
        self.assertFalse(self.pages.drawn())
        answers = []
        self.pages.reopened(answers.append)
        self.assertEqual(answers, [True])
        self.assertTrue(self.pages.drawn())
        self.assertEqual([call[0] for call in self.actions.calls], ['context', 'saved_files', 'poll'])
        self.assertFalse(self.actions.named('poll')[0][2]['library'])
        d, c = self.dashboard, self.card
        self.assertEqual(c.pill.text(), 'Paused')
        self.assertEqual(c.pill.property('tone'), 'warning')
        # What is so, a field and its value each; the buttons say what can be done.
        expected = {row['name']: row['value'] for row in dashboard_model.card_rows(
            {'Account': 'Session saved', 'Downloader': 'Ready', 'Status': 'Your download is paused',
             'Saved': '60 episodes', 'Speed': 'Fast'})}
        self.assertEqual(self.fields(), expected)
        self.assertFalse(c.message.isVisible() or c.allow.isVisible())
        # One button, for what can be done now.
        self.assertEqual([b.text() for b in c.widget.findChildren(QtWidgets.QPushButton) if not b.isHidden()], ['Continue'])
        self.assertTrue(c.action.isEnabled())
        self.assertIn(FOLDER, d.save.text())
        self.assertEqual(d.updated.text(), 'Automatic updates paused')
        self.assertEqual(d.library_title.text(), 'All sites')
        self.assertEqual([cell[0] for cell in d.metric_cells],
                         ['Saved episodes', 'Saved files', 'Episodes on this device', 'Cloud episode copies'])
        self.assertEqual([number.text() for _, number in d.metric_cells], ['70', '140', '60', '10'])
        # Each count is centered in its box, with its name under it.
        for _, number in d.metric_cells:
            for label in number.parentWidget().findChildren(QtWidgets.QLabel):
                self.assertTrue(label.alignment() & QtCore.Qt.AlignmentFlag.AlignHCenter, label.text())
        # The counts and the places speak for themselves: no sentence repeats them, nor says what the files are.
        self.assertFalse(d.library_summary.isVisible())
        self.assertNotIn('CBZ', ' '.join(label.text() for label in d.library_card.findChildren(QtWidgets.QLabel)))
        self.assertEqual([b.text() for b in d.place_labels], ['This device | 60 episodes · 120 files', 'Backblaze B2 | 10 episodes · 20 files'])

    def test_every_site_that_works_has_a_card_and_one_not_opened_yet_offers_only_its_sign_in(self):
        self.open()
        d = self.dashboard
        self.assertEqual(list(d.cards), ['tapas', 'tappytoon', 'webtoon'])
        later = d.cards['tappytoon']
        self.assertEqual(later.pill.text(), 'Not signed in')
        self.assertEqual(self.fields('tappytoon'), {r['name']: r['value'] for r in dashboard_model.unopened_rows()})
        self.assertEqual((later.action.text(), later.action.isVisible(), later.action.isEnabled(), later.allow.isHidden()),
                         ('Sign in', True, True, True))
        # One sign-in at a time, in the window: the site is chosen (and opened) first, then its sign-in starts.
        self.shell.opens = True
        later.action.click()
        self.assertEqual(self.shell.calls, [('choose_site', 'tappytoon')])
        self.assertEqual(len(self.sites['tappytoon'].named('signin')), 1)
        self.assertEqual(self.actions.named('signin'), [])

    def test_each_site_has_its_own_go_and_pause_and_every_open_site_is_refreshed(self):
        self.shell.open = ['tapas', 'webtoon']
        webtoon = self.sites['webtoon']
        webtoon.reading['account']['status'] = 'finished'
        self.open()
        self.assertEqual([call[0] for call in webtoon.calls], ['context', 'saved_files', 'poll'])
        self.assertEqual(self.dashboard.cards['webtoon'].pill.text(), 'Finished')
        self.assertEqual(self.fields('webtoon')['Status'], 'Every episode your account can read is saved')
        self.dashboard.cards['webtoon'].action.click()
        self.assertEqual((len(webtoon.named('go')), len(self.actions.named('go'))), (1, 0))
        self.assertEqual(self.dashboard.cards['webtoon'].pill.text(), 'Downloading')
        self.assertEqual(self.card.pill.text(), 'Paused')
        self.assertTrue(self.dashboard.pause_all.isVisible())
        self.pages.poll()
        self.assertEqual((len(webtoon.named('poll')), len(self.actions.named('poll'))), (2, 2))

    def test_a_card_says_its_time_and_space_left_are_rough_guesses(self):
        window_pages.paint(Q, self.window, False)
        self.actions.reading['status']['estimate'] = {'waiting_episodes': 900, 'seconds': 54_000,
                                                      'left_bytes': 409_000_000_000, 'free_bytes': 473_000_000_000}
        self.open()
        self.assertEqual((self.fields()['Time left'], self.fields()['Space']),
                         ('15 hours', '409 GB to go | 473 GB free'))
        hints = dict(zip((name.text() for name, _ in self.card.cells), self.card.hints, strict=True))
        self.assertEqual([name for name, hint in hints.items() if hint.isVisible()], ['Time left', 'Space'])
        self.assertEqual(hints['Time left'].toolTip(), 'A rough guess, based on the download so far.')
        # The ? follows its data without changing the fixed row height.
        settle()
        for name, value in self.card.cells:
            if name.text() in ('Time left', 'Space'):
                self.assertLessEqual(value.width(), value.fontMetrics().horizontalAdvance(value.text()) + 4)
                self.assertTrue(hints[name.text()].sizePolicy().retainSizeWhenHidden())
                self.assertLessEqual(hints[name.text()].x() - value.geometry().right(), 12)
        if destination := os.environ.get('COMIC_LAYOUT_ARTIFACTS'):
            from pathlib import Path
            self.assertTrue(self.card.widget.grab().save(str(Path(destination) / 'hints-next-to-values.png')))
        # A tap shows what hovering would, for a touch screen with no pointer to hover.
        hints['Space'].click()
        self.assertTrue(QtWidgets.QToolTip.isVisible())
        self.assertEqual(QtWidgets.QToolTip.text(), hints['Space'].toolTip())
        QtWidgets.QToolTip.hideText()
        self.actions.reading['status']['estimate'] = {'waiting_episodes': 900}
        self.pages.poll()
        self.assertEqual([name for name, hint in hints.items() if hint.isVisible()], [])

    def test_go_on_all_sites_starts_each_that_can_go_and_pause_all_pauses_each_that_runs(self):
        self.shell.open = ['tapas', 'tappytoon', 'webtoon']
        self.sites['tappytoon'].reading['status']['session_configured'] = False
        self.open()
        d = self.dashboard
        self.assertTrue(d.go_all.isEnabled())
        self.assertFalse(d.pause_all.isVisible())
        d.go_all.click()
        self.assertEqual([len(self.sites[key].named('go')) for key in ('tapas', 'tappytoon', 'webtoon')], [1, 0, 1])
        self.assertTrue(d.pause_all.isVisible())
        self.assertFalse(d.go_all.isEnabled())
        d.pause_all.click()
        self.assertEqual([len(self.sites[key].named('pause')) for key in ('tapas', 'tappytoon', 'webtoon')], [1, 0, 1])
        self.assertFalse(d.pause_all.isVisible())

    def test_the_cards_sit_two_to_a_row_in_a_wide_window_and_one_in_a_narrow_one(self):
        self.open()
        d = self.dashboard
        settle()

        def places():
            return [d.grid.getItemPosition(d.grid.indexOf(widget))
                    for widget in (d.library_card, *(card.widget for card in d.cards.values()))]
        # The All sites card goes all the way across, over the sites' cards.
        self.assertEqual(d.columns, 2)
        self.assertEqual(places(), [(0, 0, 1, 2), (1, 0, 1, 1), (1, 1, 1, 1), (2, 0, 1, 1)])
        self.window.resize(800, 900)
        settle()
        self.assertEqual(d.columns, 1)
        self.assertEqual(places(), [(0, 0, 1, 1), (1, 0, 1, 1), (2, 0, 1, 1), (3, 0, 1, 1)])
        self.window.resize(1400, 900)
        settle()
        self.assertEqual(d.columns, 2)
        # The page's own margins are not room for cards.
        d._resized(window_pages.CARD_COLUMNS_FROM + d.margins - 1)
        self.assertEqual(d.columns, 1)
        d._resized(window_pages.CARD_COLUMNS_FROM + d.margins)
        self.assertEqual(d.columns, 2)

    def test_the_all_sites_card_counts_every_open_site(self):
        self.shell.open = ['tapas', 'webtoon']
        self.open()
        self.assertEqual([number.text() for _, number in self.dashboard.metric_cells], ['140', '280', '120', '20'])
        self.assertEqual([b.text() for b in self.dashboard.place_labels],
                         ['This device | 120 episodes · 240 files', 'Backblaze B2 | 20 episodes · 40 files'])
        self.sites['webtoon'].summary_fails = True
        self.clock[0] += dashboard_model.LIBRARY_SECONDS + 1
        self.pages.poll()
        # A site that could not be counted is left out; the others still show.
        self.assertEqual([number.text() for _, number in self.dashboard.metric_cells], ['70', '140', '60', '10'])
        # Whatever was replaced on the way is gone from the page at once, not drawn under the new until Qt deletes it.
        d, card = self.dashboard, self.dashboard.cards['tapas']

        def shown(widget, kind, name):
            return sorted(id(w) for w in widget.findChildren(kind)
                          if name in (w.objectName(), w.property('kind')) and not w.isHidden())
        self.assertEqual(shown(card.widget, QtWidgets.QLabel, 'fieldName'), sorted(id(name) for name, _ in card.cells))
        self.assertEqual(shown(d.library_card, QtWidgets.QLabel, 'place'), sorted(map(id, d.place_labels)))
        self.assertEqual(shown(d.library_card, QtWidgets.QFrame, 'metric'), sorted(id(n.parentWidget()) for _, n in d.metric_cells))

    def test_go_pause_and_sign_in_ask_the_library_and_show_its_answer(self):
        self.open()
        c = self.card
        c.action.click()
        self.assertEqual(len(self.actions.named('go')), 1)
        self.assertEqual((c.action.text(), c.action.isEnabled()), ('Pause', True))
        self.assertEqual((c.pill.text(), self.fields()['Step']), ('Downloading', 'Downloading episodes'))
        polls = len(self.actions.named('poll'))
        c.action.click()
        self.assertEqual((len(self.actions.named('pause')), len(self.actions.named('go'))), (1, 1))
        self.assertEqual(c.action.text(), 'Continue')
        settle()
        self.assertEqual(len(self.actions.named('poll')), polls + 1)

    def test_signing_in_is_offered_when_needed_and_says_where_to_finish(self):
        self.actions.reading['status']['session_configured'] = False
        self.open()
        c = self.card
        self.assertTrue(c.action.isEnabled())
        self.assertEqual((c.action.text(), c.pill.text(), self.fields()['Account']), ('Sign in', 'Sign-in needed', 'Sign-in needed'))
        c.action.click()
        self.assertEqual((len(self.actions.named('signin')), len(self.actions.named('go'))), (1, 0))
        self.assertEqual((c.action.text(), c.action.isEnabled()), ('Signing in…', False))
        self.assertTrue(c.message.text().startswith('Finish signing in to Tapas.'))

    def test_go_then_an_expired_session_shows_signin_instead_of_the_earlier_connected_check(self):
        self.actions.reading['site_auth'] = {'status': 'verified'}
        self.open()
        c = self.card
        self.assertEqual(self.fields()['Account'], 'Connected')
        c.action.click()
        self.actions.account(status='held', code='login_required')
        self.actions.reading['status']['worker'] = {'reported_state': 'ready'}
        self.pages.poll()
        self.assertEqual((c.action.text(), c.action.isEnabled()), ('Sign in', True))
        self.assertEqual(self.fields()['Account'], 'Sign-in needed')
        self.assertEqual(self.fields()['Status'], 'Tapas signed you out')

    def test_go_that_fails_before_it_returns_still_requires_a_new_signin(self):
        self.actions.reading['site_auth'] = {'status': 'verified'}
        self.actions.reading['account'] = {'status': 'held', 'code': 'login_required'}
        self.open()
        self.assertEqual((self.card.action.text(), self.card.action.isEnabled()), ('Continue', True))
        self.actions.go = lambda: self.actions.account(status='held', code='login_required')
        self.card.action.click()
        self.pages.poll()
        self.assertEqual((self.card.action.text(), self.card.action.isEnabled()), ('Sign in', True))

    def test_download_settings_open_with_a_table_of_why_episodes_were_not_downloaded(self):
        self.actions.reading['status']['episodes_by_status'] = {'archived': 100, 'unavailable': 44}
        self.open('settings/downloads')
        self.assertFalse(hasattr(self.dashboard, 'not_saved'))
        table = self.settings.not_downloaded
        self.assertTrue(self.settings.not_downloaded_row.isVisible())
        self.assertEqual([table.horizontalHeaderItem(i).text() for i in range(2)], ['Reason', 'Episodes'])
        self.assertEqual([(table.item(i, 0).text(), table.item(i, 1).text()) for i in range(table.rowCount())][:2], [
            ('Locked on your Tapas account. Buy or unlock them on Tapas, then press Continue', '44'), ('Not released yet', '0')])
        self.assertEqual(table.rowCount(), 6)
        self.assertTrue(table.alternatingRowColors())  # Wrapped reasons stay matched to their counts.
        # Every row shows without scrolling the table itself.
        self.assertGreaterEqual(table.viewport().height(), sum(table.rowHeight(i) for i in range(6)))

    def test_a_refusal_is_said_on_its_card_and_in_settings_until_the_next_action(self):
        self.open()
        self.actions.refused['go'] = 'An import is active. Try again after this batch finishes.'
        self.card.action.click()
        message = self.card.message
        self.assertEqual((message.text(), message.property('error')), ('An import is active. Try again after this batch finishes.', 'true'))
        self.assertEqual((self.card.action.text(), self.card.action.isEnabled()), ('Continue', True))
        self.pages.go_to('settings')
        self.assertEqual(self.settings.status.text(), 'An import is active. Try again after this batch finishes.')
        del self.actions.refused['go']
        self.pages.go()
        self.assertIsNone(self.pages.state.message)
        self.assertFalse(self.settings.status.isVisible())

    def test_while_an_action_runs_the_buttons_wait_and_nothing_else_starts(self):
        self.open()
        self.shell.held = []
        self.card.action.click()
        self.assertFalse(self.card.action.isEnabled() or self.dashboard.files.isEnabled())
        self.card.action.click()
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
        self.assertEqual((answers, [call[0] for call in self.actions.calls]), ([True], ['context', 'saved_files', 'poll']))
        self.assertEqual(self.card.pill.text(), 'Paused')
        self.pages.go_to('settings/downloads')
        self.assertFalse(self.settings.site_row.isVisible())

    def test_a_failed_refresh_says_it_will_retry_and_the_next_good_one_clears_it(self):
        self.open()
        self.actions.refused['poll'] = 'The service is temporarily unavailable. Try again shortly.'
        answers = []
        self.pages.poll(answers.append)
        self.assertEqual(answers, [False])
        self.assertTrue(self.card.message.text().startswith('Could not refresh progress.'))
        self.assertEqual(self.dashboard.updated.text(), 'Status unavailable · start to refresh')
        self.assertEqual(self.card.pill.text(), 'Status unavailable')
        del self.actions.refused['poll']
        self.clock[0] += 5
        self.pages.poll()
        self.assertFalse(self.card.message.isVisible())
        self.assertEqual(self.card.pill.text(), 'Paused')

    def test_the_timer_leaves_idle_sites_untouched_and_follows_only_started_work(self):
        self.shell.open = ['tapas', 'webtoon', 'tappytoon']
        self.open()
        for key in self.shell.open:
            self.sites[key].calls.clear()
        for _ in range(3):
            self.clock[0] += dashboard_model.LIBRARY_SECONDS + 1
            self.pages.poll(automatic=True)
            self.pages.render()
        self.assertTrue(all(not self.sites[key].calls for key in self.shell.open))
        self.assertEqual(self.dashboard.updated.text(), 'Automatic updates paused')
        self.assertNotIn('Reconnecting', [card.pill.text() for card in self.dashboard.cards.values()])
        self.pages.go('tapas')
        self.sites['tapas'].calls.clear()
        self.pages.poll(automatic=True)
        self.assertEqual(len(self.sites['tapas'].named('poll')), 1)
        self.assertFalse(self.sites['webtoon'].calls or self.sites['tappytoon'].calls)
        self.sites['tapas'].reading['account']['status'] = 'finished'
        self.sites['tapas'].reading['status']['worker']['reported_state'] = 'ready'
        self.pages.poll(automatic=True)
        self.assertTrue(self.pages.states['tapas'].summary)
        for key in self.shell.open:
            self.sites[key].calls.clear()
        self.clock[0] += dashboard_model.LIBRARY_SECONDS + 1
        self.pages.poll(automatic=True)
        self.assertTrue(all(not self.sites[key].calls for key in self.shell.open))
        # An explicit refresh remains available without starting a download.
        self.pages.poll_site('webtoon')
        self.assertEqual(len(self.sites['webtoon'].named('poll')), 1)

    def test_a_pause_finishes_its_inflight_step_then_stops_local_status_reads(self):
        self.open()
        self.actions.reading['account']['status'] = 'stopped'
        self.actions.reading['status']['worker']['reported_state'] = 'busy'
        self.pages.poll_site('tapas')
        self.actions.calls.clear()
        self.pages.poll(automatic=True)
        self.assertEqual(len(self.actions.named('poll')), 1)
        self.actions.reading['status']['worker']['reported_state'] = 'ready'
        self.pages.poll(automatic=True)
        self.assertEqual(len(self.actions.named('poll')), 2)
        self.pages.poll(automatic=True)
        self.assertEqual(len(self.actions.named('poll')), 2)
        self.assertTrue(self.card.action.isEnabled())

    def test_progress_reads_between_hourly_recounts_do_not_scan_saved_files(self):
        self.open()
        initial = len(self.actions.named('saved_files'))
        for _ in range(10):
            self.clock[0] += dashboard_model.POLL_SECONDS
            self.pages.poll()
        self.assertEqual(len(self.actions.named('saved_files')), initial)
        self.clock[0] += dashboard_model.LIBRARY_SECONDS
        self.pages.poll()
        self.assertEqual(len(self.actions.named('saved_files')), initial + 1)

    def test_a_slow_recount_does_not_delay_downloader_progress_or_start_duplicate_counts(self):
        self.open()
        self.clock[0] += dashboard_model.LIBRARY_SECONDS + 1
        self.shell.held = []
        self.pages.poll()
        self.assertEqual(len(self.shell.held), 2)
        recount, progress = self.shell.held
        self.shell.held = []
        self.shell.finish(*progress)
        for _ in range(5):
            self.clock[0] += dashboard_model.POLL_SECONDS
            self.actions.reading['account']['status'] = 'running'
            self.pages.poll()
            self.assertEqual(len(self.shell.held), 1)
            self.shell.finish(*self.shell.held.pop())
            self.assertEqual(self.pages.state.refreshed_at, self.clock[0])
            self.assertFalse(self.pages.state.offline)
            self.assertNotIn('reconnect', self.dashboard.updated.text())
        # A newer count request arrives while the first count is still running.
        self.pages.state.library_refresh = True
        self.actions.summary_fails = True
        self.shell.finish(*recount)
        self.assertIs(self.pages.state.summary, False)
        self.assertTrue(self.pages.state.library_refresh)
        self.assertFalse(self.pages.state.offline)
        self.assertEqual(self.pages.state.refreshed_at, self.clock[0])
        self.pages.poll()
        self.assertEqual(len(self.shell.held), 2)
        self.shell.release()

    def test_a_count_for_an_old_folder_cannot_replace_the_new_folders_counts(self):
        self.open()
        old = self.pages.state
        self.clock[0] += dashboard_model.LIBRARY_SECONDS + 1
        self.shell.held = []
        self.pages.poll()
        recount, progress = self.shell.held
        self.shell.held = None
        self.pages.reopened()
        fresh = self.pages.state
        self.assertIsNot(fresh, old)
        fresh.summary = {'episode_count': 999}
        self.shell.finish(*recount)
        self.shell.finish(*progress)
        self.assertEqual(fresh.summary, {'episode_count': 999})
        self.assertFalse(self.pages.polling or self.pages.counting)

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

    def test_the_connection_can_be_allowed_from_a_card_when_saving_waits_for_it(self):
        self.actions.reading['network'] = {'wifi_only': True, 'allowed': False, 'reason': 'metered_connection'}
        self.open()
        c = self.card
        self.assertTrue(c.allow.isVisible() and c.allow.isEnabled())
        self.assertTrue(self.fields()['Status'].startswith('This connection is metered'))
        c.allow.click()
        self.assertEqual(self.actions.named('wifi_only')[0][1], (False,))
        self.assertFalse(c.allow.isVisible())
        self.assertEqual(self.pages.state.network, {'wifi_only': False, 'allowed': True})

    def test_the_site_list_in_settings_chooses_a_site_and_stops_nothing(self):
        self.open('settings/downloads')
        site = self.settings.site
        self.assertEqual([site.itemText(i) for i in range(site.count())], ['Tapas', 'Tappytoon', 'Webtoon', 'Elsewhere — coming later'])
        self.assertTrue(site.model().item(2).isEnabled()); self.assertFalse(site.model().item(3).isEnabled())
        site.activated.emit(1)
        self.assertEqual(self.shell.calls, [('choose_site', 'tappytoon')])
        self.assertEqual(site.currentData(), 'tapas')
        self.pages.go()
        self.assertTrue(site.isEnabled())
        self.actions.reading['site_auth'] = {'status': 'running'}
        self.pages.poll()
        self.assertFalse(site.isEnabled())

    def test_choosing_an_open_site_shows_its_settings_and_keeps_every_card(self):
        self.shell.open = ['tapas', 'webtoon']
        self.open('settings/downloads')
        self.shell.chosen = 'webtoon'
        self.pages.site_chosen()
        self.assertEqual(self.settings.site.currentData(), 'webtoon')
        self.assertEqual(self.settings.speed_label.text(), 'Speed on Webtoon')
        self.assertEqual(self.sites['webtoon'].named('context'), [('context', (), {})])
        self.assertTrue(self.pages.states['tapas'].loaded)

    def test_a_site_being_opened_says_so_on_its_card(self):
        self.open()
        s = self.pages.opening('webtoon', 'Opening Webtoon…')
        self.assertEqual(self.dashboard.cards['webtoon'].message.text(), 'Opening Webtoon…')
        self.assertFalse(self.dashboard.cards['webtoon'].action.isEnabled())
        s.busy, s.message = False, None
        self.pages.render()
        self.assertEqual(self.dashboard.cards['webtoon'].pill.text(), 'Not signed in')

    def test_the_library_card_opens_the_folder_and_says_when_it_cannot_count(self):
        self.open()
        cells = [number for _, number in self.dashboard.metric_cells]
        places = list(self.dashboard.place_labels)
        self.clock[0] += dashboard_model.LIBRARY_SECONDS + 1
        self.pages.poll()
        self.assertFalse(self.actions.named('poll')[-1][2]['library'])
        self.assertEqual(self.actions.named('saved_files')[-1][2], {'summary': True, 'refresh': False})
        self.assertEqual([number for _, number in self.dashboard.metric_cells], cells)
        self.assertEqual(self.dashboard.place_labels, places)
        self.assertEqual(self.dashboard.files.text(), 'Open library folder')
        self.dashboard.files.click()
        self.assertEqual(self.shell.calls, [('open_folder',)])
        self.assertEqual(self.pages.route, 'dashboard')
        self.actions.summary_fails = True
        self.pages.go_to('dashboard')
        self.clock[0] += dashboard_model.LIBRARY_SECONDS + 1
        self.pages.poll()
        self.assertEqual(self.dashboard.library_summary.text(), storage_model.LIBRARY_UNAVAILABLE)
        self.assertTrue(self.dashboard.library_summary.isVisible())
        self.actions.summary_fails = False
        self.clock[0] += dashboard_model.LIBRARY_SECONDS + 1
        self.pages.poll()
        self.assertFalse(self.dashboard.library_summary.isVisible())

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
        window_pages.paint(Q, self.window, False)
        settle()
        if destination := os.environ.get('COMIC_LAYOUT_ARTIFACTS'):
            from pathlib import Path
            self.assertTrue(self.window.grab().save(str(Path(destination) / 'dashboard-no-library.png')))
        nav = self.pages.header.nav
        self.assertEqual(list(nav), ['dashboard', 'settings'])
        self.assertEqual(self.pages.stack.count(), 2)
        self.assertFalse(hasattr(self.pages, 'library'))
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

    def test_about_names_the_author_and_shows_every_license_the_app_carries(self):
        self.open()
        self.settings.tabs['about'].click()
        s = self.settings
        self.assertEqual(self.pages.route, 'settings/about')
        self.assertTrue(s.panels['about'].isVisible() and not s.panels['downloads'].isVisible())
        words = ' '.join(label.text() for label in s.panels['about'].findChildren(QtWidgets.QLabel))
        self.assertIn('Ben Crute', words)
        self.assertIn('MIT license', words)
        choice = s.license_choice
        self.assertEqual([choice.itemText(i) for i in range(2)], ['Comic Archiver (MIT license)', 'Third-party notices'])
        self.assertIn('Copyright (c) 2026 Ben Crute', s.license_text.toPlainText())
        self.assertTrue(s.license_text.isReadOnly())
        choice.activated.emit(1)
        self.assertTrue(s.license_text.toPlainText().startswith('# Third-party notices'))

    def test_about_copies_details_for_support_without_the_library_folder(self):
        self.open()
        self.settings.tabs['about'].click()
        s = self.settings
        self.assertFalse(s.support_note.isVisible())
        s.support.click()
        copied = QtGui.QGuiApplication.clipboard().text()
        self.assertTrue(copied.startswith('Comic Archiver '))
        self.assertIn('\n== Tapas\nSign-in: signed in', copied)
        self.assertIn('\n== Tappytoon\nNot opened yet', copied)
        self.assertNotIn(FOLDER, copied)
        self.assertTrue(s.support_note.isVisible())
        self.assertEqual(s.support_note.text(), window_pages.SUPPORT_COPIED)

    def test_about_saves_one_log_for_support_in_the_downloads_folder_without_the_library_folder(self):
        from pathlib import Path
        import tempfile
        from unittest import mock
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        downloads, state = Path(temporary.name) / 'Downloads', Path(temporary.name) / 'state'
        state.mkdir()
        (state / 'desktop.log').write_text(f'2026-11-04 10:00:00,000 INFO the library is open\nopened {FOLDER}\n')
        self.assertTrue(self.pages.downloads())
        self.open()
        self.settings.tabs['about'].click()
        s = self.settings
        with mock.patch.object(self.pages, 'downloads', return_value=str(downloads)), \
                mock.patch('comic_archive.app_runtime.state_dir', return_value=state):
            s.save_log.click()
        saved, = downloads.iterdir()
        self.assertRegex(saved.name, r'^Comic Archiver log [0-9-]{10} [0-9.]{8}\.txt$')
        text = saved.read_text(encoding='utf-8')
        # The details, every open site's records, then the app's log, in one file.
        self.assertIn('\n== Tapas\nSign-in: signed in', text)
        self.assertIn('=== Tapas: episodes not saved\nseries 11 episode 22: failed job_failed', text)
        self.assertIn('INFO the library is open', text)
        self.assertLess(text.index('== Tapas'), text.index('=== Tapas'))
        self.assertLess(text.index('=== Tapas'), text.index('=== The app’s log'))
        self.assertNotIn(FOLDER, text)
        self.assertIn('This step stopped in <library>/tapas/Library/x.cbz', text)
        self.assertEqual(s.support_note.text(), window_pages.LOG_SAVED.format(name=saved.name))
        self.assertTrue(s.support_note.isVisible() and s.save_log.isEnabled())
        # A site that cannot be read is said in the file; a file that cannot be saved is said on the page.
        self.actions.refused['support_records'] = 'No.'
        with mock.patch.object(self.pages, 'downloads', return_value=str(downloads)), \
                mock.patch('comic_archive.app_runtime.state_dir', return_value=state), \
                mock.patch('comic_archive.support_log.save', side_effect=app_actions.Refused('x', 'No.')) as save:
            s.save_log.click()
        self.assertIn('=== A site could not be read: request_unavailable', save.call_args.args[1])
        self.assertEqual(s.support_note.text(), window_pages.LOG_FAILED)
        self.assertTrue(s.save_log.isEnabled())

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
    def test_only_the_open_sites_speed_is_chosen_here(self):
        self.open('settings/downloads')
        s = self.settings
        self.assertEqual((s.speed_label.text(), s.speed.currentData()), ('Speed on Tapas', 'fast'))
        self.assertFalse(s.signin_note.isVisible())
        pick(s.speed, 'medium')
        self.assertEqual(self.actions.named('speed')[0][1], ('medium',))
        self.assertEqual((s.speed.currentData(), s.speed_help.text()), ('medium', 'About half the Fast speed.'))
        # Another site's speed is chosen on that site: the header's site list opens it.
        self.assertFalse(hasattr(s, 'speed_rows') or hasattr(s, 'other_speeds'))

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
        self.assertEqual((self.card.action.text(), self.card.action.isEnabled()), ('Sign in', True))
        self.assertEqual(self.card.message.text(), '')

    def test_signing_out_names_the_library_folders_it_could_not_reach(self):
        self.actions.kept = ['/media/drive/Comic Archiver']
        self.open('settings/downloads')
        self.shell.answer = True
        self.settings.signout.click()
        said = ('Signed out of Tapas, except in this library folder, which Comic Archiver can\'t reach:\n'
                '/media/drive/Comic Archiver\nConnect the drive it\'s on, choose the folder with Change folder…, and sign '
                'out there.')
        self.assertEqual(self.pages.state.message, dashboard_model.Message(said, True))
        settle()  # A refresh keeps it; the next thing the person does clears it.
        self.pages.go_to('dashboard')
        self.assertEqual(self.card.message.text(), ' '.join(said.split()))
        self.assertEqual(window_pages.SIGNED_OUT_AWAY[2].format(site='Tapas', folders='/a\n/b'),
                         'Signed out of Tapas, except in these library folders, which Comic Archiver can\'t reach:\n/a\n/b\n'
                         'Connect the drives they\'re on, choose each folder with Change folder…, and sign out there.')

    def test_a_connection_the_library_folder_brought_is_asked_about_once_before_episodes_go_to_it(self):
        target = self.actions.reading['cloud']['targets'][0]
        target.update(enabled=True, confirmed=False)
        self.shell.answer = False
        self.open('dashboard')
        settle()
        question = ('ask', window_pages.CLOUD_CONFIRM.format(bucket='the Backblaze B2 bucket "comic-library-0123"'))
        self.assertIn('This library folder saves new episodes to the Backblaze B2 bucket "comic-library-0123". That '
                      'connection came with the folder', question[1])
        self.assertEqual(self.shell.calls, [question])
        self.assertEqual([call[1] for call in self.actions.named('cloud_confirm')], [('b2', False)])
        self.assertFalse(self.pages.state.cloud['targets'][0]['enabled'])  # Saved here from now on.
        # Asked once in a window, even if the answer was not kept.
        target.update(enabled=True, confirmed=False)
        self.pages.poll(); settle()
        self.assertEqual(self.shell.calls, [question])
        # Another folder opened is asked about; with no bucket name to show when its connection file has none.
        target['bucket'], self.shell.answer = None, True
        self.open('dashboard'); settle()
        self.assertEqual(self.shell.calls[-1], ('ask', window_pages.CLOUD_CONFIRM.format(bucket='a Backblaze B2 bucket')))
        self.assertEqual([call[1] for call in self.actions.named('cloud_confirm')], [('b2', False), ('b2', True)])
        self.assertTrue(self.pages.state.cloud['targets'][0]['enabled'])
        # One set up or agreed to here, or turned off, is never asked about.
        for answer in ({'confirmed': True}, {'confirmed': False, 'enabled': False}):
            target.update({'enabled': True, **answer})
            self.open('dashboard'); settle()
            self.assertEqual(len(self.shell.calls), 2)

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

    def test_the_speed_waits_while_saving_and_a_signed_out_site_has_none(self):
        self.actions.reading['account']['status'] = 'running'
        self.open('settings/downloads')
        self.assertFalse(self.settings.speed.isEnabled())
        self.assertEqual(self.settings.speed_help.text(), 'Pause to change the speed.')
        self.actions.reading['status']['session_configured'] = False
        self.pages.reopened()
        self.assertTrue(self.settings.signin_note.isVisible())
        self.assertEqual(self.settings.signin_note.text(), 'Sign in to Tapas to choose how fast it saves.')
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
        self.assertEqual(s.auth.text(), 'Sign-in canceled. Your saved files are safe.')

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



if __name__ == '__main__':
    unittest.main()
