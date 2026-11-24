"""Expandable comic choices and acquisition indicators; saved files are retained."""
from html import escape

from . import sites
from .messages import say

STATUS = {'archived': say('settings.saved'), 'queued': say('settings.pending'), 'downloading': say('dashboard.downloading'),
          'failed': say('library.download_failed'), 'unavailable': say('library.not_saved'), 'unsupported': say('library.unsupported')}


ACCESS_LABELS = {'free': say('dashboard.free'), 'bought': 'Bought / unlocked', 'limited': say('dashboard.limited'),
                 'unclassified': say('dashboard.unknown_type')}


def access_summary(kinds):
    labels = [label for key, label in ACCESS_LABELS.items() if key in kinds]
    return labels[0] if len(labels) == 1 else say('library.mixed') if labels else say('library.unknown')


def format_summary(formats):
    known = [value for value in formats if value in {'CBZ', 'EPUB', 'TAR.ZST'}]
    return known[0] if len(known) == 1 else say('library.mixed') if known else say('library.unknown')


def episode_type(mark):
    from .episode_index import access_kind
    return ACCESS_LABELS[access_kind(mark)]


def series_columns(row):
    """A recorded series as the Library lists it: title, saved of recorded, format, access."""
    return [' '.join(row['title'].splitlines()), f"{row['saved']:,} / {row['episodes']:,} saved",
            format_summary(row.get('formats', [])), access_summary(row.get('access_kinds', []))]


def episode_columns(row):
    """A recorded episode as the Library lists it: numbered title, status, format, access."""
    number = row['episode_number']
    label = f"{number} · {row['title']}" if type(number) is int else row['title']
    return [' '.join(label.splitlines()), STATUS.get(row['status'], say('library.unknown')), format_summary([row.get('format')]),
            ACCESS_LABELS.get(row.get('access_kind'), episode_type(row['access_state']))]


def empty_text(value):
    """What a site or series with nothing listed under it says."""
    return say('library.no_episodes_recorded') if value['initialized'] else say('library.not_indexed_yet')


