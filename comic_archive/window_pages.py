"""The window's own pages: the dashboard and Settings, drawn with Qt's widgets.

What each page says and allows is decided in dashboard_model, storage_model; this module only
puts it on screen and passes clicks on. Every call to the library runs on a thread (the shell's run), never on the
window's own, and its answer comes back here.
"""
import html
import time

from . import app_actions, dashboard_model, license_texts, storage_model, support_details, support_log
from .window_library import EpisodeLibrary
from .dashboard_model import SPEED_NAMES, SPEEDS, State
from .sites import DEFAULT, choices as site_choices, find as find_site

ROUTES = ('dashboard', 'library', 'settings/downloads', 'settings/storage', 'settings/b2', 'settings/about')
TABS = (('downloads', 'Downloads && storage'), ('b2', 'Backblaze B2'), ('about', 'About'))
SIGN_OUT = ('Sign out of {site} in Comic Archiver?\n\nThis signs out only Comic Archiver, on this computer. Your '
            '{site} account isn\'t changed, and anywhere else you use {site}, like your web browser, stays signed in.'
            '\n\nYour saved episodes stay where they are. To download again, sign in again.')
# After signing out: the library folders the app opened before that it could not sign out in.
SIGNED_OUT_AWAY = {1: ('Signed out of {site}, except in this library folder, which Comic Archiver can\'t reach:\n{folders}\n'
                       'Connect the drive it\'s on, choose the folder using the edit icon beside the library folder '
                       'on the dashboard, and sign out there.'),
                   2: ('Signed out of {site}, except in these library folders, which Comic Archiver can\'t reach:\n'
                       '{folders}\nConnect the drives they\'re on, choose each folder using the edit icon beside the '
                       'library folder on the dashboard, and sign out there.')}
# Asked once for a cloud connection that came with the library folder, before any new episode goes to it.
CLOUD_CONFIRM = ('This library folder saves new episodes to {bucket}. That connection came with the folder: it '
                 'wasn\'t set up in Comic Archiver on this computer.\n\nKeep saving new episodes there?\n\n'
                 'If you choose No, they\'re saved on this computer instead.')
FOOTER = ('Saving runs only while this window is open. Only episodes your account can already read, free or bought, '
          'are saved. Nothing is bought for you.')
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
B2_STEP6 = ('<b>6. Choose where to save.</b><br>Where new episodes go is chosen in one place: “Save new episodes to” on '
            'the Downloads &amp; storage tab of these settings. Once the check passes, Backblaze B2 is offered there.')
B2_YOUR_PAGES = ('<b>Your Backblaze pages</b><br><span>Each opens in your browser, after Backblaze’s sign-in if needed.</span><ul>'
                 '<li><a href="browse">Browse Files</a>: see your saved episodes and download any of them. They are ordinary CBZ '
                 'files that any comic reader opens, with or without Comic Archiver.</li>'
                 '<li><a href="reports">Reports</a>: how much you store and what it costs, day by day.</li>'
                 '<li><a href="billing">Billing</a>: your card and your monthly bills.</li>'
                 '<li><a href="caps">Caps &amp; Alerts</a>: the most you allow a day, and Backblaze’s emails as you near it.</li>'
                 '<li><a href="keys">Application Keys</a>: delete Comic Archiver’s key here to take away its access.</li></ul>')
ABOUT = ('Copyright 2026 Ben Crute. Comic Archiver is free software under the MIT license. It is built with '
         'other people’s software, each under its own license; every one of those licenses can be read here.')
SUPPORT = ('Asking for help? This copies a short summary of each site’s download to paste into your message: the '
           'app’s version, counts and the app’s own codes. It has no series titles, folder names or sign-in details.')
SUPPORT_COPIED = 'Copied. Paste it into your message or email.'
SUPPORT_LOG = ('Asked for the app’s log? Save log for support puts it in one file in your Downloads folder, to attach to '
               'an email. It has the app’s own messages and each site’s series and episode numbers, but no titles, '
               'folder names or sign-in details.')
LOG_SAVED = 'Saved in your Downloads folder as “{name}”.'
LOG_FAILED = 'The log could not be saved. Check that your Downloads folder can be written to.'
# The dashboard puts two cards in a row from this width of its page's content, and one below it.
CARD_COLUMNS_FROM = 660
CARD_THREE_COLUMNS_FROM = 1000


def route_of(name, cloud_offered):
    """The page a request leads to: a Settings tab that is not offered leads to the first one."""
    if name in ('settings', 'settings/storage'):
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
    window.setProperty('comicDark', bool(dark))
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

    def single_line(self, text='', name=None):
        """Keep full text accessible, painting only the part that fits on one line."""
        q, w = self.q, self.w
        class SingleLine(w.QLabel):
            def setText(self, value):
                value = ' '.join(value.splitlines())
                super().setText(value)
                self.setToolTip(value)
                self.setAccessibleDescription(value)
                self.updateGeometry()

            def sizeHint(self):
                # Let QLabel account for its font, styling and text margins.
                # Keep only the minimum width and single-line height under our control.
                natural = super().sizeHint()
                return q.QtCore.QSize(max(self.minimumWidth(), natural.width()), self.fontMetrics().height())

            def minimumSizeHint(self):
                return q.QtCore.QSize(self.minimumWidth(), self.fontMetrics().height())

            def paintEvent(self, event):
                painter = q.QtGui.QPainter(self)
                area = self.contentsRect()
                value = self.fontMetrics().elidedText(self.text(), q.QtCore.Qt.TextElideMode.ElideRight, area.width())
                self.style().drawItemText(painter, area, self.alignment() | q.QtCore.Qt.TextFlag.TextSingleLine,
                                          self.palette(), self.isEnabled(), value, self.foregroundRole())
        label = SingleLine()
        label.setTextFormat(q.QtCore.Qt.TextFormat.PlainText)
        label.setWordWrap(False)
        label.setSizePolicy(w.QSizePolicy.Policy.Ignored, w.QSizePolicy.Policy.Fixed)
        label.setMinimumWidth(0)
        if name:
            label.setObjectName(name)
        label.setText(text)
        return label

    def button(self, text, clicked=None, kind=None):
        button = self.w.QPushButton(text)
        if kind:
            button.setProperty('kind', kind)
        button.setCursor(self.q.QtCore.Qt.CursorShape.PointingHandCursor)
        if clicked is not None:
            button.clicked.connect(lambda checked=False: clicked())
        return button

    def hint(self):
        """A small ? that explains what is beside it: on hover, as any tooltip, and on a click or tap too."""
        hint = self.w.QToolButton()
        hint.setObjectName('hint')
        hint.setText('?')
        hint.setAccessibleName('What this means')
        hint.setCursor(self.q.QtCore.Qt.CursorShape.WhatsThisCursor)
        hint.clicked.connect(lambda checked=False: self.w.QToolTip.showText(
            hint.mapToGlobal(self.q.QtCore.QPoint(0, hint.height())), hint.toolTip(), hint))
        return hint

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

    def combo(self, choices, changed=None, *, wheel_changes=True):
        if wheel_changes:
            combo = self.w.QComboBox()
        else:
            self_kit = self

            class PageCombo(self.w.QComboBox):
                def wheelEvent(self, event):
                    # A wheel over a closed selector belongs to the page. The
                    # popup's own list still handles scrolling when opened.
                    parent = self.parentWidget()
                    while parent is not None and not isinstance(parent, self_kit.w.QScrollArea):
                        parent = parent.parentWidget()
                    if parent is not None:
                        viewport = parent.viewport()
                        position = viewport.mapFromGlobal(event.globalPosition().toPoint())
                        forwarded = self_kit.q.QtGui.QWheelEvent(self_kit.q.QtCore.QPointF(position), event.globalPosition(),
                                                                 event.pixelDelta(), event.angleDelta(), event.buttons(),
                                                                 event.modifiers(), event.phase(), event.inverted())
                        self_kit.w.QApplication.sendEvent(viewport, forwarded)
                        event.accept()
                        return
                    event.ignore()
            combo = PageCombo()
            combo.setFocusPolicy(self.q.QtCore.Qt.FocusPolicy.StrongFocus)
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


def reserved(widget):
    """A temporarily empty message or control keeps its place in the layout."""
    policy = widget.sizePolicy()
    policy.setRetainSizeWhenHidden(True)
    widget.setSizePolicy(policy)


def set_enabled(widget, enabled):
    """Release focus before Qt disables a control and advances to another card."""
    if not enabled and widget.hasFocus():
        widget.clearFocus()
    widget.setEnabled(enabled)


