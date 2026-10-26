"""The window's own pages: the dashboard, Settings and the saved library, drawn with Qt's widgets.

What each page says and allows is decided in dashboard_model, storage_model and library_model; this module only
puts it on screen and passes clicks on. Every call to the library runs on a thread (the shell's run), never on the
window's own, and its answer comes back here.
"""
import html
import time

from . import app_actions, dashboard_model, library_model, storage_model
from .dashboard_model import SPEED_NAMES, SPEEDS, State

ROUTES = ('dashboard', 'library', 'settings/downloads', 'settings/storage', 'settings/b2')
TABS = (('downloads', 'Downloads'), ('storage', 'Storage'), ('b2', 'Backblaze B2'))
SIGN_OUT = ('Sign out of {site} in Comic Archiver?\n\nThis signs out only Comic Archiver, on this computer. Your '
            '{site} account isn\'t changed, and anywhere else you use {site}, like your web browser, stays signed in.'
            '\n\nYour saved episodes stay where they are. To download again, sign in again.')
FOOTER = 'Only episodes your account can already read, free or bought, are saved. Nothing is bought for you.'
# Each Backblaze page, as its own setup steps name it. Only these leave the window, in the computer's own browser.
B2_PAGES = {
    'signup': 'https://www.backblaze.com/sign-up/s3', 'signin': 'https://secure.backblaze.com/user_signin.htm',
    'billing': 'https://secure.backblaze.com/billing.htm', 'caps': 'https://secure.backblaze.com/b2_caps_alerts.htm',
    'buckets': 'https://secure.backblaze.com/b2_buckets.htm', 'keys': 'https://secure.backblaze.com/app_keys.htm',
    'browse': 'https://secure.backblaze.com/b2_browse_files2.htm', 'reports': 'https://secure.backblaze.com/b2_reports.htm',
    'pricing': 'https://www.backblaze.com/cloud-storage/pricing',
}
B2_INTRO = ('Each new episode goes straight to your own Backblaze account as it downloads, with no copy kept on this '
            'computer, and is checked against Backblaze’s own record of it. An upload that fails keeps the episode here, to retry.')
B2_COSTS = ('<p>Backblaze bills you itself, not through Comic Archiver. Comic Archiver never sees your payment details and is paid nothing.</p><ul>'
            '<li><b>Signing up is free</b> and needs no card. The first 10 GB you store is always free.</li>'
            '<li><b>Past 10 GB</b>, Backblaze charges $6.95 a month for each 1,000 GB (1 TB) stored, and only for what is stored. '
            'Backblaze’s own pages count it <b>by the day</b>: 100 GB costs about 2 cents a day, about 63 cents a month. '
            'A large library of 12,000 episodes, about 240 GB, costs about 5 cents a day, about $1.60 a month.</li>'
            '<li><b>Uploading is free.</b> So is opening your episodes again, up to three times what you store, each month.</li>'
            '<li><b>For more than 10 GB, add a card</b> on Backblaze’s Billing page. Backblaze charges it once a month, for the month before.</li>'
            '<li><b>You can set a limit.</b> On Backblaze’s Caps &amp; Alerts page, set the most you want to spend <b>a day</b>, '
            'a little above your library’s daily cost. Backblaze emails you as you near it and stops uploads when it is reached.</li>'
            '<li>If Backblaze stops an upload, Comic Archiver tells you why and keeps the episodes on this computer.</li></ul>')
B2_PRICES = 'Prices as of October 2026. <a href="pricing">Backblaze’s current prices</a>'
B2_STEP1 = ('<b>1. Create your account.</b><br>Sign up and verify your email. No card is needed.<br>If your library is larger '
            'than 10 GB, add a card on the Billing page once you are signed in, and set the most you want to spend a day on '
            'Caps &amp; Alerts.')
B2_STEP2 = ('<b>2. Create a private library folder.</b><br>Click <b>Create a Bucket</b>. A bucket is your cloud library folder.')
B2_STEP2_MORE = ('<b>A bucket name can belong to only one Backblaze account, anywhere.</b> A plain name like <i>comics</i> is almost '
                 'certainly taken by someone else already. The name filled in above ends in letters and numbers made for your '
                 'library, so it is unlikely to be taken.<br><span>Type this name in Backblaze’s <b>Bucket Name</b> box, or choose '
                 'your own: 6 to 63 letters, numbers or hyphens. If Backblaze says the name is taken, change it here and there so '
                 'the two match. The app saves it when you connect.</span><ul><li><b>Files in Bucket:</b> Choose <b>Private</b>. '
                 'Your files can only be opened by you and apps you give access to.</li><li><b>Object Lock:</b> Choose '
                 '<b>Disable</b>. This lets the archiver remove temporary uploads after testing the connection.</li></ul>'
                 'Leave the other settings at their defaults, then click <b>Create a Bucket</b>.')
B2_STEP3 = ('<b>3. Create your library access key.</b><br>If the button opens another page, find <b>Application Keys</b> in '
            'Backblaze’s left menu, under <b>B2 Cloud Storage</b>.<br>Click <b>Add a New Application Key</b>. Fill in the form '
            'like this:<ul><li><b>Name of Key:</b> type <b>comic-archiver</b>. Backblaze allows only letters, numbers and hyphens '
            'here, no spaces.</li><li><b>Allow access to Bucket(s):</b> choose your library bucket, <b>%s</b>. Not <b>All</b>.</li>'
            '<li><b>Allow List All Bucket Names:</b> leave it unticked.</li><li><b>Type of Access:</b> choose <b>Read and Write</b>.'
            '</li><li><b>File name prefix</b> and <b>Duration:</b> leave both blank.</li></ul>Then click <b>Create New Key</b>. '
            'Backblaze shows three values: <b>keyID</b>, <b>keyName</b> and <b>applicationKey</b>. Step 4 needs keyID and '
            'applicationKey.<br><b>Backblaze shows the applicationKey only once.</b> Keep that page open until step 4 is done. '
            'If it is closed too soon, create another key the same way; the old one can be deleted.')
B2_STEP4 = ('<b>4. Paste the key and connect.</b><br>Copy the two values Backblaze just displayed. They stay on this device. '
            'You won’t need to enter them again.')
B2_STEP5 = ('<b>5. Check the connection.</b><br><span>The test uploads two saved episodes, verifies them and deletes its cloud '
            'copies. Your local originals stay here.</span>')
B2_STEP6 = ('<b>6. Choose where to save.</b><br>Where new episodes go is chosen in one place: the Storage tab of these '
            'settings. Once the check passes, Backblaze is offered there.')
B2_YOUR_PAGES = ('<b>Your Backblaze pages</b><br><span>Each opens in your browser, after Backblaze’s sign-in if needed.</span><ul>'
                 '<li><a href="browse">Browse Files</a>: see your saved episodes and download any of them. They are ordinary CBZ '
                 'files that any comic reader opens, with or without Comic Archiver.</li>'
                 '<li><a href="reports">Reports</a>: how much you store and what it costs, day by day.</li>'
                 '<li><a href="billing">Billing</a>: your card and your monthly bills.</li>'
                 '<li><a href="caps">Caps &amp; Alerts</a>: the most you allow a day, and Backblaze’s emails as you near it.</li>'
                 '<li><a href="keys">Application Keys</a>: delete Comic Archiver’s key here to take away its access.</li></ul>')
LIST_HELP = ('Sites supply lists in separate pages. We read each page and check the account between pages; '
             'large lists can take several minutes.')


def route_of(name, cloud_offered):
    """The page a request leads to: a Settings tab that is not offered leads to the first one."""
    if name == 'settings':
        return 'settings/downloads'
    if name == 'settings/b2' and not cloud_offered:
        return 'settings/downloads'
    return name if name in ROUTES else 'dashboard'


