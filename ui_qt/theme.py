"""
ui_qt/theme.py
──────────────
The Qt look: one style sheet built from core.constants COLORS and FONTS, plus
small widget helpers (cards, buttons, fonts).

CustomTkinter sizes fonts in pixels and scales them with the display, so the
sizes in FONTS are used here as px; Qt scales px with the screen's device
pixel ratio, which gives the same result at 100%, 125% and 150%.

Buttons get a look through the "role" property (see ROLE_* below), not one
style per button, so the sheet stays in one place.
"""

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QFrame, QLabel, QPushButton, QVBoxLayout

from core.constants import COLORS, FONTS

C = COLORS

# Button roles (QPushButton property "role")
ROLE_PRIMARY = "primary"      # filled accent: Generate
ROLE_OUTLINE = "outline"      # input-colored with an accent outline: Play, Preview
ROLE_STOP = "stop"            # outline in the error color: Stop
ROLE_DANGER = "danger"        # filled red: Cancel
ROLE_SUCCESS = "success"      # filled green, dark text: Save MP3
ROLE_INFO = "info"            # filled blue: Save SRT
ROLE_GHOST = "ghost"          # transparent with a thin border: Folder, footer buttons
ROLE_ICON = "icon"            # small square ghost button in a title bar

DANGER = "#CC0000"
INFO = "#3498DB"
INFO_HOVER = "#2980B9"
SUCCESS_HOVER = "#00B87A"


def font(key, bold=None) -> QFont:
    """A QFont for a FONTS entry, sized in px like CustomTkinter."""
    spec = FONTS[key]
    f = QFont(spec[0])
    f.setPixelSize(spec[1])
    if bold if bold is not None else "bold" in spec[2:]:
        f.setBold(True)
    return f


def _px(key):
    return f"font-family: '{FONTS[key][0]}'; font-size: {FONTS[key][1]}px;"


