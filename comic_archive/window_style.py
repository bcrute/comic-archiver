"""The window's look, light and dark: its colours and one Qt style sheet, as plain text with no Qt.

Widgets are named by role (objectName) and tone (the 'tone' property); the sheet says how each looks.
"""
from importlib.resources import files

# Light, then dark.
COLOURS = {
    'text': ('#352c2a', '#f6ecdf'), 'page': ('#f8f5ef', '#211c1d'), 'surface': ('#ffffff', '#2b2526'),
    'surface-soft': ('#f3eee7', '#383033'), 'surface-tint': ('#faf5ee', '#302728'), 'border': ('#ded2c9', '#59484b'),
    'muted': ('#6d605a', '#c3b7b1'), 'accent': ('#954936', '#ffac98'), 'accent-border': ('#aa7868', '#b47768'),
    'notice': ('#f8e9e1', '#473236'), 'warning-bg': ('#fff3e6', '#38291e'), 'warning': ('#b66026', '#e6a45e'),
    'focus': ('#df9a39', '#efb35e'), 'input-border': ('#a99386', '#8f7976'), 'button-hover': ('#793a2c', '#ffc4b2'),
    'button-text': ('#ffffff', '#2f1e1a'), 'disabled-text': ('#5d706a', '#b1a3a0'), 'disabled-bg': ('#e2e9e4', '#3d3334'),
    'error': ('#914416', '#ffbc8d'),
    'destructive': ('#b4232c', '#ff9fa3'), 'destructive-hover': ('#fff0f1', '#3d2428'),
    'nav-on': ('#f3d9cf', '#6f3a38'), 'nav-on-text': ('#7a3524', '#fff3ea'),
}
# The pages fill the window. Text that is read, not glanced at, stays within this width, about 90 characters: longer
# lines are hard to follow back to their start. Its lines are spaced this much (percent of the font's height).
READING = 820
LINE_HEIGHT = 135