def set_visible(widget, visible):
    """A control disappearing during an update must not focus another card."""
    if not visible and widget.hasFocus():
        widget.clearFocus()
    widget.setVisible(visible)


def say(label, text, error=False):
    if label.text() != text:
        label.setText(text)
    tone(label, 'error', error)
    label.setVisible(bool(text))


def placed(*widgets):
    """Show widgets just put on a page that is already showing: Qt would show them only on its next pass."""
    for widget in widgets:
        widget.show()


def discard(layout, *widgets):
    """Take widgets out of the page at once: deleted later, they would still be drawn until then."""
    for widget in widgets:
        layout.removeWidget(widget)
        widget.hide()
        widget.deleteLater()


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
        self.kit = kit
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
        from .window_art import icon
        from .window_style import colours
        for key, button in self.nav.items():
            button.setIcon(icon(self.kit.q, key, colours(dark)['accent' if button.isChecked() else 'text']))
        # The switch shows where it leads: the sun to light, the moon to dark.
        self.theme.setText('☀' if dark else '☾')
        tip = 'Switch to light mode' if dark else 'Switch to dark mode'
        self.theme.setToolTip(tip)
        self.theme.setAccessibleName(tip)


def watch_width(kit, widget, resized):
    """resized(width) each time the widget is resized. The watcher is the widget's child, and lives as long as it."""
    class Watcher(kit.q.QtCore.QObject):
        def eventFilter(self, watched, event):
            if event.type() == kit.q.QtCore.QEvent.Type.Resize:
                resized(watched.width())
            return False
    watcher = Watcher(widget)
    widget.installEventFilter(watcher)
    return watcher


class SiteCard:
    """One site's download at a glance: its state, its buttons, and what is so now, a field and its value each."""

    def __init__(self, kit, pages, key, name):
        w, self.kit, self.pages, self.key, self.name = kit.w, kit, pages, key, name
        from .window_art import paper_card, paper_mark
        self.widget = paper_card(kit.q)
        self.widget.setProperty('provider', key)
        box = w.QVBoxLayout(self.widget)
        box.setContentsMargins(18, 18, 18, 18)
        box.setSpacing(10)
        self.widget.setAccessibleName(name)
        self.widget.setProperty('site', True)
        top = w.QHBoxLayout()
        top.setContentsMargins(0, 0, 8, 0)
        top.setSpacing(6)
        top.addWidget(paper_mark(kit.q))
        top.addWidget(kit.single_line(name, 'providerTitle'), 1)
        self.pill = kit.label('Loading', 'pill', wrap=False)
        top.addWidget(self.pill)
        box.addLayout(top)
        self.fields = w.QGridLayout()
        self.fields.setHorizontalSpacing(10)
        self.fields.setVerticalSpacing(8)
        self.fields.setColumnStretch(1, 1)
        box.addLayout(self.fields)
        self.cells, self.hints, self.rules = [], [], []
        self._fields(dashboard_model.card_summary()['rows'])
        self.message = kit.single_line('')
        reserved(self.message)
        box.addWidget(self.message)
        self.selection = kit.button('Download: All types', lambda: pages.open_download_choices(key), 'downloadChoice')
        self.selection.setAccessibleName(f'Download choices for {name}')
        box.addWidget(self.selection)
        # One button for what can be done now: Sign in, Start, Continue or Pause.
        self.doing = 'signin'
        acts = {'signin': pages.signin, 'go': pages.go, 'pause': pages.pause}
        self.action = kit.button('Sign in', lambda: acts[self.doing](key), 'go')
        self.action.setMinimumWidth(self.action.fontMetrics().horizontalAdvance('Signing in…') + 48)
        self.signout = kit.button('Sign out', lambda: pages.sign_out(key), 'destructive')
        self.signout.setAccessibleName(f'Sign out of {name}')
        reserved(self.signout)
        self.signout.hide()
        self.allow = kit.button('Allow connection', lambda: pages.allow_network(key), 'secondary')
        self.allow.setAccessibleName('Allow this connection')
        # Connection permission precedes Start/Continue. Share their slot so a
        # hidden permission control does not leave an empty button row.
        self.primary = w.QWidget()
        self.primary_layout = w.QStackedLayout(self.primary)
        self.primary_layout.setContentsMargins(0, 0, 0, 0)
        self.primary_layout.addWidget(self.action)
        self.primary_layout.addWidget(self.allow)
        actions = kit.row(self.primary, self.signout, spacing=8, stretch=False)
        actions.layout().setStretchFactor(self.primary, 1)
        actions.layout().setStretchFactor(self.signout, 1)
        box.addWidget(actions)
        box.addStretch(1)

    def _show_primary(self, primary):
        previous = self.primary_layout.currentWidget()
        if previous is not primary and previous.hasFocus():
            previous.clearFocus()
        for button in (self.action, self.allow):
            button.setSizePolicy(self.kit.w.QSizePolicy.Policy.Preferred if button is primary
                                 else self.kit.w.QSizePolicy.Policy.Ignored, self.kit.w.QSizePolicy.Policy.Fixed)
        self.primary_layout.setCurrentWidget(primary)

    def _appearance(self, appearance):
        if self.widget.property('appearance') == appearance:
            return
        self.widget.setProperty('appearance', appearance)
        # The palette belongs to the state, so descendants must be repolished when that state changes.
        for widget in (self.widget, *self.widget.findChildren(self.kit.w.QWidget)):
            widget.style().unpolish(widget)
            widget.style().polish(widget)
            widget.update()

    def _fields(self, rows):
        paper = tuple(row['name'] for row in rows) == dashboard_model.DASHBOARD_FIELDS
        if [name.property('fieldKey') for name, _ in self.cells] != [row['name'] for row in rows]:
            for name, value in self.cells:
                discard(self.fields, name, value.parentWidget())
            for rule in self.rules:
                discard(self.fields, rule)
            self.cells, self.hints, self.rules = [], [], []
            for column in range(4):
                self.fields.setColumnStretch(column, 1 if column in ((0, 1) if paper else (1,)) else 0)
            parts = {}
            for index, row in enumerate(rows):
                key = row['name']
                captions = {'Saved': 'Episodes saved', 'Time left': 'Time left',
                            'Space': 'Space needed', 'Download speed': 'Transfer rate', 'Status': '●'}
                if paper and key in ('Time left', 'Space', 'Status'):
                    from .window_art import symbol_label
                    name = symbol_label(self.kit.q, '' if key == 'Status' else captions[key],
                                        {'Time left': 'clock', 'Space': 'storage', 'Status': 'info'}[key])
                    name.setObjectName('fieldName')
                else:
                    name = self.kit.label(captions.get(key, key) if paper else key, 'fieldName', wrap=False)
                name.setProperty('fieldKey', key)
                name.setAccessibleName(key)
                if paper and key in ('Time left', 'Space'):
                    name.setObjectName('estimateLabel')
                elif paper and key == 'Saved':
                    name.setObjectName('episodeCaption')
                elif paper and key == 'Status':
                    name.setObjectName('statusSymbol')
                if row['name'] == 'Speed':
                    self.speed = value = self.kit.combo([(key, SPEED_NAMES[key]) for key in SPEEDS],
                                                       lambda speed: self.pages.speed(speed, self.key), wheel_changes=False)
                    value.setAccessibleName(f'Speed on {self.name}')
                    value.setObjectName('dashboardSpeed')
                    value.setPlaceholderText(dashboard_model.NONE)
                    value.setCurrentIndex(-1)
                    value.setEnabled(False)
                    value.setMinimumWidth(0)
                else:
                    role = {'Saved': 'episodeTotal', 'Time left': 'estimateValue', 'Space': 'estimateValue',
                            'Download speed': 'transferValue', 'Status': 'cardStatus'}.get(key, 'fieldValue')
                    value = self.kit.single_line('', role if paper else 'fieldValue')
                hint = self.kit.hint()
                if not paper or key in ('Time left', 'Space'):
                    reserved(hint)
                holder = self.kit.row(value, hint, spacing=6)
                parts[key] = (name, holder)
                if paper:
                    if key == 'Status':
                        name.setFixedWidth(22)
                        holder.layout().insertWidget(0, name)
                        self.fields.addWidget(holder, 7, 0, 1, 2)
                    elif key not in ('Speed', 'Download speed'):
                        positions = {'Saved': ((1, 0, 1, 2), (0, 0, 1, 2)),
                                     'Time left': ((2, 0, 1, 1), (3, 0, 1, 1)),
                                     'Space': ((2, 1, 1, 1), (3, 1, 1, 1))}
                        caption_at, value_at = positions[key]
                        self.fields.addWidget(name, *caption_at)
                        self.fields.addWidget(holder, *value_at)
                else:
                    self.fields.addWidget(name, index, 0, self.kit.q.QtCore.Qt.AlignmentFlag.AlignTop)
                    self.fields.addWidget(holder, index, 1)
                placed(name, holder)
                if paper and key == 'Speed':
                    name.hide()  # The compact dropdown names its setting accessibly; it needs no extra caption.
                self.cells.append((name, value))
                self.hints.append(hint)
            if paper:
                rate_name, rate_value = parts['Download speed']
                speed_name, speed_value = parts['Speed']
                transfer = self.kit.row(rate_name, rate_value, speed_name, speed_value, spacing=8, stretch=False)
                transfer.layout().setStretchFactor(rate_value, 1)
                self.fields.addWidget(transfer, 5, 0, 1, 2)
                self.rules.append(transfer)
                for index in (4, 6):
                    rule = self.kit.w.QFrame()
                    rule.setObjectName('cardRule')
                    self.fields.addWidget(rule, index, 0, 1, 2)
                    self.rules.append(rule)
        for (name, value), hint, row in zip(self.cells, self.hints, rows, strict=True):
            if row['name'] != 'Speed':
                text = row['value']
                if paper and row['name'] == 'Saved':
                    text = text.removesuffix(' episodes').removesuffix(' episode')
                value.setText(text)
            if paper and row['name'] == 'Status':
                appearance = self.widget.property('appearance')
                name.setSymbol({'active': 'spinner', 'complete': 'check', 'paused': 'pause',
                                'waiting': 'clock', 'attention': 'alert'}.get(appearance, 'info'))
                tone(name, 'tone', 'warning' if row['warning'] else '')
            tone(value, 'tone', 'warning' if row['warning'] else '')
            tip = row.get('tip', '')
            hint.setToolTip(tip)
            hint.setAccessibleDescription(tip)
            set_visible(hint, bool(tip))
            # A hint follows the value; spare width goes after it. The row keeps
            # its height, and long values can still shrink and elide on one line.
            policy = self.kit.w.QSizePolicy.Policy
            value.setSizePolicy(policy.Fixed if row['name'] == 'Speed' else policy.Preferred if tip else policy.Ignored,
                                policy.Fixed)
            layout = value.parentWidget().layout()
            layout.setStretchFactor(value, 0 if tip else 1)
            layout.setStretch(layout.count() - 1, 1 if tip else 0)

    def show(self, s, v):
        from .download_selection import HELP, LABELS, TYPES
        from .window_art import icon
        from .window_style import card_colours
        chosen = (s.account or {}).get('download_types', dict.fromkeys(TYPES, True)) if s is not None else dict.fromkeys(TYPES, True)
        available = isinstance(chosen, dict)
        if not available:
            chosen = dict.fromkeys(TYPES, False)
        count = sum(bool(chosen.get(key)) for key in TYPES)
        suffix = 'All types' if count == len(TYPES) else 'Paid series' if chosen.get('bought') and count == 1 else LABELS[next(key for key in TYPES if chosen.get(key))] if count == 1 else f'{count} types'
        self.selection.setText('Download: ' + suffix)
        self.selection.setToolTip('\n'.join(f'{LABELS[key]}: {HELP[key]}' for key in TYPES if chosen.get(key)))
        if not available:
            self.selection.setText('Download: Unavailable')
            self.selection.setToolTip('Saved choices could not be read. Choose the desired episode types again.')
        set_enabled(self.selection, s is not None and not s.busy)
        """v is the site's view, or None for a site not opened yet: then its fields are empty and its button signs in."""
        if v is None:
            self._appearance('neutral')
            self.pill.setText('Not signed in')
            tone(self.pill, 'tone', '')  # The plain look.
            tone(self.widget, 'tone', '')
            self._fields(dashboard_model.card_summary()['rows'])
            self.speed.setCurrentIndex(-1)
            set_enabled(self.speed, False)
            self.speed.setToolTip(f'Sign in to {self.name} to choose a speed.')
            say(self.message, '')
            self.doing = 'signin'
            set_enabled(self.action, True)
            self.action.setText('Sign in')
            self._show_primary(self.action)
            set_visible(self.signout, False)
            return
        summary = dashboard_model.card_summary(s, v)
        self._appearance(summary['appearance'])
        self.pill.setText({'Needs attention': 'Attention', 'Sign-in needed': 'Sign in',
                          'Status unavailable': 'Offline'}.get(summary['state'], summary['state']))
        tone(self.pill, 'tone', summary['tone'])
        tone(self.widget, 'tone', 'warning' if summary['tone'] == 'warning' else 'status')
        self._fields(summary['rows'])
        if v['speed_visible'] and s.account is not None:
            choose(self.speed, v['speed'])
        else:
            self.speed.setCurrentIndex(-1)
        speed_enabled = v['speed_visible'] and v['speed_enabled']
        set_enabled(self.speed, speed_enabled)
        speed_tip = v['speed_help'] if v['speed_visible'] else v['signin_note']
        speed_row = next(row for row in v['rows'] if row['name'] == 'Speed')
        if speed_row['warning']:
            speed_tip += ' ' + speed_row['value']
        self.speed.setToolTip(speed_tip)
        say(self.message, v['signin_status'], v['signin_status_error'])
        self.doing = v['action']
        set_enabled(self.action, v['action_enabled'])
        self.action.setText(v['action_text'])
        colours = card_colours(summary['appearance'], bool(self.widget.window().property('comicDark')))
        self.action.setIcon(icon(self.kit.q, 'pause' if self.doing == 'pause' else 'play' if self.doing == 'go' else 'info',
                                 colours['button-text']))
        self.selection.setIcon(icon(self.kit.q, 'filter', colours['text'], 16))
        set_visible(self.signout, v['signout_visible'])
        set_enabled(self.signout, v['signout_enabled'])
        self.signout.setToolTip(v['signout_help'])
        primary = self.allow if v['allow_visible'] and self.doing == 'go' else self.action
        self._show_primary(primary)
        set_enabled(self.allow, v['allow_enabled'])