def stylesheet() -> str:
    return f"""
QWidget {{
    color: {C['text_primary']};
    {_px('body')}
}}
QMainWindow, QDialog, QMessageBox {{
    background: {C['bg_dark']};
}}
QMessageBox, QDialog {{ background: {C['bg_card']}; }}

QFrame#card {{
    background: {C['bg_card']};
    border: 1px solid {C['border']};
    border-radius: 14px;
}}
QFrame#card QLabel {{ background: transparent; }}
QLabel#cardTitle {{ {_px('subtitle')} font-weight: bold; }}
QLabel#appTitle {{ {_px('title')} font-weight: bold; }}
QLabel[muted="true"] {{ color: {C['text_muted']}; }}
QLabel[secondary="true"] {{ color: {C['text_secondary']}; }}
QLabel[small="true"] {{ {_px('body_small')} }}

QFrame#strip {{
    background: {C['bg_input']};
    border: 1px solid {C['border']};
    border-radius: 10px;
}}
QFrame#statusBar {{
    background: {C['bg_card']};
    border: 1px solid {C['border']};
    border-radius: 10px;
}}
QLabel#statusText {{ {_px('status')} color: {C['text_secondary']}; }}
QLabel#siteLink {{
    {_px('body_small')}
    color: {C['accent_primary']};
    background: {C['bg_input']};
    border-radius: 6px;
    padding: 2px 8px;
}}

QPlainTextEdit {{
    background: {C['bg_input']};
    border: 1px solid {C['border']};
    border-radius: 10px;
    padding: 6px;
    {_px('mono')}
    selection-background-color: {C['accent_primary']};
}}
QPlainTextEdit:focus {{ border-color: {C['accent_primary']}; }}
QPlainTextEdit[readOnly="true"] {{ color: {C['text_secondary']}; }}

QPushButton {{
    {_px('button')}
    border-radius: 10px;
    padding: 0 10px;
    min-height: 34px;
    border: 2px solid transparent;
}}
QPushButton:disabled {{ color: {C['text_muted']}; }}

QPushButton[role="primary"] {{ background: {C['accent_primary']}; color: #FFFFFF; }}
QPushButton[role="primary"]:hover {{ background: {C['accent_secondary']}; }}
QPushButton[role="primary"]:disabled {{ background: #3E3A80; color: {C['text_secondary']}; }}

QPushButton[role="outline"] {{ background: {C['bg_input']}; border-color: {C['accent_primary']}; }}
QPushButton[role="outline"]:hover {{ background: {C['accent_primary']}; }}
QPushButton[role="outline"]:disabled {{ border-color: #3E3A80; }}
QPushButton[role="outline"][accent="secondary"] {{ border-color: {C['accent_secondary']}; }}
QPushButton[role="outline"][accent="secondary"]:hover {{ background: {C['accent_secondary']}; }}

QPushButton[role="stop"] {{ background: {C['bg_input']}; border-color: {C['error']}; }}
QPushButton[role="stop"]:hover {{ background: {C['error']}; }}
QPushButton[role="stop"]:disabled {{ border-color: #6B2238; }}

QPushButton[role="danger"] {{ background: {DANGER}; color: #FFFFFF; }}
QPushButton[role="danger"]:hover {{ background: {C['error']}; }}
QPushButton[role="danger"]:disabled {{ background: #5C1212; color: {C['text_secondary']}; }}

QPushButton[role="success"] {{ background: {C['success']}; color: {C['bg_dark']}; }}
QPushButton[role="success"]:hover {{ background: {SUCCESS_HOVER}; }}
QPushButton[role="success"]:disabled {{ background: #0B5C43; color: {C['text_muted']}; }}

QPushButton[role="info"] {{ background: {INFO}; color: #FFFFFF; }}
QPushButton[role="info"]:hover {{ background: {INFO_HOVER}; }}
QPushButton[role="info"]:disabled {{ background: #1F4F70; color: {C['text_muted']}; }}

QPushButton[role="ghost"] {{
    background: transparent;
    border: 1px solid {C['border']};
}}
QPushButton[role="ghost"]:hover {{ background: {C['bg_input']}; }}
QPushButton[role="ghost"][small="true"] {{
    {_px('body_small')}
    min-height: 24px;
    border-radius: 7px;
    padding: 0 8px;
}}

QPushButton[role="icon"] {{
    background: transparent;
    border: 1px solid {C['border']};
    border-radius: 8px;
    min-height: 26px; max-height: 26px;
    min-width: 30px; max-width: 30px;
    padding: 0;
}}
QPushButton[role="icon"]:hover {{ background: {C['bg_input']}; }}
QPushButton[role="icon"][recording="true"] {{ background: {DANGER}; border-color: {DANGER}; }}
QPushButton[role="icon"][recording="true"]:hover {{ background: {C['error']}; }}
QPushButton::menu-indicator {{ width: 0; image: none; }}

QProgressBar {{
    background: {C['slider_track']};
    border: none;
    border-radius: 3px;
    max-height: 6px; min-height: 6px;
}}
QProgressBar::chunk {{ background: {C['accent_primary']}; border-radius: 3px; }}

QSlider::groove:horizontal {{
    height: 4px; background: {C['slider_track']}; border-radius: 2px;
}}
QSlider::sub-page:horizontal {{ background: {C['slider_fill']}; border-radius: 2px; }}
QSlider::handle:horizontal {{
    background: {C['accent_primary']}; width: 14px; height: 14px;
    margin: -5px 0; border-radius: 7px;
}}
QSlider::handle:horizontal:hover {{ background: {C['accent_secondary']}; }}

QScrollArea, QScrollArea > QWidget > QWidget {{ background: transparent; border: none; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; }}
QScrollBar::handle:vertical {{ background: {C['border']}; border-radius: 5px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: {C['accent_primary']}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

QMenu {{
    background: {C['bg_card']};
    border: 1px solid {C['border']};
    padding: 4px;
}}
QMenu::item {{ padding: 6px 18px; border-radius: 6px; }}
QMenu::item:selected {{ background: {C['accent_primary']}; color: #FFFFFF; }}
QMenu::item:disabled {{ color: {C['text_muted']}; }}

QToolTip {{
    background: {C['bg_card']};
    color: {C['text_primary']};
    border: 1px solid {C['border']};
    padding: 6px;
    {_px('body_small')}
}}
QMessageBox QPushButton {{
    background: transparent; border: 1px solid {C['border']};
    min-width: 80px; min-height: 28px; border-radius: 8px;
}}
QMessageBox QPushButton:hover {{ background: {C['bg_input']}; }}
QMessageBox QPushButton:default {{ background: {C['accent_primary']}; border-color: {C['accent_primary']}; }}
"""


def card(title=None):
    """A rounded card frame with an optional title; returns (frame, layout)."""
    frame = QFrame()
    frame.setObjectName("card")
    lay = QVBoxLayout(frame)
    lay.setContentsMargins(16, 12, 16, 12)
    lay.setSpacing(8)
    if title:
        lab = QLabel(title)
        lab.setObjectName("cardTitle")
        lay.addWidget(lab)
    return frame, lay


def button(text, role, tip=None, small=False, **props):
    """A QPushButton with a theme role (and optional tooltip / extra style properties).

    "&" is shown as is (Qt would read it as a keyboard shortcut marker)."""
    b = QPushButton(text.replace("&", "&&"))
    b.setProperty("role", role)
    if small:
        b.setProperty("small", True)
    for k, v in props.items():
        b.setProperty(k, v)
    if tip:
        b.setToolTip(tip)
    b.setCursor(Qt.PointingHandCursor)
    return b


def icon_font(widget, family, px, bold=False):
    """A font for one widget that the style sheet's font rules cannot override."""
    widget.setStyleSheet(f"font-family: '{family}'; font-size: {px}px;"
                         + (" font-weight: bold;" if bold else ""))


def label(text="", *, muted=False, secondary=False, small=False):
    lab = QLabel(text)
    if muted:
        lab.setProperty("muted", True)
    if secondary:
        lab.setProperty("secondary", True)
    if small:
        lab.setProperty("small", True)
    return lab


def set_color(widget, color):
    """Text color for one label (status lines whose color changes)."""
    widget.setStyleSheet(f"color: {color}; background: transparent;")


def repolish(widget):
    """Re-apply the style sheet after a style property changed."""
    widget.style().unpolish(widget)
    widget.style().polish(widget)
