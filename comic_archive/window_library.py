"""Expandable comic choices and acquisition indicators; saved files are retained."""
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
        w = kit.w
        self.widget = w.QWidget()
        self.widget.setObjectName('page')
        box = w.QVBoxLayout(self.widget)
        box.setContentsMargins(32, 24, 32, 24)
        self.refresh = kit.button(say('library.refresh'), self.reset, 'secondary')
        box.addWidget(kit.row(kit.label('Library', 'pageTitle'), self.refresh))
        self.check_all = kit.button('Check all', lambda: self.choose(True), 'secondary')
        self.uncheck_all = kit.button('Uncheck all', lambda: self.choose(False), 'secondary')
        box.addWidget(kit.row(self.check_all, self.uncheck_all))
        box.addWidget(kit.label('Choose which comics to download. Unchecking a comic keeps all saved files.', 'muted'))
        self.notice = kit.label('', 'muted')
        self.notice.setWordWrap(True)
        box.addWidget(self.notice)
        box.addWidget(kit.label(say('library.recorded_episodes_expand_a_provider_and'), 'muted', wrap=False))
        box.addWidget(kit.label(say('library.saved_can_include_cloud_storage_this'), 'muted'))
        self.tree = w.QTreeWidget()
        self.tree.setAccessibleName(say('library.library_episode_status'))
        self.tree.setColumnCount(4)
        self.tree.setHeaderLabels(['Provider / series / episode', say('dashboard.status'), say('library.format'), say('library.access')])
        self.tree.setUniformRowHeights(True)
        self.tree.setWordWrap(False)
        self.tree.setEditTriggers(w.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tree.setSelectionMode(w.QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.header().setSectionResizeMode(0, w.QHeaderView.ResizeMode.Stretch)
        for column in (1, 2, 3):
            self.tree.header().setSectionResizeMode(column, w.QHeaderView.ResizeMode.ResizeToContents)
        self.tree.setMinimumWidth(0)
        self.tree.itemExpanded.connect(self.expanded)
        self.tree.itemChanged.connect(self.changed)
        box.addWidget(self.tree, 1)

    def changed(self, item, column):
        if self.setting_checks or self.changing or column != 0:
            return
        data = item.data(0, self.role)
        if (not isinstance(data, dict) or 'selected' not in data
                or not item.flags() & self.kit.q.QtCore.Qt.ItemFlag.ItemIsUserCheckable):
            return
        chosen = item.checkState(0) == self.kit.q.QtCore.Qt.CheckState.Checked
        if chosen != data['selected']:
            self.choose(chosen, data['provider'], data['series_id'])

    def choose(self, chosen, provider=None, series_id=None):
        if self.changing:
            return
        self.changing = True
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
        self.tree.setEnabled(False)
        self.refresh.setEnabled(False)
        self.check_all.setEnabled(False)
        self.uncheck_all.setEnabled(False)
        self.notice.setText('Applying comic choices…')
        actions = self.pages.actions

        def enable():
            self.changing = False
            self.tree.setEnabled(True)
            self.refresh.setEnabled(True)
            self.check_all.setEnabled(True)
            self.uncheck_all.setEnabled(True)

        def done(value, error):
            self.notice.setText('Could not apply every choice. Review the saved checkboxes and try again.' if error
                                else 'Comic choices saved. Existing files are kept.')
            if reload_tree:
                self.reset()
            self.refresh_checks(enable)

        self.pages.run(lambda: actions.choose_comics(chosen, provider=provider, series_id=series_id), done)

    def refresh_checks(self, then=None):
        """Update loaded rows without losing expansion, pagination or scroll position."""
        from .comic_selection import includes
        generation = self.generation
        actions = self.pages.actions

        def done(values, error):
            if generation != self.generation:
                if then:
                    then()
                return
            if error:
                # A stale checkmark must not masquerade as a successful write.
                self.reset()
                self.notice.setText('Choices could not be read. Expand a provider to retry.')
                if then:
                    then()
                return
            self.setting_checks = True
            try:
                def update(parent):
                    for index in range(parent.childCount()):
                        child = parent.child(index)
                        data = child.data(0, self.role)
                        if isinstance(data, dict) and 'selected' in data:
                            data['selected'] = includes(values[data['provider']], data['series_id'])
                            child.setData(0, self.role, data)
                            child.setCheckState(0, self.kit.q.QtCore.Qt.CheckState.Checked if data['selected']
                                               else self.kit.q.QtCore.Qt.CheckState.Unchecked)
                        update(child)
                update(self.tree.invisibleRootItem())
            finally:
                self.setting_checks = False
                if then:
                    then()

        self.pages.run(lambda: {site.key: actions.episode_inventory(provider=site.key)['comic_selection']
                               for site in sites.SITES if site.available}, done)

    @property
    def role(self):
        return self.kit.q.QtCore.Qt.ItemDataRole.UserRole

    def item(self, parent, columns, data=None, expandable=False):
        item = self.kit.w.QTreeWidgetItem(parent, columns)
        item.setFlags(item.flags() & ~self.kit.q.QtCore.Qt.ItemFlag.ItemIsUserCheckable
                      & ~self.kit.q.QtCore.Qt.ItemFlag.ItemIsEditable)
        if data is not None:
            item.setData(0, self.role, data)
        if expandable:
            item.setChildIndicatorPolicy(self.kit.w.QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator)
        return item

    def show(self):
        if not self.initialized or self.available != tuple(self.pages.open_keys()):
            self.reset()

    def reset(self):
        self.generation += 1
        self.tree.clear()
        self.initialized = True
        self.available = tuple(self.pages.open_keys())
        for site in sites.SITES:
            if not site.available:
                continue
            data = {'provider': site.key, 'series_id': None, 'offset': 0, 'state': 'idle'}
            self.item(self.tree, [site.name, say('library.expand_to_view'), '—', '—'], data, expandable=True)

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
            data['state'] = 'loaded'
            item.setData(0, self.role, data)
            more = data['offset'] > 0
            parent = item.parent() if more else item
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
                               'summary': summary, 'selected': row['selected']},
                              expandable=True)
                    self.setting_checks = True
                    try:
                        series_item.setFlags(series_item.flags() | self.kit.q.QtCore.Qt.ItemFlag.ItemIsUserCheckable)
                        series_item.setCheckState(0, self.kit.q.QtCore.Qt.CheckState.Checked if row['selected']
                                                  else self.kit.q.QtCore.Qt.CheckState.Unchecked)
                    finally:
                        self.setting_checks = False
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
                               'episode_id': row['id'], 'state': 'leaf'})
            following = value['next_offset']
            if following is not None:
                self.item(parent, [say('library.more_series') if data['series_id'] is None else say('library.more_episodes'), say('library.expand_to_load'), '—', '—'],
                          dict(data, offset=following, state='idle'), expandable=True)
            if more:
                parent.takeChild(parent.indexOfChild(item))
            else:
                item.setText(1, data.get('summary', say('library.recorded_list')) if value['items'] else empty_text(value))
                if not value['items']:
                    item.setChildIndicatorPolicy(self.kit.w.QTreeWidgetItem.ChildIndicatorPolicy.DontShowIndicatorWhenChildless)

        self.pages.run(lambda: actions.episode_inventory(series_id=data['series_id'], offset=data['offset'], provider=data['provider']), done)