SHEET = '''
QWidget { color: %(text)s; font-size: 15px; }
QMainWindow, QDialog, QStackedWidget, QScrollArea, QWidget#page { background: %(page)s; }
QToolTip { color: %(text)s; background: %(surface)s; border: 1px solid %(border)s; }

QWidget#header, QWidget#signinHeader { background: %(surface)s; border-bottom: 1px solid %(border)s; }
QWidget#header QLabel#brand, QWidget#signinHeader QLabel#signinTitle { font-size: 18px; font-weight: 600; }
QWidget#header QLabel#brand { font-family: Georgia, "DejaVu Serif", serif; font-size: 26px; font-weight: 700; }
QFrame#headerRule { background: %(border)s; border: none; min-width: 1px; max-width: 1px; min-height: 30px; max-height: 30px; }
QWidget#signinHeader QLabel { color: %(muted)s; font-size: 14px; }
QPushButton#nav { min-height: 28px; padding: 8px 18px; border: none; border-radius: 22px; background: transparent;
                  color: %(text)s; font-size: 16px; font-weight: 500; }
QPushButton#nav:hover { color: %(text)s; background: %(surface-soft)s; }
QPushButton#nav:checked { color: %(nav-on-text)s; background: %(nav-on)s; }
QWidget#signinHeader QPushButton { min-height: 28px; padding: 8px 12px; border: none; border-radius: 6px;
                                   background: transparent; color: %(muted)s; font-size: 14px; font-weight: 500; }
QWidget#signinHeader QPushButton:hover { color: %(text)s; background: %(surface-soft)s; }
QPushButton#themeToggle { min-width: 36px; max-width: 36px; min-height: 36px; max-height: 36px; padding: 0;
                          border: none; border-radius: 18px; background: %(surface)s; color: %(text)s; font-size: 18px; }
QPushButton#themeToggle:hover { color: %(accent)s; background: %(notice)s; }

QFrame#card { background: %(surface)s; border: 1px solid %(border)s; border-radius: 14px; }
QFrame#card[site="true"] { border-top: 3px solid %(border)s; }
QFrame#card[tone="status"] { border-top: 3px solid %(accent-border)s; }
QFrame#card[tone="warning"] { border-top: 3px solid %(warning)s; }
QFrame#inner { background: %(surface)s; border: 1px solid %(border)s; border-radius: 8px; }
QFrame#card QLabel, QFrame#inner QLabel, QFrame#metric QLabel, QFrame#state QLabel, QFrame#costs QLabel { background: transparent; }
QFrame#metric { background: %(surface-soft)s; border-radius: 8px; }
QFrame#libraryOverview, QFrame#libraryOverview QFrame#metric { background: transparent; border: none; }
QFrame#libraryOverview QLabel#metricNumber, QFrame#libraryOverview QLabel#metricCaption { font-size: 20px; font-weight: 700; }
QFrame#libraryOverview QLabel#muted { font-size: 14px; }
QFrame#libraryRule { background: %(border)s; border: none; min-width: 1px; max-width: 1px; min-height: 30px; max-height: 30px; }
QLabel#metricNumber { font-size: 28px; font-weight: 600; }
QLabel#eyebrow { color: %(muted)s; font-size: 12px; font-weight: 600; letter-spacing: 1px; }
QLabel#pageTitle { font-size: 28px; font-weight: 600; }
QLabel#cardTitle { font-size: 24px; font-weight: 600; }
QLabel#sectionTitle { font-size: 18px; font-weight: 600; }
QLabel#muted, QLabel#footnote { color: %(muted)s; font-size: 14px; }
QLabel#footnote { border-top: 1px solid %(border)s; padding-top: 14px; }
QLabel#strong { font-weight: 600; }
QLabel#fieldName { color: %(muted)s; }
QLabel#fieldValue[tone="warning"] { color: %(warning)s; }
QLabel#providerTitle { font-family: Georgia, "DejaVu Serif", serif; font-size: 28px; font-weight: 700; }
QLabel#episodeTotal { font-family: Georgia, "DejaVu Serif", serif; font-size: 62px; font-weight: 700; max-height: 58px; }
QLabel#episodeCaption { font-size: 18px; }
QLabel#estimateLabel { color: %(muted)s; font-size: 13px; }
QLabel#estimateValue { font-size: 21px; font-weight: 700; }
QLabel#transferName, QLabel#transferValue { font-size: 16px; }
QLabel#transferValue { font-weight: 600; }
QLabel#cardStatus { font-size: 16px; }
QLabel#cardStatus[tone="warning"] { color: %(warning)s; }
QLabel#statusSymbol { font-size: 18px; color: %(accent)s; }
QFrame#cardRule { background: %(border)s; border: none; min-height: 1px; max-height: 1px; }
QFrame#cardDivider { background: %(border)s; border: none; min-width: 1px; max-width: 1px; }
QToolButton#hint { min-width: 16px; max-width: 16px; min-height: 16px; max-height: 16px; padding: 0; border: 1px solid %(muted)s;
                   border-radius: 9px; color: %(muted)s; background: transparent; font-size: 11px; font-weight: 600; }
QToolButton#hint:hover, QToolButton#hint:focus { color: %(accent)s; border-color: %(accent)s; }
QToolButton#editFolder { min-width: 26px; min-height: 26px; padding: 0; border: 1px solid transparent;
                         border-radius: 4px; color: %(accent)s; background: transparent; font-size: 20px; }
QToolButton#editFolder:hover { background: %(notice)s; }
QToolButton#editFolder:focus { border-color: %(focus)s; }
QToolButton#editFolder:disabled { color: %(disabled-text)s; }
QLabel[error="true"] { color: %(error)s; }
QLabel#activity { border-left: 3px solid %(accent-border)s; padding-left: 10px; }
QLabel#activity[tone="warning"] { border-left-color: %(warning)s; }
QLabel#notice { padding: 10px 14px; border-left: 4px solid %(accent-border)s; background: %(notice)s; border-radius: 4px; }
QLabel#notice[error="true"] { border-left-color: %(warning)s; background: %(warning-bg)s; color: %(text)s; }

QLabel#pill { padding: 3px 10px; border-radius: 11px; font-size: 13px; font-weight: 600; color: %(muted)s; background: %(surface-soft)s; }
QLabel#pill[tone="success"], QLabel#pill[tone="working"] { color: %(accent)s; background: %(notice)s; }
QLabel#pill[tone="warning"] { color: %(warning)s; background: %(warning-bg)s; }
QLabel#badge { padding: 3px 8px; border: 1px solid %(border)s; border-radius: 6px; color: %(muted)s; font-size: 13px; }
QLabel#badge[tone="warning"] { color: %(warning)s; border-color: %(warning)s; }
QFrame#state { border-left: 4px solid %(border)s; border-radius: 4px; background: %(surface-soft)s; }
QFrame#state[tone="success"] { border-left-color: %(accent)s; background: %(notice)s; }
QFrame#state[tone="warning"] { border-left-color: %(warning)s; background: %(warning-bg)s; }
QFrame#costs { border-left: 4px solid %(accent-border)s; border-radius: 4px; background: %(notice)s; }

QPushButton { min-height: 36px; padding: 6px 14px; font-weight: 600; color: %(button-text)s; background: %(accent)s;
              border: 1px solid %(accent)s; border-radius: 6px; }
QPushButton:hover { background: %(button-hover)s; border-color: %(button-hover)s; }
QPushButton[kind="secondary"] { color: %(accent)s; background: %(surface)s; }
QPushButton[kind="secondary"]:hover { color: %(button-text)s; background: %(accent)s; }
QPushButton[kind="destructive"] { color: %(destructive)s; background: %(surface)s; border-color: %(destructive)s; }
QPushButton[kind="destructive"]:hover { color: %(destructive)s; background: %(destructive-hover)s;
                                       border-color: %(destructive)s; }
QPushButton[kind="go"] { min-width: 96px; }
QPushButton[kind="downloadChoice"] { color: %(text)s; background: transparent; border: none; text-align: left;
                                    min-height: 22px; padding: 2px 0; font-size: 12px; }
QPushButton[kind="downloadChoice"]:hover { color: %(accent)s; }
QPushButton#toolbarAction { min-height: 30px; padding: 5px 16px; font-size: 15px; font-weight: 500; border-radius: 8px;
                           background: transparent; color: %(text)s; border-color: %(muted)s; }
QPushButton#toolbarAction:hover { background: %(notice)s; border-color: %(accent)s; }
QPushButton#toolbarAction:disabled { color: %(disabled-text)s; border-color: %(border)s; }
QPushButton[kind="link"] { min-height: 0; padding: 0; border: none; background: transparent; color: %(accent)s;
                           font-weight: 400; text-decoration: underline; text-align: left; }
QPushButton[kind="link"]:hover { color: %(button-hover)s; background: transparent; }
QLabel#place { padding: 12px; font-weight: 400; color: %(text)s; background: %(surface-tint)s;
               border: 1px solid %(input-border)s; border-radius: 8px; }
QPushButton[kind="tab"] { min-height: 32px; padding: 8px 12px; text-align: left; border: none; border-radius: 6px;
                          color: %(text)s; background: transparent; font-weight: 500; }
QPushButton[kind="tab"]:hover { background: %(surface-soft)s; color: %(text)s; }
QPushButton[kind="tab"]:checked { color: %(accent)s; background: %(notice)s; font-weight: 600; border-left: 3px solid %(accent)s; }
QPushButton:disabled, QPushButton[kind="secondary"]:disabled { color: %(disabled-text)s; background: %(disabled-bg)s; border-color: %(border)s; }
QPushButton:focus, QComboBox:focus, QLineEdit:focus, QSpinBox:focus, QCheckBox:focus, QRadioButton:focus { outline: none; }
QPushButton:focus { border: 2px solid %(focus)s; }
QPushButton[kind="destructive"]:focus { border: 2px solid %(destructive)s; }

QLineEdit, QComboBox, QSpinBox { min-height: 34px; padding: 2px 8px; border: 1px solid %(input-border)s; border-radius: 6px;
                                 background: %(surface)s; color: %(text)s; }
QLineEdit:focus, QComboBox:focus, QSpinBox:focus { border: 2px solid %(focus)s; }
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled { color: %(disabled-text)s; background: %(disabled-bg)s; }
QComboBox::drop-down { width: 24px; border: none; background: transparent; }
QComboBox::down-arrow { image: url("%(arrow)s"); width: 16px; height: 16px; }
QComboBox#dashboardSpeed { min-height: 28px; border: 1px solid %(input-border)s; border-radius: 7px; background: transparent;
                           padding: 1px 6px 1px 12px; font-size: 15px; }
QComboBox#dashboardSpeed:disabled { background: transparent; }
QComboBox#dashboardSpeed:focus { border-color: %(focus)s; }
QComboBox QAbstractItemView { background: %(surface)s; color: %(text)s; selection-background-color: %(notice)s;
                              selection-color: %(text)s; border: 1px solid %(border)s; }
QCheckBox, QRadioButton { spacing: 8px; background: transparent; }
QCheckBox::indicator, QRadioButton::indicator { width: 16px; height: 16px; border: 1px solid %(input-border)s; background: %(surface)s; }
QCheckBox::indicator { border-radius: 4px; }
QRadioButton::indicator { border-radius: 9px; }
QCheckBox::indicator:checked { border-color: %(accent)s; background: %(accent)s; image: url("%(check)s"); }
QRadioButton::indicator:checked { border-color: %(accent)s; background: qradialgradient(cx: 0.5, cy: 0.5, radius: 0.5, fx: 0.5,
    fy: 0.5, stop: 0 %(button-text)s, stop: 0.4 %(button-text)s, stop: 0.5 %(accent)s, stop: 1 %(accent)s); }
QCheckBox::indicator:disabled, QRadioButton::indicator:disabled { border-color: %(border)s; background: %(disabled-bg)s; }
QRadioButton::indicator:checked:disabled { background: qradialgradient(cx: 0.5, cy: 0.5, radius: 0.5, fx: 0.5, fy: 0.5,
    stop: 0 %(disabled-text)s, stop: 0.4 %(disabled-text)s, stop: 0.5 %(disabled-bg)s, stop: 1 %(disabled-bg)s); }
QCheckBox:disabled, QRadioButton:disabled { color: %(disabled-text)s; }
QProgressBar { min-height: 14px; max-height: 14px; border: 1px solid %(border)s; border-radius: 7px; background: %(surface-soft)s; }
QProgressBar::chunk { border-radius: 6px; background: %(accent)s; }

QTableWidget { background: %(surface)s; alternate-background-color: %(surface-soft)s; border: 1px solid %(border)s;
               border-radius: 8px; gridline-color: %(border)s;
               selection-background-color: %(notice)s; selection-color: %(text)s; }
QTreeWidget { background: %(surface)s; color: %(text)s; border: 1px solid %(border)s;
              border-radius: 8px; selection-background-color: %(notice)s; selection-color: %(text)s; }
QTreeWidget::item { padding: 5px 8px; }
QHeaderView::section { background: %(surface)s; color: %(muted)s; font-size: 13px; font-weight: 600; padding: 8px;
                       border: none; border-bottom: 1px solid %(border)s; }
QScrollBar:vertical, QScrollBar:horizontal { background: %(page)s; }
'''


