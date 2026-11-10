"""Expandable acquisition indicators; no file browsing or reader controls."""
from . import sites

STATUS = {'archived': 'Saved', 'queued': 'Pending', 'downloading': 'Downloading',
          'failed': 'Download failed', 'unavailable': 'Not saved', 'unsupported': 'Unsupported'}


ACCESS_LABELS = {'free': 'Free', 'bought': 'Bought / unlocked', 'limited': 'Limited',
                 'unclassified': 'Unknown type'}


def access_summary(kinds):
    labels = [label for key, label in ACCESS_LABELS.items() if key in kinds]
    return labels[0] if len(labels) == 1 else 'Mixed' if labels else 'Unknown'


def format_summary(formats):
    known = [value for value in formats if value in {'CBZ', 'EPUB', 'TAR.ZST'}]
    return known[0] if len(known) == 1 else 'Mixed' if known else 'Unknown'


def episode_type(mark):
    from .episode_index import access_kind
    return ACCESS_LABELS[access_kind(mark)]


class EpisodeLibrary:
    def __init__(self, kit, pages):
        self.kit, self.pages = kit, pages
        self.generation = 0
        self.initialized = False
        w = kit.w
        self.widget = w.QWidget()
        self.widget.setObjectName('page')
        box = w.QVBoxLayout(self.widget)
        box.setContentsMargins(32, 24, 32, 24)
        self.refresh = kit.button('Refresh', self.reset, 'secondary')
        box.addWidget(kit.row(kit.label('Library', 'pageTitle'), self.refresh))
        box.addWidget(kit.label('Recorded episodes · expand a provider and series', 'muted', wrap=False))
        box.addWidget(kit.label('Saved can include cloud storage. This list does not check files on this device.', 'muted'))
        self.tree = w.QTreeWidget()
        self.tree.setAccessibleName('Library episode status')
        self.tree.setColumnCount(4)
        self.tree.setHeaderLabels(['Provider / series / episode', 'Status', 'Format', 'Access'])
        self.tree.setUniformRowHeights(True)
        self.tree.setWordWrap(False)
        self.tree.setEditTriggers(w.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tree.setSelectionMode(w.QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.header().setSectionResizeMode(0, w.QHeaderView.ResizeMode.Stretch)
        for column in (1, 2, 3):
            self.tree.header().setSectionResizeMode(column, w.QHeaderView.ResizeMode.ResizeToContents)
        self.tree.setMinimumWidth(0)
        self.tree.itemExpanded.connect(self.expanded)
        box.addWidget(self.tree, 1)

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
            self.item(self.tree, [site.name, 'Expand to view', '—', '—'], data, expandable=True)

    def expanded(self, item):
        data = item.data(0, self.role)
        if not isinstance(data, dict) or data.get('state') not in {'idle', 'error'}:
            return
        data['state'] = 'loading'
        item.setData(0, self.role, data)
        item.setText(1, 'Loading…')
        generation = self.generation
        actions = self.pages.actions

        def done(value, error):
            if generation != self.generation:
                return  # A folder change or explicit refresh discarded this tree.
            if error is not None:
                data['state'] = 'error'
                item.setData(0, self.role, data)
                item.setText(1, 'Could not read · retry')
                item.setToolTip(1, 'Collapse and expand to retry when the library is available.')
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
                    summary = f"{row['saved']:,} / {row['episodes']:,} saved"
                    formats, kinds = row.get('formats', []), row.get('access_kinds', [])
                    series_item = self.item(parent, [' '.join(row['title'].splitlines()), summary,
                                       format_summary(formats), access_summary(kinds)],
                              {'provider': data['provider'], 'series_id': row['id'], 'offset': 0, 'state': 'idle', 'summary': summary},
                              expandable=True)
                    series_item.setToolTip(2, 'Recorded format; no saved files are opened or checked.')
                    series_item.setToolTip(3, ', '.join(label for key, label in ACCESS_LABELS.items() if key in kinds)
                                           or 'No access classification is recorded yet.')
            else:
                identifiers = {entry.get('episode_id') for entry in existing if isinstance(entry, dict)}
                for row in value['items']:
                    if row['id'] in identifiers:
                        continue
                    number = row['episode_number']
                    label = f"{number} · {row['title']}" if type(number) is int else row['title']
                    self.item(parent, [' '.join(label.splitlines()), STATUS.get(row['status'], 'Unknown'), format_summary([row.get('format')]),
                                       ACCESS_LABELS.get(row.get('access_kind'), episode_type(row['access_state']))],
                              {'provider': data['provider'], 'series_id': data['series_id'],
                               'episode_id': row['id'], 'state': 'leaf'})
            following = value['next_offset']
            if following is not None:
                self.item(parent, ['More series…' if data['series_id'] is None else 'More episodes…', 'Expand to load', '—', '—'],
                          dict(data, offset=following, state='idle'), expandable=True)
            if more:
                parent.takeChild(parent.indexOfChild(item))
            else:
                item.setText(1, data.get('summary', 'Recorded list') if value['items'] else
                             'No episodes recorded' if value['initialized'] else 'Not indexed yet')
                if not value['items']:
                    item.setChildIndicatorPolicy(self.kit.w.QTreeWidgetItem.ChildIndicatorPolicy.DontShowIndicatorWhenChildless)

        self.pages.run(lambda: actions.episode_inventory(series_id=data['series_id'], offset=data['offset'], provider=data['provider']), done)
