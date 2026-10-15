from dataclasses import replace
import ast
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener

from support import home_environment
from comic_archive import app_runtime, desktop_app, sites
from comic_archive.local_web import SESSION_SECONDS
from comic_archive.models import ArchiveError

RUNTIME = app_runtime.Runtime
URL = 'http://127.0.0.1:4321/'
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
            QStandardPaths=kind('QStandardPaths', writableLocation=Mock(return_value='/home/user/Downloads'),
                                StandardLocation=SimpleNamespace(DownloadLocation='downloads')),
            Qt=SimpleNamespace(AlignmentFlag=SimpleNamespace(AlignCenter='center'),
                               WidgetAttribute=SimpleNamespace(WA_StyledBackground='styled'),
                               ColorScheme=SimpleNamespace(Unknown='unknown', Light='light', Dark='dark'))),
        'QtGui': module('QtGui', QDesktopServices=kind('QDesktopServices', openUrl=Mock()), QColor=kind('QColor'),
                        QIcon=kind('QIcon')),
        'QtNetwork': module('QtNetwork', QNetworkProxyFactory=kind('QNetworkProxyFactory', setUseSystemConfiguration=Mock()),
            QLocalServer=kind('QLocalServer', removeServer=Mock(), SocketOption=SimpleNamespace(UserAccessOption='user')),
            QLocalSocket=kind('QLocalSocket', waitForConnected=Mock(return_value=True), waitForBytesWritten=Mock(return_value=True)),
            QNetworkCookie=kind('QNetworkCookie')),
        'QtWidgets': module('QtWidgets', QApplication=kind('QApplication', instance=Mock(return_value=None), exec=Mock(return_value=0)),
            QMainWindow=kind('QMainWindow'), QLabel=kind('QLabel'), QStackedWidget=kind('QStackedWidget'),
            QWidget=kind('QWidget'), QPushButton=kind('QPushButton'), QHBoxLayout=kind('QHBoxLayout'), QVBoxLayout=kind('QVBoxLayout'),
            QMessageBox=kind('QMessageBox', critical=Mock(), information=Mock(), question=Mock(),
                             StandardButton=SimpleNamespace(Yes=1, No=2)),
            QFileDialog=kind('QFileDialog', getSaveFileName=Mock(return_value=('', '')),
                             getExistingDirectory=Mock(return_value=''))),
        'QtWebEngineCore': module('QtWebEngineCore', QWebEngineProfile=kind('QWebEngineProfile', httpUserAgent=Mock(return_value=QT_AGENT)), QWebEnginePage=kind('QWebEnginePage',
            RenderProcessTerminationStatus=SimpleNamespace(NormalTerminationStatus='normal'),
            NavigationType=SimpleNamespace(NavigationTypeBackForward='back')),
            QWebEngineUrlRequestInterceptor=kind('QWebEngineUrlRequestInterceptor'),
            QWebEngineScript=kind('QWebEngineScript', ScriptWorldId=SimpleNamespace(MainWorld=0, ApplicationWorld=1),
                                  InjectionPoint=SimpleNamespace(DocumentCreation='creation')),
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
        self.runtime = Mock(url=URL, port=4321, site=sites.DEFAULT)
        self.runtime.busy.return_value = None
        self.signin = self.runtime.signin
        self.signin.take.return_value = False
        self.signin.status.return_value = {'status': 'idle'}
        self.runtime.theme.get.return_value = 'system'
        self.runtime.location.take.return_value = None
        self.runtime.site_choice.take.return_value = None
        self.runtime_kind = Mock(return_value=self.runtime)
        self.state_dir = Mock(return_value=self.state)
        self.logging = Mock(return_value=self.log)
        self.threads = SimpleNamespace(Thread=type('Thread', (InlineThread,), {}), Timer=Mock())
        for wrapper in [patch.dict(os.environ, home_environment(self.home, self.home / 'state')),
                        patch.dict(sys.modules, fake_qt()),
                        patch('comic_archive.desktop_app.threading', self.threads),
                        patch('comic_archive.desktop_app.app_runtime.Runtime', self.runtime_kind),
                        patch('comic_archive.desktop_app.app_runtime.state_dir', self.state_dir),
                        patch('comic_archive.desktop_app.app_runtime.configure_logging', self.logging)]:
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
        root = self.runtime.root = self.home / 'Comic Archiver'
        location, dialog, external = self.runtime.location, self.widgets.QFileDialog.getExistingDirectory, self.q.QtGui.QDesktopServices.openUrl
        location.get.return_value = str(root / 'Library')
        other = Mock(url='http://127.0.0.1:5555/', port=5555, site=sites.DEFAULT)
        shell.signin_tick()
        external.assert_not_called(); dialog.assert_not_called()
        location.take.return_value = 'open'
        shell.signin_tick()
        self.assertEqual([call.args[0].toString() for call in external.call_args_list], ['file://' + str(root / 'Library')])
        location.take.return_value = 'choose'
        with patch('comic_archive.desktop_app.app_runtime.move', return_value=(other, None)) as move:
            # Never while saving or signing in: the folder would change under the downloader.
            self.runtime.busy.return_value = 'download'
            shell.signin_tick()
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
                shell.signin_tick()
                self.assertEqual(self.box.question.call_count, asked)
            dialog.assert_called_with(shell.window, desktop_app.LOCATION_TITLE, str(self.home))
            first, second, third = (call.args for call in self.box.question.call_args_list)
            self.assertEqual(third[2], desktop_app.LOCATION_CONFIRM.format(new=self.home / 'Photos/Comic Archiver/Library', old=root / 'Library'))
            self.assertEqual(first, (shell.window, 'Comic Archiver', desktop_app.LOCATION_CONFIRM.format(
                new=self.home / 'Other/Library', old=root / 'Library'), 3, 1))
            self.assertEqual(second[2], desktop_app.LOCATION_EXISTING.format(new=self.home / 'Old/Library', old=root / 'Library'))
            self.assertIn('Nothing is moved', first[2]); self.assertIn('already holds', second[2])
            move.assert_not_called()
            self.assertEqual(shell.phase, 'open')
            # Yes: the window waits on a plain notice and cannot be closed half-way.
            self.box.question.return_value = 1
            dialog.return_value = str(self.home / 'Other')
            shell.signin_tick()
            move.assert_called_once_with(self.runtime, self.home / 'Other')
            self.assertEqual((shell.phase, shell.runtime), ('moving', self.runtime))
            shell.notice.setText.assert_called_with(desktop_app.MOVING); shell.stack.setCurrentWidget.assert_called_with(shell.notice)
            shell.clock.stop.assert_called_once_with(); shell.signin_clock.stop.assert_called_once_with()
            event = Mock()
            shell.close_requested(event)
            event.ignore.assert_called_once_with(); event.accept.assert_not_called(); self.runtime.stop.assert_not_called()
            shell.signin_tick()
            self.assertEqual(dialog.call_count, 7)
            shell.issued = 1000
            emit(self.core.QTimer.made[-1].timeout)
        self.assertEqual((shell.runtime, shell.phase, shell.issued, self.loads(shell.view)), (other, 'open', None, [URL, other.url]))
        self.assertEqual((shell.clock.start.call_count, shell.signin_clock.start.call_count), (2, 2))
        self.box.information.assert_called_once()
        # The new folder cannot be used: back on the old one, with the reason in words.
        other.busy.return_value = None; other.root = self.home / 'Other'; other.location.take.return_value = 'choose'
        back = Mock(url='http://127.0.0.1:6666/', port=6666, site=sites.DEFAULT)
        # The window's clock runs on under the dialogs: a second request waits instead of opening another one.
        dialog.side_effect = lambda *_: (shell.signin_tick(), str(self.home / 'Third'))[1]
        shell.issued = 2000
        with patch('comic_archive.desktop_app.app_runtime.move', return_value=(back, 'That folder cannot be written to.')) as move:
            shell.signin_tick()
            emit(self.core.QTimer.made[-1].timeout)
        move.assert_called_once_with(other, self.home / 'Third')
        self.assertEqual((dialog.call_count, shell.choosing), (8, False))
        dialog.side_effect, dialog.return_value = None, str(self.home / 'Third')
        self.assertEqual((shell.runtime, shell.phase, self.loads(shell.view)[-1]), (back, 'open', back.url))
        # The old folder's page cookie was not issued again, so its age is still counted from the first time.
        self.assertEqual((shell.issued, shell.renewing), (2000, False))
        self.assertEqual(self.box.information.call_args.args, (shell.window, 'Comic Archiver',
                         'The folder was not changed.\n\nThat folder cannot be written to.'))
        # Neither folder opens again: a plain notice, and closing still works.
        back.busy.return_value = None; back.root = self.home / 'Other'; back.location.take.return_value = 'choose'
        with patch('comic_archive.desktop_app.app_runtime.move', side_effect=RuntimeError('PRIVATE_COOKIE')):
            with self.assertLogs('comic_archive.desktop', 'ERROR') as logs:
                shell.signin_tick()
            emit(self.core.QTimer.made[-1].timeout)
        self.assertNotIn('PRIVATE', logs.output[0])
        self.assertEqual((shell.runtime, shell.phase, len(self.loads(shell.view))), (back, 'failed', 3))
        self.assertIn(desktop_app.LOAD_FAILED, shell.notice.setText.call_args.args[0])
        shell.close_requested(Mock())
        back.stop.assert_called_once_with()


    def test_a_site_chosen_in_the_dashboard_opens_its_own_library_behind_the_same_page_unless_saving_or_signing_in(self):
        example = replace(sites.DEFAULT, key='example', name='Example', available=True)
        shell = self.opened(); shell.load()
        choice, change = self.runtime.site_choice, self.runtime.change_site
        change.return_value = None
        choice.take.return_value = sites.DEFAULT  # The site already open: the page stops waiting.
        shell.signin_tick()
        choice.settle.assert_called_once_with()
        # Never while saving or signing in: the page reads the site still open.
        choice.take.return_value = example
        self.runtime.busy.return_value = 'download'
        shell.signin_tick()
        self.box.information.assert_called_once_with(shell.window, 'Comic Archiver', desktop_app.SITE_BUSY.format(site='Example'))
        self.assertEqual((choice.settle.call_count, shell.phase), (2, 'open'))
        change.assert_not_called()
        self.runtime.busy.return_value = None
        shell.signin_tick()
        change.assert_called_once_with(example)
        # Closing waits for the switch; the page stays in the window and is never reloaded.
        self.assertEqual((shell.phase, choice.settle.call_count), ('moving', 2))
        self.assertEqual(desktop_app.close_action(shell.phase, None), 'ignore')
        emit(self.core.QTimer.made[-1].timeout)
        self.assertEqual((shell.runtime, shell.phase, choice.settle.call_count, self.loads(shell.view)), (self.runtime, 'open', 3, [URL]))
        shell.view.reload.assert_not_called()
        self.box.information.assert_called_once()
        # The site asked for cannot be opened: it stays as it was, with the reason in words.
        change.return_value = 'That folder cannot be written to.'
        self.runtime.site = example
        choice.take.return_value = sites.DEFAULT
        shell.signin_tick()
        emit(self.core.QTimer.made[-1].timeout)
        self.assertEqual((shell.phase, choice.settle.call_count), ('open', 4))
        self.assertEqual(self.box.information.call_args.args, (shell.window, 'Comic Archiver',
                         'Comic Archiver stays on Example.\n\nThat folder cannot be written to.'))
        # A switch that broke off: the window says so, as when the dashboard cannot be opened.
        change.side_effect = OSError('PRIVATE')
        with self.assertLogs('comic_archive.desktop', 'ERROR'):
            shell.signin_tick()
            emit(self.core.QTimer.made[-1].timeout)
        self.assertEqual((shell.phase, choice.settle.call_count), ('failed', 5))
        self.assertIn(desktop_app.LOAD_FAILED, shell.notice.setText.call_args.args[0])

    def test_the_site_chosen_last_time_is_opened_at_the_next_start(self):
        example = replace(sites.DEFAULT, key='example', name='Example', available=True)
        self.state.mkdir(parents=True); (self.state / 'site').write_text('example\n')
        with patch('comic_archive.sites.SITES', (sites.DEFAULT, example)):
            self.assertEqual(desktop_app.run(self.q, None), 0)
        self.runtime_kind.assert_called_once_with(self.home / 'Comic Archiver', site=example)

    def test_a_folder_change_confirmed_after_a_download_started_or_the_window_began_closing_is_dropped(self):
        shell = self.opened(); shell.load()
        self.runtime.root = self.home / 'Comic Archiver'
        self.runtime.location.take.return_value = 'choose'
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
                    shell.signin_tick()
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

    def test_the_window_follows_the_theme_chosen_in_the_dashboard_and_the_computers_own_by_default(self):
        shell = self.opened()
        hints, scripts = shell.app.styleHints.return_value, shell.profile.scripts.return_value
        colours, made = self.q.QtGui.QColor.made, self.q.QtWebEngineCore.QWebEngineScript.made
        # Nothing chosen: Qt is handed back to the computer's own setting and no page is told anything.
        hints.setColorScheme.assert_called_once_with('unknown')
        self.assertEqual((made, colours[-1].args), ([], ('#f4f6f2',)))
        shell.page.setBackgroundColor.assert_called_with(colours[-1])
        # The computer itself turns dark: what Qt draws follows, the sign-in bar included.
        shell.signin_header = Mock()
        hints.colorScheme.return_value = 'dark'
        emit(hints.colorSchemeChanged, 'dark')
        self.assertEqual(colours[-1].args, ('#111918',))
        shell.signin_header.setStyleSheet.assert_called_once_with(desktop_app.signin_style(True))
        self.assertNotEqual(desktop_app.signin_style(True), desktop_app.signin_style(False))
        # Chosen in the dashboard's header: picked up by the window's own clock, never before it is open.
        self.runtime.theme.get.return_value = 'light'
        hints.colorScheme.return_value = 'light'
        shell.signin_tick()
        self.assertEqual(made, [])
        shell.load()
        for _ in range(3):
            shell.signin_tick()
        script, = made
        script.setSourceCode.assert_called_once_with(desktop_app.THEME_SCRIPT % 'light')
        self.assertIn("dataset.theme = 'light'", desktop_app.THEME_SCRIPT % 'light')
        script.setInjectionPoint.assert_called_once_with('creation'); script.setWorldId.assert_called_once_with(0)
        scripts.insert.assert_called_once_with(script); scripts.remove.assert_not_called()
        self.assertEqual((hints.setColorScheme.call_args.args, colours[-1].args), (('light',), ('#f4f6f2',)))
        self.runtime.theme.get.return_value = 'dark'
        hints.colorScheme.return_value = 'dark'
        shell.signin_tick()
        scripts.remove.assert_called_once_with(script)
        made[-1].setSourceCode.assert_called_once_with(desktop_app.THEME_SCRIPT % 'dark')
        self.assertEqual((len(made), hints.setColorScheme.call_args.args, colours[-1].args), (2, ('dark',), ('#111918',)))
        # Back to the computer's own: the last page hint goes and none replaces it.
        self.runtime.theme.get.return_value = 'system'
        shell.signin_tick()
        scripts.remove.assert_called_with(made[-1])
        self.assertEqual((len(made), scripts.insert.call_count, hints.setColorScheme.call_args.args), (2, 2, ('unknown',)))
        # One palette for the pages and for what Qt draws.
        css = (Path(desktop_app.__file__).parent / 'static/archive.css').read_text()
        light_values, dark_values = css.splitlines()[:2]
        self.assertTrue(dark_values.startswith(':root[data-theme=dark]'))
        for name, (light, dark) in desktop_app.THEME_COLOURS.items():
            self.assertIn(f'--{name}:{light};', light_values)
            self.assertIn(f'--{name}:{dark};', dark_values)

    def decide(self, text, main_frame=True):
        url = Url(text)
        return desktop_app.navigation(url.scheme(), url.host(), url.port(), 4321, main_frame)

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
        def loop():
            page = [item for item in self.q.QtWebEngineCore.QWebEnginePage.made if type(item).__name__ == 'Page'][-1]
            page.runJavaScript.side_effect = lambda script, world, callback: callback(found)
            if cookie:
                self.observe_cookie()
            emit(page.loadFinished, loaded)
        return loop

    def signing_in(self):
        """A window whose dashboard asked for a sign-in, with the panel now showing Tapas's page."""
        shell = self.opened()
        shell.load(); emit(shell.page.loadFinished, True)
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

    def test_navigation_stays_on_the_apps_own_loopback_origin(self):
        for text in (URL, URL + 'library', URL + 'api/v1/context'):
            self.assertEqual(self.decide(text), 'allow')
            self.assertEqual(self.decide(text, main_frame=False), 'allow')
        for text in ('https://tapas.io/series/example', 'http://example.com/', 'http://127.0.0.1:4322/', 'http://127.0.0.1/',
                     'http://localhost:4321/', 'https://127.0.0.1:4321/', 'http://127.0.0.1:4321@example.com/'):
            self.assertEqual(self.decide(text), 'external')
            self.assertEqual(self.decide(text, main_frame=False), 'deny')
        for text in ('file:///etc/passwd', 'data:text/html,x', 'javascript:alert(1)', 'blob:' + URL + 'x', 'about:blank',
                     'ftp://example.com/x', 'chrome://gpu', 'http:///x', ''):
            self.assertEqual(self.decide(text), 'deny')

    def test_only_the_dashboards_own_files_may_be_saved(self):
        for text in (URL + 'api/v1/workspaces/a/exports/b', 'blob:' + URL + '5b2c'):
            self.assertTrue(desktop_app.own_download(text, 4321))
        for text in ('http://127.0.0.1:43210/x', 'http://127.0.0.1:4321@example.com/x', 'https://127.0.0.1:4321/x',
                     'http://127.0.0.1:4321', 'blob:https://example.com/5b2c', 'blob:http://127.0.0.1:4322/5b2c', 'data:text/plain,x', ''):
            self.assertFalse(desktop_app.own_download(text, 4321))

    def test_sign_in_page_loads_whole_pages_only_over_https(self):
        def allowed(text, main_frame=True):
            url = Url(text)
            return desktop_app.signin_navigation(url.scheme(), url.host(), url.path(), main_frame)
        for text in (SIGNIN, 'https://tapas.io/', 'https://accounts.google.com/o/oauth2/auth?x=1', 'https://www.facebook.com/login', 'about:blank'):
            self.assertTrue(allowed(text)); self.assertTrue(allowed(text, main_frame=False))
        for text in ('http://tapas.io/account/signin', URL, 'http://localhost:4321/', 'file:///etc/passwd', 'view-source:https://tapas.io/',
                     'devtools://devtools/bundled/inspector.html', 'chrome://gpu', 'data:text/html,x', 'javascript:alert(1)',
                     'blob:https://tapas.io/5b2c', 'about:srcdoc', 'about:config', 'ftp://tapas.io/x', 'qrc:/x', 'https:///x', ''):
            self.assertFalse(allowed(text), text)
        for text in ('data:text/html,x', 'blob:https://tapas.io/5b2c', 'about:srcdoc'):
            self.assertTrue(allowed(text, main_frame=False))
        for text in ('http://tapas.io/', URL, 'file:///etc/passwd', 'chrome://gpu', 'view-source:https://tapas.io/', 'javascript:alert(1)'):
            self.assertFalse(allowed(text, main_frame=False), text)
        url = Url('about:blank')
        self.assertFalse(desktop_app.signin_navigation(url.scheme(), url.host(), url.path(), True, True))

    def test_every_name_for_this_computer_is_loopback_and_no_web_host_is(self):
        for host in ('localhost', 'LOCALHOST', 'localhost.', 'app.localhost', '127.0.0.1', '127.1.2.3', '127.255.255.254', '::1', '[::1]',
                     '0:0:0:0:0:0:0:1', '::ffff:127.0.0.1', '0.0.0.0', '::'):
            self.assertTrue(desktop_app.loopback(host), host)
        for host in ('tapas.io', 'accounts.google.com', 'localhost.example.com', 'notlocalhost', '128.0.0.1', '126.255.255.255',
                     '192.168.1.10', '::2', '2001:db8::1', '::ffff:8.8.8.8', '127.0.0.1.example.com', ''):
            self.assertFalse(desktop_app.loopback(host), host)

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

    def test_close_session_and_instance_decisions(self):
        for phase, busy, action in [('starting', False, 'finish'), ('open', False, 'finish'), ('starting', True, 'confirm'),
                                    ('open', True, 'confirm'), ('stopping', False, 'ignore'), ('stopping', True, 'ignore'),
                                    ('moving', False, 'ignore'), ('moving', True, 'ignore'), ('failed', False, 'finish'),
                                    ('done', False, 'accept'), ('done', True, 'accept')]:
            self.assertEqual(desktop_app.close_action(phase, busy), action)
        for issued, now, stale in [(None, 10**10, False), (1000, 1000, False), (1000, 1005, False),
                                   (1000, 1000 + SESSION_SECONDS + 60, False), (1000, 1000 + SESSION_SECONDS + 61, True),
                                   (1000, 999, True)]:  # The clock went backwards: the cookie is stamped in the future.
            self.assertEqual(desktop_app.session_stale(issued, now), stale)
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
            view, = self.q.QtWebEngineWidgets.QWebEngineView.made
            window.setWindowTitle.assert_called_once_with('Comic Archiver'); window.show.assert_called_once_with()
            view.load.assert_not_called()
            view.load.side_effect = lambda url: order.append(url.toString())
            emit(self.core.QTimer.made[-1].timeout)
            return 0
        self.widgets.QApplication.exec = Mock(side_effect=loop)
        self.assertEqual(desktop_app.main([]), 0)
        self.assertEqual(order, ['worker', URL])
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
        profile, = self.q.QtWebEngineCore.QWebEngineProfile.made
        page, = self.q.QtWebEngineCore.QWebEnginePage.made
        self.assertEqual(profile.args, (app,))  # No storage name: nothing is kept on disk.
        self.assertEqual(page.args, (profile, self.widgets.QMainWindow.made[0]))
        app.aboutToQuit.connect.assert_called_once_with(page.deleteLater)
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

    def test_window_loads_only_its_own_pages_and_hands_other_links_to_the_default_browser(self):
        shell = self.opened()
        external = self.q.QtGui.QDesktopServices.openUrl
        own, outside = Url(URL + 'library'), Url('https://tapas.io/series/example')
        self.assertTrue(shell.page.acceptNavigationRequest(own, None, True))
        external.assert_not_called()
        self.assertFalse(shell.page.acceptNavigationRequest(outside, None, True))
        external.assert_called_once_with(outside)
        for url, main_frame in ((outside, False), (Url('file:///etc/passwd'), True), (Url('javascript:alert(1)'), True)):
            self.assertFalse(shell.page.acceptNavigationRequest(url, None, main_frame))
        external.assert_called_once()
        for url, opened_here, opened_outside in ((own, 1, 1), (outside, 0, 2), (Url('data:text/html,x'), 0, 2)):
            request = Mock(); request.requestedUrl.return_value = url
            emit(shell.page.newWindowRequested, request)
            self.assertEqual((request.openIn.call_count, external.call_count), (opened_here, opened_outside))
        request = Mock(); request.requestedUrl.return_value = own
        emit(shell.page.newWindowRequested, request)
        request.openIn.assert_called_once_with(shell.page)
        permission = Mock()
        emit(shell.page.permissionRequested, permission)
        permission.deny.assert_called_once_with(); permission.grant.assert_not_called()

    def test_dashboard_files_are_saved_through_a_normal_dialog_and_foreign_downloads_are_refused(self):
        shell = self.opened()
        dialog = self.widgets.QFileDialog.getSaveFileName

        def offer(url):
            item = Mock(); item.url.return_value = Url(url); item.downloadFileName.return_value = 'comic-archive-audit.json'
            emit(shell.profile.downloadRequested, item)
            return item
        dialog.return_value = ('/home/user/Documents/report.json', '')
        for url in ('blob:' + URL + '5b2c', URL + 'api/v1/workspaces/a/exports/b'):
            item = offer(url)
            item.setDownloadDirectory.assert_called_once_with(str(Path('/home/user/Documents')))
            item.setDownloadFileName.assert_called_once_with('report.json')
            item.accept.assert_called_once_with(); item.cancel.assert_not_called()
        dialog.assert_called_with(shell.window, 'Save file', str(Path('/home/user/Downloads/comic-archive-audit.json')))
        self.core.QStandardPaths.writableLocation.assert_called_with('downloads')
        dialog.return_value = ('', '')
        item = offer(URL + 'api/v1/workspaces/a/exports/b')
        item.cancel.assert_called_once_with(); item.accept.assert_not_called()
        dialog.reset_mock()
        with self.assertLogs('comic_archive.desktop', 'WARNING') as logs:
            for url in ('https://example.com/file.zip', 'blob:https://example.com/5b2c', 'http://127.0.0.1:4322/x'):
                item = offer(url)
                item.cancel.assert_called_once_with(); item.accept.assert_not_called()
        dialog.assert_not_called()
        self.assertNotIn('example.com', ''.join(logs.output))

    def test_dashboard_replaces_the_starting_notice_and_its_session_is_renewed_after_expiry(self):
        shell = self.opened()
        self.assertEqual(shell.notice.args, (desktop_app.STARTING,))
        shell.clock.setInterval.assert_called_once_with(60_000)
        clock = SimpleNamespace(time=Mock(return_value=1000.0))
        with patch('comic_archive.desktop_app.time', clock):
            shell.load()
            self.assertEqual(shell.view.load.call_args.args[0].toString(), URL)
            shell.clock.start.assert_called_once_with()
            emit(shell.clock.timeout)
            shell.stack.setCurrentWidget.assert_not_called()
            emit(shell.page.loadFinished, True)
            shell.stack.setCurrentWidget.assert_called_once_with(shell.view)
            clock.time.return_value = 1000.0 + SESSION_SECONDS + 60
            emit(shell.clock.timeout)
            shell.view.reload.assert_not_called()
            # Wall-clock time, so hours asleep count as soon as the computer wakes.
            clock.time.return_value = renewed = 1000.0 + SESSION_SECONDS + 7200
            emit(shell.clock.timeout)
            shell.view.reload.assert_called_once_with()
            emit(shell.page.loadFinished, False)
            emit(shell.clock.timeout)
            self.assertEqual(shell.view.reload.call_count, 2)
            emit(shell.page.loadFinished, True)
            emit(shell.clock.timeout)
            self.assertEqual(shell.view.reload.call_count, 2)
            # Moving between the dashboard's pages issues no new cookie and must not reset the clock.
            clock.time.return_value = renewed + 3600
            emit(shell.page.loadFinished, True)
            self.assertEqual(shell.issued, renewed)
        shell.notice.setText.assert_not_called()

    def test_dashboard_that_cannot_load_says_so_with_the_log_location(self):
        for log in (True, False):
            shell = self.opened(log=log)
            shell.load()
            with self.assertLogs('comic_archive.desktop', 'ERROR'):
                emit(shell.page.loadFinished, False)
            expected = desktop_app.LOAD_FAILED + (f'\n\nDetails are saved in:\n{self.log}' if log else '')
            shell.notice.setText.assert_called_once_with(expected)
            shell.stack.setCurrentWidget.assert_not_called()
            emit(shell.clock.timeout)
            shell.view.reload.assert_not_called()
            shell.phase = 'stopping'
            emit(shell.page.loadFinished, True)
            shell.stack.setCurrentWidget.assert_not_called()

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
        shell.load(); emit(shell.page.loadFinished, True)
        shell.signin_clock.setInterval.assert_called_once_with(250); shell.signin_clock.start.assert_called_once_with()
        emit(shell.signin_clock.timeout)  # Nobody asked: no second page or profile exists.
        self.assertEqual((len(core.QWebEngineProfile.made), len(core.QWebEnginePage.made), shell.signin_panel), (1, 1, None))
        self.signin.status.assert_not_called()
        self.request_signin(shell)
        self.signin.admit.assert_called_once_with()
        dashboard, private = core.QWebEngineProfile.made
        self.assertEqual(private.args, (shell.app,))  # No storage name: Qt keeps nothing of the sign-in on disk.
        self.assertEqual((shell.profile, shell.signin_profile), (dashboard, private))
        self.assertEqual(shell.signin_page.args, (private, shell.window))
        guard, = core.QWebEngineUrlRequestInterceptor.made
        self.assertEqual(guard.args, (private,))
        private.setUrlRequestInterceptor.assert_called_once_with(guard)
        dashboard.setUrlRequestInterceptor.assert_not_called()
        # It names itself as the browser the engine is built from, without Qt's tag, and says nothing else about the requests.
        private.setHttpUserAgent.assert_called_once_with(QT_AGENT.replace(' QtWebEngine/6.11.2', ''))
        dashboard.setHttpUserAgent.assert_not_called()
        for untouched in (private.setHttpAcceptLanguage, private.setPersistentStoragePath,
                          private.setPersistentCookiesPolicy, private.setCachePath, private.scripts):
            untouched.assert_not_called()
        store = private.cookieStore.return_value
        store.loadAllCookies.assert_called_once_with(); store.deleteAllCookies.assert_called_once_with()
        shell.signin_view.setPage.assert_called_once_with(shell.signin_page)
        self.assertEqual(self.loads(shell.signin_view), [SIGNIN])
        self.assertEqual(self.loads(shell.view), [URL])
        self.assertEqual((shell.back.args, shell.retry.args, shell.where.args), (('Cancel sign-in',), ('Try again',), ('',)))
        self.assertEqual(shell.hint.args, ('Sign in to Tapas below. Comic Archiver carries on by itself once you are signed in.',))
        centred, bar = self.widgets.QHBoxLayout.made
        column, = self.widgets.QVBoxLayout.made
        # The same bar as the dashboard's header: the app's name on the left, what can be done on the right.
        self.assertEqual([call.args[0] for call in bar.addWidget.call_args_list], [shell.title, shell.where, shell.hint, shell.retry, shell.back])
        self.assertEqual(shell.title.args, ('Comic Archiver',))
        panel, header, strip = self.widgets.QWidget.made
        self.assertEqual((panel, header.args, strip.args, column.args), (shell.signin_panel, (panel,), (header,), (panel,)))
        # Its contents stay in the dashboard's centred column however wide the window is.
        self.assertEqual((centred.args, bar.args), ((header,), (strip,))); strip.setMaximumWidth.assert_called_once_with(1100)
        centred.addWidget.assert_called_once_with(strip, 100000); self.assertEqual(centred.addStretch.call_count, 2)
        self.assertIn('max-width: 1100px', (Path(desktop_app.__file__).parent / 'static/archive.css').read_text())
        header.setStyleSheet.assert_called_once_with(desktop_app.signin_style(False)); header.setAttribute.assert_called_once_with('styled', True)
        for colour in ('#d5dfd5', '#52695e', '#183331'):  # The dashboard's own (static/archive.css).
            self.assertIn(colour, desktop_app.signin_style(False))
            self.assertIn(colour, (Path(desktop_app.__file__).parent / 'static/archive.css').read_text())
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
        self.assertEqual([call.args[0] for call in shell.app.aboutToQuit.connect.call_args_list],
                         [shell.page.deleteLater, shell.signin_page.deleteLater])
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
                                          ('http://tapas.io/', True, False), ('file:///etc/passwd', True, False), ('chrome://gpu', True, False)]:
            self.assertEqual(page.acceptNavigationRequest(Url(text), None, main_frame), allowed, text)
        # Going back through the history never lands on the blank page the panel starts from.
        self.assertFalse(page.acceptNavigationRequest(Url('about:blank'), 'back', True))
        self.assertTrue(page.acceptNavigationRequest(Url(SIGNIN), 'back', True))
        self.assertTrue(page.acceptNavigationRequest(Url('about:blank'), 'link', True))
        self.q.QtGui.QDesktopServices.openUrl.assert_not_called()  # Nothing is handed to another browser.
        for text, blocked in [(URL + 'api/v1/context', True), ('http://localhost:4321/', True), ('https://[::1]:4321/', True),
                              ('ws://127.0.0.1:4321/', True), ('https://tapas.io/who-am-i', False), ('https://accounts.google.com/', False)]:
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
        self.widgets.QFileDialog.getSaveFileName.assert_not_called()
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
        shell.stack.setCurrentWidget.assert_called_once_with(shell.view)
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
        shell.stack.setCurrentWidget.assert_called_once_with(shell.view)
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
        shell.stack.setCurrentWidget.assert_called_once_with(shell.view)
        # The second sign-in uses the same panel, with the cookie store emptied again first.
        shell.signin_profile.clearHttpCache.assert_called_once_with()
        emit(shell.signin_profile.clearHttpCacheCompleted)
        self.request_signin(shell)
        self.assertEqual((len(self.q.QtWebEngineCore.QWebEngineProfile.made), len(self.widgets.QWidget.made)), (2, 3))
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
        shell.stack.setCurrentWidget.assert_called_once_with(shell.view)
        self.assertIsNone(shell.signin_stage)

    def test_a_sign_in_that_runs_out_of_time_or_whose_page_crashes_returns_to_the_dashboard(self):
        shell = self.signing_in()
        emit(shell.signin_clock.timeout)
        shell.stack.setCurrentWidget.assert_not_called()
        self.signin.status.return_value = {'status': 'error', 'code': 'signin_incomplete'}
        emit(shell.signin_clock.timeout)
        shell.stack.setCurrentWidget.assert_called_once_with(shell.view)
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
        shell.stack.setCurrentWidget.assert_called_once_with(shell.view)
        self.assertIsNone(shell.signin_stage)
        shell.view.reload.assert_not_called(); shell.notice.setText.assert_not_called()  # The dashboard's own page is fine.

    def test_the_dashboard_behind_a_sign_in_never_takes_the_window_back_by_itself(self):
        shell = self.signing_in()
        shell.issued = 1000.0
        with patch('comic_archive.desktop_app.time', SimpleNamespace(time=Mock(return_value=1000.0 + SESSION_SECONDS + 7200))):
            emit(shell.clock.timeout)
            shell.view.reload.assert_called_once_with()
            emit(shell.page.loadFinished, True)
        with self.assertLogs('comic_archive.desktop', 'ERROR'):
            emit(shell.page.renderProcessTerminated, 'crashed', 9)
        shell.notice.setText.assert_called_once_with(f'{desktop_app.RENDER_FAILED}\n\nDetails are saved in:\n{self.log}')
        shell.stack.setCurrentWidget.assert_not_called()
        self.assertEqual(shell.signin_stage, 'page')
        self.signin.cancel.assert_not_called(); self.signin.fail.assert_not_called()
        emit(shell.back.clicked, False)
        shell.stack.setCurrentWidget.assert_called_once_with(shell.notice)  # What the dashboard last had to say.
        emit(shell.page.loadFinished, True)
        shell.stack.setCurrentWidget.assert_called_with(shell.view)

    def test_a_sign_in_held_before_it_begins_or_overtaken_by_closing_shows_no_tapas_page(self):
        shell = self.opened()
        shell.load(); emit(shell.page.loadFinished, True)
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
        self.assertEqual((shell.signin_panel, shell.signin_stage, len(self.q.QtWebEngineCore.QWebEngineProfile.made)), (None, None, 1))
        self.assertEqual(self.signin.admit.call_count, 2)
        self.assertEqual([call.args[0] for call in shell.stack.setCurrentWidget.call_args_list], [shell.notice])

    def test_a_sign_in_step_that_fails_ends_the_sign_in_at_once_and_a_later_attempt_builds_afresh(self):
        shell = self.opened()
        shell.load(); emit(shell.page.loadFinished, True)
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
        shell.stack.setCurrentWidget.assert_called_once_with(shell.view)
        view = self.q.QtWebEngineWidgets.QWebEngineView.made[-1]
        for part in (failed, core.QWebEnginePage.made[-1], view):
            part.deleteLater.assert_called_once_with()
        self.assertEqual(len(shell.app.aboutToQuit.connect.call_args_list), 1)  # Only the dashboard's page.
        # The next attempt starts from nothing and works.
        shell.stack.addWidget.side_effect = None
        self.request_signin(shell)
        self.assertEqual((shell.signin_stage, len(core.QWebEngineProfile.made)), ('page', 3))
        self.assertEqual(self.signin.fail.call_count, 1)
        self.assertEqual(len(shell.app.aboutToQuit.connect.call_args_list), 2)
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
        shell.stack.setCurrentWidget.assert_called_once_with(shell.view)
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
        shell.load(); emit(shell.page.loadFinished, True)
        self.threads.Thread.alive = True
        event = Mock()
        shell.window.closeEvent(event)
        event.ignore.assert_called_once_with(); event.accept.assert_not_called()
        self.runtime.stop.assert_called_once_with()
        self.assertEqual(shell.phase, 'stopping')
        shell.notice.setText.assert_called_once_with(desktop_app.FINISHING)
        shell.stack.setCurrentWidget.assert_called_with(shell.notice)
        shell.clock.stop.assert_called_once_with()
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
        self.assertIn('Example', shell.hint.args[0]); self.assertNotIn('Tapas', shell.hint.args[0])
        self.assertIn('Example', shell.signin_message.setText.call_args.args[0])
        self.runtime.busy.return_value = 'signin'
        self.box.question.return_value = self.box.StandardButton.No
        shell.window.closeEvent(Mock())
        self.assertIn('Signing in to Example', self.box.question.call_args.args[2])

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

    def test_a_crashed_window_process_is_explained_and_reloaded_once(self):
        shell = self.opened()
        crashed, normal = 'crashed', 'normal'
        emit(shell.page.renderProcessTerminated, crashed, 9)  # Still starting: nothing to show yet.
        shell.view.reload.assert_not_called()
        shell.load(); emit(shell.page.loadFinished, True)
        shell.stack.setCurrentWidget.reset_mock()
        emit(shell.page.renderProcessTerminated, normal, 0)
        shell.view.reload.assert_not_called(); shell.notice.setText.assert_not_called()
        with self.assertLogs('comic_archive.desktop', 'ERROR'):
            emit(shell.page.renderProcessTerminated, crashed, 9)
        shell.notice.setText.assert_called_once_with(f'{desktop_app.RENDER_FAILED}\n\nDetails are saved in:\n{self.log}')
        shell.stack.setCurrentWidget.assert_called_once_with(shell.notice)
        shell.view.reload.assert_called_once_with()
        emit(shell.page.loadFinished, True)
        shell.stack.setCurrentWidget.assert_called_with(shell.view)
        with self.assertLogs('comic_archive.desktop', 'ERROR'):
            emit(shell.page.renderProcessTerminated, crashed, 9)
        shell.view.reload.assert_called_once_with()  # A second crash is left for the person to act on.
        shell.stack.setCurrentWidget.assert_called_with(shell.notice)

    def test_closing_while_the_downloader_is_still_starting_never_shows_the_dashboard(self):
        shell = self.opened()
        self.threads.Thread.alive = True
        shell.background(self.runtime.start_worker, shell.load)
        starting = self.core.QTimer.made[-1]
        shell.window.closeEvent(Mock())
        stopping = self.core.QTimer.made[-1]
        self.threads.Thread.alive = False
        emit(starting.timeout)
        shell.view.load.assert_not_called(); shell.clock.start.assert_not_called()
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
        page, signin_page = self.q.QtWebEngineCore.QWebEnginePage.made
        self.assertEqual(page.runJavaScript.call_args.args[:2], ("document.querySelector('#account-download') !== null", 0))
        # The real sign-in panel and its cookie store are exercised, and no Tapas address is ever loaded.
        dashboard, private = self.q.QtWebEngineCore.QWebEngineProfile.made
        view, signin_view = self.q.QtWebEngineWidgets.QWebEngineView.made
        self.assertEqual((self.loads(view), self.loads(signin_view)), ([URL], ['about:blank']))
        signin_page.runJavaScript.assert_not_called()
        self.assertEqual(signin_page.args, (private, self.widgets.QMainWindow.made[0]))
        cookie, origin = private.cookieStore.return_value.setCookie.call_args.args
        self.assertEqual(origin.toString(), 'https://tapas.io/')
        self.assertEqual([item.args for item in cookie.args], [(b'comic_archive_self_test',), (b'synthetic',)])
        cookie.setDomain.assert_called_once_with('.tapas.io'); cookie.setPath.assert_called_once_with('/')
        cookie.setSecure.assert_called_once_with(True); cookie.setHttpOnly.assert_called_once_with(True)
        dashboard.cookieStore.assert_not_called()
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

    def test_self_test_serves_a_real_empty_temporary_library_and_never_starts_a_downloader(self):
        self.runtime_kind.side_effect = RUNTIME
        pages = []

        def loop():
            view, _ = self.q.QtWebEngineWidgets.QWebEngineView.made
            with build_opener(ProxyHandler({})).open(view.load.call_args.args[0].toString(), timeout=5) as response:
                pages.append(response.read().decode())
            self.answer(True, 'id="account-download"' in pages[0])()
        with patch('comic_archive.app_runtime.background.start') as start, \
                patch('comic_archive.app_runtime.background.stop', return_value={'running': False}) as stop:
            status, report = self.self_test(loop)
            start.assert_not_called(); stop.assert_called_once()
        self.assertEqual((status, report['ok']), (0, True))
        root, = self.runtime_kind.call_args.args
        self.assertFalse(root.parent.exists())
        self.assertEqual(list(self.home.iterdir()), [])

    def test_main_passes_self_test_and_window_modes_to_their_own_paths(self):
        with patch('comic_archive.desktop_app.self_test', return_value=0) as check, patch('comic_archive.desktop_app.run', return_value=7) as run:
            self.assertEqual(desktop_app.main(['--self-test', '--project-root', '/ignored']), 0)
            check.assert_called_once(); run.assert_not_called()
            self.assertEqual(desktop_app.main(['--project-root', '/library']), 7)
            self.assertEqual(run.call_args.args[1], '/library')


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