def flat(text):
    """Qt measures a wrapped label with a list in it too short, and cuts the list off: lists become lines."""
    return (text.replace('<ul>', '').replace('</ul>', '<br>').replace('<li>', '<br>&nbsp;&nbsp;•&nbsp;')
            .replace('</li>', ''))


def prose(text):
    """Rich text as a label shows it: lists as lines, and the lines spaced apart for reading."""
    from .window_style import LINE_HEIGHT
    return f'<div style="line-height: {LINE_HEIGHT}%">{flat(text)}</div>'


def clean_key(text):
    """A copied key often carries a space or line break at its ends, or inside: none belongs in it."""
    return ''.join(text.split())


def paint(q, window, dark):
    """Give the window the look for light or dark: its style sheet, and the colour of links in its text."""
    from .window_style import colours, stylesheet
    # The link colour goes on the application's palette: the style sheet gives each widget a palette of its own,
    # made from that one, so a colour set on the window alone does not reach the labels.
    app = q.QtWidgets.QApplication.instance()
    palette = app.palette()
    for role in (q.QtGui.QPalette.ColorRole.Link, q.QtGui.QPalette.ColorRole.LinkVisited):
        palette.setColor(role, q.QtGui.QColor(colours(dark)['accent']))
    app.setPalette(palette)
    window.setStyleSheet(stylesheet(dark))


class Kit:
    """Builds the widgets every page uses, named for the style sheet."""

    def __init__(self, q, link):
        self.q, self.w, self.link = q, q.QtWidgets, link

    def label(self, text='', name=None, rich=False, wrap=True):
        label = self.w.QLabel()
        flags = self.q.QtCore.Qt
        label.setTextFormat(flags.TextFormat.RichText if rich else flags.TextFormat.PlainText)
        label.setText(prose(text) if rich else text)
        label.setWordWrap(wrap)
        if name:
            label.setObjectName(name)
        if rich:
            label.setOpenExternalLinks(False)
            label.setTextInteractionFlags(flags.TextInteractionFlag.LinksAccessibleByMouse
                                          | flags.TextInteractionFlag.LinksAccessibleByKeyboard)
            label.linkActivated.connect(self.link)
        return label

    def button(self, text, clicked=None, kind=None):
        button = self.w.QPushButton(text)
        if kind:
            button.setProperty('kind', kind)
        button.setCursor(self.q.QtCore.Qt.CursorShape.PointingHandCursor)
        if clicked is not None:
            button.clicked.connect(lambda checked=False: clicked())
        return button

    def frame(self, name, vertical=True, margins=(20, 20, 20, 20), spacing=10):
        frame = self.w.QFrame()
        frame.setObjectName(name)
        frame.setAttribute(self.q.QtCore.Qt.WidgetAttribute.WA_StyledBackground, True)
        box = self.w.QVBoxLayout(frame) if vertical else self.w.QHBoxLayout(frame)
        box.setContentsMargins(*margins)
        box.setSpacing(spacing)
        return frame, box

    def row(self, *widgets, spacing=10, stretch=True):
        holder = self.w.QWidget()
        box = self.w.QHBoxLayout(holder)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(spacing)
        for widget in widgets:
            box.addWidget(widget)
        if stretch:
            box.addStretch(1)
        return holder

    def combo(self, choices, changed=None):
        combo = self.w.QComboBox()
        for key, text in choices:
            combo.addItem(text, key)
        if changed is not None:
            combo.activated.connect(lambda index: changed(combo.itemData(index)))
        return combo

    def scroll(self, content):
        """A page that scrolls, as wide as the window."""
        w = self.w
        area = w.QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(w.QFrame.Shape.NoFrame)
        content.setObjectName('page')
        area.setWidget(content)
        return area


def tone(widget, name, value):
    """Restyle a widget whose look depends on a property, as the style sheet reads it again only when told."""
    value = '' if value is None else str(value).lower() if isinstance(value, bool) else value
    if widget.property(name) != value:
        widget.setProperty(name, value)
        widget.style().unpolish(widget)
        widget.style().polish(widget)


def say(label, text, error=False):
    if label.text() != text:
        label.setText(text)
    tone(label, 'error', error)
    label.setVisible(bool(text))


def choose(combo, key):
    index = combo.findData(key)
    if index >= 0 and combo.currentIndex() != index:
        blocked = combo.blockSignals(True)
        combo.setCurrentIndex(index)
        combo.blockSignals(blocked)


def heading(kit, title, eyebrow='Your archive', name='pageTitle'):
    holder = kit.w.QWidget()
    box = kit.w.QVBoxLayout(holder)
    box.setContentsMargins(0, 0, 0, 0)
    box.setSpacing(4)
    box.addWidget(kit.label(eyebrow.upper(), 'eyebrow'))
    title_label = kit.label(title, name)
    box.addWidget(title_label)
    return holder, title_label


class Header:
    """The bar along the top: the app's name, its three pages and the light/dark switch."""

    def __init__(self, kit, pages):
        w = kit.w
        self.widget = w.QWidget()
        self.widget.setObjectName('header')
        self.widget.setAttribute(kit.q.QtCore.Qt.WidgetAttribute.WA_StyledBackground, True)
        bar = w.QHBoxLayout(self.widget)
        bar.setContentsMargins(32, 10, 32, 10)
        bar.setSpacing(4)
        bar.addWidget(kit.label('Comic Archiver', 'brand', wrap=False))
        bar.addSpacing(16)
        self.nav = {}
        group = w.QButtonGroup(self.widget)
        group.setExclusive(True)
        for key, text in (('dashboard', 'Dashboard'), ('library', 'Library'), ('settings', 'Settings')):
            button = kit.button(text, lambda key=key: pages.go_to(key))
            button.setObjectName('nav')
            button.setCheckable(True)
            group.addButton(button)
            bar.addWidget(button)
            self.nav[key] = button
        bar.addStretch(1)
        self.theme = kit.button('', pages.toggle_theme)
        self.theme.setObjectName('themeToggle')
        bar.addWidget(self.theme)

    def show(self, route, dark):
        self.nav[route.partition('/')[0]].setChecked(True)
        # The switch shows where it leads: the sun to light, the moon to dark.
        self.theme.setText('☀' if dark else '☾')
        tip = 'Switch to light mode' if dark else 'Switch to dark mode'
        self.theme.setToolTip(tip)
        self.theme.setAccessibleName(tip)


