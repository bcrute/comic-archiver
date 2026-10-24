"""One trusted-native, workspace-bound offline reader; no browser or remote authority."""
import base64
import copy
import re
import threading
from pathlib import Path

from .local_library import LOCAL_OWNER
from .local_reader import ReadingError, UNAVAILABLE

MAX_ANDROID_RENDER = 8 * 1024**2


class ReaderHost:
    def __init__(self):
        self._lock = threading.RLock()
        self._session = self._root = self._token = self._identifier = None

    @staticmethod
    def token(value):
        if not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{32}', value):
            raise ReadingError('Choose a currently open reader.')
        return value

    def _selected(self, root, token):
        self.token(token)
        if self._session is None or root != self._root or token != self._token:
            raise ReadingError('This reader is no longer open. Choose Read here again.')
        self._session.book.check()
        return self._session

    def _snapshot(self):
        reader = self._session
        return {'reader_id': self._token, 'title': reader.book.title,
                'kind': reader.book.kind, 'units': reader.book.units,
                'content_sha256': reader.book.content, 'record': copy.deepcopy(reader.record),
                'can_save': reader.can_save, 'warning': reader.warning}

    def open(self, root, registry, identifier, token, *, active=lambda: True):
        with self._lock:
            self.token(token)
            if registry.project_root != root or not active():
                raise ReadingError(UNAVAILABLE)
            if self._token == token:
                if self._root != root or self._identifier != identifier:
                    raise ReadingError('Choose a new reader for this saved file.')
                self._selected(root, token)
                return self._snapshot()
            candidate = registry.open_reader(LOCAL_OWNER, registry.workspace_id, identifier,
                                             max_member_bytes=MAX_ANDROID_RENDER, max_text_bytes=1024**2)
            try:
                if not active():
                    raise ReadingError('The working library changed. Choose Read here again.')
                self.close_all()
            except BaseException:
                candidate.close()
                raise
            self._root, self._token, self._identifier = root, token, identifier
            self._session = candidate
            return self._snapshot()

    def page(self, root, token, unit):
        with self._lock:
            reader = self._selected(root, token)
            page = reader.book.render(unit, max_bytes=MAX_ANDROID_RENDER, max_text_bytes=1024**2)
            encode = lambda data: base64.b64encode(data).decode('ascii')
            return {'reader_id': token, 'unit': unit, 'kind': page['kind']} | (
                {'data': encode(page['data'])} if page['kind'] == 'cbz' else
                {'html': page['html'], 'images': {name: encode(data) for name, data in page['images'].items()}})

    def open_selected(self, root, relative, token, *, active=lambda: True):
        from .selected_reader import SelectedReadingSession, selected_path
        with self._lock:
            self.token(token)
            library = Path(root) / 'Library'
            path = selected_path(library, relative)
            identifier = 'selected:' + relative
            if not active():
                raise ReadingError(UNAVAILABLE)
            if self._token == token:
                if self._root != root or self._identifier != identifier:
                    raise ReadingError('Choose a new reader for this saved file.')
                self._selected(root, token)
                return self._snapshot()
            candidate = SelectedReadingSession(library, path,
                max_member_bytes=MAX_ANDROID_RENDER, max_text_bytes=1024**2)
            try:
                if not active():
                    raise ReadingError('The working library changed. Choose Read here again.')
                self.close_all()
            except BaseException:
                candidate.close()
                raise
            self._root, self._token, self._identifier = root, token, identifier
            self._session = candidate
            return self._snapshot()

    def save(self, root, token, position, *, bookmark=False, preferences=None):
        with self._lock:
            reader = self._selected(root, token)
            if type(bookmark) is not bool:
                raise ReadingError('Choose a supported reading action.')
            try:
                reader.save(position, bookmark=bookmark, preferences=preferences)
            except (ValueError, TypeError, KeyError):
                raise ReadingError('Choose a supported reading position or preference.') from None
            return self._snapshot()

    def reload(self, root, token):
        with self._lock:
            reader = self._selected(root, token)
            try:
                reader.reload()
            except ReadingError as error:
                reader.warning, reader.can_save = str(error), False
            return self._snapshot()

    def close(self, root, token):
        with self._lock:
            self.token(token)
            if self._session is None or root != self._root or token != self._token:
                return {'closed': False}
            self.close_all()
            return {'closed': True}

    def close_all(self):
        with self._lock:
            reader = self._session
            self._session = self._root = self._token = self._identifier = None
            if reader is not None:
                reader.close()