class EpisodeLibrary:
    def __init__(self, kit, pages):
        self.kit, self.pages = kit, pages
        self.generation = 0
        self.initialized = False
        self.changing = False
        self.setting_checks = False
        self.reading_checks = False
        self.selection_ready = False
        self.requested_list = None
        w = kit.w
        self.widget = w.QWidget()
        self.widget.setObjectName('page')
        box = w.QVBoxLayout(self.widget)
        box.setContentsMargins(32, 24, 32, 24)
        box.setSpacing(8)
        self.refresh = kit.button(say('library.refresh'), self.refresh_view)
        self.refresh.setObjectName('toolbarAction')
        self.get_list = kit.button('Get episode list', self.request_episode_list)
        self.get_list.setObjectName('toolbarAction')
        self.get_list.setEnabled(False)
        header = kit.row(kit.label('Library', 'pageTitle', wrap=False), spacing=8)
        header.layout().addWidget(self.refresh)
        header.layout().addWidget(self.get_list)
        box.addWidget(header)
        box.addWidget(kit.label('Check a series or episode to include it in downloads. Unchecking keeps saved files.', 'muted'))
        self.list_notice = kit.label('', 'muted')
        self.list_notice.hide()
        box.addWidget(self.list_notice)
        saved_note = say('library.saved_can_include_cloud_storage_this')
        self.saved_note = saved_note
        self.notice = kit.label(saved_note, 'muted')
        self.tree = w.QTreeWidget()
        self.tree.setAccessibleName(say('library.library_episode_status'))
        self.tree.setAccessibleDescription(saved_note)
        self.tree.setColumnCount(4)
        self.tree.setHeaderLabels(['', say('dashboard.status'), say('library.format'), say('library.access')])
        self.tree.headerItem().setData(0, kit.q.QtCore.Qt.ItemDataRole.AccessibleTextRole,
                                       'Provider / series / episode')
        self.tree.setUniformRowHeights(True)
        self.tree.setWordWrap(False)
        self.tree.setEditTriggers(w.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tree.setSelectionMode(w.QAbstractItemView.SelectionMode.SingleSelection)
        table_header = self.tree.header()
        table_header.setStretchLastSection(False)
        table_header.setSectionResizeMode(0, w.QHeaderView.ResizeMode.Stretch)
        for column in (1, 2, 3):
            table_header.setSectionResizeMode(column, w.QHeaderView.ResizeMode.ResizeToContents)
        self.selection = self.header_checkbox(table_header)
        self.tree.setMinimumWidth(0)
        self.tree.itemExpanded.connect(self.expanded)
        self.tree.itemChanged.connect(self.changed)
        self.tree.currentItemChanged.connect(self.update_listing)
        box.addWidget(self.tree, 1)
        box.addWidget(self.notice)

    def header_checkbox(self, header):
        from shiboken6 import isValid

        core = self.kit.q.QtCore
        qt = core.Qt
        choose = self.choose

        class AllSeries(self.kit.w.QCheckBox):
            def nextCheckState(self):
                # Mixed behaves like unchecked. Show the result after it is read back.
                choose(self.checkState() != qt.CheckState.Checked)

            @core.Slot(qt.CheckState)
            def describe_next_click(self, state):
                self.setText('Uncheck all' if state == qt.CheckState.Checked else 'Check all')
                self.align_to_header()

            @core.Slot()
            def align_to_header(self):
                if not isValid(header):
                    return  # The tree can emit geometry changes while closing.
                size = self.sizeHint()
                left = header.sectionViewportPosition(0) + 7
                self.setGeometry(left, (header.viewport().height() - size.height()) // 2,
                                 size.width(), size.height())
                self.setVisible(left >= 0 and left + size.width() <= header.sectionSize(0) - header.offset())

        checkbox = AllSeries('Check all', header.viewport())
        checkbox.setObjectName('allSeries')
        scope = 'Include or exclude all series across all providers. Saved files are kept.'
        checkbox.setAccessibleDescription(scope)
        checkbox.setToolTip(scope)
        checkbox.setTristate(True)
        checkbox.setEnabled(False)
        checkbox.checkStateChanged.connect(checkbox.describe_next_click)

        header.geometriesChanged.connect(checkbox.align_to_header)
        header.sectionResized.connect(checkbox.align_to_header)
        self.tree.horizontalScrollBar().valueChanged.connect(checkbox.align_to_header)
        checkbox.align_to_header()
        return checkbox

    def changed(self, item, column):
        if self.setting_checks or self.changing or column != 0:
            return
        data = item.data(0, self.role)
        if (not isinstance(data, dict) or 'selected' not in data
                or not item.flags() & self.kit.q.QtCore.Qt.ItemFlag.ItemIsUserCheckable):
            return
        chosen = item.checkState(0) == self.kit.q.QtCore.Qt.CheckState.Checked
        if chosen != data['selected'] or data.get('mixed'):
            self.choose(chosen, data['provider'], data['series_id'], data.get('episode_id'))

    def choose(self, chosen, provider=None, series_id=None, episode_id=None):
        if self.changing:
            return
        self.set_busy(True)

        def loading(parent):
            for index in range(parent.childCount()):
                child = parent.child(index)
                data = child.data(0, self.role)
                if isinstance(data, dict) and data.get('state') == 'loading' or loading(child):
                    return True
            return False
        reload_tree = loading(self.tree.invisibleRootItem())
        if reload_tree:
            self.generation += 1  # A pre-change index response cannot restore stale checks.
        self.notice.setText('Applying comic choices…')
        actions = self.pages.actions

        def done(value, error):
            self.notice.setText('Could not apply every choice. Review the saved checkboxes and try again.' if error
                                else 'Comic choices saved. Existing files are kept.')
            if reload_tree:
                self.reset(load_header=False)
            self.refresh_checks(lambda: self.set_busy(False))

        options = {'episode_id': episode_id} if episode_id is not None else {}
        self.pages.run(lambda: actions.choose_comics(chosen, provider=provider, series_id=series_id, **options), done)

    def set_busy(self, busy):
        self.changing = busy
        self.tree.setEnabled(not busy)
        self.refresh.setEnabled(not busy)
        self.selection.setEnabled(not busy and self.selection_ready and not self.reading_checks)
        self.update_listing()

    def list_provider(self):
        key = self.row_key(self.tree.currentItem())
        return key[0] if key is not None else self.pages.chosen

    def request_episode_list(self):
        self.requested_list = self.list_provider()
        self.pages.get_episode_list(self.requested_list)

    def update_listing(self):
        key = self.list_provider()
        site = sites.find(key)
        state = self.pages.states.get(key)
        status = (state.status or {}) if state is not None else {}
        account = (state.account or {}) if state is not None else {}
        signed_in = bool(status.get('session_configured'))
        ready = (status.get('worker') or {}).get('reported_state') == 'ready'
        running = account.get('status') == 'running'
        name = site.name if site is not None else 'this provider'
        self.get_list.setText(f'Get {name} episode list')
        self.get_list.setEnabled(bool(state and not state.busy and not self.changing and signed_in and ready and not running))
        self.get_list.setToolTip(f'Fetch episode titles and availability from {name} without downloading files.'
                                 if signed_in else f'Sign in to {name} on the Dashboard first.')
        text = ''
        if account.get('list_only'):
            if running:
                text = f'Getting the episode list from {name}…'
            elif account.get('episode_list_ready'):
                text = (f'{name} returned a partial episode list. You can review the episodes it provided.'
                        if account.get('episode_list_partial') else f'{name} episode list is ready. Choose what to download.')
            elif account.get('status') == 'held':
                text = f'Getting the {name} episode list stopped. See Dashboard for details.'
            else:
                text = f'Getting the {name} episode list is paused.'
        if key == self.requested_list and state is not None and state.message and state.message.error:
            text = state.message.text
        self.list_notice.setText(text)
        self.list_notice.setVisible(bool(text))

    def rows(self):
        iterator = self.kit.w.QTreeWidgetItemIterator(self.tree)
        while (item := iterator.value()) is not None:
            yield item
            iterator += 1

    def row_key(self, item):
        data = item.data(0, self.role) if item is not None else None
        if isinstance(data, dict) and not data.get('offset', 0):
            return data['provider'], data.get('series_id'), data.get('episode_id')
        return None

    def refresh_view(self):
        """Reload open branches and their loaded pages, then restore the browsing position."""
        if self.changing:
            return
        scopes = {}
        for item in self.rows():
            key = self.row_key(item)
            parent = item.parent()
            if key is not None and key[2] is None and item.isExpanded() and (parent is None or parent.isExpanded()):
                scopes[key] = item.data(0, self.role).get('loaded_offset', 0)
        current = self.row_key(self.tree.currentItem())
        column = max(0, self.tree.currentColumn())
        anchor = self.row_key(self.tree.itemAt(1, 1))
        scroll = self.tree.verticalScrollBar()
        previous_scroll = scroll.value()
        generation = self.generation
        actions = self.pages.actions
        self.set_busy(True)
        self.notice.setText('Refreshing the Library…')

        def read():
            result = {}
            for key, limit in scopes.items():
                provider, series, _ = key
                offset = 0
                items = []
                while True:
                    value = actions.episode_inventory(provider=provider, series_id=series, offset=offset)
                    items.extend(value['items'])
                    following = value['next_offset']
                    if following is None or not offset < following <= limit:
                        result[key] = dict(value, items=items, loaded_offset=offset)
                        break
                    offset = following
            return result

        def done(values, error):
            if generation != self.generation:
                self.set_busy(False)
                if self.widget.isVisible():
                    self.show()
                return
            if error:
                self.notice.setText('Could not refresh the Library. Try Refresh again.')
                self.set_busy(False)
                return
            self.reset(load_header=False)
            blocked = self.tree.blockSignals(True)
            try:
                for index in range(self.tree.topLevelItemCount()):
                    provider = self.tree.topLevelItem(index)
                    value = values.get(self.row_key(provider))
                    if value is None:
                        continue
                    self.populate(provider, provider.data(0, self.role), value)
                    provider.setExpanded(True)
                    for row in range(provider.childCount()):
                        series = provider.child(row)
                        value = values.get(self.row_key(series))
                        if value is not None:
                            self.populate(series, series.data(0, self.role), value)
                            series.setExpanded(True)
                restored = {self.row_key(item): item for item in self.rows()}
                if current is not None and current in restored:
                    self.tree.setCurrentItem(restored[current], column)
                if anchor is not None and anchor in restored:
                    self.tree.scrollToItem(restored[anchor], self.kit.w.QAbstractItemView.ScrollHint.PositionAtTop)
                else:
                    scroll.setValue(previous_scroll)
            finally:
                self.tree.blockSignals(blocked)
            self.notice.setText(self.saved_note)
            self.refresh_checks(lambda: self.set_busy(False))

        self.pages.run(read, done)

    def refresh_checks(self, then=None):
        """Update loaded rows without losing expansion, pagination or scroll position."""
        from .comic_selection import includes, includes_episode
        generation = self.generation
        self.reading_checks = True
        self.selection.setEnabled(False)

        def done(values, error):
            if generation != self.generation:
                if then:
                    then()
                return
            self.reading_checks = False
            if error:
                # A stale checkmark must not masquerade as a successful write.
                self.reset(load_header=False)
                self.notice.setText('Choices could not be read. Choose Refresh to retry.')
                if then:
                    then()
                return
            self.setting_checks = True
            try:
                episodes = {key: policy.get('episode_choices', {}) for key, policy in values.items()}
                mixed_series = {key: {series for series, _ in override.values()} for key, override in episodes.items()}
                for row in self.rows():
                    data = row.data(0, self.role)
                    if not isinstance(data, dict) or 'selected' not in data:
                        continue
                    provider, series = data['provider'], data['series_id']
                    if 'episode_id' in data:
                        data['selected'] = includes_episode(values[provider], episodes[provider], series, data['episode_id'])
                    else:
                        data['selected'] = includes(values[provider], series)
                        data['mixed'] = series in mixed_series[provider]
                    row.setData(0, self.role, data)
                    self.paint_check(row, data)
                states = self.kit.q.QtCore.Qt.CheckState
                defaults = {value['default'] for value in values.values()}
                if len(defaults) != 1 or any(value['exceptions'] for value in values.values()) or any(episodes.values()):
                    state = states.PartiallyChecked
                else:
                    state = states.Checked if True in defaults else states.Unchecked
                self.selection.setCheckState(state)
                self.selection_ready = True
            finally:
                self.setting_checks = False
                if then:
                    then()
                else:
                    self.selection.setEnabled(self.selection_ready and not self.changing)

        self.pages.run(self.pages.actions.library_comic_choices, done)

    @property
    def role(self):
        return self.kit.q.QtCore.Qt.ItemDataRole.UserRole

    def item(self, parent, columns, data=None, expandable=False):
        item = self.kit.w.QTreeWidgetItem(parent, columns)
        item.setFlags(item.flags() & ~self.kit.q.QtCore.Qt.ItemFlag.ItemIsUserCheckable
                      & ~self.kit.q.QtCore.Qt.ItemFlag.ItemIsEditable)
        if data is not None:
            item.setData(0, self.role, data)
            if 'episode_id' in data or 'selected' in data:
                item.setToolTip(0, '<qt>' + escape(columns[0]) + '</qt>')
        if expandable:
            item.setChildIndicatorPolicy(self.kit.w.QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator)
        if data is not None and 'selected' in data:
            previous = self.setting_checks
            self.setting_checks = True
            try:
                item.setFlags(item.flags() | self.kit.q.QtCore.Qt.ItemFlag.ItemIsUserCheckable)
                self.paint_check(item, data)
            finally:
                self.setting_checks = previous
        return item

    def paint_check(self, row, data):
        states = self.kit.q.QtCore.Qt.CheckState
        state = states.Checked if data['selected'] else states.Unchecked
        row.setCheckState(0, states.PartiallyChecked if data.get('mixed') else state)

    def show(self):
        if not self.initialized or self.available != tuple(self.pages.open_keys()):
            self.reset(load_header=False)
        if not self.selection_ready and not self.reading_checks and not self.changing:
            self.refresh_checks()

    def reset(self, *, load_header=True):
        self.generation += 1
        self.reading_checks = self.selection_ready = False
        self.selection.setEnabled(False)
        self.selection.setCheckState(self.kit.q.QtCore.Qt.CheckState.Unchecked)
        self.tree.clear()
        self.initialized = True
        self.available = tuple(self.pages.open_keys())
        for site in sites.SITES:
            if not site.available:
                continue
            data = {'provider': site.key, 'series_id': None, 'offset': 0, 'state': 'idle'}
            self.item(self.tree, [site.name, say('library.expand_to_view'), '—', '—'], data, expandable=True)
        if load_header and self.widget.isVisible() and not self.changing:
            self.refresh_checks()

    def expanded(self, item):
        data = item.data(0, self.role)
        if not isinstance(data, dict) or data.get('state') not in {'idle', 'error'}:
            return
        data['state'] = 'loading'
        item.setData(0, self.role, data)
        item.setText(1, say('library.loading'))
        generation = self.generation
        actions = self.pages.actions

        def done(value, error):
            if generation != self.generation:
                return  # A folder change or explicit refresh discarded this tree.
            if error is not None:
                data['state'] = 'error'
                item.setData(0, self.role, data)
                item.setText(1, say('library.could_not_read_retry'))
                item.setToolTip(1, say('library.collapse_and_expand_to_retry_when'))
                return
            self.populate(item, data, value)

        self.pages.run(lambda: actions.episode_inventory(series_id=data['series_id'], offset=data['offset'], provider=data['provider']), done)

    def populate(self, item, data, value):
        data['state'] = 'loaded'
        item.setData(0, self.role, data)
        more = data['offset'] > 0
        parent = item.parent() if more else item
        parent_data = parent.data(0, self.role)
        parent_data['loaded_offset'] = value.get('loaded_offset', data['offset'])
        parent.setData(0, self.role, parent_data)
        existing = [parent.child(index).data(0, self.role) for index in range(parent.childCount())]
        if data['series_id'] is None:
            identifiers = {entry.get('series_id') for entry in existing if isinstance(entry, dict) and entry.get('offset') == 0}
            for row in value['items']:
                if row['id'] in identifiers:
                    continue
                columns = series_columns(row)
                summary, kinds = columns[1], row.get('access_kinds', [])
                series_item = self.item(parent, columns,
                          {'provider': data['provider'], 'series_id': row['id'], 'offset': 0, 'state': 'idle',
                           'summary': summary, 'selected': row['selected'], 'mixed': row.get('mixed', False)},
                          expandable=True)
                series_item.setToolTip(2, say('library.recorded_format_no_saved_files_are'))
                series_item.setToolTip(3, ', '.join(label for key, label in ACCESS_LABELS.items() if key in kinds)
                                       or say('library.no_access_classification_is_recorded_yet'))
        else:
            identifiers = {entry.get('episode_id') for entry in existing if isinstance(entry, dict)}
            for row in value['items']:
                if row['id'] in identifiers:
                    continue
                self.item(parent, episode_columns(row),
                          {'provider': data['provider'], 'series_id': data['series_id'],
                           'episode_id': row['id'], 'state': 'leaf', 'selected': row.get('selected', data.get('selected', True))})
        following = value['next_offset']
        if following is not None:
            more_data = {key: data[key] for key in ('provider', 'series_id')}
            self.item(parent, [say('library.more_series') if data['series_id'] is None else say('library.more_episodes'), say('library.expand_to_load'), '—', '—'],
                      dict(more_data, offset=following, state='idle'), expandable=True)
        if more:
            parent.takeChild(parent.indexOfChild(item))
        else:
            item.setText(1, data.get('summary', say('library.recorded_list')) if value['items'] else empty_text(value))
            if not value['items']:
                item.setChildIndicatorPolicy(self.kit.w.QTreeWidgetItem.ChildIndicatorPolicy.DontShowIndicatorWhenChildless)