def colours(dark):
    return {name: pair[bool(dark)] for name, pair in COLOURS.items()}


CARD_STATES = {
    'neutral': {
        'surface': ('#f7f4f0', '#2e2b2e'), 'shade': ('#eee8e2', '#252326'),
        'accent': ('#625650', '#d0c3bc'), 'text': ('#352c2a', '#f6ecdf'),
        'button-text': ('#ffffff', '#2a2422')},
    'active': {
        'surface': ('#fff4ee', '#573535'), 'shade': ('#ffe6dc', '#2b2227'),
        'accent': ('#914333', '#ff9d88'), 'text': ('#402c29', '#fff0e1'),
        'button-text': ('#ffffff', '#32201a')},
    'complete': {
        'surface': ('#f0f8eb', '#345439'), 'shade': ('#e1efd6', '#1f2b23'),
        'accent': ('#315e32', '#a1e39a'), 'text': ('#263e28', '#f1f5e4'),
        'button-text': ('#ffffff', '#19311d')},
    'paused': {
        'surface': ('#f5f1ff', '#514169'), 'shade': ('#e7dff8', '#292536'),
        'accent': ('#5b438c', '#bcb0ff'), 'text': ('#352c49', '#f4efff'),
        'button-text': ('#ffffff', '#28203d')},
    'waiting': {
        'surface': ('#fff7e5', '#58462c'), 'shade': ('#f5e6c7', '#2c2720'),
        'accent': ('#805a17', '#efc373'), 'text': ('#49371d', '#fff1d2'),
        'button-text': ('#ffffff', '#382b19')},
    'attention': {
        'surface': ('#fff0f0', '#583235'), 'shade': ('#ffdddd', '#302225'),
        'accent': ('#9d2432', '#ff9fa3'), 'text': ('#4a2429', '#ffede7'),
        'button-text': ('#ffffff', '#3a1d22')},
}


