from dataclasses import replace
import ast
import contextlib
import io
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

from support import home_environment
from comic_archive import app_runtime, desktop_app, qt_engine, sites, window_style
from comic_archive.app_actions import Refused
from comic_archive.dashboard_model import Message, error_text
from comic_archive.models import ArchiveError

RUNTIME = app_runtime.Runtime
URL = 'http://127.0.0.1:4321/'  # An address on this computer, which no page may reach.
SIGNIN = 'https://tapas.io/account/signin'
QT_AGENT = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) QtWebEngine/6.11.2 Chrome/140.0.0.0 Safari/537.36'


class Fake:
    """Any Qt object: keeps its constructor arguments and records every call made on it."""
    made = []

    def __init__(self, *args):
        self.args = args
        type(self).made.append(self)

    def __getattr__(self, name):
        if name.startswith('__'):
            raise AttributeError(name)
        value = Mock(name=name)
        setattr(self, name, value)
        return value


class Url:
    def __init__(self, text):
        self.text = text

    def scheme(self):
        return urlsplit(self.text).scheme

    def host(self):
        return urlsplit(self.text).hostname or ''

    def port(self):
        port = urlsplit(self.text).port
        return -1 if port is None else port

    def path(self):
        return urlsplit(self.text).path

    def toString(self):
        return self.text

    @staticmethod
    def fromLocalFile(path):
        return Url('file://' + path)


class InlineThread:
    """Runs the work at start(); 'alive' lets a test hold its completion back."""
    alive = False

    def __init__(self, target, daemon=None):
        self.target = target

    def start(self):
        self.target()

    def is_alive(self):
        return type(self).alive

    def join(self, timeout=None):
        pass


def emit(signal, *args):
    signal.connect.call_args.args[0](*args)


def qt_cookie(name=b'session', value=b'PRIVATE_COOKIE', domain='.tapas.io', path='/', expires=None, same_site='Lax',
              http_only=True, secure=True):
    """One cookie as Qt hands it over; a cookie that ends with the session has no expiry date."""
    return SimpleNamespace(
        name=lambda: SimpleNamespace(data=lambda: name), value=lambda: SimpleNamespace(data=lambda: value),
        domain=lambda: domain, path=lambda: path, isSessionCookie=lambda: expires is None,
        expirationDate=lambda: SimpleNamespace(toSecsSinceEpoch=lambda: expires),
        isHttpOnly=lambda: http_only, isSecure=lambda: secure, sameSitePolicy=lambda: SimpleNamespace(name=same_site))


def page_answer(user=11, origin='https://tapas.io', storage=(('theme', 'dark'),)):
    """What the sign-in page's own who-am-i request left for the window to read."""
    answer = json.dumps({'data': {'user': {'id': user} if user else None}, 'error_details': None})
    return json.dumps({'origin': origin, 'answer': answer, 'storage': [list(pair) for pair in storage]})


def fake_qt():
    """Fresh PySide6 stand-ins; class-level mocks are the static calls and scripted answers."""
    def kind(name, **members):
        return type(name, (Fake,), {'made': [], **members})

    def module(name, **members):
        value = ModuleType('PySide6.' + name)
        vars(value).update(members)
        return value
    modules = {
        'QtCore': module('QtCore', QUrl=Url, QTimer=kind('QTimer', singleShot=Mock()), QByteArray=kind('QByteArray'),
            QDateTime=kind('QDateTime', fromSecsSinceEpoch=Mock(side_effect=lambda seconds: seconds)),
            QLockFile=kind('QLockFile', tryLock=Mock(return_value=True), error=Mock(return_value='held'),
                           removeStaleLockFile=Mock(return_value=False), unlock=Mock(),
                           LockError=SimpleNamespace(LockFailedError='held', PermissionError='denied')),
            Qt=SimpleNamespace(AlignmentFlag=SimpleNamespace(AlignCenter='center'),
                               WidgetAttribute=SimpleNamespace(WA_StyledBackground='styled'),
                               ColorScheme=SimpleNamespace(Unknown='unknown', Light='light', Dark='dark'))),
        'QtGui': module('QtGui', QDesktopServices=kind('QDesktopServices', openUrl=Mock()), QIcon=kind('QIcon')),
        'QtNetwork': module('QtNetwork', QNetworkProxyFactory=kind('QNetworkProxyFactory', setUseSystemConfiguration=Mock()),
            QLocalServer=kind('QLocalServer', removeServer=Mock(), SocketOption=SimpleNamespace(UserAccessOption='user')),
            QLocalSocket=kind('QLocalSocket', waitForConnected=Mock(return_value=True), waitForBytesWritten=Mock(return_value=True)),
            QNetworkCookie=kind('QNetworkCookie')),
        'QtWidgets': module('QtWidgets', QApplication=kind('QApplication', instance=Mock(return_value=None), exec=Mock(return_value=0)),
            QMainWindow=kind('QMainWindow'), QLabel=kind('QLabel'), QStackedWidget=kind('QStackedWidget'),
            QWidget=kind('QWidget'), QPushButton=kind('QPushButton'), QHBoxLayout=kind('QHBoxLayout'), QVBoxLayout=kind('QVBoxLayout'),
            QMessageBox=kind('QMessageBox', critical=Mock(), information=Mock(), question=Mock(), warning=Mock(),
                             StandardButton=SimpleNamespace(Yes=1, No=2)),
            QFileDialog=kind('QFileDialog', getExistingDirectory=Mock(return_value=''))),
        'QtWebEngineCore': module('QtWebEngineCore', QWebEngineProfile=kind('QWebEngineProfile', httpUserAgent=Mock(return_value=QT_AGENT),
            defaultProfile=Mock(return_value=SimpleNamespace(httpUserAgent=lambda: QT_AGENT))), QWebEnginePage=kind('QWebEnginePage',
            RenderProcessTerminationStatus=SimpleNamespace(NormalTerminationStatus='normal'),
            NavigationType=SimpleNamespace(NavigationTypeBackForward='back')),
            QWebEngineUrlRequestInterceptor=kind('QWebEngineUrlRequestInterceptor'),
            QWebEngineScript=kind('QWebEngineScript', ScriptWorldId=SimpleNamespace(MainWorld=0, ApplicationWorld=1)),
            QWebEngineLoadingInfo=kind('QWebEngineLoadingInfo',
                LoadStatus=SimpleNamespace(LoadStartedStatus='started', LoadStoppedStatus='stopped',
                                           LoadSucceededStatus='succeeded', LoadFailedStatus='failed'),
                ErrorDomain=SimpleNamespace(ConnectionErrorDomain='connection', HttpStatusCodeDomain='http-status'))),
        'QtWebEngineWidgets': module('QtWebEngineWidgets', QWebEngineView=kind('QWebEngineView')),
    }
    package = ModuleType('PySide6')
    vars(package).update(modules)
    return {'PySide6': package, **{'PySide6.' + name: value for name, value in modules.items()}}


class DesktopAppTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.state, self.log = self.home / 'state/comic-archiver', self.home / 'state/comic-archiver/desktop.log'
        self.runtime = Mock(site=sites.DEFAULT)
        self.runtime.busy.return_value = None
        self.signin = self.runtime.signin
        self.signin.take.return_value = False
        self.signin.status.return_value = {'status': 'idle'}
        self.runtime.theme.get.return_value = 'system'
        # The pages are window_pages' own, which these stand-ins for Qt cannot draw: each window gets a stand-in
        # that records what the window asks of it, and keeps the calls it was given to read the library with.
        self.pages = []

        def pages(q, shell, actions):
            made = Mock(name='pages', actions=actions, state=SimpleNamespace(busy=False, message=None))
            made.close_readers.side_effect = lambda then: then()
            self.pages.append(made)
            return made
        self.paint = Mock()
        self.runtime_kind = Mock(return_value=self.runtime)
        self.state_dir = Mock(return_value=self.state)
        self.logging = Mock(return_value=self.log)
        self.threads = SimpleNamespace(Thread=type('Thread', (InlineThread,), {}), Timer=Mock())
        for wrapper in [patch.dict(os.environ, home_environment(self.home, self.home / 'state')),
                        patch.dict(sys.modules, fake_qt()),
                        patch('comic_archive.desktop_app.threading', self.threads),
                        patch('comic_archive.desktop_app.app_runtime.Runtime', self.runtime_kind),
                        patch('comic_archive.desktop_app.app_runtime.state_dir', self.state_dir),
                        patch('comic_archive.desktop_app.app_runtime.configure_logging', self.logging),
                        patch('comic_archive.window_pages.Pages', Mock(side_effect=pages)),
                        patch('comic_archive.window_pages.paint', self.paint)]:
            wrapper.start(); self.addCleanup(wrapper.stop)
        for name in ('QT_QPA_PLATFORM', 'QT_QUICK_BACKEND', 'QTWEBENGINE_CHROMIUM_FLAGS', 'QTWEBENGINE_DISABLE_SANDBOX'):
            os.environ.pop(name, None)
        self.q = desktop_app.qt()
        self.widgets, self.core, self.network = self.q.QtWidgets, self.q.QtCore, self.q.QtNetwork
        self.box, self.lock = self.widgets.QMessageBox, self.core.QLockFile

    def opened(self, log=True, probe=None):
        shell = desktop_app.Shell(self.q, desktop_app.application(self.q), self.runtime, self.log if log else None, probe)
        shell.open()
        return shell

    def test_the_dashboard_can_have_the_library_folder_opened_or_another_one_chosen_in_the_computers_own_dialog(self):
        shell = self.opened(); shell.load()
        pages = shell.pages
        root = self.runtime.root = self.home / 'Comic Archiver'
        dialog, external = self.widgets.QFileDialog.getExistingDirectory, self.q.QtGui.QDesktopServices.openUrl
        self.runtime.folder = root / 'Library'
        other = Mock(site=sites.DEFAULT)
        shell.open_folder()
        self.assertEqual([call.args[0].toString() for call in external.call_args_list], ['file://' + str(root / 'Library')])
        dialog.assert_not_called()
        with patch('comic_archive.desktop_app.app_runtime.move', return_value=(other, None)) as move:
            # Never while saving or signing in: the folder would change under the downloader.
            self.runtime.busy.return_value = 'download'
            shell.choose_location()
            self.box.information.assert_called_once_with(shell.window, 'Comic Archiver', desktop_app.LOCATION_BUSY)
            dialog.assert_not_called()
            self.runtime.busy.return_value = None
            # Cancelled, the folder already in use, and No all leave everything as it was.
            (root / 'Library/.archive-state').mkdir(parents=True)
            (self.home / 'Old/Library/.archive-state').mkdir(parents=True)
            (self.home / 'Photos/Library').mkdir(parents=True)  # Not ours: it gets its own folder inside.
            self.box.question.return_value = 2
            for chosen, asked in [('', 0), (str(root), 0), (str(root / 'Library'), 0), (str(self.home / 'Other'), 1), (str(self.home / 'Old'), 2), (str(self.home / 'Photos'), 3)]:
                dialog.return_value = chosen
                shell.choose_location()
                self.assertEqual(self.box.question.call_count, asked)
            dialog.assert_called_with(shell.window, desktop_app.LOCATION_TITLE, str(self.home))
            first, second, third = (call.args for call in self.box.question.call_args_list)
            self.assertEqual(third[2], desktop_app.LOCATION_CONFIRM.format(new=self.home / 'Photos/Comic Archiver/Library', old=root / 'Library', root=root))
            self.assertEqual(first, (shell.window, 'Comic Archiver', desktop_app.LOCATION_CONFIRM.format(
                new=self.home / 'Other/Library', old=root / 'Library', root=root), 3, 1))
            self.assertEqual(second[2], desktop_app.LOCATION_EXISTING.format(new=self.home / 'Old/Library', old=root / 'Library', root=root))
            self.assertIn('Nothing is moved', first[2]); self.assertIn('already holds', second[2])
            # A new folder starts empty: Go saves everything again there, unless the whole library folder is moved first.
            self.assertIn('Go then saves everything again in the new folder, and your library page shows only what is there.', first[2])
            self.assertIn(f'move the whole folder\n{root}\nto the new place', first[2])
            self.assertIn('library page then shows only what is saved in this folder', second[2])
            move.assert_not_called()
            self.assertEqual(shell.phase, 'open')
            # Yes: the window waits on a plain notice, with its clocks stopped, and cannot be closed half-way.
            self.box.question.return_value = 1
            dialog.return_value = str(self.home / 'Other')
            shell.choose_location()
            move.assert_called_once_with(self.runtime, self.home / 'Other')
            self.assertEqual((shell.phase, shell.runtime), ('moving', self.runtime))
            shell.notice.setText.assert_called_with(desktop_app.MOVING); shell.stack.setCurrentWidget.assert_called_with(shell.notice)
            for clock in (shell.poll_clock, shell.tick_clock, shell.signin_clock):
                clock.stop.assert_called_once_with()
            event = Mock()
            shell.close_requested(event)
            event.ignore.assert_called_once_with(); event.accept.assert_not_called(); self.runtime.stop.assert_not_called()
            shell.choose_location()
            self.assertEqual(dialog.call_count, 7)
            emit(self.core.QTimer.made[-1].timeout)
        # The same pages read the new folder's library, afresh, on clocks that run again.
        self.assertEqual((shell.runtime, shell.phase, pages.actions._runtime()), (other, 'open', other))
        self.assertEqual([call.args for call in pages.reopened.call_args_list], [(None,), (None,)])
        shell.stack.setCurrentWidget.assert_called_with(pages.widget)
        for clock in (shell.poll_clock, shell.tick_clock, shell.signin_clock):
            self.assertEqual(clock.start.call_count, 2)
        self.box.information.assert_called_once()
        # The new folder cannot be used: back on the old one, with the reason in words.
        other.busy.return_value = None; other.root = self.home / 'Other'
        back = Mock(site=sites.DEFAULT)
        # The window's clocks run on under the dialogs: a second request waits instead of opening another one.
        dialog.side_effect = lambda *_: (shell.choose_location(), str(self.home / 'Third'))[1]
        with patch('comic_archive.desktop_app.app_runtime.move', return_value=(back, 'That folder cannot be written to.')) as move:
            shell.choose_location()
            emit(self.core.QTimer.made[-1].timeout)
        move.assert_called_once_with(other, self.home / 'Third')
        self.assertEqual((dialog.call_count, shell.choosing), (8, False))
        dialog.side_effect, dialog.return_value = None, str(self.home / 'Third')
        self.assertEqual((shell.runtime, shell.phase, pages.reopened.call_count), (back, 'open', 3))
        self.assertEqual(self.box.information.call_args.args, (shell.window, 'Comic Archiver',
                         'The folder was not changed.\n\nThat folder cannot be written to.'))
        # Neither folder opens again: a plain notice, and closing still works.
        back.busy.return_value = None; back.root = self.home / 'Other'
        with patch('comic_archive.desktop_app.app_runtime.move', side_effect=RuntimeError('PRIVATE_COOKIE')):
            with self.assertLogs('comic_archive.desktop', 'ERROR') as logs:
                shell.choose_location()
            emit(self.core.QTimer.made[-1].timeout)
        self.assertNotIn('PRIVATE', logs.output[0])
        self.assertEqual((shell.runtime, shell.phase, pages.reopened.call_count), (back, 'failed', 3))
        self.assertIn(desktop_app.LOAD_FAILED, shell.notice.setText.call_args.args[0])
        shell.close_requested(Mock())
        back.stop.assert_called_once_with()

    def test_a_question_is_asked_over_the_window_and_only_yes_agrees(self):
        shell = self.opened()
        for answer, agreed in ((1, True), (2, False), (None, False)):
            self.box.question.return_value = answer
            self.assertIs(shell.ask('Sign out of Tapas?'), agreed)
            self.assertEqual(self.box.question.call_args.args, (shell.window, 'Comic Archiver', 'Sign out of Tapas?', 3, 2))

    def test_the_dashboard_asks_the_site_about_the_saved_sign_in_once_it_opens_and_shows_the_answer_at_once(self):
        shell = self.opened()
        self.signin.check.assert_not_called()  # Not while the downloader is starting.
        shell.load()
        # Asked off the window's thread, as the app's browser, which needs no profile of its own for its name.
        self.signin.check.assert_called_once_with(QT_AGENT.replace(' QtWebEngine/6.11.2', ''))
        self.assertEqual(self.q.QtWebEngineCore.QWebEngineProfile.made, [])
        shell.pages.poll.assert_not_called()
        emit(self.core.QTimer.made[-1].timeout)
        shell.pages.poll.assert_called_once_with()
        # A self-test asks no site anything.
        self.signin.check.reset_mock()
        tested = self.opened(probe=Mock()); tested.load()
        self.signin.check.assert_not_called()

    def test_a_site_chosen_in_the_dashboard_opens_its_own_library_behind_the_same_page_unless_saving_or_signing_in(self):
        example = replace(sites.DEFAULT, key='example', name='Example', available=True)
        hidden = replace(sites.DEFAULT, key='hidden', name='Hidden', available=False)
        shell = self.opened()
        pages, change = shell.pages, self.runtime.change_site
        change.return_value = None
        with patch('comic_archive.sites.SITES', (sites.DEFAULT, example, hidden)):
            shell.choose_site('example')  # Still starting.
            shell.load()
            # The site already open, one that cannot be chosen and one that does not exist change nothing.
            for key in ('tapas', 'hidden', 'nowhere'):
                shell.choose_site(key)
            self.box.question.assert_not_called()
            # Asked first, in plain words; No leaves everything as it was.
            self.box.question.return_value = 2
            shell.choose_site('example')
            self.assertEqual(self.box.question.call_args.args, (shell.window, 'Comic Archiver', desktop_app.SITE_SWITCH.format(
                site='Example', open='Tapas'), 3, 2))
            # Never while saving or signing in: the dashboard goes on reading the site still open.
            self.box.question.return_value = 1
            self.runtime.busy.return_value = 'download'
            shell.choose_site('example')
            self.box.information.assert_called_once_with(shell.window, 'Comic Archiver', desktop_app.SITE_BUSY.format(site='Example'))
            self.runtime.busy.return_value = None
            # The window began closing while the question was open.
            def closing(*_):
                shell.phase = 'stopping'
                return 1
            self.box.question.side_effect = closing
            shell.choose_site('example')
            self.box.question.side_effect = None
            shell.phase = 'open'
            change.assert_not_called(); pages.render.assert_called_once_with()  # Only when the window was coloured.
            self.threads.Thread.alive = True
            shell.choose_site('example')
            change.assert_called_once_with(example)
            # Closing waits for the switch, and the dashboard says what is going on until it is done.
            self.assertEqual((shell.phase, pages.state.busy, pages.state.message), ('moving', True, Message('Switching to Example…')))
            self.assertEqual(desktop_app.close_action(shell.phase, None), 'ignore')
            pages.render.assert_called_with()
            self.threads.Thread.alive = False
            emit(self.core.QTimer.made[-1].timeout)
            # The pages stay in the window and read the other site's library, afresh.
            self.assertEqual((shell.runtime, shell.phase, pages.reopened.call_count), (self.runtime, 'open', 2))
            pages.reopened.assert_called_with()
            self.assertEqual(self.signin.check.call_count, 2)  # The other site's saved sign-in is asked about too.
            self.box.information.assert_called_once()
            # The site asked for cannot be opened: it stays as it was, with the reason in words.
            change.return_value = 'That folder cannot be written to.'
            self.runtime.site = example
            shell.choose_site('tapas')
            emit(self.core.QTimer.made[-1].timeout)
            self.assertEqual((shell.phase, pages.reopened.call_count), ('open', 3))
            self.assertEqual(self.box.information.call_args.args, (shell.window, 'Comic Archiver',
                             'Comic Archiver stays on Example.\n\nThat folder cannot be written to.'))
            # A switch that broke off: the window says so, with where the details are, and closing still works.
            change.side_effect = OSError('PRIVATE')
            with self.assertLogs('comic_archive.desktop', 'ERROR') as logs:
                shell.choose_site('tapas')
                emit(self.core.QTimer.made[-1].timeout)
        self.assertNotIn('PRIVATE', ''.join(logs.output))
        self.assertEqual((shell.phase, pages.reopened.call_count), ('failed', 3))
        shell.notice.setText.assert_called_with(f'{desktop_app.LOAD_FAILED}\n\nDetails are saved in:\n{self.log}')
        shell.stack.setCurrentWidget.assert_called_with(shell.notice)
        self.assertEqual(desktop_app.close_action(shell.phase, None), 'finish')

    def test_the_site_chosen_last_time_is_opened_at_the_next_start(self):
        example = replace(sites.DEFAULT, key='example', name='Example', available=True)
        self.state.mkdir(parents=True); (self.state / 'site').write_text('example\n')
        with patch('comic_archive.sites.SITES', (sites.DEFAULT, example)):
            self.assertEqual(desktop_app.run(self.q, None), 0)
        self.runtime_kind.assert_called_once_with(self.home / 'Comic Archiver', site=example)

    def test_a_folder_change_confirmed_after_a_download_started_or_the_window_began_closing_is_dropped(self):
        shell = self.opened(); shell.load()
        self.runtime.root = self.home / 'Comic Archiver'
        self.widgets.QFileDialog.getExistingDirectory.return_value = str(self.home / 'Other')
        def started(*_):
            self.runtime.busy.return_value = 'download'  # The dialog let time pass.
            return 1
        def closing(*_):
            shell.phase = 'stopping'
            return 1
        with patch('comic_archive.desktop_app.app_runtime.move') as move:
            for answer, phase in ((started, 'open'), (closing, 'stopping')):
                with self.subTest(answer.__name__):
                    shell.phase = 'open'; self.runtime.busy.return_value = None
                    self.box.question.side_effect = answer
                    shell.choose_location()
                    move.assert_not_called()
                    self.assertEqual((shell.phase, shell.choosing, shell.runtime), (phase, False, self.runtime))
        self.box.question.side_effect = None

    def test_a_folder_chosen_in_the_app_is_used_at_the_next_start_and_a_missing_one_offers_the_standard_folder(self):
        chosen, standard = self.home / 'drive/Comic Archiver', self.home / 'Comic Archiver'
        chosen.mkdir(parents=True); self.state.mkdir(parents=True)
        (self.state / 'library').write_text(f'{chosen}\n')
        self.assertEqual(desktop_app.run(self.q, None), 0)
        self.runtime_kind.assert_called_once_with(chosen, site=sites.DEFAULT)
        self.runtime_kind.reset_mock()
        self.assertEqual(desktop_app.run(self.q, '~/Comics'), 0)  # Asked for by name: that wins.
        self.runtime_kind.assert_called_once_with(self.home / 'Comics', site=sites.DEFAULT)
        self.box.question.assert_not_called()
        # The drive is not connected today. No: nothing is created or started and the choice is kept.
        chosen.rmdir()
        self.runtime_kind.reset_mock(); self.runtime.start.reset_mock(); self.widgets.QMainWindow.made.clear()
        self.box.question.return_value = 2
        with self.assertLogs('comic_archive.desktop', 'WARNING'):
            self.assertEqual(desktop_app.run(self.q, None), 0)
        parent, title, text, buttons, default = self.box.question.call_args.args
        self.assertEqual((parent, title, buttons, default), (None, 'Comic Archiver', 3, 2))
        self.assertIn(str(chosen), text); self.assertIn(str(standard), text)
        self.runtime_kind.assert_not_called(); self.runtime.start.assert_not_called()
        self.assertEqual(((self.state / 'library').read_text(), self.widgets.QMainWindow.made, chosen.exists()), (f'{chosen}\n', [], False))
        self.assertEqual(self.lock.unlock.call_count, 3); self.box.critical.assert_not_called()
        # Yes, but the standard folder does not start either: the choice is still there next time.
        self.box.question.return_value = 1
        self.runtime.start.side_effect = ArchiveError(app_runtime.UNWRITABLE)
        with self.assertLogs('comic_archive.desktop', 'WARNING'):
            self.assertEqual(desktop_app.run(self.q, None), 1)
        self.runtime_kind.assert_called_once_with(standard, site=sites.DEFAULT)
        self.assertEqual(((self.state / 'library').exists(), self.box.critical.call_count), (True, 1))
        # Yes: the standard folder from now on.
        self.runtime.start.side_effect = None
        self.assertEqual(desktop_app.run(self.q, None), 0)
        self.assertEqual([call.args for call in self.runtime_kind.call_args_list], [(standard,), (standard,)])
        self.assertFalse((self.state / 'library').exists())
        self.assertEqual((len(self.widgets.QMainWindow.made), self.box.question.call_count), (1, 3))
        # A chosen folder that is there but cannot be started is an ordinary startup failure, with no question.
        chosen.mkdir(); (self.state / 'library').write_text(f'{chosen}\n')
        self.runtime.start.side_effect = ArchiveError(app_runtime.UNWRITABLE)
        with self.assertLogs('comic_archive.desktop', 'ERROR'):
            self.assertEqual(desktop_app.run(self.q, None), 1)
        self.assertEqual((self.box.question.call_count, self.box.critical.call_count, (self.state / 'library').exists()), (3, 2, True))

    def test_the_window_follows_the_theme_chosen_in_its_header_and_the_computers_own_by_default(self):
        shell = self.opened()
        hints, pages, choice = shell.app.styleHints.return_value, shell.pages, self.runtime.theme
        hints.colorScheme.return_value = 'light'
        # Nothing chosen: Qt is handed back to the computer's own setting, and the window is coloured for what shows.
        hints.setColorScheme.assert_called_once_with('unknown')
        self.paint.assert_called_once_with(self.q, shell.window, False)
        pages.render.assert_called_once_with()
        # The computer itself turns dark: the window and its pages follow, the sign-in bar with them.
        hints.colorScheme.return_value = 'dark'
        emit(hints.colorSchemeChanged, 'dark')
        self.paint.assert_called_with(self.q, shell.window, True)
        self.assertEqual((pages.render.call_count, choice.set.call_count), (2, 0))
        # The header's switch shows the other look and keeps it for the next start.
        choice.set.side_effect = lambda value: setattr(choice.get, 'return_value', value)
        shell.toggle_theme()
        choice.set.assert_called_once_with('light')
        hints.setColorScheme.assert_called_with('light')
        hints.colorScheme.return_value = 'light'
        shell.toggle_theme()
        choice.set.assert_called_with('dark')
        hints.setColorScheme.assert_called_with('dark')
        self.assertEqual(shell.theme_choice, 'dark')
        # Back to the computer's own, as an older choice or an unknown word reads.
        for chosen in ('system', 'sepia'):
            choice.get.return_value = chosen
            shell.theme_apply()
            hints.setColorScheme.assert_called_with('unknown')
        # Before the pages exist only the window is coloured.
        shell.pages = None
        shell.theme_paint()
        self.assertEqual(self.paint.call_count, pages.render.call_count + 1)

    def held_lock(self, *, answered, stale):
        self.lock.tryLock = Mock(side_effect=[False, True])
        self.network.QLocalSocket.waitForConnected = Mock(return_value=answered)
        self.lock.removeStaleLockFile = Mock(return_value=stale)

    def self_test(self, loop):
        self.widgets.QApplication.exec = Mock(side_effect=loop)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = desktop_app.self_test(self.q)
        line, = output.getvalue().splitlines()
        return status, json.loads(line)

    def observe_cookie(self, **changes):
        """Qt's part in --self-test: the cookie the app stored comes back through the store's own signal."""
        sent = self.network.QNetworkCookie.made[-1]
        name, value = (item.args[0] for item in sent.args)
        fields = {'name': name, 'value': value, 'domain': sent.setDomain.call_args.args[0], 'path': sent.setPath.call_args.args[0],
                  'expires': sent.setExpirationDate.call_args.args[0], 'same_site': 'Default'} | changes
        emit(self.q.QtWebEngineCore.QWebEngineProfile.made[-1].cookieStore.return_value.cookieAdded, qt_cookie(**fields))

    def answer(self, loaded, found=None, cookie=True):
        """The pages' part in --self-test: their first refresh came back (loaded) and they drew what it read (found)."""
        def loop():
            pages = self.pages[-1]
            pages.drawn.return_value = found
            if cookie:
                self.observe_cookie()
            then, = pages.reopened.call_args.args
            then(loaded)
        return loop

    def signing_in(self):
        """A window whose dashboard asked for a sign-in, with the panel now showing Tapas's page."""
        shell = self.opened()
        shell.load()
        self.request_signin(shell)
        shell.signin_page.url.return_value = Url(SIGNIN)
        shell.stack.setCurrentWidget.reset_mock()
        return shell

    def request_signin(self, shell):
        self.signin.take.return_value, self.signin.status.return_value = True, {'status': 'running'}
        emit(shell.signin_clock.timeout)
        self.signin.take.return_value = False
        emit(self.core.QTimer.made[-1].timeout)  # The network check has finished on its thread.

    def loading(self, shell, status, error_page=False, domain='connection'):
        info = Mock()
        info.status.return_value, info.isErrorPage.return_value, info.errorDomain.return_value = status, error_page, domain
        emit(shell.signin_page.loadingChanged, info)

    def scripts(self, shell):
        return [call.args[0] for call in shell.signin_page.runJavaScript.call_args_list]

    def read(self, shell, text):
        """Qt's answer to the window's latest look at what the sign-in page found out."""
        script, world, callback = shell.signin_page.runJavaScript.call_args.args
        self.assertEqual((script, world), (desktop_app.SIGNIN_READ, 1))
        callback(text)

    def loads(self, view):
        return [call.args[0].toString() for call in view.load.call_args_list]

    # Decisions that need no window.

    def test_package_and_runtime_import_without_the_window_toolkit(self):
        tree = ast.parse(Path(desktop_app.__file__).read_text())
        top = [node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))]
        names = [alias.name for node in top for alias in node.names] + [node.module or '' for node in top if isinstance(node, ast.ImportFrom)]
        self.assertFalse([name for name in names if 'PySide6' in name])
        self.assertNotIn('PySide6', Path(app_runtime.__file__).read_text())

    def test_arguments_default_to_the_home_library_and_reject_unknown_options(self):
        args = desktop_app.arguments([])
        self.assertEqual((args.project_root, args.self_test, args.version), (None, False, False))
        args = desktop_app.arguments(['--project-root', '/library', '--self-test', '--version'])
        self.assertEqual((args.project_root, args.self_test, args.version), ('/library', True, True))
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as stopped:
            desktop_app.arguments(['--port', '8765'])
        self.assertEqual(stopped.exception.code, 2)

    def test_version_prints_without_loading_the_window_toolkit(self):
        output = io.StringIO()
        with patch('comic_archive.desktop_app.metadata.version', return_value='9.8.7') as lookup, \
                patch('comic_archive.desktop_app.qt', side_effect=AssertionError('Qt must not load')), contextlib.redirect_stdout(output):
            self.assertEqual(desktop_app.main(['--version']), 0)
            lookup.assert_called_once_with('comic-archive')
            lookup.side_effect = desktop_app.metadata.PackageNotFoundError('comic-archive')
            self.assertEqual(desktop_app.version(), 'unknown')
        self.assertEqual(output.getvalue(), 'Comic Archiver 9.8.7\n')

    def test_missing_window_toolkit_is_explained_instead_of_a_traceback(self):
        output, errors = io.StringIO(), io.StringIO()
        with patch.dict(sys.modules, {'PySide6': None}), contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            self.assertEqual(desktop_app.main([]), 1)
            self.assertEqual(output.getvalue(), '')
            self.assertEqual(desktop_app.main(['--self-test']), 1)
        self.assertIn('comic-archive[desktop]', errors.getvalue())
        self.assertEqual(json.loads(output.getvalue()), {'ok': False, 'reason': 'window_toolkit_missing'})
        self.logging.assert_called_once_with()
        self.box.critical.assert_not_called()
        # Only the browser engine fails to load: the plain widgets can still say why.
        failure = ImportError('libnss3.so: cannot open shared object file', name='PySide6.QtWebEngineCore')
        with patch('comic_archive.desktop_app.qt', side_effect=failure), contextlib.redirect_stderr(errors), \
                self.assertLogs('comic_archive.desktop', 'ERROR'):
            self.assertEqual(desktop_app.main([]), 1)
        parent, title, text = self.box.critical.call_args.args
        self.assertEqual((parent, title), (None, 'Comic Archiver'))
        self.assertEqual(text, desktop_app.failure_text(ArchiveError(desktop_app.TOOLKIT_BROKEN), self.log))
        self.assertNotIn('libnss3', text); self.assertIn('libnss3.so', errors.getvalue())
        self.assertEqual(len(self.widgets.QApplication.made), 1)
        self.runtime_kind.assert_not_called()

    def test_sign_in_page_loads_whole_pages_only_over_https(self):
        def allowed(text, main_frame=True):
            url = Url(text)
            return desktop_app.signin_navigation(url.scheme(), url.host(), url.path(), main_frame)
        for text in (SIGNIN, 'https://tapas.io/', 'https://accounts.google.com/o/oauth2/auth?x=1', 'https://www.facebook.com/login', 'about:blank'):
            self.assertTrue(allowed(text)); self.assertTrue(allowed(text, main_frame=False))
        for text in ('http://tapas.io/account/signin', URL, 'http://localhost:4321/', 'https://192.168.1.10/',
                     'https://169.254.169.254/latest/meta-data/', 'https://printer.local/', 'https://router.home.arpa/',
                     'file:///etc/passwd', 'view-source:https://tapas.io/',
                     'devtools://devtools/bundled/inspector.html', 'chrome://gpu', 'data:text/html,x', 'javascript:alert(1)',
                     'blob:https://tapas.io/5b2c', 'about:srcdoc', 'about:config', 'ftp://tapas.io/x', 'qrc:/x', 'https:///x', ''):
            self.assertFalse(allowed(text), text)
        for text in ('data:text/html,x', 'blob:https://tapas.io/5b2c', 'about:srcdoc'):
            self.assertTrue(allowed(text, main_frame=False))
        for text in ('http://tapas.io/', URL, 'file:///etc/passwd', 'chrome://gpu', 'view-source:https://tapas.io/', 'javascript:alert(1)'):
            self.assertFalse(allowed(text, main_frame=False), text)
        url = Url('about:blank')
        self.assertFalse(desktop_app.signin_navigation(url.scheme(), url.host(), url.path(), True, True))

    def test_private_destinations_include_local_names_and_every_non_public_ip_literal(self):
        for host in ('localhost', 'app.localhost.', 'local', 'printer.local', 'home.arpa', 'router.home.arpa.',
                     '0.0.0.0', '10.0.0.1', '100.64.0.1', '127.0.0.1', '169.254.169.254', '172.16.0.1',
                     '192.168.1.10', '224.0.0.1', '255.255.255.255', '::', '[::1]', 'fe80::1', 'fd00::1',
                     '2001:db8::1', '::ffff:192.168.1.10'):
            self.assertTrue(desktop_app.private_destination(host), host)
        for host in ('tapas.io', 'accounts.google.com', 'localhost.example.com', 'printer.local.example.com',
                     '8.8.8.8', '1.1.1.1', '2001:4860:4860::8888', ''):
            self.assertFalse(desktop_app.private_destination(host), host)

    def test_only_the_tapas_site_itself_is_asked_who_is_signed_in_and_the_bar_names_the_site_plainly(self):
        self.assertTrue(desktop_app.on_site('https', 'tapas.io'))
        for scheme, host in (('http', 'tapas.io'), ('https', 'www.tapas.io'), ('https', 'tapas.io.example.com'), ('https', 'accounts.google.com'),
                             ('about', ''), ('https', '')):
            self.assertFalse(desktop_app.on_site(scheme, host))
        for scheme, host, shown in [('https', 'tapas.io', 'tapas.io'), ('https', 'accounts.google.com', 'accounts.google.com'),
                                    ('https', 'tap\u0430s.io', 'xn--taps-73d.io'),  # A look-alike letter cannot pass for the real name.
                                    ('http', 'tapas.io', ''), ('about', '', ''), ('https', '', ''), ('https', 'a..b', '')]:
            self.assertEqual(desktop_app.host_text(scheme, host), shown)

    def test_signed_in_needs_the_tapas_page_itself_to_report_a_real_account(self):
        self.assertEqual(desktop_app.signed_in(page_answer()), ('11', [['theme', 'dark']]))
        self.assertEqual(desktop_app.signed_in(page_answer(storage=())), ('11', []))
        self.assertEqual(desktop_app.signed_in(json.dumps({'origin': 'https://tapas.io', 'answer': '{"data":{"user":{"id":7}}}', 'storage': None})), ('7', []))
        answers = [page_answer(user=None), page_answer(origin='https://tapas.io.example.com'), page_answer(origin='null'), None, '', 'not json',
                   '[]', '{}', '7', '[' * 100000, json.dumps({'origin': 'https://tapas.io', 'answer': 'PRIVATE not json', 'storage': []}),
                   json.dumps({'origin': 'https://tapas.io', 'answer': None, 'storage': []}),
                   json.dumps({'origin': 'https://tapas.io', 'answer': '{"data":{"user":{"id":11}}}'}),
                   json.dumps({'origin': 'https://tapas.io', 'answer': '{"data":{"user":{"id":11}},"error_details":"x"}', 'storage': []})]
        for text in answers:
            self.assertIsNone(desktop_app.signed_in(text))

    def test_qt_cookies_become_plain_records_that_convert_to_the_saved_session_shape(self):
        session = desktop_app.cookie_record(qt_cookie())
        self.assertEqual(session, {'name': 'session', 'value': 'PRIVATE_COOKIE', 'domain': '.tapas.io', 'path': '/', 'expires': None,
                                   'httpOnly': True, 'secure': True, 'sameSite': 'Lax'})
        kept = desktop_app.cookie_record(qt_cookie(b'remember', b'caf\xc3\xa9', 'tapas.io', '/account', 1790893649, 'None_', False, False))
        self.assertEqual(kept, {'name': 'remember', 'value': 'caf\u00e9', 'domain': 'tapas.io', 'path': '/account', 'expires': 1790893649,
                                'httpOnly': False, 'secure': False, 'sameSite': 'None_'})
        self.assertEqual(desktop_app.cookie_record(qt_cookie(value=b'\xff'))['value'], '\ufffd')
        state = desktop_app.storage_state([session, kept, desktop_app.cookie_record(qt_cookie(domain='.google.com'))])
        self.assertEqual([(item['name'], item['expires'], item['sameSite']) for item in state['cookies']],
                         [('remember', 1790893649, 'None'), ('session', -1, 'Lax')])
        for name, saved in (('Default', 'Lax'), ('Strict', 'Strict')):
            record = desktop_app.cookie_record(qt_cookie(same_site=name))
            self.assertEqual(desktop_app.storage_state([record])['cookies'][0]['sameSite'], saved)

    def test_close_and_instance_decisions(self):
        for phase, busy, action in [('starting', False, 'finish'), ('open', False, 'finish'), ('starting', True, 'confirm'),
                                    ('open', True, 'confirm'), ('stopping', False, 'ignore'), ('stopping', True, 'ignore'),
                                    ('moving', False, 'ignore'), ('moving', True, 'ignore'), ('failed', False, 'finish'),
                                    ('done', False, 'accept'), ('done', True, 'accept')]:
            self.assertEqual(desktop_app.close_action(phase, busy), action)
        self.assertTrue(desktop_app.raise_requested(b'raise\n'))
        for message in (b'', b'raise', b'quit\n', b'raise\nopen /etc/passwd\n'):
            self.assertFalse(desktop_app.raise_requested(message))
        name = desktop_app.server_name(self.state)
        self.assertRegex(name, r'^comic-archiver-[a-f0-9]{16}$')
        self.assertEqual(name, desktop_app.server_name(self.state))
        self.assertNotEqual(name, desktop_app.server_name(self.home / 'another-user/comic-archiver'))

    def test_self_test_report_and_startup_failure_wording(self):
        with patch('comic_archive.desktop_app.metadata.version', return_value='9.8.7'):
            self.assertEqual(desktop_app.self_test_result(True, True, cookie=True), ({'ok': True, 'version': '9.8.7', 'signin_cookie': True}, 0))
        for loaded, found, timed_out, reason in [(False, None, False, 'dashboard_not_loaded'), (True, False, False, 'dashboard_incomplete'),
                                                 (True, None, False, 'dashboard_incomplete'), (True, 'yes', False, 'dashboard_incomplete'),
                                                 (True, True, True, 'timeout')]:
            for cookie in (True, False):
                self.assertEqual(desktop_app.self_test_result(loaded, found, timed_out, cookie),
                                 ({'ok': False, 'reason': reason, 'signin_cookie': cookie}, 1))
        # The dashboard alone is not enough: the sign-in cookie path must have worked too.
        for cookie in (False, None, 'yes'):
            self.assertEqual(desktop_app.self_test_result(True, True, cookie=cookie),
                             ({'ok': False, 'reason': 'signin_cookie_check_failed', 'signin_cookie': False}, 1))
        self.assertEqual(desktop_app.self_test_result(True, True)[1], 1)
        text = desktop_app.failure_text(ArchiveError('Storage paths must not be symlinked.'), self.log)
        self.assertEqual(text, f'Comic Archiver could not start.\n\nStorage paths must not be symlinked.\n\nDetails are saved in:\n{self.log}')
        text = desktop_app.failure_text(RuntimeError('PRIVATE_COOKIE'), None)
        self.assertEqual(text, 'Comic Archiver could not start.\n\nSomething unexpected went wrong.')

    # Starting: one instance, one window, failures shown.

    def test_first_launch_opens_one_window_and_shows_the_dashboard_only_after_the_downloader_start(self):
        order = []
        self.runtime.start_worker.side_effect = lambda: order.append('worker')

        def loop():
            window, = self.widgets.QMainWindow.made
            pages, = self.pages
            window.setWindowTitle.assert_called_once_with('Comic Archiver'); window.show.assert_called_once_with()
            pages.reopened.assert_not_called()
            pages.reopened.side_effect = lambda then: order.append('dashboard')
            emit(self.core.QTimer.made[-1].timeout)
            return 0
        self.widgets.QApplication.exec = Mock(side_effect=loop)
        self.assertEqual(desktop_app.main([]), 0)
        self.assertEqual(order, ['worker', 'dashboard'])
        self.runtime_kind.assert_called_once_with(self.home / 'Comic Archiver', site=sites.DEFAULT)
        self.runtime.start.assert_called_once_with()
        self.runtime.stop.assert_called_once_with(wait_seconds=0)
        lock, = self.lock.made
        self.assertEqual(lock.args, (str(self.state / 'desktop.lock'),))
        lock.setStaleLockTime.assert_called_once_with(0)
        self.lock.tryLock.assert_called_once_with(0); self.lock.unlock.assert_called_once_with()
        name = desktop_app.server_name(self.state)
        channel, = self.network.QLocalServer.made
        self.network.QLocalServer.removeServer.assert_called_once_with(name)
        channel.setSocketOptions.assert_called_once_with('user'); channel.listen.assert_called_once_with(name)
        self.assertEqual(self.network.QLocalSocket.made, [])
        app, = self.widgets.QApplication.made
        app.setDesktopFileName.assert_called_once_with('comic-archiver')
        self.network.QNetworkProxyFactory.setUseSystemConfiguration.assert_called_once_with(False)
        # The pages are drawn by Qt itself: no web page, profile or address exists until a sign-in asks for one.
        core = self.q.QtWebEngineCore
        self.assertEqual(core.QWebEngineProfile.made + core.QWebEnginePage.made + self.q.QtWebEngineWidgets.QWebEngineView.made, [])
        app.aboutToQuit.connect.assert_not_called()
        stack, = self.widgets.QStackedWidget.made
        notice, = self.widgets.QLabel.made
        self.assertEqual([call.args[0] for call in stack.addWidget.call_args_list], [notice, self.pages[0].widget])
        self.box.critical.assert_not_called()

    def test_chosen_library_folder_and_an_existing_application_are_used_as_given(self):
        existing = Fake()
        existing.exec = Mock(return_value=0)
        self.widgets.QApplication.instance.return_value = existing
        self.assertEqual(desktop_app.main(['--project-root', '~/Comics']), 0)
        self.runtime_kind.assert_called_once_with(self.home / 'Comics', site=sites.DEFAULT)
        self.assertEqual(self.widgets.QApplication.made, [])
        existing.exec.assert_called_once_with()

    def test_the_engine_is_given_nothing_but_the_windows_own_name(self):
        self.assertEqual(desktop_app.run(self.q, None), 0)
        app, = self.widgets.QApplication.made
        self.assertEqual(app.args, (['comic-archiver'],))
        # Nor the environment, which the downloader and the programs it opens would inherit.
        self.assertNotIn('QTWEBENGINE_DISABLE_SANDBOX', os.environ)

    def test_startup_drops_inherited_web_engine_overrides_before_qt_runs(self):
        for name in qt_engine.UNSAFE_QT_ENVIRONMENT:
            os.environ[name] = 'unsafe'
        self.assertEqual(desktop_app.main([]), 0)
        for name in qt_engine.UNSAFE_QT_ENVIRONMENT:
            self.assertNotIn(name, os.environ)

    def test_second_launch_wakes_the_first_and_starts_no_server_or_worker(self):
        self.held_lock(answered=True, stale=False)
        self.assertEqual(desktop_app.run(self.q, None), 0)
        socket, = self.network.QLocalSocket.made
        socket.connectToServer.assert_called_once_with(desktop_app.server_name(self.state))
        socket.write.assert_called_once_with(b'raise\n')
        socket.disconnectFromServer.assert_called_once_with()
        self.lock.tryLock.assert_called_once_with(0); self.lock.removeStaleLockFile.assert_not_called()
        self.runtime_kind.assert_not_called()
        self.assertEqual(self.widgets.QMainWindow.made + self.network.QLocalServer.made, [])
        self.widgets.QApplication.exec.assert_not_called()
        self.box.information.assert_not_called(); self.box.critical.assert_not_called()

    def test_a_newer_copy_started_while_an_older_one_is_open_says_how_to_switch(self):
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / 'desktop.lock').write_text('4242\npython3.14\nhost\n', encoding='ascii')
        here, there = self.home / 'new', self.home / 'old'
        versions = {here: (0, 2, 4)}
        with patch('comic_archive.uninstall.this_copy', return_value=here), \
                patch('comic_archive.uninstall.running_copy', side_effect=lambda pid: there if pid == 4242 else None), \
                patch('comic_archive.uninstall.copy_version', side_effect=lambda folder: versions[folder]):
            for open_version, told in (((0, 2, 3), True), ((0, 2, 4), False), ((0, 3, 0), False)):
                versions[there] = open_version
                self.held_lock(answered=True, stale=False)
                self.assertEqual(desktop_app.run(self.q, None), 0)
                self.network.QLocalSocket.made[-1].write.assert_called_once_with(b'raise\n')  # Brought forward as well.
                if told:
                    self.box.information.assert_called_once_with(
                        None, 'Comic Archiver', desktop_app.NEWER_WAITING.format(new='0.2.4', old='0.2.3'))
                else:
                    self.box.information.assert_not_called()
                self.box.information.reset_mock()
            # The open window is this very copy (one version), or the lock names no process: it only comes forward.
            for text in ('4242\n', 'garbled\n', ''):
                versions[there] = (0, 2, 3)
                (self.state / 'desktop.lock').write_text(text, encoding='ascii')
                with patch('comic_archive.uninstall.running_copy', return_value=here if text == '4242\n' else there):
                    self.held_lock(answered=True, stale=False)
                    self.assertEqual(desktop_app.run(self.q, None), 0)
                self.box.information.assert_not_called()
        self.runtime_kind.assert_not_called(); self.widgets.QApplication.exec.assert_not_called()

    def test_lock_left_by_a_crash_is_recovered_but_a_silent_live_owner_is_never_displaced(self):
        self.held_lock(answered=False, stale=True)
        with self.assertLogs('comic_archive.desktop', 'INFO'):
            self.assertEqual(desktop_app.run(self.q, None), 0)
        self.assertEqual(self.lock.tryLock.call_count, 2)
        self.runtime.start.assert_called_once_with(); self.widgets.QApplication.exec.assert_called_once_with()
        self.network.QLocalSocket.made[0].write.assert_not_called()
        self.box.information.assert_not_called()
        self.runtime_kind.reset_mock(); self.widgets.QApplication.exec.reset_mock()
        for answered, written in ((False, True), (True, False)):
            self.held_lock(answered=answered, stale=False)
            self.network.QLocalSocket.waitForBytesWritten = Mock(return_value=written)
            self.assertEqual(desktop_app.run(self.q, None), 0)
            self.lock.tryLock.assert_called_once_with(0)
            self.box.information.assert_called_once_with(None, 'Comic Archiver', desktop_app.ALREADY_RUNNING)
            self.box.information.reset_mock()
        self.runtime_kind.assert_not_called(); self.widgets.QApplication.exec.assert_not_called()

    def test_startup_failures_show_a_plain_message_with_the_log_location_and_leave_nothing_running(self):
        for failure, shown in [(ArchiveError('Storage paths must not be symlinked.'), 'Storage paths must not be symlinked.'),
                               (RuntimeError('PRIVATE_COOKIE'), 'Something unexpected went wrong.')]:
            self.runtime.start.side_effect = failure
            with self.assertLogs('comic_archive.desktop', 'ERROR') as logs:
                self.assertEqual(desktop_app.run(self.q, None), 1)
            parent, title, text = self.box.critical.call_args.args
            self.assertEqual((parent, title), (None, 'Comic Archiver'))
            self.assertIn(shown, text); self.assertIn(str(self.log), text)
            self.assertNotIn('PRIVATE', text + logs.output[0]); self.assertIn(type(failure).__name__, logs.output[0])
            self.runtime.stop.assert_called_with(wait_seconds=0)
        self.assertEqual(self.box.critical.call_count, 2); self.assertEqual(self.lock.unlock.call_count, 2)
        self.assertEqual(self.widgets.QMainWindow.made, [])
        self.widgets.QApplication.exec.assert_not_called(); self.runtime.start_worker.assert_not_called()

    def test_unusable_settings_folder_or_lock_is_reported_before_any_runtime_exists(self):
        self.state_dir.side_effect = ArchiveError('Comic Archiver cannot create its settings folder in your home folder.')
        self.assertEqual(desktop_app.run(self.q, None), 1)
        self.assertIn('settings folder', self.box.critical.call_args.args[2])
        self.assertEqual(self.lock.made, [])
        self.state_dir.side_effect = None
        self.lock.tryLock = Mock(return_value=False); self.lock.error = Mock(return_value=self.lock.LockError.PermissionError)
        self.assertEqual(desktop_app.run(self.q, None), 1)
        self.assertIn('settings folder', self.box.critical.call_args.args[2])
        self.assertEqual(self.network.QLocalSocket.made, [])
        self.runtime_kind.assert_not_called(); self.box.information.assert_not_called()

    # The open window.

    def test_the_dashboard_replaces_the_starting_notice_and_reads_and_redraws_the_library_on_its_clocks(self):
        shell = self.opened()
        pages = shell.pages
        # The pages read whichever library the window has now.
        self.assertEqual(pages.actions._runtime(), self.runtime)
        self.assertEqual(shell.notice.args, (desktop_app.STARTING,))
        shell.poll_clock.setInterval.assert_called_once_with(desktop_app.POLL_MS)
        shell.tick_clock.setInterval.assert_called_once_with(desktop_app.CLOCK_MS)
        self.assertEqual((desktop_app.POLL_MS, desktop_app.CLOCK_MS), (5000, 1000))
        emit(shell.poll_clock.timeout); emit(shell.tick_clock.timeout)  # Still starting: nothing to read or draw.
        pages.poll.assert_not_called(); pages.render.assert_called_once_with()
        shell.stack.setCurrentWidget.assert_not_called()
        shell.load()
        shell.stack.setCurrentWidget.assert_called_once_with(pages.widget)
        pages.reopened.assert_called_once_with(None)
        for clock in (shell.poll_clock, shell.tick_clock, shell.signin_clock):
            clock.start.assert_called_once_with()
        emit(shell.poll_clock.timeout)
        pages.poll.assert_called_once_with()
        emit(shell.tick_clock.timeout)
        self.assertEqual(pages.render.call_count, 2)
        shell.load()  # Only once.
        pages.reopened.assert_called_once_with(None)
        # While another site or folder opens, or the window closes, the pages are left alone.
        for phase in ('moving', 'stopping', 'failed'):
            shell.phase = phase
            emit(shell.poll_clock.timeout); emit(shell.tick_clock.timeout)
        self.assertEqual((pages.poll.call_count, pages.render.call_count), (1, 2))
        shell.notice.setText.assert_not_called()

    def test_a_library_that_cannot_be_shown_says_so_with_the_log_location_when_there_is_one(self):
        for log in (True, False):
            shell = self.opened(log=log)
            shell.show_notice(desktop_app.LOAD_FAILED)
            expected = desktop_app.LOAD_FAILED + (f'\n\nDetails are saved in:\n{self.log}' if log else '')
            shell.notice.setText.assert_called_once_with(expected)

    def test_the_pages_work_on_a_thread_and_hear_back_in_the_words_the_person_is_shown(self):
        shell = self.opened()
        done = Mock()
        self.threads.Thread.alive = True
        shell.run(lambda: {'status': 'ok'}, done)
        done.assert_not_called()  # Back on the window's own thread, once the work is over.
        self.threads.Thread.alive = False
        emit(self.core.QTimer.made[-1].timeout)
        done.assert_called_once_with({'status': 'ok'}, None)
        # Refused by the library: its own words.
        shell.run(Mock(side_effect=Refused('library_busy', 'An import is active.')), done)
        emit(self.core.QTimer.made[-1].timeout)
        done.assert_called_with(None, 'An import is active.')
        # Anything else is logged by its kind only, and the person is told that the library did not answer.
        with self.assertLogs('comic_archive.desktop', 'ERROR') as logs:
            shell.run(Mock(side_effect=RuntimeError('PRIVATE_COOKIE')), done)
        emit(self.core.QTimer.made[-1].timeout)
        done.assert_called_with(None, error_text('service_unavailable'))
        self.assertIn('RuntimeError', logs.output[0]); self.assertNotIn('PRIVATE', logs.output[0])
        self.assertEqual(done.call_count, 3)

    def test_backblaze_pages_and_the_save_folder_open_in_the_computers_own_programs(self):
        shell = self.opened()
        external = self.q.QtGui.QDesktopServices.openUrl
        shell.open_url('https://www.backblaze.com/sign-up/cloud-storage')
        self.runtime.folder = self.home / 'Comic Archiver/Library'
        shell.open_folder()
        self.assertEqual([call.args[0].toString() for call in external.call_args_list],
                         ['https://www.backblaze.com/sign-up/cloud-storage', 'file://' + str(self.home / 'Comic Archiver/Library')])
        # The folder is the open library's own, never one a page names.
        shell.runtime = Mock(folder=self.home / 'Other/Library')
        shell.open_folder()
        self.assertEqual(external.call_args.args[0].toString(), 'file://' + str(self.home / 'Other/Library'))

    def test_another_launch_brings_this_window_forward_and_nothing_else_does(self):
        shell = desktop_app.Shell(self.q, desktop_app.application(self.q), self.runtime)
        shell.listen('comic-archive-test')

        def connect(message, waiting=0):
            socket = Mock(); socket.readAll.return_value.data.return_value = message; socket.bytesAvailable.return_value = waiting
            shell.channel.nextPendingConnection.return_value = socket
            emit(shell.channel.newConnection)
            return socket
        connect(b'raise\n', waiting=6)  # The window is still being built.
        shell.open()
        shell.window.show.reset_mock(); shell.window.isMinimized.return_value = False
        socket = connect(b'raise\n')
        shell.window.raise_.assert_not_called()
        socket.disconnected.connect.assert_called_once_with(socket.deleteLater)
        emit(socket.readyRead)
        for call in (shell.window.show, shell.window.raise_, shell.window.activateWindow):
            call.assert_called_once_with()
        shell.window.showNormal.assert_not_called()
        shell.window.isMinimized.return_value = True
        connect(b'raise\n', waiting=6)
        shell.window.showNormal.assert_called_once_with()
        connect(b'open /etc/passwd\n', waiting=17)
        shell.channel.nextPendingConnection.return_value = None
        emit(shell.channel.newConnection)
        self.assertEqual(shell.window.raise_.call_count, 2)
        self.network.QLocalServer.listen = Mock(return_value=False)
        with self.assertLogs('comic_archive.desktop', 'WARNING'):
            desktop_app.Shell(self.q, shell.app, self.runtime).listen('comic-archive-test')

    # Signing in to Tapas inside the window.

    def test_sign_in_opens_inside_the_window_on_tapas_own_page_in_a_separate_private_profile(self):
        shell = self.opened()
        core = self.q.QtWebEngineCore
        self.signin.take.return_value = True
        emit(shell.signin_clock.timeout)  # The dashboard is not on screen yet.
        self.signin.take.assert_not_called()
        self.signin.take.return_value = False
        shell.load()
        shell.signin_clock.setInterval.assert_called_once_with(250); shell.signin_clock.start.assert_called_once_with()
        emit(shell.signin_clock.timeout)  # Nobody asked: no web page or profile exists.
        self.assertEqual((len(core.QWebEngineProfile.made), len(core.QWebEnginePage.made), shell.signin_panel), (0, 0, None))
        self.signin.status.assert_not_called()
        self.request_signin(shell)
        self.signin.admit.assert_called_once_with()
        private, = core.QWebEngineProfile.made
        self.assertEqual(private.args, (shell.app,))  # No storage name: Qt keeps nothing of the sign-in on disk.
        self.assertEqual(shell.signin_profile, private)
        self.assertEqual(shell.signin_page.args, (private, shell.window))
        guard, = core.QWebEngineUrlRequestInterceptor.made
        self.assertEqual(guard.args, (private,))
        private.setUrlRequestInterceptor.assert_called_once_with(guard)
        # It names itself as the browser the engine is built from, without Qt's tag, and says nothing else about the requests.
        private.setHttpUserAgent.assert_called_once_with(QT_AGENT.replace(' QtWebEngine/6.11.2', ''))
        for untouched in (private.setHttpAcceptLanguage, private.setPersistentStoragePath,
                          private.setPersistentCookiesPolicy, private.setCachePath, private.scripts):
            untouched.assert_not_called()
        store = private.cookieStore.return_value
        store.loadAllCookies.assert_called_once_with(); store.deleteAllCookies.assert_called_once_with()
        shell.signin_view.setPage.assert_called_once_with(shell.signin_page)
        self.assertEqual(self.loads(shell.signin_view), [SIGNIN])
        self.assertEqual((shell.back.args, shell.retry.args, shell.where.args), (('Cancel sign-in',), ('Try again',), ('',)))
        shell.hint.setText.assert_called_once_with(desktop_app.site_text(desktop_app.SIGNIN_HINT))
        self.assertTrue(shell.hint.setText.call_args.args[0].startswith('Sign in to Tapas below. Comic Archiver carries on by itself once you are signed in.\n'))
        bar, = self.widgets.QHBoxLayout.made
        column, = self.widgets.QVBoxLayout.made
        # The same bar as the dashboard's header: the app's name on the left, what can be done on the right.
        self.assertEqual([call.args[0] for call in bar.addWidget.call_args_list], [shell.title, shell.where, shell.hint, shell.retry, shell.back])
        self.assertEqual(shell.title.args, ('Comic Archiver',))
        panel, header = self.widgets.QWidget.made
        self.assertEqual((panel, header.args, column.args), (shell.signin_panel, (panel,), (panel,)))
        # As wide as the window, with the pages' own side margins.
        self.assertEqual(bar.args, (header,)); bar.setContentsMargins.assert_called_once_with(32, 10, 32, 10)
        # Coloured by the window's own style sheet, light or dark, as the pages' header is: nothing of its own.
        header.setStyleSheet.assert_not_called(); header.setAttribute.assert_called_once_with('styled', True)
        header.setObjectName.assert_called_once_with('signinHeader'); shell.title.setObjectName.assert_called_once_with('signinTitle')
        for name in ('QWidget#signinHeader', 'QLabel#signinTitle'):
            self.assertIn(name, window_style.SHEET)
        self.assertEqual([call.args[0] for call in column.addWidget.call_args_list], [header, shell.signin_message, shell.signin_view])
        shell.stack.addWidget.assert_called_with(shell.signin_panel)
        shell.stack.setCurrentWidget.assert_called_with(shell.signin_panel)
        # Until the first answer from Tapas the panel says what it is doing instead of showing a blank page.
        for part, visible in ((shell.signin_view, False), (shell.where, True), (shell.back, True), (shell.retry, False), (shell.signin_message, True)):
            part.setVisible.assert_called_once_with(visible)
        shell.signin_message.setText.assert_called_once_with('Opening the Tapas sign-in page…')
        store.deleteAllCookies.assert_called_once_with()
        private.clearHttpCache.assert_not_called()  # Qt drops a page asked for while it empties the cache.
        shell.signin_page.history.return_value.clear.assert_called_once_with()
        self.loading(shell, 'started'); shell.signin_view.setVisible.assert_called_once_with(False)
        self.loading(shell, 'succeeded')
        shell.signin_view.setVisible.assert_called_with(True); shell.signin_message.setVisible.assert_called_with(False)
        shell.signin_message.setText.assert_called_with('')
        self.assertEqual(shell.world, 1)  # An isolated world, not the page's own.
        shell.signin_probe.setInterval.assert_called_once_with(500); shell.signin_probe.start.assert_called_once_with()
        self.assertEqual([call.args[0] for call in shell.app.aboutToQuit.connect.call_args_list], [shell.signin_page.deleteLater])
        # The bar always says whose page is showing.
        for text, shown in ((SIGNIN, 'tapas.io'), ('https://accounts.google.com/o/oauth2/auth?x=PRIVATE', 'accounts.google.com'), ('about:blank', '')):
            emit(shell.signin_page.urlChanged, Url(text))
            shell.where.setText.assert_called_with(shown)
        self.q.QtGui.QDesktopServices.openUrl.assert_not_called()

    def test_sign_in_page_keeps_to_https_cannot_reach_this_computer_and_saves_or_grants_nothing(self):
        shell = self.signing_in()
        page = shell.signin_page
        for text, main_frame, allowed in [(SIGNIN, True, True), ('https://accounts.google.com/o/oauth2/auth', True, True),
                                          ('about:srcdoc', False, True), (URL, True, False), (URL, False, False),
                                          ('https://192.168.1.10/', True, False), ('https://router.local/', True, False),
                                          ('http://tapas.io/', True, False), ('file:///etc/passwd', True, False), ('chrome://gpu', True, False)]:
            self.assertEqual(page.acceptNavigationRequest(Url(text), None, main_frame), allowed, text)
        # Going back through the history never lands on the blank page the panel starts from.
        self.assertFalse(page.acceptNavigationRequest(Url('about:blank'), 'back', True))
        self.assertTrue(page.acceptNavigationRequest(Url(SIGNIN), 'back', True))
        self.assertTrue(page.acceptNavigationRequest(Url('about:blank'), 'link', True))
        self.q.QtGui.QDesktopServices.openUrl.assert_not_called()  # Nothing is handed to another browser.
        for text, blocked in [(URL + 'api/v1/context', True), ('http://localhost:4321/', True), ('https://[::1]:4321/', True),
                              ('ws://127.0.0.1:4321/', True), ('https://192.168.1.10/', True),
                              ('https://169.254.169.254/latest/meta-data/', True), ('https://router.local/', True),
                              ('https://device.home.arpa/', True), ('https://tapas.io/who-am-i', False),
                              ('https://accounts.google.com/', False), ('https://8.8.8.8/', False)]:
            info = Mock(); info.requestUrl.return_value = Url(text)
            shell.signin_guard.interceptRequest(info)
            self.assertEqual([call.args for call in info.block.call_args_list], [(True,)] if blocked else [], text)
        # A link meant for a new window opens in the same page or not at all.
        for text, here in (('https://accounts.google.com/popup', True), ('http://example.com/', False), ('javascript:alert(1)', False), (URL, False)):
            request = Mock(); request.requestedUrl.return_value = url = Url(text)
            shell.signin_view.load.reset_mock()
            emit(page.newWindowRequested, request)
            self.assertEqual([call.args for call in shell.signin_view.load.call_args_list], [(url,)] if here else [], text)
            request.openIn.assert_not_called()
        item = Mock()
        emit(shell.signin_profile.downloadRequested, item)
        item.cancel.assert_called_once_with(); item.accept.assert_not_called()
        permission = Mock()
        emit(page.permissionRequested, permission)
        permission.deny.assert_called_once_with(); permission.grant.assert_not_called()

    def test_a_finished_sign_in_is_noticed_from_the_page_itself_and_handed_to_the_downloader(self):
        shell = self.signing_in()
        page, store = shell.signin_page, shell.signin_profile.cookieStore.return_value
        now = [100.0]
        shell.cookies.clock = lambda: now[0]
        for cookie in (qt_cookie(), qt_cookie(b'visitor', b'old', 'tapas.io'), qt_cookie(b'visitor', b'new', 'tapas.io'),
                       qt_cookie(b'gone', b'x'), qt_cookie(b'tracker', b'x', '.google.com')):
            emit(store.cookieAdded, cookie)
        emit(store.cookieRemoved, qt_cookie(b'gone', b'other'))
        emit(store.cookieRemoved, qt_cookie(b'never-seen', b'x'))
        # Qt reports a cookie that a site sets again unchanged as removed twice and never as added: it is still there.
        emit(store.cookieRemoved, qt_cookie())
        emit(store.cookieRemoved, qt_cookie())
        # A removal that no second one followed is real once the grace is over.
        now[0] += shell.cookies.GRACE + 1
        self.loading(shell, 'started'); self.loading(shell, 'stopped')
        page.runJavaScript.assert_not_called()
        self.loading(shell, 'succeeded')
        page.runJavaScript.assert_called_once_with(desktop_app.SIGNIN_READ, 1, shell.signin_answer)
        self.read(shell, None)  # Nothing was asked yet, so the page is asked now.
        self.assertEqual(page.runJavaScript.call_args.args, (desktop_app.SIGNIN_ASK, 1))
        emit(shell.signin_probe.timeout)
        self.read(shell, page_answer(user=None))  # Still a visitor.
        self.assertEqual(self.scripts(shell), [desktop_app.SIGNIN_READ, desktop_app.SIGNIN_ASK] * 2)
        self.signin.finish.assert_not_called(); shell.stack.setCurrentWidget.assert_not_called()
        self.threads.Thread.alive = True
        emit(shell.signin_probe.timeout)
        with self.assertLogs('comic_archive.desktop', 'INFO') as logs:
            self.read(shell, page_answer())
        self.assertEqual(len(logs.output), 1)  # That an account answered, and nothing of it.
        self.assertNotIn('PRIVATE', logs.output[0]); self.assertNotIn('11', logs.output[0])
        (key, cookies, storage), = [call.args for call in self.signin.finish.call_args_list]
        self.assertEqual((key, storage), ('11', [['theme', 'dark']]))
        self.assertEqual(sorted((cookie['name'], cookie['value'], cookie['domain']) for cookie in cookies),
                         [('session', 'PRIVATE_COOKIE', '.tapas.io'), ('tracker', 'x', '.google.com'), ('visitor', 'new', 'tapas.io')])
        # The window keeps nothing of the account once the page is no longer needed.
        self.assertEqual(shell.cookies, {})
        self.assertEqual(self.scripts(shell)[-1], desktop_app.SIGNIN_FORGET)
        self.assertEqual(self.loads(shell.signin_view), [SIGNIN, 'about:blank'])
        self.assertEqual(store.deleteAllCookies.call_count, 2)
        shell.signin_probe.stop.assert_called_once_with(); shell.where.setText.assert_called_with('')
        # Saving takes a while: the panel says so and offers no way back in the middle of it.
        self.assertEqual(shell.signin_stage, 'finishing')
        shell.signin_message.setText.assert_called_with(desktop_app.SIGNIN_FINISHING)
        shell.signin_message.setVisible.assert_called_with(True); shell.signin_view.setVisible.assert_called_with(False)
        shell.back.setVisible.assert_called_with(False); shell.retry.setVisible.assert_called_with(False)
        saving = self.core.QTimer.made[-1]
        emit(saving.timeout)
        emit(shell.back.clicked, False); emit(shell.retry.clicked, False)
        emit(shell.signin_probe.timeout); shell.signin_answer(page_answer()); self.loading(shell, 'failed')
        self.signin.status.return_value = {'status': 'connected'}
        emit(shell.signin_clock.timeout)
        shell.stack.setCurrentWidget.assert_not_called()
        self.signin.finish.assert_called_once(); self.signin.cancel.assert_not_called()
        self.assertEqual(len(self.scripts(shell)), 6)
        self.threads.Thread.alive = False
        emit(saving.timeout)
        shell.stack.setCurrentWidget.assert_called_once_with(shell.pages.widget)
        self.assertIsNone(shell.signin_stage)
        self.assertEqual(store.deleteAllCookies.call_count, 2); self.assertEqual(len(self.loads(shell.signin_view)), 2)

    def test_a_cookie_removed_just_before_the_answer_is_not_saved_the_hand_over_waits_once_for_the_grace(self):
        shell = self.signing_in()
        store, later = shell.signin_profile.cookieStore.return_value, self.core.QTimer.singleShot
        now = [100.0]
        shell.cookies.clock = lambda: now[0]
        emit(store.cookieAdded, qt_cookie())
        emit(store.cookieAdded, qt_cookie(b'dropped', b'x'))
        emit(store.cookieRemoved, qt_cookie(b'dropped', b'x'))
        shell.signin_answer(page_answer())
        self.signin.finish.assert_not_called()
        self.assertEqual(shell.signin_stage, 'page')
        later.assert_called_once()
        # The wait is longer than the grace, so the removal is decided by the time the hand-over goes on.
        self.assertGreater(later.call_args.args[0] / 1000, shell.cookies.GRACE)
        now[0] += later.call_args.args[0] / 1000
        later.call_args.args[1]()
        (key, cookies, storage), = [call.args for call in self.signin.finish.call_args_list]
        self.assertEqual([cookie['name'] for cookie in cookies], ['session'])
        later.assert_called_once()

    def test_a_hand_over_that_waited_goes_on_even_if_removals_keep_coming(self):
        shell = self.signing_in()
        store, later = shell.signin_profile.cookieStore.return_value, self.core.QTimer.singleShot
        shell.cookies.clock = lambda: 100.0
        emit(store.cookieAdded, qt_cookie())
        emit(store.cookieRemoved, qt_cookie())
        shell.signin_answer(page_answer())
        later.assert_called_once()
        self.assertFalse(shell.cookies.settled())
        later.call_args.args[1]()
        self.signin.finish.assert_called_once()
        later.assert_called_once()

    def test_a_postponed_hand_over_does_nothing_when_the_sign_in_was_left_in_the_meantime(self):
        shell = self.signing_in()
        store, later = shell.signin_profile.cookieStore.return_value, self.core.QTimer.singleShot
        shell.cookies.clock = lambda: 100.0
        emit(store.cookieAdded, qt_cookie())
        emit(store.cookieRemoved, qt_cookie())
        shell.signin_answer(page_answer())
        emit(shell.back.clicked, False)
        later.call_args.args[1]()
        self.signin.finish.assert_not_called()

    def test_cookies_that_are_settled_are_handed_over_at_once_without_waiting(self):
        shell = self.signing_in()
        store = shell.signin_profile.cookieStore.return_value
        now = [100.0]
        shell.cookies.clock = lambda: now[0]
        emit(store.cookieAdded, qt_cookie())
        emit(store.cookieAdded, qt_cookie(b'dropped', b'x'))
        emit(store.cookieRemoved, qt_cookie(b'dropped', b'x'))
        now[0] += shell.cookies.GRACE + 0.1
        shell.signin_answer(page_answer())
        self.core.QTimer.singleShot.assert_not_called()
        self.signin.finish.assert_called_once()

    def test_only_tapas_own_page_is_asked_who_is_signed_in(self):
        shell = self.signing_in()
        page = shell.signin_page
        for text in ('https://accounts.google.com/o/oauth2/auth', 'https://www.tapas.io/', 'http://tapas.io/', 'about:blank'):
            page.url.return_value = Url(text)
            self.loading(shell, 'succeeded'); emit(shell.signin_probe.timeout)
        page.runJavaScript.assert_not_called()
        page.url.return_value = Url(SIGNIN)
        emit(shell.signin_probe.timeout)
        page.url.return_value = Url('https://accounts.google.com/o/oauth2/auth')  # The page moved on before the answer came.
        self.read(shell, None)
        self.assertEqual(self.scripts(shell), [desktop_app.SIGNIN_READ])
        page.url.return_value = Url(SIGNIN)
        emit(shell.signin_probe.timeout)
        self.read(shell, page_answer(origin='https://accounts.google.com'))  # Not Tapas's word, whatever it says.
        self.signin.finish.assert_not_called()
        self.assertEqual(shell.signin_stage, 'page')
        page.url.return_value = Url('https://accounts.google.com/o/oauth2/auth')
        page.runJavaScript.reset_mock()
        emit(shell.back.clicked, False)
        page.runJavaScript.assert_not_called()  # Another site's storage is not this app's to clear.
        self.signin.cancel.assert_called_once_with()

    def test_back_to_comic_archive_cancels_the_sign_in_forgets_the_page_and_a_later_sign_in_starts_clean(self):
        shell = self.signing_in()
        page, store = shell.signin_page, shell.signin_profile.cookieStore.return_value
        emit(store.cookieAdded, qt_cookie())
        emit(shell.signin_probe.timeout)
        emit(shell.back.clicked, False)
        self.signin.cancel.assert_called_once_with(); self.signin.finish.assert_not_called()
        shell.stack.setCurrentWidget.assert_called_once_with(shell.pages.widget)
        self.assertEqual((shell.signin_stage, shell.cookies), (None, {}))
        shell.signin_probe.stop.assert_called_once_with()
        self.assertEqual(self.loads(shell.signin_view), [SIGNIN, 'about:blank'])
        self.assertEqual(self.scripts(shell), [desktop_app.SIGNIN_READ, desktop_app.SIGNIN_FORGET])
        self.assertEqual(store.deleteAllCookies.call_count, 2)
        # What Qt still delivers for the page that was left changes nothing.
        shell.signin_answer(page_answer()); self.loading(shell, 'failed'); self.loading(shell, 'succeeded')
        emit(shell.back.clicked, False); emit(shell.retry.clicked, False); emit(shell.signin_probe.timeout)
        emit(page.renderProcessTerminated, 'crashed', 9)
        request = Mock(); request.requestedUrl.return_value = Url('https://accounts.google.com/popup')
        emit(page.newWindowRequested, request)
        self.signin.status.return_value = {'status': 'cancelled', 'code': 'signin_incomplete'}
        emit(shell.signin_clock.timeout)
        self.signin.cancel.assert_called_once_with(); self.signin.finish.assert_not_called(); self.signin.fail.assert_not_called()
        self.assertEqual((len(self.loads(shell.signin_view)), len(self.scripts(shell))), (2, 2))
        shell.stack.setCurrentWidget.assert_called_once_with(shell.pages.widget)
        # The second sign-in uses the same panel, with the cookie store emptied again first.
        shell.signin_profile.clearHttpCache.assert_called_once_with()
        emit(shell.signin_profile.clearHttpCacheCompleted)
        self.request_signin(shell)
        self.assertEqual((len(self.q.QtWebEngineCore.QWebEngineProfile.made), len(self.widgets.QWidget.made)), (1, 2))
        self.assertEqual(self.loads(shell.signin_view), [SIGNIN, 'about:blank', SIGNIN])
        self.assertEqual((shell.signin_stage, store.deleteAllCookies.call_count), ('page', 3))
        shell.stack.setCurrentWidget.assert_called_with(shell.signin_panel)
        self.assertEqual(shell.signin_probe.start.call_count, 2)

    def test_the_sign_in_page_waits_for_the_cache_to_empty_and_never_waits_for_ever(self):
        shell = self.signing_in()
        private, view = shell.signin_profile, shell.signin_view
        emit(shell.back.clicked, False)  # Leaving empties the cache, after the page has gone.
        private.clearHttpCache.assert_called_once_with()
        self.assertEqual(self.loads(view), [SIGNIN, 'about:blank'])
        # Qt drops a page asked for while it is emptying the cache, so a new attempt waits for it.
        self.request_signin(shell)
        self.assertEqual((shell.signin_stage, self.loads(view)), ('page', [SIGNIN, 'about:blank']))
        emit(private.clearHttpCacheCompleted)
        self.assertEqual(self.loads(view), [SIGNIN, 'about:blank', SIGNIN])
        emit(private.clearHttpCacheCompleted)
        self.assertEqual(len(self.loads(view)), 3)
        # Nothing from Tapas in time: plain words and Try again, not an endless wait.
        emit(shell.signin_probe.timeout)
        self.assertEqual((shell.signin_stage, shell.signin_opening), ('page', True))
        shell.signin_since -= desktop_app.SIGNIN_OPEN_SECONDS
        with self.assertLogs('comic_archive.desktop', 'WARNING'):
            emit(shell.signin_probe.timeout)
        self.assertEqual(shell.signin_stage, 'offline')
        shell.signin_message.setText.assert_called_with(desktop_app.site_text(desktop_app.SIGNIN_OFFLINE))
        view.stop.assert_called_once_with()
        # A page that has begun to arrive is shown, not given up on.
        emit(shell.retry.clicked, False)
        self.assertEqual(self.loads(view)[3:], [SIGNIN])
        emit(shell.signin_page.urlChanged, Url(SIGNIN))
        shell.signin_since -= desktop_app.SIGNIN_OPEN_SECONDS
        emit(shell.signin_probe.timeout)
        self.assertEqual((shell.signin_stage, shell.signin_opening), ('page', False))
        view.setVisible.assert_called_with(True); view.stop.assert_called_once_with()
        # A cache that never reports back holds a new attempt only until the time limit; Try again then loads.
        emit(shell.back.clicked, False)
        self.request_signin(shell)
        count = len(self.loads(view))
        shell.signin_since -= desktop_app.SIGNIN_OPEN_SECONDS
        with self.assertLogs('comic_archive.desktop', 'WARNING'):
            emit(shell.signin_probe.timeout)
        emit(private.clearHttpCacheCompleted)  # Late, with the message showing: no page is loaded behind it.
        self.assertEqual((shell.signin_stage, len(self.loads(view))), ('offline', count))
        emit(shell.retry.clicked, False)
        self.assertEqual(self.loads(view)[count:], [SIGNIN])
        self.signin.fail.assert_not_called()

    def test_a_sign_in_page_that_cannot_load_says_so_in_plain_words_and_offers_to_try_again(self):
        shell = self.signing_in()
        emit(shell.retry.clicked, False)  # Nothing to retry while the page is showing.
        self.assertEqual(self.loads(shell.signin_view), [SIGNIN])
        # An error status that still shows the site's own page, such as a visitor check, is that site's page.
        self.loading(shell, 'failed', domain='http-status')
        self.assertEqual((shell.signin_stage, self.scripts(shell)), ('page', [desktop_app.SIGNIN_READ]))
        # A page that is showing stays when the engine reports the rest of it as failed; the log says what kind.
        with self.assertLogs('comic_archive.desktop', 'WARNING') as logs:
            self.loading(shell, 'failed')
        self.assertIn('connection', logs.output[0]); self.assertNotIn('http', logs.output[0])
        self.assertEqual((shell.signin_stage, len(self.scripts(shell))), ('page', 2))
        with self.assertLogs('comic_archive.desktop', 'WARNING'):
            self.loading(shell, 'failed', error_page=True)
        self.assertEqual(shell.signin_stage, 'offline')
        shell.signin_message.setText.assert_called_with(desktop_app.site_text(desktop_app.SIGNIN_OFFLINE))
        self.assertIn('Try again', desktop_app.SIGNIN_OFFLINE); self.assertIn('internet connection', desktop_app.SIGNIN_OFFLINE)
        for part, visible in ((shell.signin_message, True), (shell.retry, True), (shell.back, True), (shell.signin_view, False), (shell.where, False)):
            part.setVisible.assert_called_with(visible)
        shell.stack.setCurrentWidget.assert_not_called()
        emit(shell.signin_probe.timeout); self.loading(shell, 'succeeded')  # The engine's own error page is not asked anything.
        self.assertEqual(len(self.scripts(shell)), 2)
        emit(shell.signin_clock.timeout)
        self.assertEqual(shell.signin_stage, 'offline')
        emit(shell.retry.clicked, False)
        self.assertEqual((shell.signin_stage, self.loads(shell.signin_view)), ('page', [SIGNIN, SIGNIN]))
        shell.retry.setVisible.assert_called_with(False); shell.signin_view.setVisible.assert_called_with(False)
        shell.signin_message.setText.assert_called_with(desktop_app.site_text(desktop_app.SIGNIN_OPENING))
        self.loading(shell, 'succeeded')
        shell.signin_view.setVisible.assert_called_with(True); shell.signin_message.setText.assert_called_with('')
        with self.assertLogs('comic_archive.desktop', 'WARNING'):
            self.loading(shell, 'failed', error_page=True, domain='http-status')
        self.assertEqual(shell.signin_stage, 'offline')
        # A page that never arrived and fails without an error page of the engine's is not left as a blank wait.
        emit(shell.retry.clicked, False)
        with self.assertLogs('comic_archive.desktop', 'WARNING'):
            self.loading(shell, 'failed')
        self.assertEqual(shell.signin_stage, 'offline')
        # One that answers is shown at once, without waiting for the engine to call it loaded.
        emit(shell.retry.clicked, False)
        self.assertTrue(shell.signin_opening)
        shell.signin_answer(page_answer(origin='https://accounts.google.com'))
        self.assertTrue(shell.signin_opening)
        shell.signin_answer(page_answer(user=None))
        self.assertEqual((shell.signin_stage, shell.signin_opening), ('page', False))
        shell.signin_view.setVisible.assert_called_with(True)
        with self.assertLogs('comic_archive.desktop', 'WARNING'):
            self.loading(shell, 'failed')
        self.assertEqual(shell.signin_stage, 'page')
        with self.assertLogs('comic_archive.desktop', 'WARNING'):
            self.loading(shell, 'failed', error_page=True)
        self.signin.fail.assert_not_called(); self.signin.cancel.assert_not_called()
        emit(shell.back.clicked, False)
        self.signin.cancel.assert_called_once_with()
        shell.stack.setCurrentWidget.assert_called_once_with(shell.pages.widget)
        self.assertIsNone(shell.signin_stage)

    def test_a_sign_in_that_runs_out_of_time_or_whose_page_crashes_returns_to_the_dashboard(self):
        shell = self.signing_in()
        emit(shell.signin_clock.timeout)
        shell.stack.setCurrentWidget.assert_not_called()
        self.signin.status.return_value = {'status': 'error', 'code': 'signin_incomplete'}
        emit(shell.signin_clock.timeout)
        shell.stack.setCurrentWidget.assert_called_once_with(shell.pages.widget)
        self.assertEqual((shell.signin_stage, self.loads(shell.signin_view)), (None, [SIGNIN, 'about:blank']))
        self.signin.cancel.assert_not_called(); self.signin.finish.assert_not_called()
        emit(shell.signin_profile.clearHttpCacheCompleted)
        self.request_signin(shell)
        shell.stack.setCurrentWidget.reset_mock()
        emit(shell.signin_page.renderProcessTerminated, 'normal', 0)
        self.signin.fail.assert_not_called()
        with self.assertLogs('comic_archive.desktop', 'ERROR'):
            emit(shell.signin_page.renderProcessTerminated, 'crashed', 9)
        self.signin.fail.assert_called_once_with('signin_failed')
        shell.stack.setCurrentWidget.assert_called_once_with(shell.pages.widget)
        self.assertIsNone(shell.signin_stage)
        shell.notice.setText.assert_not_called()  # The dashboard itself is fine.

    def test_the_dashboard_behind_a_sign_in_never_takes_the_window_back_by_itself(self):
        shell = self.signing_in()
        pages = shell.pages
        # The pages go on reading and redrawing the library behind the panel.
        emit(shell.poll_clock.timeout); emit(shell.tick_clock.timeout)
        pages.poll.assert_called_once_with()
        # What the dashboard puts on screen meanwhile is only remembered.
        shell.show(shell.notice)
        shell.stack.setCurrentWidget.assert_not_called()
        self.assertEqual(shell.signin_stage, 'page')
        self.signin.cancel.assert_not_called(); self.signin.fail.assert_not_called()
        emit(shell.back.clicked, False)
        shell.stack.setCurrentWidget.assert_called_once_with(shell.notice)  # What the dashboard last had to say.
        shell.show(pages.widget)
        shell.stack.setCurrentWidget.assert_called_with(pages.widget)

    def test_a_sign_in_held_before_it_begins_or_overtaken_by_closing_shows_no_tapas_page(self):
        shell = self.opened()
        shell.load()
        shell.stack.setCurrentWidget.reset_mock()

        def held():
            self.signin.status.return_value = {'status': 'error', 'code': 'network_held'}
        self.signin.admit.side_effect = held
        self.request_signin(shell)
        self.signin.admit.side_effect = None
        self.threads.Thread.alive = True
        self.signin.take.return_value, self.signin.status.return_value = True, {'status': 'running'}
        emit(shell.signin_clock.timeout)
        admitting = self.core.QTimer.made[-1]
        shell.window.closeEvent(Mock())
        self.signin.cancel.assert_called_once_with()
        self.threads.Thread.alive = False
        emit(admitting.timeout)
        self.assertEqual((shell.signin_panel, shell.signin_stage, len(self.q.QtWebEngineCore.QWebEngineProfile.made)), (None, None, 0))
        self.assertEqual(self.signin.admit.call_count, 2)
        self.assertEqual([call.args[0] for call in shell.stack.setCurrentWidget.call_args_list], [shell.notice])

    def test_a_sign_in_step_that_fails_ends_the_sign_in_at_once_and_a_later_attempt_builds_afresh(self):
        shell = self.opened()
        shell.load()
        shell.stack.setCurrentWidget.reset_mock()
        core = self.q.QtWebEngineCore
        shell.stack.addWidget.side_effect = RuntimeError('PRIVATE text')  # The last step of the build.
        with self.assertLogs('comic_archive.desktop', 'ERROR') as logs:
            self.request_signin(shell)
        self.assertNotIn('PRIVATE', ''.join(logs.output))
        self.signin.fail.assert_called_once_with('signin_failed')
        self.assertEqual((shell.signin_panel, shell.signin_stage, shell.cookies), (None, None, {}))
        failed = core.QWebEngineProfile.made[-1]
        self.assertEqual((shell.signin_page, shell.signin_view, shell.signin_profile), (None, None, None))
        shell.stack.setCurrentWidget.assert_called_once_with(shell.pages.widget)
        view = self.q.QtWebEngineWidgets.QWebEngineView.made[-1]
        for part in (failed, core.QWebEnginePage.made[-1], view):
            part.deleteLater.assert_called_once_with()
        shell.app.aboutToQuit.connect.assert_not_called()
        # The next attempt starts from nothing and works.
        shell.stack.addWidget.side_effect = None
        self.request_signin(shell)
        self.assertEqual((shell.signin_stage, len(core.QWebEngineProfile.made)), ('page', 2))
        self.assertEqual(self.signin.fail.call_count, 1)
        self.assertEqual(len(shell.app.aboutToQuit.connect.call_args_list), 1)
        shell.stack.setCurrentWidget.assert_called_with(shell.signin_panel)

    def test_a_failure_while_opening_the_page_or_handing_over_the_answer_does_not_strand_the_dashboard(self):
        shell = self.signing_in()
        shell.signin_view.load.side_effect = RuntimeError('PRIVATE text')
        shell.signin_page.url.return_value = Url(SIGNIN)
        with self.assertLogs('comic_archive.desktop', 'ERROR') as logs:
            shell.signin_answer(page_answer())
        self.assertNotIn('PRIVATE', ''.join(logs.output))
        self.signin.fail.assert_called_once_with('signin_failed'); self.signin.finish.assert_not_called()
        self.assertEqual(shell.signin_stage, None)
        shell.stack.setCurrentWidget.assert_called_once_with(shell.pages.widget)
        # The same when the thread cannot be started for a request that was already taken.
        shell = self.signing_in()
        self.signin.fail.reset_mock(); shell.stack.setCurrentWidget.reset_mock()
        self.signin.take.return_value = True
        with patch.object(shell, 'background', side_effect=RuntimeError('PRIVATE text')), self.assertLogs('comic_archive.desktop', 'ERROR'):
            emit(shell.signin_clock.timeout)
        self.signin.fail.assert_called_once_with('signin_failed')

    # Closing.

    def test_closing_while_idle_shows_finishing_up_stops_the_runtime_and_only_then_quits(self):
        shell = self.opened()
        shell.load()
        self.threads.Thread.alive = True
        event = Mock()
        shell.window.closeEvent(event)
        event.ignore.assert_called_once_with(); event.accept.assert_not_called()
        self.runtime.stop.assert_called_once_with()
        self.assertEqual(shell.phase, 'stopping')
        shell.notice.setText.assert_called_once_with(desktop_app.FINISHING)
        shell.stack.setCurrentWidget.assert_called_with(shell.notice)
        for clock in (shell.poll_clock, shell.tick_clock, shell.signin_clock):
            clock.stop.assert_called_once_with()
        waiting = self.core.QTimer.made[-1]
        emit(waiting.timeout)
        again = Mock()
        shell.window.closeEvent(again)
        again.ignore.assert_called_once_with(); again.accept.assert_not_called()
        waiting.stop.assert_not_called(); shell.app.quit.assert_not_called()
        self.threads.Thread.alive = False
        emit(waiting.timeout)
        waiting.stop.assert_called_once_with(); shell.app.quit.assert_called_once_with()
        self.assertEqual(shell.phase, 'done')
        final = Mock()
        shell.window.closeEvent(final)
        final.accept.assert_called_once_with(); final.ignore.assert_not_called()
        self.runtime.stop.assert_called_once_with(); self.runtime.busy.assert_called_once_with()
        self.box.question.assert_not_called()

    def test_closing_during_a_download_asks_in_plain_words_and_respects_the_answer(self):
        shell = self.opened()
        shell.load()
        self.runtime.busy.return_value = 'download'
        self.box.question.return_value = self.box.StandardButton.No
        event = Mock()
        shell.window.closeEvent(event)
        event.ignore.assert_called_once_with(); event.accept.assert_not_called()
        self.runtime.stop.assert_not_called()
        self.assertEqual(shell.phase, 'open')
        parent, title, text, buttons, default = self.box.question.call_args.args
        self.assertEqual((parent, title, buttons, default), (shell.window, 'Comic Archiver', 3, self.box.StandardButton.No))
        self.assertEqual(text, desktop_app.CLOSE_DOWNLOAD)
        self.assertIn('Everything saved so far is kept', text); self.assertIn('The next time you open Comic Archiver, press Go to continue', text)
        self.assertIn('Saving stops right away', text); self.assertNotIn('background', text)
        self.box.question.return_value = self.box.StandardButton.Yes
        shell.window.closeEvent(Mock())
        self.runtime.stop.assert_called_once_with()
        emit(self.core.QTimer.made[-1].timeout)
        shell.app.quit.assert_called_once_with()

    def test_closing_during_sign_in_does_not_claim_that_a_download_is_being_saved(self):
        shell = self.opened()
        shell.load()
        self.runtime.busy.return_value = 'signin'
        self.box.question.return_value = self.box.StandardButton.No
        shell.window.closeEvent(Mock())
        self.assertEqual(self.box.question.call_args.args[2], desktop_app.site_text(desktop_app.CLOSE_SIGNIN))
        self.assertNotIn('saving', desktop_app.CLOSE_SIGNIN)
        self.runtime.stop.assert_not_called()

    def test_the_sign_in_texts_name_the_site_that_is_open(self):
        self.runtime.site = replace(sites.DEFAULT, key='example', name='Example', available=True)
        shell = self.signing_in()
        hint = shell.hint.setText.call_args.args[0]
        self.assertIn('Example', hint); self.assertNotIn('Tapas', hint)
        self.assertIn('Example', shell.signin_message.setText.call_args.args[0])
        self.runtime.busy.return_value = 'signin'
        self.box.question.return_value = self.box.StandardButton.No
        shell.window.closeEvent(Mock())
        self.assertIn('Signing in to Example', self.box.question.call_args.args[2])

    def test_the_sign_in_bar_names_the_site_signed_in_to_now_and_asks_for_an_email_and_password(self):
        shell = self.signing_in()
        hint = shell.hint.setText.call_args.args[0]
        self.assertIn('Sign in to Tapas below', hint)
        # Google refuses a sign-in inside an app's window, and Facebook may: the site's own password always works.
        self.assertIn('Use your email and password: Google and Facebook buttons may not work here.', hint)
        # Which site gets the password is said plainly: the comic site, not Google, Facebook or this app.
        self.assertIn('first add a password to your Tapas account: sign in to Tapas in your usual web browser', hint)
        shell.signin_back()
        # Switched to another site in the dashboard: the panel built for Tapas is used again, and says so.
        self.runtime.site = replace(sites.DEFAULT, key='example', name='Example', available=True)
        self.request_signin(shell)
        self.assertEqual(len(self.q.QtWebEngineCore.QWebEngineProfile.made), 1)
        hint = shell.hint.setText.call_args.args[0]
        self.assertIn('Sign in to Example below', hint); self.assertIn('add a password to your Example account', hint)
        self.assertNotIn('Tapas', hint)

    def test_the_computer_stays_awake_only_while_a_download_runs_and_may_sleep_once_it_ends_or_the_window_closes(self):
        with patch('comic_archive.desktop_app.osdep.keep_awake') as awake:
            shell = self.opened()
            shell.awake_clock.setInterval.assert_called_once_with(5000); shell.awake_clock.start.assert_called_once_with()
            self.runtime.busy.return_value = 'download'
            emit(shell.awake_clock.timeout)  # The dashboard is not open yet.
            awake.assert_not_called(); self.runtime.busy.assert_not_called()
            shell.load()
            emit(shell.awake_clock.timeout); emit(shell.awake_clock.timeout)
            self.assertEqual([call.args for call in awake.call_args_list], [(True,)])  # Asked once, not every time.
            for busy in ('signin', None, None):  # Paused, finished or failed: nothing is being saved.
                self.runtime.busy.return_value = busy
                emit(shell.awake_clock.timeout)
            self.assertEqual([call.args for call in awake.call_args_list], [(True,), (False,)])
            self.runtime.busy.return_value = 'download'
            emit(shell.awake_clock.timeout)
            # Closing lets the computer sleep at once, while the download is still being stopped.
            self.box.question.return_value = self.box.StandardButton.Yes
            self.threads.Thread.alive = True
            shell.window.closeEvent(Mock())
            self.assertEqual([call.args for call in awake.call_args_list], [(True,), (False,), (True,), (False,)])
            shell.awake_clock.stop.assert_called_once_with(); shell.app.quit.assert_not_called()
            self.threads.Thread.alive = False
            emit(self.core.QTimer.made[-1].timeout)
            shell.app.quit.assert_called_once_with()
            self.assertEqual(awake.call_count, 4)
            # Whatever ends the window lets the computer sleep again.
            other = self.opened(); other.load()
            emit(other.awake_clock.timeout)
            other.quit()
            self.assertEqual([call.args for call in awake.call_args_list[4:]], [(True,), (False,)])

    def test_a_slow_first_start_says_that_it_can_take_a_minute(self):
        self.assertEqual(desktop_app.STARTING, 'Starting Comic Archiver…\n\nThe first start can take up to a minute.')
        self.assertIn('already open or still starting. The first start can take up to a minute; its window then appears by itself.',
                      desktop_app.ALREADY_RUNNING)

    def test_closing_during_a_window_sign_in_cancels_it_and_hands_nothing_to_the_downloader(self):
        shell = self.signing_in()
        emit(shell.signin_profile.cookieStore.return_value.cookieAdded, qt_cookie())
        self.runtime.busy.return_value = 'signin'
        self.box.question.return_value = self.box.StandardButton.No
        shell.window.closeEvent(Mock())
        self.assertEqual(self.box.question.call_args.args[2], desktop_app.site_text(desktop_app.CLOSE_SIGNIN))
        self.signin.cancel.assert_not_called()
        self.assertEqual((shell.signin_stage, shell.phase), ('page', 'open'))
        self.box.question.return_value = self.box.StandardButton.Yes
        self.threads.Thread.alive = True
        asked = self.signin.take.call_count
        shell.window.closeEvent(Mock())
        self.signin.cancel.assert_called_once_with()
        shell.signin_clock.stop.assert_called_once_with()
        self.assertEqual((shell.signin_stage, shell.cookies), (None, {}))
        self.assertEqual(self.loads(shell.signin_view), [SIGNIN, 'about:blank'])
        shell.notice.setText.assert_called_once_with(desktop_app.FINISHING)
        shell.stack.setCurrentWidget.assert_called_once_with(shell.notice)
        # Whatever the page or the dashboard still reports is too late.
        emit(shell.signin_clock.timeout); shell.signin_answer(page_answer()); self.loading(shell, 'failed')
        emit(shell.signin_page.renderProcessTerminated, 'crashed', 9); emit(shell.back.clicked, False)
        self.assertEqual(self.signin.take.call_count, asked)
        self.signin.finish.assert_not_called(); self.signin.fail.assert_not_called(); self.signin.cancel.assert_called_once_with()
        self.threads.Thread.alive = False
        emit(self.core.QTimer.made[-1].timeout)
        shell.app.quit.assert_called_once_with()
        shell.stack.setCurrentWidget.assert_called_once_with(shell.notice)

    def test_closing_while_a_sign_in_is_being_saved_asks_it_to_stop_and_leaves_the_outcome_to_it(self):
        shell = self.signing_in()
        self.threads.Thread.alive = True
        emit(shell.signin_probe.timeout)
        self.read(shell, page_answer())
        saving = self.core.QTimer.made[-1]
        shell.window.closeEvent(Mock())
        self.signin.finish.assert_called_once(); self.signin.cancel.assert_called_once_with()
        self.assertIsNone(shell.signin_stage)
        shell.signin_probe.stop.assert_called_once_with()  # The page was already cleared when saving began.
        shell.stack.setCurrentWidget.assert_called_once_with(shell.notice)
        stopping = self.core.QTimer.made[-1]
        self.threads.Thread.alive = False
        emit(saving.timeout)
        shell.stack.setCurrentWidget.assert_called_once_with(shell.notice); shell.app.quit.assert_not_called()
        emit(stopping.timeout)
        shell.app.quit.assert_called_once_with()

    def test_the_window_asks_whether_a_download_runs_on_a_thread_and_never_waits_long_for_a_slow_drive(self):
        made, joined = [], []

        class Asleep(InlineThread):
            """A library on a drive that is asleep: the answer has not come back yet."""
            alive = True

            def __init__(self, target, daemon=None):
                self.target, self.daemon = target, daemon
                made.append(self)

            def start(self):
                pass

            def join(self, timeout=None):
                joined.append(timeout)
        with patch('comic_archive.desktop_app.osdep.keep_awake') as awake:
            shell = self.opened(); shell.load()
            self.threads.Thread = Asleep
            self.runtime.busy.return_value = 'download'
            emit(shell.awake_clock.timeout); emit(shell.awake_clock.timeout)
            # One look at a time, on its own thread; the window's own thread is never held by it.
            self.assertEqual((len(made), made[0].daemon), (1, True))
            self.runtime.busy.assert_not_called(); awake.assert_not_called()
            made[0].target()  # The drive answers.
            emit(shell.awake_clock.timeout)
            self.assertEqual([call.args for call in awake.call_args_list], [(True,)])
            self.assertEqual(len(made), 1)
            # Closing waits a moment at most for a fresh answer, then goes by the last one seen.
            self.box.question.return_value = self.box.StandardButton.No
            shell.window.closeEvent(Mock())
            self.assertEqual(joined, [desktop_app.CLOSE_CHECK_SECONDS])
            self.assertEqual(self.box.question.call_args.args[2], desktop_app.CLOSE_DOWNLOAD)
            self.runtime.stop.assert_not_called()
            # An answer about a library the window has since left is not taken for the new one's.
            Asleep.alive = False
            emit(shell.awake_clock.timeout)
            shell.runtime = Mock(busy=Mock(return_value=None))
            self.runtime.busy.return_value = None
            made[-1].target()
            self.assertEqual(shell.busy_seen, 'download')

    def test_ending_the_app_from_outside_stops_the_downloader_as_closing_the_window_does(self):
        shell = self.opened()
        shell.end_clock.setInterval.assert_called_once_with(desktop_app.END_CHECK_MS); shell.end_clock.start.assert_called_once_with()
        emit(shell.end_clock.timeout)
        self.runtime.stop.assert_not_called()
        shell.load()
        self.runtime.busy.return_value = 'download'
        shell.phase = 'moving'
        shell.end(signal.SIGTERM, None)
        self.runtime.stop.assert_not_called()  # A signal handler only takes note.
        emit(shell.end_clock.timeout)
        self.runtime.stop.assert_not_called()  # A library folder change is finished first.
        shell.phase = 'open'
        self.threads.Thread.alive = True
        emit(shell.end_clock.timeout)
        self.box.question.assert_not_called()  # No one is there to answer.
        self.runtime.stop.assert_called_once_with(); self.runtime.signin.cancel.assert_called_once_with()
        self.assertEqual(shell.phase, 'stopping')
        shell.notice.setText.assert_called_with(desktop_app.FINISHING)
        emit(shell.end_clock.timeout)
        self.runtime.stop.assert_called_once_with()
        self.threads.Thread.alive = False
        emit(self.core.QTimer.made[-1].timeout)
        shell.app.quit.assert_called_once_with()

    def test_a_new_versions_setup_has_the_open_window_close_without_asking_as_ending_it_from_outside_does(self):
        shell = self.opened(); shell.load()
        shell.listen('comic-archive-test')
        self.runtime.busy.return_value = 'download'

        def ask():
            socket = Mock(); socket.readAll.return_value.data.return_value = b'close\n'; socket.bytesAvailable.return_value = 6
            shell.channel.nextPendingConnection.return_value = socket
            with self.assertLogs('comic_archive.desktop', 'INFO'):
                emit(shell.channel.newConnection)
            socket.write.assert_called_once_with(b'closing\n'); socket.flush.assert_called_once_with()
        shell.phase = 'moving'
        ask()
        self.runtime.stop.assert_not_called()  # A library folder change is finished first.
        shell.phase = 'open'
        self.threads.Thread.alive = True
        with self.assertLogs('comic_archive.desktop', 'INFO'):
            emit(shell.end_clock.timeout)
        self.box.question.assert_not_called()  # No one is asked, so the setup is never left waiting on a question.
        self.runtime.stop.assert_called_once_with(); self.assertEqual(shell.phase, 'stopping')
        ask()  # Asked again while closing: it says so, and closes once.
        self.runtime.stop.assert_called_once_with()
        shell.window.raise_.assert_not_called()

    def test_the_setup_has_an_open_window_close_and_waits_until_it_is_gone(self):
        socket_kind = self.network.QLocalSocket
        socket_kind.waitForReadyRead = Mock(return_value=True)
        socket_kind.readAll = Mock(return_value=SimpleNamespace(data=lambda: b'closing\n'))
        now = [0.0]
        with patch.object(desktop_app.time, 'sleep', side_effect=lambda seconds: now.__setitem__(0, now[0] + seconds)), \
                patch.object(desktop_app.time, 'monotonic', side_effect=lambda: now[0]):
            # Nothing open: nothing is asked.
            self.assertEqual(desktop_app.main(['--close-for-update']), 0)
            self.assertEqual(socket_kind.made, []); self.lock.unlock.assert_called_once_with()
            # Open: asked to close, and waited for until its lock is free.
            self.lock.tryLock = Mock(side_effect=[False, False, False, True])
            self.assertEqual(desktop_app.main(['--close-for-update']), 0)
            socket, = socket_kind.made
            socket.connectToServer.assert_called_once_with(desktop_app.server_name(self.state))
            socket.write.assert_called_once_with(b'close\n')
            self.assertEqual(now, [1.5])
            # Still open after the wait, or an older version that does not answer: the setup asks the person.
            self.lock.tryLock = Mock(return_value=False)
            self.assertEqual(desktop_app.main(['--close-for-update']), 1)
            self.assertEqual(now, [1.5 + desktop_app.CLOSE_WAIT_SECONDS])
            for ready, reply in ((False, b'closing\n'), (True, b''), (True, b'raise\n')):
                socket_kind.waitForReadyRead = Mock(return_value=ready)
                socket_kind.readAll = Mock(return_value=SimpleNamespace(data=lambda: reply))
                self.assertEqual(desktop_app.main(['--close-for-update']), 1)
            self.assertEqual(now, [1.5 + desktop_app.CLOSE_WAIT_SECONDS])  # Not waited for.
            socket_kind.waitForConnected = Mock(return_value=False)
            self.assertEqual(desktop_app.main(['--close-for-update']), 1)
        self.runtime_kind.assert_not_called(); self.assertEqual(self.widgets.QMainWindow.made, [])
        self.box.information.assert_not_called(); self.box.critical.assert_not_called()

    def test_the_app_listens_for_the_system_asking_it_to_end_only_while_it_runs(self):
        self.assertIn(signal.SIGTERM, desktop_app.ENDING_SIGNALS)
        before = {number: signal.getsignal(number) for number in desktop_app.ENDING_SIGNALS}
        seen = []

        def loop():
            seen.extend(signal.getsignal(number) for number in desktop_app.ENDING_SIGNALS)
            return 0
        self.widgets.QApplication.exec = Mock(side_effect=loop)
        self.assertEqual(desktop_app.main([]), 0)
        self.assertEqual([getattr(handler, '__name__', None) for handler in seen], ['end'] * len(desktop_app.ENDING_SIGNALS))
        self.assertEqual({number: signal.getsignal(number) for number in desktop_app.ENDING_SIGNALS}, before)

    def test_closing_while_the_downloader_is_still_starting_never_shows_the_dashboard(self):
        shell = self.opened()
        self.threads.Thread.alive = True
        shell.background(self.runtime.start_worker, shell.load)
        starting = self.core.QTimer.made[-1]
        shell.window.closeEvent(Mock())
        stopping = self.core.QTimer.made[-1]
        self.threads.Thread.alive = False
        emit(starting.timeout)
        shell.pages.reopened.assert_not_called(); shell.poll_clock.start.assert_not_called()
        self.assertEqual(shell.phase, 'stopping')
        emit(stopping.timeout)
        shell.app.quit.assert_called_once_with()

    def test_background_failures_are_logged_without_their_text_and_still_report_back(self):
        shell = self.opened()
        done = Mock()
        with self.assertLogs('comic_archive.desktop', 'ERROR') as logs:
            shell.background(Mock(side_effect=RuntimeError('PRIVATE_COOKIE')), done)
        done.assert_not_called()
        emit(self.core.QTimer.made[-1].timeout)
        done.assert_called_once_with()
        self.assertIn('RuntimeError', logs.output[0]); self.assertNotIn('PRIVATE', logs.output[0])

    # --self-test.

    def test_self_test_passes_when_the_real_window_shows_the_dashboard_without_lock_log_or_worker(self):
        status, report = self.self_test(self.answer(True, True))
        self.assertEqual((status, report), (0, {'ok': True, 'version': desktop_app.version(), 'signin_cookie': True}))
        # The dashboard is asked to report its first refresh, and is the one the window shows.
        pages, = self.pages
        then, = pages.reopened.call_args.args
        self.assertEqual(then.__name__, 'answered'); pages.drawn.assert_called_once_with()
        self.widgets.QStackedWidget.made[0].setCurrentWidget.assert_called_with(pages.widget)
        # The real sign-in panel and its cookie store are exercised, and no Tapas address is ever loaded.
        signin_page, = self.q.QtWebEngineCore.QWebEnginePage.made
        private, = self.q.QtWebEngineCore.QWebEngineProfile.made
        signin_view, = self.q.QtWebEngineWidgets.QWebEngineView.made
        self.assertEqual(self.loads(signin_view), ['about:blank'])
        signin_page.runJavaScript.assert_not_called()
        self.assertEqual(signin_page.args, (private, self.widgets.QMainWindow.made[0]))
        cookie, origin = private.cookieStore.return_value.setCookie.call_args.args
        self.assertEqual(origin.toString(), 'https://tapas.io/')
        self.assertEqual([item.args for item in cookie.args], [(b'comic_archive_self_test',), (b'synthetic',)])
        cookie.setDomain.assert_called_once_with('.tapas.io'); cookie.setPath.assert_called_once_with('/')
        cookie.setSecure.assert_called_once_with(True); cookie.setHttpOnly.assert_called_once_with(True)
        self.signin.take.assert_not_called(); self.signin.finish.assert_not_called()
        root, = self.runtime_kind.call_args.args
        self.assertEqual(root.name, 'library')
        self.assertNotIn(self.home, root.parents); self.assertFalse(root.parent.exists())
        self.runtime.start.assert_called_once_with(); self.runtime.start_worker.assert_not_called()
        self.runtime.stop.assert_called_once_with(wait_seconds=0)
        self.assertEqual(self.lock.made + self.network.QLocalServer.made, [])
        self.state_dir.assert_not_called(); self.logging.assert_not_called()
        self.assertEqual(list(self.home.iterdir()), [])
        self.assertEqual([os.environ[name] for name in ('QT_QPA_PLATFORM', 'QT_QUICK_BACKEND', 'QTWEBENGINE_CHROMIUM_FLAGS')],
                         ['offscreen', 'software', '--disable-gpu'])
        self.assertNotIn('QTWEBENGINE_DISABLE_SANDBOX', os.environ)
        self.widgets.QApplication.made[0].quit.assert_called_once_with()
        self.assertEqual(self.core.QTimer.singleShot.call_args.args[0], desktop_app.SELF_TEST_SECONDS * 1000)
        self.assertEqual(self.threads.Timer.call_args.args[0], desktop_app.SELF_TEST_SECONDS + 15)
        watchdog = self.threads.Timer.return_value
        watchdog.start.assert_called_once_with(); watchdog.cancel.assert_called_once_with()

    def test_self_test_fails_when_the_dashboard_is_missing_incomplete_closed_or_cannot_start(self):
        os.environ['QT_QPA_PLATFORM'] = 'xcb'
        for loop, reason, cookie in [(self.answer(False), 'dashboard_not_loaded', True), (self.answer(True, False), 'dashboard_incomplete', True),
                                     (self.answer(True, None, cookie=False), 'dashboard_incomplete', False),
                                     (lambda: None, 'dashboard_not_loaded', False)]:
            self.assertEqual(self.self_test(loop), (1, {'ok': False, 'reason': reason, 'signin_cookie': cookie}))
        self.assertEqual(os.environ['QT_QPA_PLATFORM'], 'xcb'); self.assertNotIn('QTWEBENGINE_DISABLE_SANDBOX', os.environ)
        self.assertEqual([app.args for app in self.widgets.QApplication.made], [(['comic-archiver'],)] * 4)
        self.runtime.start.side_effect = ArchiveError('PRIVATE_COOKIE')
        # The report says where in the app it happened, not what the error said: here the line that starts the pieces.
        source = Path(desktop_app.__file__).read_text().splitlines()
        line = 1 + source.index('                runtime.start()', source.index('def self_test(q):'))
        self.assertEqual(self.self_test(lambda: None), (1, {'ok': False, 'reason': 'ArchiveError', 'where': f'comic_archive.desktop_app:{line}'}))
        self.assertEqual(self.runtime.stop.call_count, 5); self.assertEqual(self.threads.Timer.return_value.cancel.call_count, 5)
        self.widgets.QApplication.exec.assert_not_called()

    def test_self_test_times_out_instead_of_hanging_even_when_the_window_never_answers(self):
        def loop():
            self.core.QTimer.singleShot.call_args.args[1]()
            self.answer(True, True)()  # Too late to change the verdict.
        self.assertEqual(self.self_test(loop), (1, {'ok': False, 'reason': 'timeout', 'signin_cookie': False}))
        self.widgets.QApplication.made[0].quit.assert_called_once_with()
        output = io.StringIO()
        with patch('comic_archive.desktop_app.os._exit') as leave, contextlib.redirect_stdout(output):
            self.threads.Timer.call_args.args[1]()
        leave.assert_called_once_with(1)
        self.assertEqual(json.loads(output.getvalue()), {'ok': False, 'reason': 'timeout', 'signin_cookie': False})

    def test_self_test_fails_when_the_sign_in_cookie_never_arrives_or_comes_back_changed(self):
        failed = (1, {'ok': False, 'reason': 'signin_cookie_check_failed', 'signin_cookie': False})

        def never():
            self.answer(True, True, cookie=False)()
            self.widgets.QApplication.made[-1].quit.assert_not_called()  # A good dashboard still waits for the cookie.
            self.core.QTimer.singleShot.call_args.args[1]()
        self.assertEqual(self.self_test(never), failed)
        for changes in ({'domain': '.example.com'}, {'value': b'other'}, {'name': b'another'}, {'expires': None}, {'secure': False},
                        {'http_only': False}, {'path': '/account'}):
            def changed():
                self.observe_cookie(**changes)
                self.answer(True, True, cookie=False)()
                self.core.QTimer.singleShot.call_args.args[1]()
            self.assertEqual(self.self_test(changed), failed, changes)

        def late():
            self.answer(True, True, cookie=False)()
            self.observe_cookie()
        self.assertEqual(self.self_test(late)[0], 0)
        # A build whose toolkit lacks a part the sign-in panel needs must not pass either.
        del self.q.QtWebEngineCore.QWebEngineUrlRequestInterceptor
        status, report = self.self_test(lambda: None)
        self.assertEqual((status, report['ok'], report['reason']), (1, False, 'AttributeError'))
        self.assertRegex(report['where'], r'^comic_archive\.\w+:\d+$')
        self.widgets.QApplication.exec.assert_not_called()

    def test_self_test_reads_a_real_empty_temporary_library_and_never_starts_a_downloader(self):
        self.runtime_kind.side_effect = RUNTIME
        read = []

        def loop():
            # What the dashboard reads, through the same calls, from the library the self-test opened.
            actions = self.pages[-1].actions
            read.append((actions.context(), actions.poll(network=False)))
            context, reading = read[0]
            self.answer(True, reading['account'].get('configured') is False and context['library_folder'].endswith('Library'))()
        with patch('comic_archive.app_runtime.background.start') as start, \
                patch('comic_archive.app_runtime.background.stop', return_value={'running': False}) as stop:
            status, report = self.self_test(loop)
            start.assert_not_called(); stop.assert_called_once()
        self.assertEqual((status, report['ok']), (0, True))
        context, reading = read[0]
        self.assertEqual([site['key'] for site in context['sites'] if site['current']], ['tapas'])
        self.assertEqual(reading['site_auth'], {'status': 'idle'})
        root, = self.runtime_kind.call_args.args
        self.assertEqual(context['library_folder'], str(root / 'Library'))
        self.assertFalse(root.parent.exists())
        self.assertEqual(list(self.home.iterdir()), [])

    def test_main_passes_self_test_and_window_modes_to_their_own_paths(self):
        with patch('comic_archive.desktop_app.self_test', return_value=0) as check, patch('comic_archive.desktop_app.run', return_value=7) as run:
            self.assertEqual(desktop_app.main(['--self-test', '--project-root', '/ignored']), 0)
            check.assert_called_once(); run.assert_not_called()
            self.assertEqual(desktop_app.main(['--project-root', '/library']), 7)
            self.assertEqual(run.call_args.args[1], '/library')
        with patch('comic_archive.desktop_app.uninstall', return_value=3) as uninstall:
            self.assertEqual(desktop_app.main(['--uninstall', '--quiet']), 3)
            self.assertEqual(desktop_app.main(['--uninstall']), 3)
        self.assertEqual([call.args[1] for call in uninstall.call_args_list], [True, False]); run.assert_called_once()

    # Older copies.

    def test_each_older_copy_is_offered_for_removal_once_this_one_is_the_only_window(self):
        older = [self.home / 'old', self.home / 'zip']
        with patch('comic_archive.uninstall.older_copies', return_value=older), \
                patch('comic_archive.uninstall.remove_copy', side_effect=[False]) as remove:
            self.box.question.side_effect = [1, 2]
            desktop_app.remove_older_copies(self.q)
        self.assertEqual([call.args for call in self.box.question.call_args_list],
                         [(None, 'Comic Archiver', desktop_app.OLD_COPY.format(folder=folder), 3, 2) for folder in older])
        remove.assert_called_once_with(self.home / 'old')
        self.box.warning.assert_called_once_with(None, 'Comic Archiver', desktop_app.OLD_COPY_LEFT.format(folder=self.home / 'old'))
        self.box.question.reset_mock(side_effect=True); self.box.warning.reset_mock()
        with patch('comic_archive.uninstall.older_copies', return_value=older[:1]), \
                patch('comic_archive.uninstall.remove_copy', return_value=True), self.assertLogs('comic_archive.desktop', 'INFO'):
            self.box.question.return_value = 1
            desktop_app.remove_older_copies(self.q)
        self.box.warning.assert_not_called()
        with patch('comic_archive.uninstall.older_copies', side_effect=ArchiveError('PRIVATE')), \
                self.assertLogs('comic_archive.desktop', 'WARNING') as logs:
            desktop_app.remove_older_copies(self.q)
        self.assertNotIn('PRIVATE', logs.output[0]); self.assertEqual(self.box.question.call_count, 1)
        with patch('comic_archive.desktop_app.remove_older_copies') as offer:
            self.assertEqual(desktop_app.run(self.q, None), 0)
            offer.assert_called_once_with(self.q)
            self.held_lock(answered=True, stale=False)
            self.assertEqual(desktop_app.run(self.q, None), 0)
            offer.assert_called_once_with(self.q)

    # Uninstalling.

    def removal(self, forgotten=True, removed=True, folder=None):
        """The uninstall step's own work, recorded in the order it happens alongside the lock's."""
        order = Mock()
        self.lock.unlock = order.unlock
        for name, value in (('forget_all', forgotten), ('remove_app_folders', removed), ('app_folder', folder)):
            wrapper = patch('comic_archive.uninstall.' + name, getattr(order, name))
            getattr(order, name).return_value = value
            wrapper.start(); self.addCleanup(wrapper.stop)
        wrapper = patch('comic_archive.uninstall.remove_menu_entry', order.remove_menu_entry)
        wrapper.start(); self.addCleanup(wrapper.stop)
        return order

    def test_uninstalling_asks_first_then_forgets_everything_and_says_where_the_saved_episodes_are(self):
        order = self.removal(folder=self.home / 'App')
        self.box.question.return_value = 2
        self.assertEqual(desktop_app.uninstall(self.q, False), desktop_app.UNINSTALL_INCOMPLETE)
        self.assertEqual(self.box.question.call_args.args[:3], (None, 'Comic Archiver', desktop_app.UNINSTALL_ASK))
        self.assertEqual((self.lock.made, order.mock_calls), ([], []))
        self.box.question.return_value = 1
        self.assertEqual(desktop_app.uninstall(self.q, False), desktop_app.UNINSTALLED)
        self.assertEqual([name for name, *_ in order.mock_calls],
                         ['forget_all', 'unlock', 'remove_app_folders', 'app_folder', 'remove_menu_entry'])
        order.remove_menu_entry.assert_called_once_with(self.home / 'App')
        lock, = self.lock.made
        self.assertEqual(lock.args, (str(self.state / 'desktop.lock'),)); lock.setStaleLockTime.assert_called_once_with(0)
        self.box.information.assert_called_once_with(None, 'Comic Archiver', desktop_app.UNINSTALL_DONE.format(
            library=self.home / 'Comic Archiver') + desktop_app.UNINSTALL_FOLDER.format(folder=self.home / 'App'))
        self.assertIn(f'{self.home / "Comic Archiver"}\n\nTo finish, delete this folder:\n{self.home / "App"}',
                      self.box.information.call_args.args[2])
        self.box.warning.assert_not_called()

    def test_the_windows_uninstaller_is_asked_and_told_nothing_and_reads_the_exit_status(self):
        order = self.removal()
        with patch('comic_archive.desktop_app.app_runtime.saved_root', return_value=self.home / 'Elsewhere'):
            self.assertEqual(desktop_app.uninstall(self.q, True), desktop_app.UNINSTALLED)
        order.remove_menu_entry.assert_not_called()
        self.assertEqual(self.widgets.QApplication.made, [])
        for shown in (self.box.question, self.box.information, self.box.warning):
            shown.assert_not_called()
        order.forget_all.return_value = False
        self.assertEqual(desktop_app.uninstall(self.q, True), desktop_app.UNINSTALL_INCOMPLETE)
        # What lists the library folders, and the menu entry, stay so that uninstalling again finds what stayed.
        self.assertEqual(order.remove_app_folders.call_count, 1)
        order.forget_all.return_value, order.remove_app_folders.return_value = True, False
        self.assertEqual(desktop_app.uninstall(self.q, True), desktop_app.UNINSTALL_INCOMPLETE)
        self.box.question.return_value = 1
        self.assertEqual(desktop_app.uninstall(self.q, False), desktop_app.UNINSTALL_INCOMPLETE)
        self.box.warning.assert_called_once_with(None, 'Comic Archiver', desktop_app.UNINSTALL_FAILED)
        self.box.information.assert_not_called()
        self.assertEqual(order.remove_app_folders.call_count, 3)
        self.assertEqual((desktop_app.UNINSTALLED, desktop_app.UNINSTALL_INCOMPLETE, desktop_app.UNINSTALL_BLOCKED), (0, 1, 3))

    def test_nothing_is_forgotten_while_the_window_is_open_or_the_settings_folder_cannot_be_used(self):
        order = self.removal()
        self.held_lock(answered=True, stale=False)
        self.lock.tryLock = Mock(return_value=False)
        self.assertEqual(desktop_app.uninstall(self.q, True), desktop_app.UNINSTALL_BLOCKED)
        self.box.question.return_value = 1
        self.assertEqual(desktop_app.uninstall(self.q, False), desktop_app.UNINSTALL_BLOCKED)
        self.box.warning.assert_called_once_with(None, 'Comic Archiver', desktop_app.UNINSTALL_OPEN)
        self.box.warning.reset_mock()
        self.lock.removeStaleLockFile = Mock(return_value=True)
        self.lock.tryLock = Mock(side_effect=[False, True])  # Left by a crash: the window is not open.
        self.assertEqual(desktop_app.uninstall(self.q, True), desktop_app.UNINSTALLED)
        order.forget_all.assert_called_once_with(); order.forget_all.reset_mock()
        self.lock.tryLock = Mock(return_value=False); self.lock.error = Mock(return_value=self.lock.LockError.PermissionError)
        self.assertEqual(desktop_app.uninstall(self.q, False), desktop_app.UNINSTALL_INCOMPLETE)
        self.box.warning.assert_called_once_with(None, 'Comic Archiver', desktop_app.UNINSTALL_FAILED)
        self.lock.removeStaleLockFile.assert_called_once_with()
        self.state_dir.side_effect = ArchiveError('Comic Archiver cannot create its settings folder in your home folder.')
        self.assertEqual(desktop_app.uninstall(self.q, True), desktop_app.UNINSTALL_INCOMPLETE)
        order.forget_all.assert_not_called(); order.remove_app_folders.assert_called_once_with()


class WindowsIdentityTests(unittest.TestCase):
    def test_on_windows_the_window_takes_the_icon_that_lies_beside_the_bundled_python(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve(); (root / 'python').mkdir(); (root / 'comic-archiver.ico').write_bytes(b'icon')
            q, app = Mock(), Mock()
            with patch.object(desktop_app.sys, 'executable', str(root / 'python/pythonw.exe')):
                desktop_app.windows_identity(q, app)
                q.QtGui.QIcon.assert_called_once_with(str(root / 'comic-archiver.ico'))
                app.setWindowIcon.assert_called_once_with(q.QtGui.QIcon.return_value)
                (root / 'comic-archiver.ico').unlink(); app.reset_mock()
                desktop_app.windows_identity(q, app)  # Run from the source folder: no icon, and the window still opens.
                app.setWindowIcon.assert_not_called()


if __name__ == '__main__':
    unittest.main()