class Dashboard:
    """Every site at a glance, as cards: the saved library over all of them first, across the page, then one card
    for each site, two to a row when the window is wide enough and one when it is not."""

    def __init__(self, kit, pages):
        w, self.kit, self.pages = kit.w, kit, pages
        content = w.QWidget()
        column = w.QVBoxLayout(content)
        column.setContentsMargins(32, 24, 32, 24)
        column.setSpacing(16)
        self.updated = kit.single_line('Checking current status…', 'muted')
        self.updated.setParent(content)
        self.updated.hide()
        self.go_all = kit.button('Start all', pages.go_all, 'secondary')
        self.pause_all = kit.button('Pause all', pages.pause_all, 'secondary')
        self.go_all.setObjectName('toolbarAction')
        self.pause_all.setObjectName('toolbarAction')
        reserved(self.pause_all)
        self.grid_holder = w.QWidget()
        self.grid = w.QGridLayout(self.grid_holder)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(16)
        self.columns = 0
        self.library_card = self._library(kit, pages)
        self.cards = {}
        column.addWidget(self.grid_holder)
        from .window_art import symbol_label
        estimate_note = symbol_label(kit.q, 'Estimates adjust as episodes are discovered.', 'info')
        estimate_note.setObjectName('muted')
        column.addWidget(estimate_note)
        column.addWidget(kit.label(FOOTER, 'muted'))
        column.addStretch(1)
        self.widget = kit.scroll(content)
        self._lay_out(1)
        # The page's own width, not the cards': two cards a row can be wider than a narrowed window.
        self.margins = column.contentsMargins().left() + column.contentsMargins().right()
        watch_width(kit, self.widget.viewport(), self._resized)

    def _library(self, kit, pages):
        w = kit.w
        card, box = kit.frame('libraryOverview', margins=(0, 0, 0, 0), spacing=8)
        card.setAccessibleName('All sites library')
        self.library_title = kit.label('All sites', 'sectionTitle')
        self.library_title.setParent(card)
        self.library_title.hide()
        self.counting = kit.label('⌛ Counting episodes…', 'muted', wrap=False)
        self.counting.setAccessibleName('Counting saved episodes')
        self.counting.setToolTip('Reading saved-file counts. Previous totals stay visible until the count finishes.')
        reserved(self.counting)
        self.counting.hide()
        self.metrics = w.QGridLayout()
        self.metrics.setContentsMargins(0, 0, 0, 0)
        self.metrics.setSpacing(0)
        metric_holder = w.QWidget()
        metric_holder.setLayout(self.metrics)
        self.metric_cells = []
        self.files = kit.button('Open library folder', pages.open_folder, 'secondary')
        self.files.setObjectName('toolbarAction')
        library_top = kit.row(metric_holder, self.files, spacing=12)
        library_top.layout().addWidget(self.go_all)
        library_top.layout().addWidget(self.pause_all)
        box.addWidget(library_top)
        self.save = kit.single_line('')
        self.save.setSizePolicy(w.QSizePolicy.Policy.Preferred, w.QSizePolicy.Policy.Fixed)
        reserved(self.save)
        self.change_folder = w.QToolButton()
        self.change_folder.setObjectName('editFolder')
        self.change_folder.setText('✎')
        self.change_folder.setAccessibleName('Change library folder for all sites')
        self.change_folder.setCursor(kit.q.QtCore.Qt.CursorShape.PointingHandCursor)
        self.change_folder.clicked.connect(pages.change_folder)
        location = kit.row(self.save, self.change_folder, spacing=6)
        location.layout().addWidget(self.counting)
        box.addWidget(location)
        self.destination = kit.single_line('', 'muted')
        box.addWidget(self.destination)
        # Keep the existing aggregate data available for descriptions without turning the overview into a table.
        self.places_holder = w.QWidget(card)
        self.places_holder.hide()
        self.places = w.QVBoxLayout(self.places_holder)
        self.place_labels = []
        # Said only when the counts could not be read.
        self.library_summary = kit.single_line('')
        reserved(self.library_summary)
        self.library_summary.setVisible(False)
        box.addWidget(self.library_summary)
        return card

    def _resized(self, width):
        available = width - self.margins
        self._lay_out(3 if available >= CARD_THREE_COLUMNS_FROM else 2 if available >= CARD_COLUMNS_FROM else 1)

    def _lay_out(self, columns, force=False):
        """Put the cards back in the grid, in order, so many to a row; the All sites card has a row to itself."""
        if columns == self.columns and not force:
            return
        self.columns = columns
        cards = [card.widget for card in self.cards.values()]
        for card in (self.library_card, *cards):
            self.grid.removeWidget(card)
        self.grid.addWidget(self.library_card, 0, 0, 1, columns)
        for index, card in enumerate(cards):
            self.grid.addWidget(card, 1 + index // columns, index % columns)
        for index in range(3):
            self.grid.setColumnStretch(index, 1 if index < columns else 0)

    def _sites(self, sites):
        """One card for each site that works, in the sites' order."""
        keys = [item['key'] for item in sites if item.get('available')]
        if list(self.cards) == keys:
            return
        for card in self.cards.values():
            discard(self.grid, card.widget)
        names = {item['key']: item['name'] for item in sites}
        self.cards = {key: SiteCard(self.kit, self.pages, key, names[key]) for key in keys}
        self._lay_out(self.columns, force=True)
        placed(*(card.widget for card in self.cards.values()))

    def _metrics(self, metrics):
        if [cell[0] for cell in self.metric_cells] == [name for _, name in metrics]:
            for (_, number), (value, _) in zip(self.metric_cells, metrics, strict=True):
                number.setText(value)
            if self.metric_cells:
                self.metric_cells[0][1].parentWidget().setToolTip(' | '.join(f'{value} {name}' for value, name in metrics))
            return
        for _, number in self.metric_cells:
            discard(self.metrics, number.parentWidget())
        self.metric_cells = []
        for index, (value, name) in enumerate(metrics):
            cell, box = self.kit.frame('metric', vertical=False, margins=(0, 0, 0, 0), spacing=8)
            cell.setParent(self.library_card)
            number = self.kit.single_line(value, 'metricNumber')
            number.setSizePolicy(self.kit.w.QSizePolicy.Policy.Preferred, self.kit.w.QSizePolicy.Policy.Fixed)
            caption = self.kit.label('saved episodes' if index == 0 else name, 'muted', wrap=False)
            for label in (number, caption):
                box.addWidget(label)
            if index == 0:
                self.metrics.addWidget(cell, 0, 0)
                cell.setToolTip(' | '.join(f'{value} {name}' for value, name in metrics))
            cell.setVisible(index == 0)
            self.metric_cells.append((name, number))

    def _places(self, places):
        if [b.property('place') for b in self.place_labels] == [p['id'] for p in places]:
            for label, place in zip(self.place_labels, places, strict=True):
                label.setText(f"{place['label']} | {place['counts']}")
            return
        for label in self.place_labels:
            discard(self.places, label)
        self.place_labels = []
        for place in places:
            label = self.kit.label(f"{place['label']} | {place['counts']}", 'place', wrap=False)
            label.setProperty('place', place['id'])
            self.places.addWidget(label)
            label.hide()
            self.place_labels.append(label)

    def show(self, s, sites, views, everything):
        """s is the chosen site's state; views each site's state and view by key, for the sites open; everything the
        dashboard's own: its updated line and its two buttons."""
        from .window_art import icon
        from .window_style import colours
        ink = colours(bool(self.widget.window().property('comicDark')))['text']
        for button, symbol in ((self.files, 'folder'), (self.go_all, 'play'), (self.pause_all, 'pause')):
            button.setIcon(icon(self.kit.q, symbol, ink))
        self.updated.setText(everything['updated'])
        set_enabled(self.go_all, everything['go_all'])
        set_visible(self.pause_all, everything['pause_all'])
        self._sites(sites)
        for key, card in self.cards.items():
            card.show(*views.get(key, (None, None)))
        place = dashboard_model.save_place(s)
        root = s.library_root or ''
        self.save.setText(f'Library folder: {root}' if root else '')
        self.save.setVisible(bool(root))
        set_enabled(self.change_folder, bool(root) and everything['folder_enabled'])
        self.change_folder.setToolTip(everything['folder_tooltip'] or 'Change library folder for all sites')
        self.destination.setText(f'Saving to {place}' if place and place != root else '')
        self.destination.setVisible(bool(place and place != root))
        set_enabled(self.files, not s.busy)
        self.counting.setVisible(bool(self.pages.counting))
        summary = storage_model.combined([state.summary for state, _ in views.values()])
        say(self.library_summary, storage_model.LIBRARY_UNAVAILABLE if summary is False else '')
        if summary is False:
            return
        overview = storage_model.library_overview(summary, s.cloud_offered)
        self._metrics(overview['metrics'])
        self._places(overview['locations'])


class ProviderSettingsCard:
    """One provider's download exceptions and storage controls, bound to that provider."""

    def __init__(self, kit, pages, key, name):
        self.kit, self.pages, self.key = kit, pages, key
        w = kit.w
        self.widget, box = kit.frame('card', margins=(24, 24, 24, 24), spacing=12)
        self.widget.setAccessibleName(f'{name} downloads and storage')
        self.pill = kit.label('Not signed in', 'pill', wrap=False)
        box.addWidget(kit.row(kit.label(name, 'sectionTitle'), self.pill))
        self.device_stored = kit.single_line('—')
        self.device_stored.setSizePolicy(w.QSizePolicy.Policy.Preferred, w.QSizePolicy.Policy.Fixed)
        box.addWidget(kit.row(kit.label('This device', 'fieldName', wrap=False), self.device_stored))
        self.cloud_row = w.QWidget()
        cloud = w.QHBoxLayout(self.cloud_row)
        cloud.setContentsMargins(0, 0, 0, 0)
        self.b2_stored = kit.single_line('—')
        self.b2_pill = kit.label('Not set up', 'pill', wrap=False)
        cloud.addWidget(kit.label('Backblaze B2', 'fieldName', wrap=False))
        cloud.addWidget(self.b2_stored, 1)
        cloud.addWidget(self.b2_pill)
        box.addWidget(self.cloud_row)
        self.choice = w.QWidget()
        choice = w.QVBoxLayout(self.choice)
        choice.setContentsMargins(0, 0, 0, 0)
        choice.addWidget(kit.label('Save new episodes to', 'strong'))
        self.options_box = w.QHBoxLayout()
        self.options_box.setSpacing(24)
        self.options_box.addStretch(1)
        choice.addLayout(self.options_box)
        self.option_group = w.QButtonGroup(self.choice)
        self.option_group.setExclusive(True)
        self.option_group.idClicked.connect(lambda index: pages.choose_storage(self.options[index]['key'], key))
        self.options, self.option_buttons = [], []
        self.choice_lock, self.choice_help = kit.label('', 'muted'), kit.label('', 'muted')
        for part in (self.choice_lock, self.choice_help):
            choice.addWidget(part)
        box.addWidget(self.choice)
        self.choice_status = kit.label('')
        box.addWidget(self.choice_status)
        self.episode_types = {}
        types = w.QWidget()
        types.setAccessibleName(f'{name} episode types')
        types.setToolTip('Saved episodes and episodes waiting to download. Types reflect the provider’s last report.')
        grid = w.QGridLayout(types)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setColumnStretch(0, 1)
        for column, text in enumerate(('Episode type', 'Saved', 'Pending')):
            grid.addWidget(kit.label(text, 'strong', wrap=False), 0, column)
        for index, item in enumerate(dashboard_model.episode_types({}), 1):
            grid.addWidget(kit.label(item['label'], 'fieldName', wrap=False), index, 0)
            cells = []
            for column in (1, 2):
                cell = kit.label('—', wrap=False)
                cell.setAlignment(kit.q.QtCore.Qt.AlignmentFlag.AlignRight | kit.q.QtCore.Qt.AlignmentFlag.AlignVCenter)
                grid.addWidget(cell, index, column)
                cells.append(cell)
            self.episode_types[item['key']] = cells
        box.addWidget(types)
        self.not_downloaded = w.QTableWidget(0, 2)
        self.not_downloaded.setHorizontalHeaderLabels(['Issue', 'Episodes'])
        self.not_downloaded.horizontalHeaderItem(0).setTextAlignment(kit.q.QtCore.Qt.AlignmentFlag.AlignLeft
                                                                   | kit.q.QtCore.Qt.AlignmentFlag.AlignVCenter)
        self.not_downloaded.horizontalHeaderItem(1).setTextAlignment(kit.q.QtCore.Qt.AlignmentFlag.AlignRight
                                                                   | kit.q.QtCore.Qt.AlignmentFlag.AlignVCenter)
        self.not_downloaded.setEditTriggers(w.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.not_downloaded.setSelectionMode(w.QAbstractItemView.SelectionMode.NoSelection)
        self.not_downloaded.setWordWrap(False)
        self.not_downloaded.verticalHeader().setVisible(False)
        self.not_downloaded.setShowGrid(False)
        self.not_downloaded.setAlternatingRowColors(True)
        self.not_downloaded.setVerticalScrollBarPolicy(kit.q.QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.not_downloaded.setHorizontalScrollBarPolicy(kit.q.QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.not_downloaded.horizontalHeader().setSectionResizeMode(0, w.QHeaderView.ResizeMode.Stretch)
        self.not_downloaded.horizontalHeader().setSectionResizeMode(1, w.QHeaderView.ResizeMode.ResizeToContents)
        self.not_downloaded.setAccessibleName(f'{name} download issues')
        self.issues_status = kit.label('Not checked', 'muted', wrap=False)
        box.addWidget(kit.label('Download issues', 'strong'))
        box.addWidget(self.issues_status)
        box.addWidget(self.not_downloaded)
        self.b2_setup = kit.button('Cloud settings', lambda: pages.provider_cloud(key), 'secondary')
        box.addWidget(kit.row(self.b2_setup))

    def show(self, s, v):
        if s is None:
            self.pill.setText('Not signed in')
            tone(self.pill, 'tone', '')
            self.device_stored.setText('—')
            self.choice.hide()
            self.cloud_row.hide()
            self.b2_setup.hide()
            self.choice_status.hide()
            self.not_downloaded.hide()
            self.issues_status.setText('Not checked')
            self.issues_status.show()
            for cells in self.episode_types.values():
                for cell in cells:
                    cell.setText('—')
            return
        self.pill.setText(v['state'])
        tone(self.pill, 'tone', v['tone'])
        place = storage_model.storage(s)
        self.device_stored.setText(place['device_stored'] if s.summary is not False else 'Counts unavailable')
        self.cloud_row.setVisible(s.cloud_offered)
        self.b2_stored.setText(place['b2_stored'] or '—')
        self.b2_pill.setText(place['b2_pill'])
        tone(self.b2_pill, 'tone', place['b2_tone'])
        self.b2_setup.setVisible(s.cloud_offered)
        self.b2_setup.setEnabled(v['site_enabled'])
        choice = storage_model.storage_choice(s)
        self.choice.setVisible(choice['visible'])
        self._storage_options(choice['options'])
        say(self.choice_lock, choice['lock'])
        self.choice_help.setText(choice['help'])
        message = s.choice_message or v['settings_status']
        say(self.choice_status, message, bool(s.choice_message) or v['settings_status_error'])
        for item in v['episode_types']:
            saved, waiting = self.episode_types[item['key']]
            saved.setText(item['saved'])
            waiting.setText(item['waiting'])
        rows = v['not_downloaded']
        self._not_downloaded(rows)
        self.not_downloaded.setVisible(bool(rows))
        self.issues_status.setVisible(not rows)
        self.issues_status.setText('No recorded issues' if s.status is not None else 'Not checked')

    def _not_downloaded(self, rows):
        table, w = self.not_downloaded, self.kit.w
        table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            table.setItem(index, 0, w.QTableWidgetItem(row['reason']))
            count = w.QTableWidgetItem(row['count'])
            count.setTextAlignment(self.kit.q.QtCore.Qt.AlignmentFlag.AlignRight | self.kit.q.QtCore.Qt.AlignmentFlag.AlignVCenter)
            table.setItem(index, 1, count)
        table.resizeRowsToContents()
        # The whole table shows at once: it is short, and sits in a page that scrolls.
        table.setFixedHeight(table.horizontalHeader().height() + sum(table.rowHeight(i) for i in range(len(rows)))
                             + 2 * table.frameWidth())

    def _storage_options(self, options):
        if [(o['key'], o['label']) for o in self.options] != [(o['key'], o['label']) for o in options]:
            for button in self.option_buttons:
                self.option_group.removeButton(button)
                discard(self.options_box, button)
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



class Settings:
    """Provider download/storage cards, cloud setup and About."""

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
        side.setFixedWidth(224)
        side.setLayout(tabs)
        layout.addWidget(side, 0, kit.q.QtCore.Qt.AlignmentFlag.AlignTop)
        panels = w.QVBoxLayout()
        panels.setSpacing(12)
        self.status = kit.label('')
        panels.addWidget(self.status)
        # Only the open tab takes room, so a short one is not as tall as the longest.
        self.panels = {'downloads': self._downloads(kit, pages), 'b2': self._b2(kit, pages),
                       'about': self._about(kit, pages)}
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

    def _downloads(self, kit, pages):
        self.providers = kit.w.QWidget()
        self.provider_box = kit.w.QVBoxLayout(self.providers)
        self.provider_box.setContentsMargins(0, 0, 0, 0)
        self.provider_box.setSpacing(16)
        self.provider_cards = {}
        return self.providers

    def _providers(self, sites, views):
        available = [item for item in sites if item.get('available')]
        if list(self.provider_cards) != [item['key'] for item in available]:
            for card in self.provider_cards.values():
                discard(self.provider_box, card.widget)
            self.provider_cards = {item['key']: ProviderSettingsCard(self.kit, self.pages, item['key'], item['name'])
                                   for item in available}
            for card in self.provider_cards.values():
                self.provider_box.addWidget(card.widget)
            placed(*(card.widget for card in self.provider_cards.values()))
        for key, card in self.provider_cards.items():
            card.show(*views.get(key, (None, None)))

    def _about(self, kit, pages):
        card, box = kit.frame('card', margins=(24, 24, 24, 24), spacing=12)
        box.addWidget(kit.label('About', 'sectionTitle'))
        box.addWidget(kit.label(f'Comic Archiver {license_texts.version()}', 'strong'))
        box.addWidget(kit.label(ABOUT, 'muted'))
        box.addWidget(kit.label(SUPPORT, 'muted'))
        box.addWidget(kit.label(SUPPORT_LOG, 'muted'))
        self.support = kit.button('Copy details for support', pages.copy_support_details, 'secondary')
        self.save_log = kit.button('Save log for support', pages.save_support_log, 'secondary')
        self.support_note = kit.label('', 'muted', wrap=False)
        self.support_note.setVisible(False)
        box.addWidget(kit.row(self.support, self.save_log, self.support_note))
        self.license_choice = kit.combo([(path, name) for name, path in license_texts.texts()], self._license)
        self.license_choice.setAccessibleName('License to read')
        box.addWidget(kit.row(self.license_choice))
        self.license_text = kit.w.QPlainTextEdit()
        self.license_text.setReadOnly(True)
        self.license_text.setMinimumHeight(420)
        self.license_text.setAccessibleName('License text')
        box.addWidget(self.license_text)
        self._license(self.license_choice.itemData(0))
        return card

    def _license(self, path):
        text = license_texts.read(path)
        self.license_text.setPlainText('This text could not be read.' if text is None else text)

    def _b2(self, kit, pages):
        w = kit.w
        card, box = kit.frame('card', margins=(24, 24, 24, 24), spacing=12)
        box.addWidget(kit.label('Save to your Backblaze B2 account', 'sectionTitle'))
        self.site = kit.combo([], pages.choose_site)
        self.site.setAccessibleName('Site')
        self.site_row = kit.row(kit.label('Site', 'strong', wrap=False), self.site)
        box.addWidget(self.site_row)
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
        box.addWidget(kit.row(kit.button('Go to Downloads && storage', lambda: pages.go_to('settings/downloads'), 'secondary')))
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

    def _cleanups(self, cleanup):
        if [c['test_id'] for c in cleanup] != [b.property('test') for b in self.cleanups]:
            discard(self.cleanup_box, *self.cleanups)
            self.cleanups = []
            for record in cleanup:
                button = self.kit.button(record['text'], lambda test=record['test_id']: self.pages.cloud_cleanup(test), 'secondary')
                button.setProperty('test', record['test_id'])
                self.cleanup_box.addWidget(button)
                self.cleanups.append(button)
        for button, record in zip(self.cleanups, cleanup, strict=True):
            button.setEnabled(record['enabled'])

    def show(self, s, v, tab, sites, views):
        for key, button in self.tabs.items():
            button.setVisible(key != 'b2' or s.cloud_offered)
        self.tabs[tab].setChecked(True)
        for key, panel in self.panels.items():
            panel.setVisible(key == tab)
        say(self.status, v['settings_status'] if tab != 'downloads' else '', v['settings_status_error'])
        self._providers(sites, views)
        if [self.site.itemData(i) for i in range(self.site.count())] != [item['key'] for item in sites]:
            blocked = self.site.blockSignals(True)
            self.site.clear()
            for item in sites:
                self.site.addItem(item['name'] if item.get('available') else item['name'] + ' — coming later', item['key'])
                self.site.model().item(self.site.count() - 1).setEnabled(bool(item.get('available')))
            self.site.blockSignals(blocked)
        choose(self.site, self.pages.chosen)
        self.site_row.setVisible(bool(sites))
        self.site.setEnabled(v['site_enabled'])
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


class Pages:
    """The window's pages and every open site's state, refreshed while its download is active.

    shell gives the pages what lies outside them: run(work, done) puts work on a thread and done(value, error)
    back on the window's; the sites open and the one chosen, whose settings and sign-in the window shows
    (open_sites(), chosen_site()); and the window's own steps for sites, folders, the theme and links.
    actions_for(key) is one site's calls to its library.
    """

    def __init__(self, q, shell, actions_for, now=time.time):
        self.q, self.shell, self.actions_for, self.now = q, shell, actions_for, now
        self.states = {}
        self.cloud_offered = True
        self.route = 'dashboard'
        self.counting = {}  # Saved-file counts must never hold up downloader progress.
        self.polling = {}  # Each site's state with a refresh on its way.
        self.acts = {}  # How many changes each site has finished: a reading asked for before one is older than it.
        self.again = set()  # The sites to read once more when the reading under way is back.
        self.drawn_from = None  # What the last drawing was made from.
        self.cloud_asked = set()  # The sites asked about a connection their library folder brought.
        self.polled_task = None
        kit = self.kit = Kit(q, self.link)
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
        self.library = EpisodeLibrary(kit, self)
        for page in (self.dashboard, self.library, self.settings):
            self.stack.addWidget(page.widget)
        column.addWidget(self.stack, 1)

    # The sites.

    @property
    def chosen(self):
        return self.shell.chosen_site()

    @property
    def state(self):
        """The chosen site's state: the one Settings shows."""
        return self.of(self.chosen)

    @property
    def actions(self):
        return self.actions_for(self.chosen)

    def of(self, key):
        """A site's state, made empty the first time it is asked for."""
        if key not in self.states:
            self.states[key] = State(cloud_offered=self.cloud_offered)
        return self.states[key]

    def open_keys(self):
        return list(self.shell.open_sites())

    def shown(self):
        """The sites with a state to show: those open, and one being opened."""
        open_keys = self.open_keys()
        return [key for key, s in self.states.items() if key in open_keys or s.busy]

    def opening(self, key, text):
        """A site is being opened: its card says so until it is open. The site's state, for the shell to settle."""
        s = self.of(key)
        s.busy, s.message = True, dashboard_model.Message(text)
        self.render()
        return s

    # Moving between pages.

    def go_to(self, name):
        route = route_of(name, self.cloud_offered)
        if self.route == 'settings/b2' and route != 'settings/b2':
            self.settings.forget_keys()
        opened = route.partition('/')[0] != self.route.partition('/')[0]
        self.route = route
        page = self.dashboard if route == 'dashboard' else self.library if route == 'library' else self.settings
        self.stack.setCurrentWidget(page.widget)
        if opened and hasattr(page.widget, 'verticalScrollBar'):
            page.widget.verticalScrollBar().setValue(0)
        if route == 'library':
            self.library.show()
        self.render()

    def link(self, target):
        """A link in a page: another page of the window, or one of Backblaze's pages in the computer's browser."""
        if target in B2_PAGES:
            self.shell.open_url(B2_PAGES[target])
        else:
            self.go_to(target)

    # Reading the library.

    def start(self, then=None):
        """The library is open: read each open site's context, then refresh it. then(ok) follows the chosen site's
        first refresh."""
        chosen = self.chosen
        for key in self.open_keys():
            self.read_context(key, then if key == chosen else None)

    def read_context(self, key, then=None):
        s = self.of(key)

        def done(value, error):
            if self.states.get(key) is not s:
                return  # Another folder opened meanwhile; its own reading follows.
            if error is None:
                self.received_context(s, value)
            self.poll_site(key, then)
        self.run(self.actions_for(key).context, done)

    def received_context(self, s, value):
        s.sites = value['sites']
        s.library_folder, s.cloud_offered = value['library_folder'], value['cloud_offered']
        s.library_root = value.get('library_root')
        s.workspace_id = value['workspace_id']
        self.cloud_offered = value['cloud_offered']
        self.render()

    def reopened(self, then=None):
        """A library folder is open, at the start or after another folder was chosen: read every site, afresh."""
        self.states = {}
        self.counting = {}
        self.polling = {}
        self.acts = {}
        self.again = set()
        self.cloud_asked = set()
        self.settings.forget_keys()
        self.library.reset()
        self.start(then)
        self.render()

    def site_chosen(self):
        """Another site was chosen in Settings, or opened: Settings shows it, and a site opened just now is read."""
        self.settings.forget_keys()
        if not self.state.loaded:
            self.read_context(self.chosen)
        self.render()

    def drawn(self):
        """Whether the dashboard shows the chosen site's library, read."""
        card = self.dashboard.cards.get(self.chosen)
        return (self.state.loaded and card is not None
                and tuple(name.property('fieldKey') for name, _ in card.cells) == dashboard_model.DASHBOARD_FIELDS)

    def poll(self, then=None, *, automatic=False):
        """Read active sites on the timer; explicit reads may refresh an idle site."""
        chosen = self.chosen
        for key in self.open_keys():
            s = self.of(key)
            if (automatic and s.loaded and not dashboard_model.updating(s)
                    and dashboard_model.worker_state(s, self.now()) != 'starting'):
                continue  # A downloader still starting is read on the timer too, until it says it is there.
            self.poll_site(key, then if key == chosen else None, timed=automatic)

    def poll_site(self, key, then=None, *, timed=False):
        """Read one site. Asked for because something happened (not timed) while a reading is under way, one more
        follows that reading, and only one: the timer never adds a read of its own."""
        s = self.of(key)
        if s.busy:
            return
        if key in self.polling:
            if not timed:
                self.again.add(key)
            return
        self.polling[key] = s
        now = self.now()
        network, library, recount = app_actions.due(s, now)
        task, acts = s.task, self.acts.get(key, 0)

        def done(value, error):
            if self.polling.get(key) is s:
                del self.polling[key]
            if self.states.get(key) is not s:
                return  # Another folder opened meanwhile; its own refresh follows.
            again = key in self.again
            self.again.discard(key)
            if again or self.acts.get(key, 0) != acts:
                # A change finished since this was asked for, and what it left is newer: read again, show nothing old.
                self.poll_site(key, then)
                return
            if error is not None:
                s.refresh_failed()
            elif app_actions.received(s, value, self.now(), task) and key == self.chosen:
                self.settings.forget_keys()
            self.render()
            if error is None:
                self.confirm_cloud(key)
                if s.library_refresh:
                    self.count_site(key, s, actions, True)
            if then is not None:
                then(error is None)
        actions = self.actions_for(key)
        if library:
            self.count_site(key, s, actions, recount)
        self.run(lambda: actions.poll(task, network=network, cloud=s.cloud_offered, library=False,
                                      refresh=False), done)

    def count_site(self, key, s, actions, recount):
        if key in self.counting or self.states.get(key) is not s:
            return
        self.counting[key] = s
        self.render()
        # A later request to recount (for example a completed upload) must survive
        # this callback. Clear only the request this read is about to serve.
        s.library_refresh = False

        def done(value, error):
            if self.counting.get(key) is s:
                del self.counting[key]
            if self.states.get(key) is not s:
                return
            s.summary, s.library_at = False if error is not None else value, self.now()
            if s.summary:
                s.locations = s.summary.get('locations') or []
            self.render()
        self.run(lambda: actions.saved_files(summary=True, refresh=recount), done)

    def tick(self):
        """The clocks' second. The window is drawn again only when what it would show has changed: drawn every
        second regardless, a list under the pointer or a field being typed in was remade under the person."""
        self.render(changed=True)

    def render(self, changed=False):
        """Draw the pages from what the window holds; with changed, only when that differs from the last drawing."""
        now, chosen = self.now(), self.chosen
        s = self.state
        for key in self.open_keys():
            state = self.states.get(key)
            if state is not None and dashboard_model.start_overdue(state, now):
                # Its time to start is up: one look, asked for once, before the card says it is not working.
                state.start_read = state.connected_at
                self.poll_soon(key)
        views = {key: (self.states[key], dashboard_model.view(self.states[key], now)) for key in self.shown()}
        v = views[chosen][1] if chosen in views else dashboard_model.view(s, now)
        # Until a site's context is read, the app's own list stands in, so every open site has its card.
        sites = (s.sites or next((state.sites for state, _ in views.values() if state.sites), None)
                 or site_choices(find_site(chosen) or DEFAULT))
        # Updates are as late as the latest site's; a site that could not be reached says so first.
        active = [pair for pair in views.values() if dashboard_model.updating(pair[0])]
        oldest = min(active or views.values(), key=lambda pair: (not pair[0].offline, pair[0].refreshed_at), default=(s, v))
        everything = {'updated': oldest[1]['updated'] if active or any(state.offline for state, _ in views.values())
                      or not all(state.loaded for state, _ in views.values())
                      else 'Automatic updates paused', 'go_all': any(view['go_enabled'] for _, view in views.values()),
                      'pause_all': any(view['pause_visible'] for _, view in views.values()),
                      'folder_enabled': v['folder_enabled'] and all(view['folder_enabled'] for _, view in views.values()),
                      'folder_tooltip': next((view['folder_tooltip'] for _, view in views.values()
                                              if view['folder_tooltip']), v['folder_tooltip'])}
        dark = self.shell.dark()
        # Everything a drawing is made from, as text: the same text is the same drawing.
        drawing = repr((self.route, dark, chosen, s, sites, sorted(views.items()), everything, sorted(self.counting)))
        if changed and drawing == self.drawn_from:
            return
        self.drawn_from = drawing
        self.header.show(self.route, dark)
        self.dashboard.show(s, sites, views, everything)
        if self.route.startswith('settings/'):
            self.settings.show(s, v, self.route.partition('/')[2], sites, views)

    # What the person asks for.

    def run(self, work, done):
        self.shell.run(work, done)

    def act(self, work, done=None, cloud=False, after=None, key=None):
        """One change at a time on a site: its buttons wait while it runs, and a refusal is said on its card and in
        Settings. key is the site's; the chosen site's when None."""
        key = key or self.chosen
        s = self.of(key)
        if s.busy:
            return
        s.busy = True
        self.render()

        def finished(value, error):
            s.busy = False
            if self.states.get(key) is s:
                self.acts[key] = self.acts.get(key, 0) + 1
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

    def account_act(self, work, done=None, after=None, key=None):
        s = self.of(key or self.chosen)
        if s.busy:
            return
        s.message = None
        self.act(work, done, after=after, key=key)

    def signin(self, key=None):
        """Sign in to a site, in the window: one site at a time, so the site is chosen first."""
        key = key or self.chosen
        if key != self.chosen or key not in self.open_keys():
            self.shell.choose_site(key, then=lambda: self.signin(key))
            return
        s = self.of(key)
        self.account_act(self.actions_for(key).signin, lambda value: s.received_site_auth(value, self.now()), key=key)

    def sign_out(self, key=None):
        key = key or self.chosen
        s, actions = self.of(key), self.actions_for(key)
        if s.busy or not (s.status or {}).get('session_configured'):
            return
        if not self.shell.ask(SIGN_OUT.format(site=s.site())):
            return

        def signed_out(value):
            # Said at once; the next refresh reads the same from the library.
            s.status = dict(s.status or {}, session_configured=False)
            s.received_site_auth({'status': 'idle'}, self.now())
            kept = (value or {}).get('kept') or []
            if kept:
                s.message = dashboard_model.Message(SIGNED_OUT_AWAY[min(len(kept), 2)].format(
                    site=s.site(), folders='\n'.join(kept)), True)
            self.poll_soon(key)
        self.account_act(actions.sign_out, signed_out, key=key)

    def go(self, key=None):
        s = self.of(key or self.chosen)

        def started(value):
            s.received_account(value, self.now())
            # A fast failure can arrive before Go returns, with no running refresh in between.
            s.download_started()
            s.offline = False
        self.account_act(self.actions_for(key or self.chosen).go, started, key=key)

    def pause(self, key=None):
        s = self.of(key or self.chosen)

        def paused(value):
            s.received_account(value, self.now())
            s.refreshed(self.now())
            self.poll_soon(key)
        self.account_act(self.actions_for(key or self.chosen).pause, paused, key=key)

    def go_all(self):
        """Start or continue every site that can now; the others stay as they are."""
        now = self.now()
        for key in self.open_keys():
            if dashboard_model.view(self.of(key), now)['go_enabled']:
                self.go(key)

    def support_details(self):
        """Every working site's details for support, as the person would paste them into a message."""
        sites = self.state.sites or site_choices(find_site(self.chosen) or DEFAULT)
        shown = self.shown()
        return support_details.text(license_texts.version(), [
            (item['name'], self.states.get(item['key']) if item['key'] in shown else None)
            for item in sites if item.get('available')])

    def copy_support_details(self):
        """Put the details for support on the clipboard, for the person to paste into a message."""
        self.q.QtGui.QGuiApplication.clipboard().setText(self.support_details())
        say(self.settings.support_note, SUPPORT_COPIED)

    def downloads(self):
        """The folder a log saved for support goes in: the person's Downloads folder, or their home folder."""
        paths = self.q.QtCore.QStandardPaths
        return (paths.writableLocation(paths.StandardLocation.DownloadLocation)
                or paths.writableLocation(paths.StandardLocation.HomeLocation))

    def save_support_log(self):
        """Save one file for support in the Downloads folder: the details, each open site's records and the app log."""
        folder = self.downloads()
        details, sites = self.support_details(), [self.actions_for(key) for key in self.open_keys()]
        button = self.settings.save_log
        button.setEnabled(False)

        def work():
            from .app_runtime import state_dir
            records, libraries = [], []
            for actions in sites:
                try:
                    text, library = actions.support_records()
                except app_actions.Refused as refusal:
                    text, library = f'=== A site could not be read: {refusal.code}', None
                records.append(text)
                libraries += [library] if library else []
            text = support_log.bundle(details, records, support_log.app_log(state_dir()), libraries)
            return support_log.save(folder, text)

        def done(path, error):
            button.setEnabled(True)
            say(self.settings.support_note, LOG_FAILED if error else LOG_SAVED.format(name=path.name), error is not None)
        self.run(work, done)

    def pause_all(self):
        now = self.now()
        for key in self.open_keys():
            v = dashboard_model.view(self.of(key), now)
            if v['pause_visible'] and v['pause_enabled']:
                self.pause(key)

    def poll_soon(self, key=None):
        key = key or self.chosen
        self.q.QtCore.QTimer.singleShot(0, self.widget, lambda: self.poll_site(key))  # Dropped with the pages if they go first.

    def speed(self, value, key=None):
        key = key or self.chosen
        s, actions = self.of(key), self.actions_for(key)
        if s.busy:
            self.render()
            return
        s.message = dashboard_model.Message(f'Changing speed to {SPEED_NAMES.get(value, "the selected speed")}…')

        def work():
            try:
                return actions.speed(value), None
            except app_actions.Refused as error:
                # A failed change may have paused the controller. Read that local state now.
                try:
                    account = actions.account()
                except app_actions.Refused:
                    account = None
                return account, str(error)

        def done(result):
            account, error = result
            if account is not None:
                s.received_account(account, self.now())
            s.message = dashboard_model.Message(error, True) if error else None
        self.act(work, done, key=key)

    def open_download_choices(self, key):
        """Type choices only; selecting individual series/episodes comes later."""
        from .download_selection import HELP, LABELS, TYPES
        state = self.of(key)
        if state.busy:
            return
        dialog = self.q.QtWidgets.QDialog(self.widget)
        dialog.setWindowTitle(f'What to download from {find_site(key).name}')
        dialog.setMinimumWidth(440)
        box = self.q.QtWidgets.QVBoxLayout(dialog)
        box.setContentsMargins(24, 24, 24, 24)
        box.setSpacing(12)
        content = self.q.QtWidgets.QWidget()
        body = self.q.QtWidgets.QVBoxLayout(content)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(12)
        body.setSizeConstraint(self.q.QtWidgets.QLayout.SizeConstraint.SetMinAndMaxSize)
        area = self.kit.scroll(content)
        area.setHorizontalScrollBarPolicy(self.q.QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        box.addWidget(area, 1)
        chosen = (state.account or {}).get('download_types', dict.fromkeys(TYPES, True))
        if chosen is None:
            chosen = dict.fromkeys(TYPES, False)
        checks = {}
        for kind in TYPES:
            check = self.q.QtWidgets.QCheckBox(LABELS[kind])
            check.setObjectName('downloadType_' + kind)
            check.setChecked(bool(chosen.get(kind)))
            check.setToolTip(HELP[kind])
            body.addWidget(check)
            description = self.q.QtWidgets.QLabel(HELP[kind])
            description.setWordWrap(True)
            description.setObjectName('muted')
            description.setSizePolicy(self.q.QtWidgets.QSizePolicy.Policy.Preferred,
                                      self.q.QtWidgets.QSizePolicy.Policy.Minimum)
            body.addWidget(description)
            checks[kind] = check
        note = self.q.QtWidgets.QLabel('Already saved files are kept. Choices also apply on future runs. '
                                       'A running download pauses to apply changes, then continues.')
        note.setWordWrap(True)
        note.setSizePolicy(self.q.QtWidgets.QSizePolicy.Policy.Preferred,
                           self.q.QtWidgets.QSizePolicy.Policy.Minimum)
        body.addWidget(note)
        buttons = self.q.QtWidgets.QDialogButtonBox(
            self.q.QtWidgets.QDialogButtonBox.StandardButton.Save | self.q.QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        save = buttons.button(self.q.QtWidgets.QDialogButtonBox.StandardButton.Save)
        def changed():
            save.setEnabled(any(check.isChecked() for check in checks.values()))
        for check in checks.values():
            check.toggled.connect(changed)
        changed()
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        box.addWidget(buttons)
        # Native dialogs can inherit their styled fonts after the initial size
        # calculation. Measure the wrapped content after polishing; on a short
        # screen only the descriptions scroll, leaving both actions reachable.
        dialog.ensurePolished()
        margins = box.contentsMargins()
        width = max(dialog.minimumWidth(), box.minimumSize().width())
        content_width = width - margins.left() - margins.right()
        height = max(body.totalHeightForWidth(content_width), body.minimumSize().height())
        height += buttons.sizeHint().height()
        height += margins.top() + margins.bottom() + box.spacing()
        dialog.resize(width, min(height, dialog.screen().availableGeometry().height() - 80))
        if dialog.exec() == self.q.QtWidgets.QDialog.DialogCode.Accepted:
            self.download_types({kind: check.isChecked() for kind, check in checks.items()}, key)
        dialog.deleteLater()

    def download_types(self, value, key):
        state, actions = self.of(key), self.actions_for(key)
        if state.busy:
            return
        state.message = dashboard_model.Message('Applying download choices…')

        def work():
            try:
                return actions.download_types(value), None
            except app_actions.Refused as error:
                try:
                    account = actions.account()
                except app_actions.Refused:
                    account = None
                return account, str(error)

        def done(result):
            account, error = result
            if account is not None:
                state.received_account(account, self.now())
            state.message = dashboard_model.Message(error, True) if error else None
            self.poll_soon(key)
        self.act(work, done, key=key)

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
            for other in self.states.values():
                other.network_at = 0  # The rule is every site's.
            s.network, s.network_at = value, self.now()

        def settled():
            # The box shows the choice while it is kept; the drawing after shows what was saved.
            s.network_saving = False
        self.account_act(checked, kept, settled)

    def allow_network(self, key=None):
        """The connection rule is every site's: allowed on one card, every site reads it again at its next refresh."""
        key = key or self.chosen
        s, actions = self.of(key), self.actions_for(key)

        def allowed():
            actions.wifi_only(False)
            return actions.poll(network=True, cloud=False)['network']

        def kept(value):
            for other in self.states.values():
                other.network_at = 0
            s.network, s.network_at = value, self.now()
            self.poll_soon()
        self.account_act(allowed, kept, key=key)

    def choose_storage(self, chosen, key=None):
        key = key or self.chosen
        s, actions = self.of(key), self.actions_for(key)
        if s.busy:
            self.render()
            return
        targets = list(s.cloud_targets())

        def kept(value):
            s.cloud, s.choice_message = value, ''

        def finished(value, error):
            if self.states.get(key) is not s:
                return
            s.busy = False
            self.acts[key] = self.acts.get(key, 0) + 1
            if error is None:
                kept(value)
            else:
                s.choice_message = error
            self.render()
            if error is not None:
                self.poll_soon(key)  # Shows the choice as it was kept.
        s.busy = True
        self.render()
        self.run(lambda: actions.choose_storage(chosen, targets), finished)

    def confirm_cloud(self, key):
        """Ask once whether new episodes may go to a connection the library folder brought; until the person
        says yes, they are saved here."""
        s = self.of(key)
        targets = (s.cloud or {}).get('targets') or []
        target = next((t for t in targets if t.get('enabled') and t.get('confirmed') is False), None)
        if target is None or key != self.chosen or key in self.cloud_asked or s.busy:
            return
        self.cloud_asked.add(key)  # Never twice in one window, even while the answer is being kept.
        bucket = target.get('bucket')
        use = self.shell.ask(CLOUD_CONFIRM.format(
            bucket=f'the Backblaze B2 bucket "{bucket}"' if bucket else 'a Backblaze B2 bucket'))
        if use is None or self.states.get(key) is not s:
            self.cloud_asked.discard(key)  # The app ended or another folder opened under the question: not an answer.
            return

        def kept(value):
            s.cloud = value
        self.act(lambda: self.actions_for(key).cloud_confirm(target['provider'], use), kept, cloud=True, key=key)

    # Each of these is for the site chosen when it was asked for: its state and library are taken then, so an
    # answer that comes back after another site was chosen still lands on the site it belongs to.

    def share_b2(self, shared):
        key, s, actions = self.chosen, self.state, self.actions

        def kept(value):
            s.cloud = value
        self.act(lambda: actions.cloud_storage('shared' if shared else 'separate'), kept, key=key)

    def cloud_connect(self, bucket, key_id, application_key):
        key, s, actions = self.chosen, self.state, self.actions
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
            self.poll_soon(key)
        self.act(lambda: actions.cloud_signin('b2', credentials), started, cloud=True, key=key)

    def cloud_cancel(self):
        key, s, actions = self.chosen, self.state, self.actions

        def cancelled(value):
            s.cloud_auth = value
        self.act(actions.cloud_cancel, cancelled, key=key)

    def _cloud_task(self, kind, work):
        key, s = self.chosen, self.state

        def started(value):
            s.task, s.task_kind = value.get('task_id'), kind
            s.cloud_message = dashboard_model.Message(storage_model.TASK_STARTED[kind])
        self.act(work, started, cloud=True, key=key)

    def cloud_test(self):
        self._cloud_task('cloud-test', self.actions.cloud_test)

    def cloud_upload(self):
        self._cloud_task('cloud', self.actions.cloud_upload)

    def cloud_cleanup(self, test_id):
        actions = self.actions
        self._cloud_task('cloud-cleanup', lambda: actions.cloud_cleanup(test_id))

    # Asked of the window itself.

    def provider_cloud(self, key):
        if key == self.chosen:
            self.go_to('settings/b2')
        else:
            self.shell.choose_site(key, lambda: self.go_to('settings/b2'))

    def choose_site(self, key):
        self.shell.choose_site(key)
        self.render()  # The list shows the chosen site until the other one is open.

    def open_folder(self):
        self.shell.open_folder()

    def change_folder(self):
        self.shell.choose_location()

    def toggle_theme(self):
        self.shell.toggle_theme()
        self.render()