def card_colours(appearance, dark):
    return {name: pair[bool(dark)] for name, pair in CARD_STATES.get(appearance, CARD_STATES['neutral']).items()}


def card_stylesheet(dark):
    sheet = []
    for appearance in CARD_STATES:
        c = card_colours(appearance, dark)
        highlight = '#' + ''.join(f'{round(int(c["accent"][index:index + 2], 16) * .92 + 255 * .08):02x}'
                                  for index in (1, 3, 5))
        scope = f'QFrame#card[appearance="{appearance}"]'
        # The card's own ink, thinned: for the rules between its parts and its quieter captions.
        red, green, blue = (int(c['text'][index:index + 2], 16) for index in (1, 3, 5))
        rule, soft = f'rgba({red}, {green}, {blue}, 70)', f'rgba({red}, {green}, {blue}, 205)'
        sheet.extend((
            f'{scope} QLabel {{ color: {c["text"]}; background: transparent; }}',
            f'{scope} QLabel#pill {{ color: {c["button-text"]}; background: {c["accent"]}; padding: 5px 12px; '
            f'border-radius: 8px; font-size: 14px; }}',
            f'{scope} QLabel#pill[tone="working"], {scope} QLabel#pill[tone="success"] '
            f'{{ color: {c["button-text"]}; background: {c["accent"]}; }}',
            f'{scope} QPushButton[kind="go"] {{ color: {c["button-text"]}; '
            f'background: qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 {highlight},stop:1 {c["accent"]}); '
            f'border-color: {c["accent"]}; border-radius: 10px; min-height: 44px; font-size: 17px; }}',
            f'{scope} QPushButton[kind="destructive"] {{ background: transparent; border-radius: 10px; '
            f'min-height: 44px; }}',
            f'{scope} QPushButton[kind="destructive"]:hover {{ background: {colours(dark)["destructive-hover"]}; }}',
            f'{scope} QPushButton[kind="destructive"]:disabled {{ background: transparent; '
            f'color: {colours(dark)["disabled-text"]}; border-color: {rule}; }}',
            f'{scope} QFrame#cardRule, {scope} QFrame#cardDivider {{ background: {rule}; }}',
            f'{scope} QPushButton[kind="go"]:hover {{ background: {c["text"]}; color: {c["shade"]}; }}',
            f'{scope} QPushButton[kind="go"]:disabled {{ background: {c["shade"]}; '
            f'color: {colours(dark)["disabled-text"]}; border-color: {colours(dark)["border"]}; }}',
            f'{scope} QComboBox#dashboardSpeed {{ color: {c["text"]}; border-color: {rule}; }}',
            f'{scope} QLabel#estimateLabel {{ color: {soft}; }}',
            f'{scope} QPushButton[kind="downloadChoice"] {{ color: {c["text"]}; }}',
            f'{scope} QPushButton[kind="downloadChoice"]:hover {{ color: {c["accent"]}; }}',
            f'{scope} QLabel#statusSymbol {{ color: {c["accent"]}; }}',
            f'{scope} QLabel#statusSymbol[tone="warning"] {{ color: {colours(dark)["error"]}; }}',
            f'{scope} QLabel[tone="warning"], {scope} QLabel[error="true"] '
            f'{{ color: {colours(dark)["error"]}; }}',
        ))
    return '\n'.join(sheet)


def stylesheet(dark):
    return SHEET % (colours(dark) | {name: picture(name, dark) for name in ('check', 'arrow')}) + card_stylesheet(dark)


def picture(name, dark):
    """The path of one of the app's own small pictures, with an @2x one beside it for sharp screens. They are PNG,
    which Qt reads without a plugin; Qt reads the path with forward slashes on every system."""
    return str(files('comic_archive').joinpath('window', f"{name}-{'dark' if dark else 'light'}.png")).replace('\\', '/')
