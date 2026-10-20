"""The window's look, light and dark: its colours and one Qt style sheet, as plain text with no Qt.

Widgets are named by role (objectName) and tone (the 'tone' property); the sheet says how each looks.
"""
from importlib.resources import files

# Light, then dark.
COLOURS = {
    'text': ('#183331', '#e3ece8'), 'page': ('#f4f6f2', '#111918'), 'surface': ('#ffffff', '#1a2523'),
    'surface-soft': ('#f0f5f1', '#23332f'), 'surface-tint': ('#f6faf6', '#1b2b26'), 'border': ('#d5dfd5', '#3d5149'),
    'muted': ('#52695e', '#adbbb3'), 'accent': ('#216154', '#94d8bd'), 'accent-border': ('#75a18d', '#72ac94'),
    'notice': ('#e7f0e9', '#243e33'), 'warning-bg': ('#fff3e6', '#38291e'), 'warning': ('#b66026', '#e6a45e'),
    'focus': ('#df9a39', '#efb35e'), 'input-border': ('#9cafaa', '#687e72'), 'button-hover': ('#184c42', '#b4e9d1'),
    'button-text': ('#ffffff', '#122d22'), 'disabled-text': ('#5d706a', '#a0afa6'), 'disabled-bg': ('#e2e9e4', '#2d3d35'),
    'error': ('#914416', '#ffbc8d'),
}
# The pages fill the window. Text that is read, not glanced at, stays within this width, about 90 characters: longer
# lines are hard to follow back to their start. Its lines are spaced this much (percent of the font's height).
READING = 820
LINE_HEIGHT = 135

SHEET = '''
QWidget { color: %(text)s; font-size: 15px; }
QMainWindow, QStackedWidget, QScrollArea, QWidget#page { background: %(page)s; }
QToolTip { color: %(text)s; background: %(surface)s; border: 1px solid %(border)s; }

QWidget#header, QWidget#signinHeader { background: %(surface)s; border-bottom: 1px solid %(border)s; }
QWidget#header QLabel#brand, QWidget#signinHeader QLabel#signinTitle { font-size: 18px; font-weight: 600; }
QWidget#signinHeader QLabel { color: %(muted)s; font-size: 14px; }
QPushButton#nav { min-height: 28px; padding: 8px 12px; border: none; border-radius: 6px; background: transparent;
                  color: %(muted)s; font-size: 14px; font-weight: 500; }
QPushButton#nav:hover { color: %(text)s; background: %(surface-soft)s; }
QPushButton#nav:checked { color: %(accent)s; background: %(notice)s; font-weight: 600; }
QWidget#signinHeader QPushButton { min-height: 28px; padding: 8px 12px; border: none; border-radius: 6px;
                                   background: transparent; color: %(muted)s; font-size: 14px; font-weight: 500; }
QWidget#signinHeader QPushButton:hover { color: %(text)s; background: %(surface-soft)s; }
QPushButton#themeToggle { min-width: 36px; max-width: 36px; min-height: 36px; max-height: 36px; padding: 0;
                          border: none; border-radius: 6px; background: transparent; color: %(muted)s; font-size: 18px; }
QPushButton#themeToggle:hover { color: %(accent)s; background: %(notice)s; }

QFrame#card { background: %(surface)s; border: 1px solid %(border)s; border-radius: 14px; }
QFrame#card[tone="status"] { border-top: 3px solid %(accent-border)s; }
QFrame#card[tone="warning"] { border-top: 3px solid %(warning)s; }
QFrame#inner { background: %(surface)s; border: 1px solid %(border)s; border-radius: 8px; }
QFrame#card QLabel, QFrame#inner QLabel, QFrame#metric QLabel, QFrame#state QLabel, QFrame#costs QLabel { background: transparent; }
QFrame#metric { background: %(surface-soft)s; border-radius: 8px; }
QLabel#metricNumber { font-size: 28px; font-weight: 600; }
QLabel#eyebrow { color: %(muted)s; font-size: 12px; font-weight: 600; letter-spacing: 1px; }
QLabel#pageTitle { font-size: 28px; font-weight: 600; }
QLabel#cardTitle { font-size: 24px; font-weight: 600; }
QLabel#sectionTitle { font-size: 18px; font-weight: 600; }
QLabel#muted, QLabel#footnote { color: %(muted)s; font-size: 14px; }
QLabel#footnote { border-top: 1px solid %(border)s; padding-top: 14px; }
QLabel#strong { font-weight: 600; }
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
QPushButton[kind="go"] { min-width: 140px; min-height: 44px; font-size: 18px; }
QPushButton[kind="link"] { min-height: 0; padding: 0; border: none; background: transparent; color: %(accent)s;
                           font-weight: 400; text-decoration: underline; text-align: left; }
QPushButton[kind="link"]:hover { color: %(button-hover)s; background: transparent; }
QPushButton[kind="place"] { padding: 12px; text-align: left; font-weight: 400; color: %(text)s; background: %(surface-tint)s;
                            border: 1px solid %(input-border)s; border-radius: 8px; }
QPushButton[kind="place"]:hover { color: %(text)s; background: %(notice)s; }
QPushButton[kind="place"]:checked { border: 2px solid %(accent)s; background: %(notice)s; }
QPushButton[kind="tab"] { min-height: 32px; padding: 8px 12px; text-align: left; border: none; border-radius: 6px;
                          color: %(text)s; background: transparent; font-weight: 500; }
QPushButton[kind="tab"]:hover { background: %(surface-soft)s; color: %(text)s; }
QPushButton[kind="tab"]:checked { color: %(accent)s; background: %(notice)s; font-weight: 600; border-left: 3px solid %(accent)s; }
QPushButton:disabled, QPushButton[kind="secondary"]:disabled { color: %(disabled-text)s; background: %(disabled-bg)s; border-color: %(border)s; }
QPushButton:focus, QComboBox:focus, QLineEdit:focus, QSpinBox:focus, QCheckBox:focus, QRadioButton:focus { outline: none; }
QPushButton:focus { border: 2px solid %(focus)s; }

QLineEdit, QComboBox, QSpinBox { min-height: 34px; padding: 2px 8px; border: 1px solid %(input-border)s; border-radius: 6px;
                                 background: %(surface)s; color: %(text)s; }
QLineEdit:focus, QComboBox:focus, QSpinBox:focus { border: 2px solid %(focus)s; }
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled { color: %(disabled-text)s; background: %(disabled-bg)s; }
QComboBox::drop-down { width: 24px; border: none; background: transparent; }
QComboBox::down-arrow { image: url("%(arrow)s"); width: 16px; height: 16px; }
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

QTableWidget { background: %(surface)s; border: 1px solid %(border)s; border-radius: 8px; gridline-color: %(border)s;
               selection-background-color: %(notice)s; selection-color: %(text)s; }
QHeaderView::section { background: %(surface)s; color: %(muted)s; font-size: 13px; font-weight: 600; padding: 8px;
                       border: none; border-bottom: 1px solid %(border)s; }
QScrollBar:vertical, QScrollBar:horizontal { background: %(page)s; }
'''


def colours(dark):
    return {name: pair[bool(dark)] for name, pair in COLOURS.items()}


def stylesheet(dark):
    return SHEET % (colours(dark) | {name: picture(name, dark) for name in ('check', 'arrow')})


def picture(name, dark):
    """The path of one of the app's own small pictures, with an @2x one beside it for sharp screens. They are PNG,
    which Qt reads without a plugin; Qt reads the path with forward slashes on every system."""
    return str(files('comic_archive').joinpath('window', f"{name}-{'dark' if dark else 'light'}.png")).replace('\\', '/')
