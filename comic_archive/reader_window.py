"""The desktop's built-in reader. Qt is supplied by the existing window, never imported on Android."""
import copy
import math

from .app_actions import Refused
from .local_reader import ReadingError


def decoded_image(q, data):
    """Inspect dimensions before pixel allocation, with Qt's allocation guard still enabled."""
    limit = q.QtGui.QImageReader.allocationLimit()
    if not limit or limit > 128:
        q.QtGui.QImageReader.setAllocationLimit(128)
    buffer = q.QtCore.QBuffer()
    buffer.setData(q.QtCore.QByteArray(data))
    buffer.open(q.QtCore.QIODevice.OpenModeFlag.ReadOnly)
    reader = q.QtGui.QImageReader(buffer)
    size = reader.size()
    if size.width() < 1 or size.height() < 1 or size.width() * size.height() > 32_000_000:
        raise ReadingError('This image exceeds the supported size or cannot be decoded. The original is retained.')
    image = reader.read()
    if image.isNull() or image.width() * image.height() > 32_000_000:
        raise ReadingError('This image could not be decoded. The original is retained.')
    return image


def reader_window(q, session, run, parent=None, closed=lambda _: None):
    """All file/persistence work uses the shell's existing worker/GUI completion handoff."""
    w, core, gui = q.QtWidgets, q.QtCore, q.QtGui

    class NovelView(w.QTextBrowser):
        def __init__(self):
            super().__init__()
            self.images = {}
            self.setOpenLinks(False)
            self.setOpenExternalLinks(False)
            self.setSearchPaths([])
            self.setReadOnly(True)
            self.setAccessibleName('Novel text')
            self.setObjectName('novel-text')
            self.document().setBaseUrl(core.QUrl('reader:/EPUB/'))

        def loadResource(self, kind, name):
            if kind == gui.QTextDocument.ResourceType.ImageResource:
                image = self.images.get(name.toString())
                if image is not None:
                    return image
            # A valid empty result also prevents Qt's ordinary file/resource fallback.
            return core.QByteArray()

    class Reader(w.QDialog):
        def __init__(self):
            super().__init__(parent)
            self.session = session
            self.busy = self.closing = self.finished_close = self.restoring = self.dirty = False
            self.position = copy.deepcopy(session.record['position'])
            self.preferences = copy.deepcopy(session.record['preferences'])
            self.image = None
            self.setWindowTitle(session.book.title + ' — Reader')
            self.resize(840, 900)
            self.setMinimumSize(400, 360)
            layout = w.QVBoxLayout(self)
            self.title = w.QLabel(session.book.title)
            self.title.setTextFormat(core.Qt.TextFormat.PlainText)
            self.title.setWordWrap(True)
            self.title.setMaximumHeight(self.fontMetrics().lineSpacing() * 3)
            layout.addWidget(self.title)
            navigation = w.QHBoxLayout()
            self.previous = w.QPushButton('Previous')
            self.next = w.QPushButton('Next')
            self.previous.setAccessibleName('Previous page')
            self.next.setAccessibleName('Next page')
            self.counter = w.QLabel()
            for widget in (self.previous, self.counter, self.next):
                navigation.addWidget(widget)
            layout.addLayout(navigation)
            choices = w.QHBoxLayout()
            self.fit = w.QComboBox()
            self.fit.setAccessibleName('Comic page fit')
            self.fit.addItem('Fit width', 'width')
            self.fit.addItem('Fit page', 'page')
            self.fit.setCurrentIndex(self.fit.findData(self.preferences['fit']))
            self.text_scale = w.QComboBox()
            self.text_scale.setAccessibleName('Novel text size')
            for scale in sorted({.75, 1., 1.25, 1.5, 2., self.preferences['text_scale']}):
                self.text_scale.addItem(f'Text {scale:.0%}', scale)
            self.text_scale.setCurrentIndex(self.text_scale.findData(self.preferences['text_scale']))
            self.bookmark = w.QPushButton('Add bookmark')
            self.bookmark.setAccessibleName('Add bookmark at this reading position')
            for widget in (self.fit, self.text_scale, self.bookmark):
                choices.addWidget(widget)
            layout.addLayout(choices)
            self.bookmarks = w.QComboBox()
            self.bookmarks.setAccessibleName('Saved bookmarks')
            layout.addWidget(self.bookmarks)
            self.stack = w.QStackedWidget()
            self.comic = w.QScrollArea()
            self.comic.setAlignment(core.Qt.AlignmentFlag.AlignCenter)
            self.page = w.QLabel()
            self.page.setAlignment(core.Qt.AlignmentFlag.AlignCenter)
            self.comic.setWidget(self.page)
            self.novel = NovelView()
            self.stack.addWidget(self.comic)
            self.stack.addWidget(self.novel)
            layout.addWidget(self.stack, 1)
            self.notice = w.QLabel(session.warning or '')
            self.notice.setWordWrap(True)
            self.notice.setTextFormat(core.Qt.TextFormat.PlainText)
            self.notice.setAccessibleName('Reading status')
            layout.addWidget(self.notice)
            self.reload = w.QPushButton('Reload saved progress')
            self.reload.setAccessibleName('Reload saved reading progress')
            layout.addWidget(self.reload)
            self.save_timer = core.QTimer(self)
            self.save_timer.setSingleShot(True)
            self.save_timer.setInterval(500)
            self.save_timer.timeout.connect(self.save_position)
            self.previous.clicked.connect(lambda: self.navigate(self.position['unit'] - 1))
            self.next.clicked.connect(lambda: self.navigate(self.position['unit'] + 1))
            self.bookmark.clicked.connect(lambda: self.save_position(bookmark=True))
            self.bookmarks.activated.connect(self.choose_bookmark)
            self.reload.clicked.connect(self.reload_progress)
            self.fit.activated.connect(self.change_preferences)
            self.text_scale.activated.connect(self.change_preferences)
            self.comic.verticalScrollBar().valueChanged.connect(self.scrolled)
            self.novel.verticalScrollBar().valueChanged.connect(self.scrolled)
            for key, step in ((core.Qt.Key.Key_Left, -1), (core.Qt.Key.Key_Right, 1)):
                shortcut = gui.QShortcut(gui.QKeySequence(key), self)
                shortcut.activated.connect(lambda amount=step: self.navigate(self.position['unit'] + amount))
            self.refresh_bookmarks()
            self.request_page(self.position, persist=False)

        def _run(self, work, done, message='Loading saved content…'):
            self.busy = True
            self.controls()
            self.notice.setText(message)

            def guarded():
                try:
                    return work()
                except ReadingError as error:
                    raise Refused('reading_unavailable', str(error)) from None

            def received(value, error):
                self.busy = False
                if error:
                    self.notice.setText(error)
                else:
                    done(value)
                if self.closing:
                    self.finish_close()
                else:
                    self.controls()
                    if self.dirty:
                        self.save_timer.start()
            run(guarded, received)

        def controls(self):
            enabled = not self.busy and not self.closing
            self.previous.setEnabled(enabled and self.position['unit'] > 0)
            self.next.setEnabled(enabled and self.position['unit'] + 1 < session.book.units)
            for widget in (self.fit, self.text_scale, self.bookmark, self.bookmarks, self.reload):
                widget.setEnabled(enabled)
            self.fit.setVisible(session.book.kind == 'cbz')
            self.text_scale.setVisible(session.book.kind == 'epub')

        def request_page(self, position, *, persist=True):
            if self.busy or self.closing:
                return
            position = dict(position)
            if not 0 <= position['unit'] < session.book.units:
                return
            self.save_timer.stop()
            self.dirty = False

            def work():
                payload = session.book.render(position['unit'])
                if payload['kind'] == 'cbz':
                    payload['image'] = decoded_image(q, payload.pop('data'))
                else:
                    payload['images'] = {name: decoded_image(q, data) for name, data in payload['images'].items()}
                session.book.check()
                if persist:
                    session.save(position, preferences=self.preferences)
                return payload

            def show(payload):
                self.restoring = True
                self.position = position
                self.image = payload.get('image')
                if payload['kind'] == 'cbz':
                    self.stack.setCurrentWidget(self.comic)
                    self.page.setAccessibleName(f"Comic page {position['unit'] + 1} of {session.book.units}")
                    self.scale_image()
                else:
                    self.stack.setCurrentWidget(self.novel)
                    self.novel.images = dict(payload['images'])
                    self.novel.images.update({name.removeprefix('reader:/EPUB/'): image
                                              for name, image in payload['images'].items()})
                    font = self.novel.font()
                    base = self.font().pointSizeF()
                    font.setPointSizeF(max(8., base if base > 0 else 12.) * self.preferences['text_scale'])
                    self.novel.document().setDefaultFont(font)
                    self.novel.setHtml(payload['html'])
                self.counter.setText(('Page' if session.book.kind == 'cbz' else 'Chapter')
                    + f" {position['unit'] + 1} of {session.book.units}")
                self.notice.setText(session.warning or 'Reading locally. Original files are unchanged.')
                self.refresh_bookmarks()

                def restore():
                    bar = self.current_scrollbar()
                    bar.setValue(round(bar.maximum() * position['offset'] / 10_000))
                    self.restoring = False
                core.QTimer.singleShot(0, restore)
            self._run(work, show)

        def navigate(self, unit):
            self.request_page({'unit': unit, 'offset': 0})

        def current_scrollbar(self):
            return (self.comic if session.book.kind == 'cbz' else self.novel).verticalScrollBar()

        def scrolled(self, value):
            if self.restoring or self.closing:
                return
            bar = self.current_scrollbar()
            self.position['offset'] = round(value * 10_000 / max(1, bar.maximum()))
            self.dirty = True
            self.save_timer.start()

        def save_position(self, *, bookmark=False):
            if self.busy or self.closing:
                return
            self.save_timer.stop()
            self.dirty = False
            position, prefs = dict(self.position), dict(self.preferences)

            def saved(value):
                _, warning = value
                self.notice.setText(warning or 'Reading progress saved.')
                self.refresh_bookmarks()
            self._run(lambda: session.save(position, bookmark=bookmark, preferences=prefs), saved,
                      'Saving reading progress…')

        def refresh_bookmarks(self):
            self.bookmarks.clear()
            self.bookmarks.addItem('Bookmarks', None)
            for position in session.record['bookmarks']:
                label = f"Page {position['unit'] + 1}" if session.book.kind == 'cbz' else f"Chapter {position['unit'] + 1}"
                self.bookmarks.addItem(label + f" · {position['offset'] / 100:.0f}%", dict(position))
            present = self.position in session.record['bookmarks']
            self.bookmark.setText('Remove bookmark' if present else 'Add bookmark')
            self.bookmark.setAccessibleName(('Remove' if present else 'Add') + ' bookmark at this reading position')

        def choose_bookmark(self, index):
            position = self.bookmarks.itemData(index)
            if isinstance(position, dict):
                self.request_page(position)

        def change_preferences(self, _):
            self.preferences = {'fit': self.fit.currentData(), 'text_scale': self.text_scale.currentData()}
            self.request_page(self.position)

        def reload_progress(self):
            if self.busy or self.closing:
                return

            def loaded(record):
                self.preferences = dict(record['preferences'])
                self.fit.setCurrentIndex(self.fit.findData(self.preferences['fit']))
                if self.text_scale.findData(self.preferences['text_scale']) < 0:
                    self.text_scale.addItem(f"Text {self.preferences['text_scale']:.0%}", self.preferences['text_scale'])
                self.text_scale.setCurrentIndex(self.text_scale.findData(self.preferences['text_scale']))
                core.QTimer.singleShot(0, lambda: self.request_page(record['position'], persist=False))
            self._run(session.reload, loaded, 'Loading saved progress…')

        def scale_image(self):
            if self.image is None:
                return
            viewport = self.comic.viewport().size()
            scale = (viewport.width() - 4) / self.image.width()
            if self.preferences['fit'] == 'page':
                scale = min(scale, (viewport.height() - 4) / self.image.height())
            scale = max(.000001, min(scale, math.sqrt(32_000_000 / (self.image.width() * self.image.height()))))
            width, height = max(1, int(self.image.width() * scale)), max(1, int(self.image.height() * scale))
            pixmap = gui.QPixmap.fromImage(self.image).scaled(width, height,
                core.Qt.AspectRatioMode.KeepAspectRatio, core.Qt.TransformationMode.SmoothTransformation)
            self.page.setPixmap(pixmap)
            self.page.resize(pixmap.size())

        def resizeEvent(self, event):
            super().resizeEvent(event)
            if hasattr(self, 'comic'):
                self.scale_image()

        def finish_close(self):
            if self.busy or self.finished_close:
                return
            if self.dirty:
                self.dirty = False
                self._run(lambda: session.save(dict(self.position), preferences=dict(self.preferences)), lambda _: None,
                          'Saving reading progress…')
                return
            session.close()
            self.finished_close = True
            closed(self)
            self.close()

        def closeEvent(self, event):
            if self.finished_close:
                event.accept()
                return
            event.ignore()
            self.closing = True
            self.save_timer.stop()
            self.notice.setText('Closing the reader and saving your position…')
            self.controls()
            self.finish_close()

        def reject(self):
            self.close()

        def accept(self):
            self.close()

        def done(self, result):
            if self.finished_close:
                super().done(result)
            else:
                self.close()

    return Reader()