class Dashboard:
    """The status of the download, with Go and Pause, and the saved library at a glance."""

    def __init__(self, kit, pages):
        w, self.kit, self.pages = kit.w, kit, pages
        content = w.QWidget()
        column = w.QVBoxLayout(content)
        column.setContentsMargins(32, 24, 32, 24)
        column.setSpacing(20)
        top = w.QHBoxLayout()
        title, _ = heading(kit, 'Dashboard')
        top.addWidget(title, 1)
        self.updated = kit.label('Checking current status…', 'muted', wrap=False)
        top.addWidget(self.updated, 0, kit.q.QtCore.Qt.AlignmentFlag.AlignBottom)
        column.addLayout(top)
        grid = w.QHBoxLayout()
        grid.setSpacing(20)
        grid.addWidget(self._status(kit, pages), 16, kit.q.QtCore.Qt.AlignmentFlag.AlignTop)
        grid.addWidget(self._library(kit, pages), 10, kit.q.QtCore.Qt.AlignmentFlag.AlignTop)
        column.addLayout(grid)
        column.addWidget(kit.label(FOOTER, 'muted'))
        column.addStretch(1)
        self.widget = kit.scroll(content)

    def _status(self, kit, pages):
        w = kit.w
        self.card, box = kit.frame('card', margins=(24, 24, 24, 24), spacing=12)
        tone(self.card, 'tone', 'status')
        top = w.QHBoxLayout()
        top.addWidget(kit.label('CURRENT STATUS', 'eyebrow'), 1)
        self.pill = kit.label('Loading', 'pill', wrap=False)
        top.addWidget(self.pill)
        box.addLayout(top)
        self.site = kit.combo([], pages.choose_site)
        self.site.setAccessibleName('Site')
        self.site_row = kit.row(kit.label('Site', 'strong', wrap=False), self.site)
        box.addWidget(self.site_row)
        self.title = kit.label('Loading your status…', 'cardTitle')
        box.addWidget(self.title)
        self.session = kit.label('Account · Checking…', 'badge', wrap=False)
        self.worker = kit.label('Downloader · Checking…', 'badge', wrap=False)
        box.addWidget(kit.row(self.session, self.worker, spacing=8))
        self.save = kit.label('', rich=True)
        box.addWidget(self.save)
        self.status = kit.label('Connecting to your library…')
        box.addWidget(self.status)
        self.signin = kit.button('Sign in', pages.signin, 'secondary')
        self.go = kit.button('Go', pages.go, 'go')
        self.allow = kit.button('Allow this connection', pages.allow_network, 'secondary')
        self.pause = kit.button('Pause', pages.pause, 'secondary')
        box.addWidget(kit.row(self.signin, self.go, self.allow, self.pause, spacing=12))
        self.settings_link = kit.label('<a href="settings/downloads">Open download settings</a>', rich=True)
        box.addWidget(self.settings_link)
        self.cloud_entry = kit.label('Optional: keep a copy in your own cloud storage. '
                                     '<a href="settings/b2">Set it up in Settings</a>.', 'muted', rich=True)
        box.addWidget(self.cloud_entry)
        self.signin_status = kit.label('')
        box.addWidget(self.signin_status)
        self.live = w.QWidget()
        live = w.QVBoxLayout(self.live)
        live.setContentsMargins(0, 0, 0, 0)
        live.setSpacing(8)
        self.list_count = kit.label('')
        self.meter = w.QProgressBar()
        self.meter.setTextVisible(False)
        self.meter.setAccessibleName('Account lists checked')
        self.list_help = kit.label(LIST_HELP, 'muted')
        self.clock = kit.label('', 'muted')
        self.activity = kit.label('', 'activity')
        for part in (self.list_count, self.meter, self.list_help, self.clock, self.activity):
            live.addWidget(part)
        box.addWidget(self.live)
        self.detail = kit.label('', 'muted')
        self.estimate = kit.label('', 'muted')
        self.space = kit.label('', 'muted')
        self.footnote = kit.label('', 'footnote')
        for part in (self.detail, self.estimate, self.space, self.footnote):
            box.addWidget(part)
        return self.card

    def _library(self, kit, pages):
        w = kit.w
        card, box = kit.frame('card', margins=(24, 24, 24, 24), spacing=12)
        self.library_title = kit.label('Your saved library', 'sectionTitle')
        box.addWidget(self.library_title)
        self.metrics = w.QGridLayout()
        self.metrics.setSpacing(12)
        box.addLayout(self.metrics)
        self.metric_cells = []
        self.library_summary = kit.label('Counting your saved files…')
        box.addWidget(self.library_summary)
        self.places = w.QVBoxLayout()
        self.places.setSpacing(10)
        box.addLayout(self.places)
        self.place_buttons = []
        self.files = kit.button('View saved files  →', lambda: pages.go_to('library', location='all'), 'secondary')
        box.addWidget(kit.row(self.files))
        box.addWidget(kit.label('Comics in CBZ. Novels in EPUB.', 'muted'))
        return card

    def _metrics(self, metrics):
        if [cell[0] for cell in self.metric_cells] == [name for _, name in metrics]:
            for (_, number), (value, _) in zip(self.metric_cells, metrics, strict=True):
                number.setText(value)
            return
        for _, number in self.metric_cells:
            number.parentWidget().deleteLater()
        self.metric_cells = []
        for index, (value, name) in enumerate(metrics):
            cell, box = self.kit.frame('metric', margins=(12, 16, 12, 16), spacing=2)
            number = self.kit.label(value, 'metricNumber', wrap=False)
            box.addWidget(number)
            box.addWidget(self.kit.label(name, 'muted'))
            self.metrics.addWidget(cell, index // 2, index % 2)
            self.metric_cells.append((name, number))

    def _places(self, places):
        if [b.property('place') for b in self.place_buttons] == [p['id'] for p in places]:
            for button, place in zip(self.place_buttons, places, strict=True):
                button.setText(f"{place['label']}\n{place['counts']}")
            return
        for button in self.place_buttons:
            button.deleteLater()
        self.place_buttons = []
        for place in places:
            button = self.kit.button(f"{place['label']}\n{place['counts']}",
                                     lambda key=place['id']: self.pages.go_to('library', location=key), 'place')
            button.setProperty('place', place['id'])
            self.places.addWidget(button)
            self.place_buttons.append(button)

    def show(self, s, v):
        say(self.updated, v['updated'])
        self.pill.setText(v['state'])
        tone(self.pill, 'tone', v['tone'])
        tone(self.card, 'tone', 'warning' if v['tone'] == 'warning' else 'status')
        sites = [item for item in s.sites]
        if [self.site.itemData(i) for i in range(self.site.count())] != [item['key'] for item in sites]:
            blocked = self.site.blockSignals(True)
            self.site.clear()
            for item in sites:
                self.site.addItem(item['name'] if item.get('available') else item['name'] + ' — coming later', item['key'])
                self.site.model().item(self.site.count() - 1).setEnabled(bool(item.get('available')))
            self.site.blockSignals(blocked)
        choose(self.site, next((item['key'] for item in sites if item.get('current')), None))
        self.site_row.setVisible(bool(sites))
        self.site.setEnabled(v['site_enabled'])
        self.site.setToolTip(v['site_tooltip'])
        self.title.setText(v['title'])
        self.session.setText(v['session'])
        tone(self.session, 'tone', v['session_tone'])
        self.worker.setText(v['worker'])
        tone(self.worker, 'tone', v['worker_tone'])
        place = v['save_place']
        self.save.setText(f'<span>Saving to</span> <b>{html.escape(place)}</b> <a href="settings/storage">Change in Settings</a>'
                          if place else '')
        self.save.setVisible(bool(place))
        say(self.status, v['status'])
        self.signin.setVisible(v['signin_visible'])
        self.signin.setEnabled(v['signin_enabled'])
        self.signin.setText(v['signin_text'])
        self.go.setEnabled(v['go_enabled'])
        self.go.setText(v['go_text'])
        self.allow.setVisible(v['allow_visible'])
        self.allow.setEnabled(v['allow_enabled'])
        self.pause.setVisible(v['pause_visible'])
        self.pause.setEnabled(v['pause_enabled'])
        self.settings_link.setVisible(v['settings_link_visible'])
        self.cloud_entry.setVisible(storage_model.storage_choice(s)['entry_visible'])
        say(self.signin_status, v['signin_status'], v['signin_status_error'])
        self.live.setVisible(v['live_visible'])
        say(self.list_count, v['list_count'])
        self.meter.setVisible(v['meter_visible'])
        self.meter.setMaximum(v['meter_max'])
        self.meter.setValue(min(v['meter_value'], v['meter_max']))
        self.list_help.setVisible(v['list_help_visible'])
        say(self.clock, v['clock'])
        say(self.activity, v['activity'])
        tone(self.activity, 'tone', 'warning' if v['live_waiting'] else '')
        say(self.detail, v['detail'])
        say(self.estimate, v['estimate'])
        say(self.space, v['space'])
        say(self.footnote, v['footnote'])
        self.files.setEnabled(not s.busy)
        if s.summary is False:
            self.library_summary.setText(storage_model.LIBRARY_UNAVAILABLE)
            return
        overview = storage_model.library_overview(s.summary or None, s.cloud_offered)
        self._metrics(overview['metrics'])
        self._places(overview['locations'])
        self.library_summary.setText(overview['summary'])


class Settings:
    """Downloads, Storage and Backblaze B2, each on a tab of its own."""

    def __init__(self, kit, pages):
        w, self.kit, self.pages = kit.w, kit, pages
        content = w.QWidget()
        column = w.QVBoxLayout(content)
        column.setContentsMargins(32, 24, 32, 24)
        column.setSpacing(20)
        top = w.QHBoxLayout()
        title, _ = heading(kit, 'Settings')
        top.addWidget(title, 1)
        top.addWidget(kit.button('←  Back to dashboard', lambda: pages.go_to('dashboard'), 'secondary'), 0,
                      kit.q.QtCore.Qt.AlignmentFlag.AlignBottom)
        column.addLayout(top)
        layout = w.QHBoxLayout()
        layout.setSpacing(24)
        tabs = w.QVBoxLayout()
        tabs.setSpacing(4)
        self.tabs = {}
        group = w.QButtonGroup(content)
        for key, text in TABS:
            button = kit.button(text, lambda key=key: pages.go_to('settings/' + key), 'tab')
            button.setCheckable(True)
            group.addButton(button)
            tabs.addWidget(button)
            self.tabs[key] = button
        tabs.addStretch(1)
        side = w.QWidget()
        side.setFixedWidth(200)
        side.setLayout(tabs)
        layout.addWidget(side, 0, kit.q.QtCore.Qt.AlignmentFlag.AlignTop)
        panels = w.QVBoxLayout()
        panels.setSpacing(12)
        self.status = kit.label('')
        panels.addWidget(self.status)
        # Only the open tab takes room, so a short one is not as tall as the longest.
        self.panels = {'downloads': self._downloads(kit, pages), 'storage': self._storage(kit, pages), 'b2': self._b2(kit, pages)}
        from .window_style import READING
        for panel in self.panels.values():
            panels.addWidget(panel)
            # The cards fill the page; what is read in them, and typed, keeps to a width that reads easily.
            # A state box's words fill the box they sit in beside its label.
            for part in (*panel.findChildren(w.QLabel), *panel.findChildren(w.QLineEdit)):
                if not isinstance(part, w.QLabel) or (part.wordWrap() and part.parent().objectName() != 'state'):
                    part.setMaximumWidth(READING)
        panels.addStretch(1)
        layout.addLayout(panels, 1)
        column.addLayout(layout)
        column.addStretch(1)
        self.widget = kit.scroll(content)
        self.speed_rows = {}

    def _downloads(self, kit, pages):
        card, box = kit.frame('card', margins=(24, 24, 24, 24), spacing=12)
        box.addWidget(kit.label('Downloads', 'sectionTitle'))
        self.signin_note = kit.label('Sign in to a site to choose how fast it saves.', 'muted')
        box.addWidget(self.signin_note)
        self.speed_label = kit.label('Speed', 'strong')
        self.speed = kit.combo([(key, SPEED_NAMES[key]) for key in SPEEDS], pages.speed)
        self.speed_help = kit.label('', 'muted')
        self.speed_row = kit.w.QWidget()
        row = kit.w.QVBoxLayout(self.speed_row)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        row.addWidget(self.speed_label)
        row.addWidget(kit.row(self.speed))
        row.addWidget(self.speed_help)
        box.addWidget(self.speed_row)
        self.other_speeds = kit.w.QVBoxLayout()
        self.other_speeds.setSpacing(12)
        box.addLayout(self.other_speeds)
        self.network = kit.w.QCheckBox('Also save on metered and mobile connections')
        self.network.toggled.connect(pages.wifi_choice)
        self.network_help = kit.label('', 'muted')
        self.network_row = kit.w.QWidget()
        row = kit.w.QVBoxLayout(self.network_row)
        row.setContentsMargins(0, 8, 0, 0)
        row.addWidget(self.network)
        row.addWidget(self.network_help)
        box.addWidget(self.network_row)
        self.signout = kit.button('Sign out', pages.sign_out, 'secondary')
        self.signout_help = kit.label('', 'muted')
        self.signout_row = kit.w.QWidget()
        row = kit.w.QVBoxLayout(self.signout_row)
        row.setContentsMargins(0, 8, 0, 0)
        row.setSpacing(6)
        row.addWidget(kit.label('Sign-in', 'strong'))
        row.addWidget(self.signout_help)
        row.addWidget(kit.row(self.signout))
        box.addWidget(self.signout_row)
        return card

    def _storage(self, kit, pages):
        w = kit.w
        card, box = kit.frame('card', margins=(24, 24, 24, 24), spacing=12)
        box.addWidget(kit.label('Where your episodes are kept', 'sectionTitle'))
        self.choice = w.QWidget()
        choice = w.QVBoxLayout(self.choice)
        choice.setContentsMargins(0, 0, 0, 8)
        choice.setSpacing(6)
        choice.addWidget(kit.label('Store new episodes on', 'strong'))
        self.options_box = w.QHBoxLayout()
        self.options_box.setSpacing(24)
        self.options_box.addStretch(1)
        choice.addLayout(self.options_box)
        self.option_group = w.QButtonGroup(self.choice)
        self.option_group.setExclusive(True)
        self.option_group.idClicked.connect(lambda index: pages.choose_storage(self.options[index]['key']))
        self.options, self.option_buttons = [], []
        self.choice_lock = kit.label('', 'muted')
        self.choice_help = kit.label('', 'muted')
        self.choice_status = kit.label('')
        for part in (self.choice_lock, self.choice_help, self.choice_status):
            choice.addWidget(part)
        box.addWidget(self.choice)
        box.addWidget(kit.label('Each place shows what it holds. Cloud storage is set up on its own tab.', 'muted'))
        cards = w.QVBoxLayout()
        cards.setSpacing(16)
        device, inner = kit.frame('inner', margins=(16, 16, 16, 16), spacing=8)
        inner.addWidget(kit.label('This device', 'sectionTitle'))
        self.folder = kit.label('', rich=True)
        self.folder.setTextInteractionFlags(kit.q.QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        inner.addWidget(self.folder)
        self.device_stored = kit.label('Counting…')
        inner.addWidget(self.device_stored)
        self.open_folder = kit.button('Open folder', pages.open_folder, 'secondary')
        self.change_folder = kit.button('Change folder…', pages.change_folder, 'secondary')
        inner.addWidget(kit.row(kit.button('View stored episodes', lambda: pages.go_to('library', location='device'), 'secondary'),
                                self.open_folder, self.change_folder, spacing=8))
        self.folder_lock = kit.label('', 'muted')
        inner.addWidget(self.folder_lock)
        cards.addWidget(device)
        self.b2_card, inner = kit.frame('inner', margins=(16, 16, 16, 16), spacing=8)
        top = w.QHBoxLayout()
        top.addWidget(kit.label('Backblaze B2', 'sectionTitle'), 1)
        self.b2_pill = kit.label('Not set up', 'pill', wrap=False)
        top.addWidget(self.b2_pill)
        inner.addLayout(top)
        self.b2_text = kit.label('')
        self.b2_stored = kit.label('')
        inner.addWidget(self.b2_text)
        inner.addWidget(self.b2_stored)
        self.b2_files = kit.button('View stored episodes', lambda: pages.go_to('library', location='b2'), 'secondary')
        self.b2_open = kit.button('Open in Backblaze', lambda: pages.link('browse'), 'secondary')
        self.b2_setup = kit.button('Set up', lambda: pages.go_to('settings/b2'))
        inner.addWidget(kit.row(self.b2_files, self.b2_open, self.b2_setup, spacing=8))
        cards.addWidget(self.b2_card)
        box.addLayout(cards)
        return card

    def _b2(self, kit, pages):
        w = kit.w
        card, box = kit.frame('card', margins=(24, 24, 24, 24), spacing=12)
        box.addWidget(kit.label('Save to your Backblaze B2 account', 'sectionTitle'))
        self.b2_state, state = kit.frame('state', vertical=False, margins=(14, 10, 14, 10), spacing=12)
        self.b2_state_pill = kit.label('Not set up', 'pill', wrap=False)
        self.b2_state_text = kit.label('')
        state.addWidget(self.b2_state_pill)
        state.addWidget(self.b2_state_text, 1)
        box.addWidget(self.b2_state)
        self.shared = w.QCheckBox('Use the same Backblaze setup for every site')
        self.shared.toggled.connect(pages.share_b2)
        self.sharing_help = kit.label('', 'muted')
        self.sharing = w.QWidget()
        sharing = w.QVBoxLayout(self.sharing)
        sharing.setContentsMargins(0, 0, 0, 0)
        sharing.addWidget(self.shared)
        sharing.addWidget(self.sharing_help)
        box.addWidget(self.sharing)
        box.addWidget(kit.label(B2_INTRO))
        self.cloud_status = kit.label('Checking cloud connections…')
        self.action_status = kit.label('')
        self.repair = kit.label('')
        for part in (self.cloud_status, self.action_status, self.repair):
            box.addWidget(part)
        costs, inner = kit.frame('costs', margins=(16, 10, 16, 10), spacing=6)
        self.costs_toggle = kit.button('', self._fold_costs, 'link')
        inner.addWidget(self.costs_toggle)
        self.costs_body = w.QWidget()
        body = w.QVBoxLayout(self.costs_body)
        body.setContentsMargins(0, 0, 0, 0)
        body.addWidget(kit.label(B2_COSTS, rich=True))
        self.cost = kit.label('')
        body.addWidget(self.cost)
        body.addWidget(kit.label(B2_PRICES, 'muted', rich=True))
        self.costs_body.setVisible(False)
        inner.addWidget(self.costs_body)
        box.addWidget(costs)
        self.setup = w.QWidget()
        steps = w.QVBoxLayout(self.setup)
        steps.setContentsMargins(0, 0, 0, 0)
        steps.setSpacing(16)
        steps.addWidget(kit.label('One-time setup. Already have a Backblaze account? Start at step 2.'))
        steps.addWidget(kit.label(B2_STEP1, rich=True))
        steps.addWidget(kit.row(kit.button('Create B2 account', lambda: pages.link('signup'), 'secondary'),
                                kit.button('Sign in to Backblaze', lambda: pages.link('signin'), 'secondary'),
                                kit.button('Open Billing', lambda: pages.link('billing'), 'secondary'),
                                kit.button('Open Caps && Alerts', lambda: pages.link('caps'), 'secondary'), spacing=8))
        steps.addWidget(kit.label(B2_STEP2, rich=True))
        steps.addWidget(kit.row(kit.button('Open Buckets', lambda: pages.link('buckets'), 'secondary')))
        self.bucket = w.QLineEdit()
        self.bucket.setMaxLength(63)
        self.bucket.setAccessibleName('Bucket name')
        self.bucket.textEdited.connect(self._bucket_typed)
        steps.addWidget(kit.label('Bucket name', 'strong'))
        steps.addWidget(self.bucket)
        steps.addWidget(kit.label(B2_STEP2_MORE, rich=True))
        self.step3 = kit.label('', rich=True)
        steps.addWidget(self.step3)
        steps.addWidget(kit.row(kit.button('Open Application Keys', lambda: pages.link('keys'), 'secondary')))
        steps.addWidget(kit.label(B2_STEP4, rich=True))
        self.key_id = w.QLineEdit()
        self.key_id.setMaxLength(256)
        self.key_id.setAccessibleName('keyID')
        self.application_key = w.QLineEdit()
        self.application_key.setMaxLength(256)
        self.application_key.setEchoMode(w.QLineEdit.EchoMode.Password)
        self.application_key.setAccessibleName('applicationKey')
        for field in (self.key_id, self.application_key):
            field.textEdited.connect(lambda text, field=field: self._key_typed(field, text))
        self.application_key.returnPressed.connect(self._connect)
        self.connect = kit.button('Connect B2', self._connect)
        form = w.QFormLayout()
        form.addRow('keyID', self.key_id)
        form.addRow('applicationKey', self.application_key)
        steps.addLayout(form)
        self.auth = kit.label('')
        self.cancel = kit.button('Cancel sign-in', pages.cloud_cancel, 'secondary')
        steps.addWidget(kit.row(self.connect, self.cancel))
        steps.addWidget(self.auth)
        box.addWidget(self.setup)
        self.fold = kit.button('Show the setup steps', self._fold_steps, 'secondary')
        self.fold_row = kit.row(self.fold, kit.label('to use another bucket or key.', 'muted', wrap=False))
        box.addWidget(self.fold_row)
        box.addWidget(kit.label(B2_STEP5, rich=True))
        self.test = kit.button('Test cloud connection', pages.cloud_test)
        box.addWidget(kit.row(self.test))
        self.cleanup_box = w.QVBoxLayout()
        box.addLayout(self.cleanup_box)
        self.cleanups = []
        box.addWidget(kit.label(B2_STEP6, rich=True))
        box.addWidget(kit.row(kit.button('Go to Storage', lambda: pages.go_to('settings/storage'), 'secondary')))
        self.retry = kit.button('Retry unfinished uploads', pages.cloud_upload)
        box.addWidget(kit.row(self.retry))
        box.addWidget(kit.label(B2_YOUR_PAGES, rich=True))
        return card

    def _fold_costs(self):
        self.costs_body.setVisible(not self.costs_body.isVisible())

    def _fold_steps(self):
        self.pages.state.steps_open = not self.pages.state.steps_open
        self.pages.render()

    def _bucket_typed(self, text):
        value = clean_key(text)
        if value != text:
            self.bucket.setText(value)
        self.pages.state.bucket_draft = value
        self.pages.render()

    def _key_typed(self, field, text):
        value = clean_key(text)
        if value != text:
            field.setText(value)

    def _connect(self):
        if not self.connect.isEnabled():
            return
        self.pages.cloud_connect(self.bucket.text().strip(), self.key_id.text().strip(), self.application_key.text().strip())

    def forget_keys(self):
        """Typed keys last only while the B2 tab is open, and until they connect."""
        self.key_id.clear()
        self.application_key.clear()

    def _speeds(self, others):
        for key in list(self.speed_rows):
            if key not in [item['key'] for item in others]:
                self.speed_rows.pop(key)[0].deleteLater()
        for item in others:
            if item['key'] not in self.speed_rows:
                holder = self.kit.w.QWidget()
                row = self.kit.w.QVBoxLayout(holder)
                row.setContentsMargins(0, 0, 0, 0)
                row.setSpacing(6)
                label = self.kit.label('', 'strong')
                combo = self.kit.combo([(key, SPEED_NAMES[key]) for key in SPEEDS],
                                       lambda value, key=item['key']: self.pages.site_speed(key, value))
                help_text = self.kit.label('', 'muted')
                row.addWidget(label)
                row.addWidget(self.kit.row(combo))
                row.addWidget(help_text)
                self.other_speeds.addWidget(holder)
                self.speed_rows[item['key']] = (holder, label, combo, help_text)
            _, label, combo, help_text = self.speed_rows[item['key']]
            label.setText(item['label'])
            if item['speed']:
                choose(combo, item['speed'])
            combo.setEnabled(item['enabled'])
            help_text.setText(item['help'])

    def _storage_options(self, options):
        if [(o['key'], o['label']) for o in self.options] != [(o['key'], o['label']) for o in options]:
            for button in self.option_buttons:
                self.option_group.removeButton(button)
                button.deleteLater()
            self.option_buttons = []
            for index, option in enumerate(options):
                button = self.kit.w.QRadioButton(option['label'])
                self.option_group.addButton(button, index)
                self.options_box.insertWidget(self.options_box.count() - 1, button)
                self.option_buttons.append(button)
        self.options = options
        for button, option in zip(self.option_buttons, options, strict=True):
            button.setEnabled(option['enabled'])
            if option['checked'] and not button.isChecked():
                button.setChecked(True)

    def _cleanups(self, cleanup):
        if [c['test_id'] for c in cleanup] != [b.property('test') for b in self.cleanups]:
            for button in self.cleanups:
                button.deleteLater()
            self.cleanups = []
            for record in cleanup:
                button = self.kit.button(record['text'], lambda test=record['test_id']: self.pages.cloud_cleanup(test), 'secondary')
                button.setProperty('test', record['test_id'])
                self.cleanup_box.addWidget(button)
                self.cleanups.append(button)
        for button, record in zip(self.cleanups, cleanup, strict=True):
            button.setEnabled(record['enabled'])

    def show(self, s, v, tab):
        for key, button in self.tabs.items():
            button.setVisible(key != 'b2' or s.cloud_offered)
        self.tabs[tab].setChecked(True)
        for key, panel in self.panels.items():
            panel.setVisible(key == tab)
        say(self.status, v['settings_status'], v['settings_status_error'])
        self.signin_note.setVisible(v['signin_note_visible'])
        self.speed_row.setVisible(v['speed_visible'])
        self.speed_label.setText(v['speed_label'])
        choose(self.speed, v['speed'])
        self.speed.setEnabled(v['speed_enabled'])
        self.speed_help.setText(v['speed_help'])
        self._speeds(v['other_speeds'])
        self.network_row.setVisible(v['network_visible'])
        if not s.network_saving and self.network.isChecked() != v['network_checked']:
            blocked = self.network.blockSignals(True)
            self.network.setChecked(v['network_checked'])
            self.network.blockSignals(blocked)
        self.network.setEnabled(v['network_enabled'])
        self.network_help.setText(v['network_help'])
        self.signout_row.setVisible(v['signout_visible'])
        self.signout.setText(v['signout_text'])
        self.signout.setEnabled(v['signout_enabled'])
        self.signout_help.setText(v['signout_help'])
        # Storage.
        choice = storage_model.storage_choice(s)
        self.choice.setVisible(choice['visible'])
        self._storage_options(choice['options'])
        say(self.choice_lock, choice['lock'])
        self.choice_help.setText(choice['help'])
        say(self.choice_status, s.choice_message, bool(s.choice_message))
        place = storage_model.storage(s)
        self.folder.setText(f'<span>Folder</span> <b>{html.escape(s.library_folder or "")}</b>')
        self.device_stored.setText(place['device_stored'])
        self.change_folder.setEnabled(v['folder_enabled'])
        self.change_folder.setToolTip(v['folder_tooltip'])
        self.open_folder.setEnabled(not s.busy)
        say(self.folder_lock, v['folder_lock'])
        self.b2_card.setVisible(s.cloud_offered)
        self.b2_pill.setText(place['b2_pill'])
        tone(self.b2_pill, 'tone', place['b2_tone'])
        self.b2_text.setText(place['b2_text'])
        say(self.b2_stored, place['b2_stored'])
        self.b2_files.setVisible(place['b2_links_visible'])
        self.b2_open.setVisible(place['b2_links_visible'])
        self.b2_setup.setText(place['b2_setup'])
        # Backblaze B2.
        b2 = storage_model.b2(s, s.steps_open)
        self.b2_state_pill.setText(b2['pill'])
        tone(self.b2_state_pill, 'tone', b2['tone'])
        tone(self.b2_state, 'tone', b2['tone'])
        self.b2_state_text.setText(b2['text'])
        self.sharing.setVisible(b2['sharing_visible'])
        if self.shared.isChecked() != b2['shared']:
            blocked = self.shared.blockSignals(True)
            self.shared.setChecked(b2['shared'])
            self.shared.blockSignals(blocked)
        self.shared.setEnabled(b2['sharing_enabled'])
        self.sharing_help.setText(b2['sharing_help'])
        self.cloud_status.setText(b2['status'])
        message = s.cloud_message
        say(self.action_status, message.text if message else '', bool(message and message.error))
        say(self.repair, b2['repair'])
        self.costs_toggle.setText(f"Estimated Backblaze cost: {b2['cost_short']}  ·  Cost details")
        say(self.cost, b2['cost'])
        bucket = storage_model.bucket_name(s.bucket_draft, b2['bucket'], s.workspace_id or '')
        if self.bucket.text() != bucket:
            self.bucket.setText(bucket)
        self.step3.setText(prose(B2_STEP3 % html.escape(bucket or 'the one from step 2')))
        self.setup.setVisible(not b2['folded'])
        self.fold_row.setVisible(b2['fold_visible'])
        self.fold.setText(b2['fold_text'])
        self.connect.setText(b2['connect_text'])
        self.connect.setEnabled(b2['connect_enabled'])
        for field in (self.key_id, self.application_key):
            field.setEnabled(b2['connect_enabled'])
        self.cancel.setVisible(b2['cancel_visible'])
        self.cancel.setEnabled(b2['cancel_enabled'])
        say(self.auth, b2['auth'])
        self.test.setEnabled(b2['test_enabled'])
        self._cleanups(b2['cleanup'])
        self.retry.setEnabled(b2['retry_enabled'])


class Library:
    """Every saved file, a page at a time: found by name, filtered, sorted, and opened in the computer's own app."""

    def __init__(self, kit, pages):
        w, self.kit, self.pages = kit.w, kit, pages
        self.location, self.offset, self.total, self.limit = 'all', 0, 0, library_model.PAGE_SIZES[0]
        self.request, self.busy, self.value, self.desktop_only = 0, False, None, False
        content = w.QWidget()
        column = w.QVBoxLayout(content)
        column.setContentsMargins(32, 24, 32, 24)
        column.setSpacing(14)
        self.notice = kit.label(library_model.LOADING, 'notice')
        column.addWidget(self.notice)
        top = w.QHBoxLayout()
        self.title = kit.label(library_model.title('Tapas'), 'pageTitle')
        top.addWidget(self.title, 1)
        self.open_folder = kit.button('Open folder', pages.open_folder, 'secondary')
        self.refresh = kit.button('Refresh files', lambda: self.load(refresh=True), 'secondary')
        top.addWidget(self.open_folder)
        top.addWidget(self.refresh)
        column.addLayout(top)
        self.summary = kit.label('')
        column.addWidget(self.summary)
        self.places = w.QHBoxLayout()
        self.places.setSpacing(12)
        column.addLayout(self.places)
        self.place_buttons = []
        self.search = w.QLineEdit()
        self.search.setMaxLength(library_model.MAX_QUERY)
        self.search.setPlaceholderText('Series, title or filename')
        self.search.setAccessibleName('Find saved files')
        self.search.setMinimumWidth(300)
        self.search.returnPressed.connect(self.first_page)
        self.kind = kit.combo(library_model.KINDS, lambda _: self.first_page())
        self.sort = kit.combo(library_model.SORTS, lambda _: self.first_page())
        choose(self.kind, 'episodes')
        self.find = kit.button('Search', self.first_page)
        self.clear = kit.button('Clear', self.clear_search, 'secondary')
        filters = w.QHBoxLayout()
        filters.setSpacing(12)
        for text, widget in (('Find saved files', self.search), ('File types', self.kind), ('Sort by', self.sort)):
            box = w.QVBoxLayout()
            box.setSpacing(4)
            box.addWidget(kit.label(text, 'strong', wrap=False))
            box.addWidget(widget)
            filters.addLayout(box)
        filters.addWidget(self.find, 0, kit.q.QtCore.Qt.AlignmentFlag.AlignBottom)
        filters.addWidget(self.clear, 0, kit.q.QtCore.Qt.AlignmentFlag.AlignBottom)
        filters.addStretch(1)
        column.addLayout(filters)
        self.range = kit.label('')
        column.addWidget(self.range)
        self.table = w.QTableWidget(0, len(library_model.COLUMNS))
        self.table.setHorizontalHeaderLabels(list(library_model.COLUMNS))
        self.table.setEditTriggers(w.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(w.QAbstractItemView.SelectionMode.NoSelection)
        self.table.setWordWrap(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(False)
        self.table.setMinimumHeight(420)
        header = self.table.horizontalHeader()
        header.setStretchLastSection(False)
        # The names share what room there is; the short columns take what they need, and the File column its button's.
        modes = w.QHeaderView.ResizeMode
        for index in range(len(library_model.COLUMNS)):
            header.setSectionResizeMode(index, modes.Stretch if index < 3 else modes.Fixed if index == 7 else modes.ResizeToContents)
        self.table.setColumnWidth(7, 120)
        self.table.setAccessibleName('Saved file details')
        column.addWidget(self.table, 1)
        self.previous = kit.button('Previous', self.previous_page, 'secondary')
        self.next = kit.button('Next', self.next_page, 'secondary')
        self.page = w.QSpinBox()
        self.page.setMinimum(1)
        self.page.setButtonSymbols(w.QAbstractSpinBox.ButtonSymbols.NoButtons)  # Typed, then Go.
        self.page.setMinimumWidth(64)
        self.page.setAccessibleName('Page')
        self.page_count = kit.label('', wrap=False)
        self.jump = kit.button('Go', self.jump_page, 'secondary')
        self.size = kit.combo([(n, str(n)) for n in library_model.PAGE_SIZES], lambda _: self.first_page())
        column.addWidget(kit.row(self.previous, kit.label('Page', 'strong', wrap=False), self.page, self.page_count, self.jump,
                                 self.next, kit.label('Files per page', 'strong', wrap=False), self.size, spacing=10))
        column.addWidget(kit.label(library_model.NOTE, 'muted'))
        self.widget = kit.scroll(content)
        self.buttons()

    def buttons(self):
        for widget in (self.refresh, self.find, self.clear, self.search, self.kind, self.sort, self.size, self.page, self.jump):
            widget.setEnabled(not self.busy)
        self.previous.setEnabled(not self.busy and self.offset > 0)
        self.next.setEnabled(not self.busy and self.offset + self.size.currentData() < self.total)
        for button in self.place_buttons:
            button.setEnabled(not self.busy)

    def open_at(self, location, site, folder, desktop_only):
        """Opened from the dashboard or Settings: at a storage place, or where it was."""
        self.title.setText(library_model.title(site))
        self.open_folder.setVisible(bool(folder))
        self.desktop_only = desktop_only
        if desktop_only:
            self.location = 'device'
        elif location is not None:
            self.location = location
        if location is not None:
            self.offset = 0
        self.load()

    def first_page(self):
        self.offset = 0
        self.load()

    def clear_search(self):
        self.search.clear()
        choose(self.kind, 'episodes')
        self.first_page()

    def previous_page(self):
        self.offset = max(0, self.offset - self.size.currentData())
        self.load()

    def next_page(self):
        self.offset += self.size.currentData()
        self.load()

    def jump_page(self):
        self.offset = library_model.offset_for(self.page.value(), self.size.currentData())
        self.load()

    def choose_place(self, key):
        self.location, self.offset = key, 0
        self.load()

    def load(self, refresh=False):
        self.request += 1
        request, self.busy = self.request, True
        say(self.notice, library_model.LOADING)
        self.buttons()
        options = {'location': self.location, 'query': self.search.text().strip()[:library_model.MAX_QUERY],
                   'kind': self.kind.currentData(), 'sort': self.sort.currentData(), 'offset': self.offset,
                   'limit': self.size.currentData(), 'refresh': refresh}
        self.pages.run(lambda: self.pages.actions.saved_files(**options), lambda value, error: self.loaded(request, value, error))

    def loaded(self, request, value, error):
        if request != self.request:
            return
        self.busy = False
        if error is not None:
            self.total = 0
            self.table.setRowCount(0)
            self.range.setText('')
            say(self.notice, error, True)
        else:
            self.value = value
            self.offset, self.total = value.get('offset') or 0, value.get('total') or 0
            self.show_places(value)
            self.show_rows(value)
            page, count = library_model.pages(value)
            self.page.setMaximum(count)
            self.page.setValue(page)
            self.page_count.setText(f'of {count:,}')
            self.range.setText(library_model.range_text(value))
            say(self.notice, library_model.notice(value))
        self.buttons()

    def show_places(self, value):
        places = library_model.places(value, self.desktop_only, self.location)
        for button in self.place_buttons:
            button.deleteLater()
        self.place_buttons = []
        for place in places:
            button = self.kit.button(f"{place['label']}\n{place['counts']}", lambda key=place['id']: self.choose_place(key), 'place')
            button.setCheckable(True)
            button.setChecked(place['current'])
            self.places.addWidget(button)
            self.place_buttons.append(button)
        self.summary.setText(library_model.summary(value, self.desktop_only))
        self.table.setColumnHidden(6, not library_model.has_cloud(value, self.desktop_only))

    def show_rows(self, value):
        w = self.kit.w
        rows = library_model.rows(value, self.desktop_only)
        self.table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            saved = row['saved'] + ('\nFile time' if row['file_time'] else '')
            for column, text in enumerate((row['series'], row['title'], row['filename'], row['type'], row['size'], saved,
                                           row['storage'])):
                item = w.QTableWidgetItem(text)
                if column == 5 and row['exact']:
                    item.setToolTip(row['exact'])
                self.table.setItem(index, column, item)
            if row['open']:
                self.table.setCellWidget(index, 7, self.opener(row))
            else:
                self.table.removeCellWidget(index, 7)
                self.table.setItem(index, 7, w.QTableWidgetItem(row['action']))
        self.table.resizeRowsToContents()

    def opener(self, row):
        holder = self.kit.w.QWidget()
        box = self.kit.w.QVBoxLayout(holder)
        box.setContentsMargins(4, 4, 4, 4)
        box.setSpacing(4)
        button = self.kit.button('Open', None, 'secondary')
        button.setAccessibleName('Open ' + row['filename'])
        feedback = self.kit.label('', 'muted')
        feedback.setVisible(False)
        button.clicked.connect(lambda checked=False: self.open_file(row['open'], button, feedback))
        box.addWidget(button)
        box.addWidget(feedback)
        return holder




    def open_file(self, export_id, button, feedback):
        button.setEnabled(False)
        button.setText('Opening…')
        say(feedback, library_model.OPENING)

        def done(value, error):
            button.setEnabled(True)
            button.setText('Open')
            say(feedback, error or library_model.OPENED, error is not None)
            self.table.resizeRowsToContents()
        self.pages.run(lambda: self.pages.actions.open_file(export_id), done)


class Pages:
    """The window's pages and the state they show, refreshed every few seconds while the window is open.

    shell gives the pages what lies outside them: run(work, done) puts work on a thread and done(value, error)
    back on the window's; and the window's own steps for sites, folders, the theme and links.
    """

    def __init__(self, q, shell, actions, now=time.time):
        self.q, self.shell, self.actions, self.now = q, shell, actions, now
        self.state = State()
        self.route = 'dashboard'
        self.polling = False
        self.polled_task = None
        kit = Kit(q, self.link)
        w = q.QtWidgets
        self.widget = w.QWidget()
        column = w.QVBoxLayout(self.widget)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)
        self.header = Header(kit, self)
        column.addWidget(self.header.widget)
        self.stack = w.QStackedWidget()
        self.dashboard = Dashboard(kit, self)
        self.settings = Settings(kit, self)
        self.library = Library(kit, self)
        for page in (self.dashboard, self.settings, self.library):
            self.stack.addWidget(page.widget)
        column.addWidget(self.stack, 1)

    # Moving between pages.

    def go_to(self, name, location=None):
        route = route_of(name, self.state.cloud_offered)
        if self.route == 'settings/b2' and route != 'settings/b2':
            self.settings.forget_keys()
        opened = route.partition('/')[0] != self.route.partition('/')[0]
        self.route = route
        page = {'dashboard': self.dashboard, 'library': self.library}.get(route, self.settings)
        self.stack.setCurrentWidget(page.widget)
        if opened:
            page.widget.verticalScrollBar().setValue(0)
        if route == 'library':
            self.library.open_at(location if location is not None else
                                 None if self.library.value is not None else 'all',
                                 self.state.site(), self.state.library_folder, not self.state.cloud_offered)
        self.render()

    def link(self, target):
        """A link in a page: another page of the window, or one of Backblaze's pages in the computer's browser."""
        if target in B2_PAGES:
            self.shell.open_url(B2_PAGES[target])
        else:
            self.go_to(target)

    # Reading the library.

    def start(self, then=None):
        """The library is open: read its context, then refresh it. then(ok) follows that first refresh."""
        def done(value, error):
            if error is None:
                self.received_context(value)
            self.poll(then)
        self.run(self.actions.context, done)

    def received_context(self, value):
        s = self.state
        s.sites, s.site_settings = value['sites'], value['site_settings']
        s.library_folder, s.cloud_offered = value['library_folder'], value['cloud_offered']
        s.workspace_id = value['workspace_id']
        self.render()

    def reopened(self, then=None):
        """A library is open, at the start or after another site or folder was chosen: read everything, afresh."""
        s, route = self.state, self.route
        self.state = State(cloud_offered=s.cloud_offered)
        self.polling = False
        self.library.value = None
        self.settings.forget_keys()
        self.start(then)
        if route == 'library':
            self.go_to('library', location='all')
        else:
            self.render()

    def drawn(self):
        """Whether the dashboard shows a library it has read."""
        return self.state.loaded and self.dashboard.status.text() != 'Connecting to your library…'

    def poll(self, then=None):
        s = self.state
        if s.busy or self.polling:
            return
        self.polling = True
        now = self.now()
        network, library, recount = app_actions.due(s, now)
        task = s.task

        def done(value, error):
            self.polling = False
            if self.state is not s:
                return  # Another site or folder opened meanwhile; its own refresh follows.
            if error is not None:
                s.refresh_failed()
            elif app_actions.received(s, value, self.now(), task):
                self.settings.forget_keys()
            self.render()
            if then is not None:
                then(error is None)
        self.run(lambda: self.actions.poll(task, network=network, cloud=s.cloud_offered, library=library, refresh=recount), done)

    def render(self):
        s, now = self.state, self.now()
        v = dashboard_model.view(s, now)
        self.header.show(self.route, self.shell.dark())
        self.dashboard.show(s, v)
        if self.route.startswith('settings/'):
            self.settings.show(s, v, self.route.partition('/')[2])

    # What the person asks for.

    def run(self, work, done):
        self.shell.run(work, done)




    def act(self, work, done=None, cloud=False, after=None):
        """One change at a time: the buttons wait while it runs, and a refusal is said on the dashboard and in Settings."""
        s = self.state
        if s.busy:
            return
        s.busy = True
        self.render()

        def finished(value, error):
            s.busy = False
            if after is not None:
                after()
            if error is not None:
                s.failed(error, self.now())
                if cloud:
                    s.cloud_message = dashboard_model.Message(error, True)
            elif done is not None:
                done(value)
            self.render()
        self.run(work, finished)

    def account_act(self, work, done=None, after=None):
        if self.state.busy:
            return
        self.state.message = None
        self.act(work, done, after=after)

    def signin(self):
        self.account_act(self.actions.signin, lambda value: self.state.received_site_auth(value, self.now()))

    def sign_out(self):
        s = self.state
        if s.busy or not self.shell.ask(SIGN_OUT.format(site=s.site())):
            return

        def signed_out(value):
            # Said at once; the next refresh reads the same from the library.
            s.status = dict(s.status or {}, session_configured=False)
            s.received_site_auth({'status': 'idle'}, self.now())
            self.poll_soon()
        self.account_act(self.actions.sign_out, signed_out)

    def go(self):
        def started(value):
            self.state.received_account(value, self.now())
            self.state.offline = False
        self.account_act(self.actions.go, started)

    def pause(self):
        def paused(value):
            self.state.received_account(value, self.now())
            self.poll_soon()
        self.account_act(self.actions.pause, paused)

    def poll_soon(self):
        self.q.QtCore.QTimer.singleShot(0, self.poll)

    def speed(self, value):
        self.account_act(lambda: self.actions.speed(value), lambda account: self.state.received_account(account, self.now()))

    def site_speed(self, key, value):
        def kept(settings):
            self.state.site_settings = settings
        self.act(lambda: self.actions.site_speed(key, value), kept)

    def wifi_choice(self, allowed):
        s = self.state
        if s.busy:
            self.render()  # Another action is running; show the saved choice again.
            return
        s.network_saving = True

        def checked():
            self.actions.wifi_only(not allowed)
            return self.actions.poll(network=True, cloud=False)['network']

        def kept(value):
            s.network, s.network_at = value, self.now()

        def settled():
            # The box shows the choice while it is kept; the drawing after shows what was saved.
            s.network_saving = False
        self.account_act(checked, kept, settled)

    def allow_network(self):
        s = self.state

        def allowed():
            self.actions.wifi_only(False)
            return self.actions.poll(network=True, cloud=False)['network']

        def kept(value):
            s.network, s.network_at = value, self.now()
        self.account_act(allowed, kept)

    def choose_storage(self, chosen):
        s = self.state
        if s.busy:
            self.render()
            return
        targets = list(s.cloud_targets())

        def kept(value):
            s.cloud, s.choice_message = value, ''

        def finished(value, error):
            s.busy = False
            if error is None:
                kept(value)
            else:
                s.choice_message = error
            self.render()
            if error is not None:
                self.poll_soon()  # Shows the choice as it was kept.
        s.busy = True
        self.render()
        self.run(lambda: self.actions.choose_storage(chosen, targets), finished)

    def share_b2(self, shared):
        def kept(value):
            self.state.cloud = value
        self.act(lambda: self.actions.cloud_storage('shared' if shared else 'separate'), kept)

    def cloud_connect(self, bucket, key_id, application_key):
        s = self.state
        problem = storage_model.key_problem(bucket, key_id, application_key)
        if problem:
            s.cloud_message = dashboard_model.Message(problem, True)
            self.render()
            return
        # The name is kept as typed, so a refused key leaves it there to correct.
        s.bucket_draft = bucket
        credentials = {'key_id': key_id, 'application_key': application_key, 'bucket': bucket}

        def started(value):
            s.cloud_auth = value
            s.cloud_message = dashboard_model.Message(storage_model.CONNECTING)
            self.poll_soon()
        self.act(lambda: self.actions.cloud_signin('b2', credentials), started, cloud=True)

    def cloud_cancel(self):
        def cancelled(value):
            self.state.cloud_auth = value
        self.act(self.actions.cloud_cancel, cancelled)

    def _task(self, kind):
        def started(value):
            self.state.task, self.state.task_kind = value.get('task_id'), kind
            self.state.cloud_message = dashboard_model.Message(storage_model.TASK_STARTED[kind])
        return started

    def cloud_test(self):
        self.act(self.actions.cloud_test, self._task('cloud-test'), cloud=True)

    def cloud_upload(self):
        self.act(self.actions.cloud_upload, self._task('cloud'), cloud=True)

    def cloud_cleanup(self, test_id):
        self.act(lambda: self.actions.cloud_cleanup(test_id), self._task('cloud-cleanup'), cloud=True)

    # Asked of the window itself.

    def choose_site(self, key):
        self.shell.choose_site(key)
        self.render()  # The list shows the open site until the switch is done.

    def open_folder(self):
        self.shell.open_folder()

    def change_folder(self):
        self.shell.choose_location()

    def toggle_theme(self):
        self.shell.toggle_theme()
        self.render()
