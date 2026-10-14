"""
ui/widgets.py
────────────────
Small widgets shared by the Qt panels.
"""
from PySide6.QtCore import QEvent, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFontMetrics, QPainter, QPainterPath, QPalette
from PySide6.QtWidgets import (QAbstractButton, QButtonGroup, QHBoxLayout, QLabel, QMessageBox,
                               QPushButton, QSizePolicy, QSlider, QStyle, QStyleOptionSlider, QWidget)

from core.constants import COLORS
from ui import icons

ICON_GAP = 6


class IconLabel(QLabel):
    """A QLabel that shows a leading emoji as a line icon (ui.icons).

    setText("⚠  Could not play") draws the warning icon, then "Could not play".
    The icon takes the label's current text color, so a status that turns red
    gets a red icon. Every other QLabel feature (word wrap, alignment, style
    sheet colors) works as usual.
    """

    def __init__(self, text="", parent=None):
        super().__init__(parent)
        self._icon = None
        self._icon_color = None         # None: the text color
        self.setText(text)

    def set_icon_color(self, color):
        self._icon_color = QColor(color) if color else None
        self.update()

    def setText(self, text):
        self._icon, plain = icons.split(text)
        self._apply_margin()
        super().setText(plain)

    def icon_name(self):
        return self._icon

    def set_icon_name(self, name):
        """Show *name* as the icon (after setText, which picks the icon from the text)."""
        self._icon = name
        self._apply_margin()
        self.update()

    def _icon_px(self):
        return max(12, QFontMetrics(self.font()).height() - 3)

    def _apply_margin(self):
        m = self.contentsMargins()
        left = (self._icon_px() + ICON_GAP) if self._icon else 0
        if m.left() != left:
            self.setContentsMargins(left, m.top(), m.right(), m.bottom())

    def changeEvent(self, e):
        if e.type() in (QEvent.FontChange, QEvent.StyleChange):
            self._apply_margin()
        super().changeEvent(e)

    def paintEvent(self, e):
        super().paintEvent(e)
        if not self._icon:
            return
        size = self._icon_px()
        fm = QFontMetrics(self.font())
        cr = self.contentsRect()
        # Centre the icon on the first line of text, wherever the label's
        # alignment and wrapping put that line (a wrapped one-line label in a
        # tall row is centred too, so cr.top() would be too high).
        flags = int(self.alignment()) | (int(Qt.TextWordWrap) if self.wordWrap() else 0)
        first = fm.boundingRect(cr, flags, self.text()).top() if self.text() else cr.top()
        y = first + (fm.height() - size) / 2
        color = self._icon_color or self.palette().color(QPalette.WindowText)
        p = QPainter(self)
        p.drawPixmap(int(round(cr.left() - size - ICON_GAP)), int(round(y)),
                     icons.pixmap(self._icon, color, size))


class IconButton(QPushButton):
    """A QPushButton that shows a leading emoji as a line icon (ui.icons).

    Works for later setText() calls too ("▶  Play" -> "■  Stop"). The icon is
    drawn in the button's text color, and muted when the button is disabled.
    """

    def __init__(self, text="", parent=None):
        super().__init__(parent)
        self._icon_name = None
        self.setText(text)

    def setText(self, text):
        self._icon_name, plain = icons.split(text)
        plain = plain.replace("&", "&&")
        # A space between the icon and the words; the style's own gap is too tight.
        super().setText(" " + plain if self._icon_name and plain else plain)
        self._refresh_icon()

    def text(self):
        t = super().text()
        return (t[1:] if self._icon_name and t.startswith(" ") else t).replace("&&", "&")

    def set_icon_name(self, name):
        self._icon_name = name
        self._refresh_icon()

    def icon_name(self):
        return self._icon_name

    def _refresh_icon(self):
        if not self._icon_name:
            self.setIcon(icons.QIcon())
            return
        size = max(14, QFontMetrics(self.font()).height() - 2)
        # The active color: a button built disabled must not keep a muted icon
        # once enabled (icons.icon() makes the disabled look itself).
        color = self.palette().color(QPalette.Active, QPalette.ButtonText)
        self.setIcon(icons.icon(self._icon_name, color, size))
        self.setIconSize(QSize(size, size))

    def changeEvent(self, e):
        if e.type() in (QEvent.StyleChange, QEvent.PaletteChange, QEvent.FontChange):
            self._refresh_icon()
        super().changeEvent(e)


class ProgressButton(IconButton):
    """An IconButton that can show progress as a fill growing from the left.

    set_progress(0.4) fills 40% of the button with a translucent *fill_color*
    over the normal look, so the text and icon stay readable. None hides it.
    """
    RADIUS = 10                         # QPushButton border-radius in theme.stylesheet()

    def __init__(self, text="", parent=None, fill_color="#FFFFFF", fill_alpha=46):
        self._progress = None
        self._fill = QColor(fill_color)
        self._fill.setAlpha(fill_alpha)
        super().__init__(text, parent)

    def set_fill_color(self, color, alpha=46):
        self._fill = QColor(color)
        self._fill.setAlpha(alpha)
        self.update()

    def progress(self):
        return self._progress

    def set_progress(self, frac):
        frac = None if frac is None else max(0.0, min(1.0, float(frac)))
        if frac != self._progress:
            self._progress = frac
            self.update()

    def paintEvent(self, e):
        super().paintEvent(e)
        if not self._progress:
            return
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        clip = QPainterPath()
        clip.addRoundedRect(r, self.RADIUS - 1, self.RADIUS - 1)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setClipPath(clip)
        p.fillRect(QRectF(r.left(), r.top(), r.width() * self._progress, r.height()), self._fill)


class ToggleSwitch(QAbstractButton):
    """An on/off switch drawn like CustomTkinter's CTkSwitch: a rounded track,
    a round knob, and the text to its right (cut with "…" when it does not fit).

    on_color: the track color when on (CTk uses switch_on for effect tiles and
    the success green for option switches). Emits toggled(bool) like a QCheckBox.
    """
    TRACK_W, TRACK_H, GAP = 36, 18, 10

    def __init__(self, text="", checked=False, on_color=None, parent=None, icon=None):
        super().__init__(parent)
        lead, text = icons.split(text)
        self._icon = icon or lead       # line icon between the switch and the text
        self.setText(text)
        self.setCheckable(True)
        self.setChecked(checked)
        self.setCursor(Qt.PointingHandCursor)
        self._on = QColor(on_color or COLORS["switch_on"])
        self._off = QColor(COLORS["switch_off"])
        self._knob = QColor(COLORS["accent_primary"])
        self._knob_hover = QColor(COLORS["accent_secondary"])
        self._text_color = QColor(COLORS["text_primary"])
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)

    def set_text_color(self, color):
        self._text_color = QColor(color)
        self.update()

    def set_icon_name(self, name):
        self._icon = name
        self.updateGeometry()
        self.update()

    def _icon_px(self):
        return max(14, QFontMetrics(self.font()).height() - 2)

    def sizeHint(self):
        fm = QFontMetrics(self.font())
        text_w = fm.horizontalAdvance(self.text()) + self.GAP if self.text() else 0
        icon_w = self._icon_px() + ICON_GAP if self._icon else 0
        return QSize(self.TRACK_W + icon_w + text_w + 2, max(self.TRACK_H + 6, fm.height() + 4))

    def minimumSizeHint(self):
        return QSize(self.TRACK_W + 2, self.TRACK_H + 6)

    def hitButton(self, pos):
        return self.rect().contains(pos)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        h, w = self.TRACK_H, self.TRACK_W
        y = (self.height() - h) / 2
        enabled = self.isEnabled()
        track = self._on if self.isChecked() else self._off
        if not enabled:
            track = track.darker(160)
        p.setPen(Qt.NoPen)
        p.setBrush(track)
        p.drawRoundedRect(QRectF(0, y, w, h), h / 2, h / 2)
        k = 2
        x0 = w - h if self.isChecked() else 0
        knob = self._knob_hover if (self.underMouse() and enabled) else self._knob
        p.setBrush(knob if enabled else knob.darker(160))
        p.drawEllipse(QRectF(x0 + k, y + k, h - 2 * k, h - 2 * k))
        color = self._text_color if enabled else QColor(COLORS["text_muted"])
        left = w + self.GAP
        if self._icon:
            size = self._icon_px()
            p.drawPixmap(int(left), int(round((self.height() - size) / 2)),
                         icons.pixmap(self._icon, color, size))
            left += size + ICON_GAP
        if self.text():
            p.setPen(color)
            p.setFont(self.font())
            fm = QFontMetrics(self.font())
            text = fm.elidedText(self.text(), Qt.ElideRight, max(0, self.width() - left))
            p.drawText(QRectF(left, 0, self.width() - left, self.height()),
                       Qt.AlignVCenter | Qt.AlignLeft, text)

    def enterEvent(self, e):
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self.update()
        super().leaveEvent(e)


class ValueSlider(QSlider):
    """A horizontal slider over a float range (QSlider only knows ints).

    step: the smallest change (0.01 for speeds, 1 for whole numbers).
    valueChangedF(float) fires while it moves, like the CTk slider's command.
    """
    valueChangedF = Signal(float)

    def __init__(self, lo, hi, value, step=0.01, width=None, parent=None):
        super().__init__(Qt.Horizontal, parent)
        self._lo, self._step = float(lo), float(step)
        self.setRange(0, int(round((hi - lo) / step)))
        self.set_value(value)
        self.setCursor(Qt.PointingHandCursor)
        if width:
            self.setFixedWidth(width)
        self.valueChanged.connect(lambda _v: self.valueChangedF.emit(self.value_f()))

    def set_range(self, lo, hi, step):
        """New range and step (the handle keeps its value where it fits)."""
        v = self.value_f()
        self.blockSignals(True)
        self._lo, self._step = float(lo), float(step)
        self.setRange(0, int(round((hi - lo) / step)))
        self.blockSignals(False)
        self.set_value(max(lo, min(hi, v)))

    def value_f(self) -> float:
        v = self._lo + self.value() * self._step
        return float(round(v)) if self._step >= 1 else round(v, 6)

    def set_value(self, v, emit=False):
        """Move the handle to v (clamped); emit=False keeps valueChangedF quiet."""
        self.blockSignals(not emit)
        self.setValue(int(round((float(v) - self._lo) / self._step)))
        self.blockSignals(False)


class ElidedLabel(QLabel):
    """A one-line label that cuts its text with "…" at the end instead of
    widening its column (CTk descriptions clip at the end, anchor "w")."""

    def __init__(self, text="", parent=None):
        super().__init__(parent)
        self._full = text
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setText(text)

    def setText(self, text):
        self._full = text
        self.setToolTip(text if text else "")
        self._elide()

    def full_text(self):
        return self._full

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._elide()

    def _elide(self):
        fm = QFontMetrics(self.font())
        super().setText(fm.elidedText(self._full, Qt.ElideRight, max(10, self.contentsRect().width())))


class SeekSlider(QSlider):
    """Horizontal seek bar in milliseconds, like the CTk players' sliders:
    sliderPressed / sliderMoved while dragging, sliderReleased to seek.
    A click on the groove jumps there (Qt's default moves one page)."""

    def __init__(self, parent=None):
        super().__init__(Qt.Horizontal, parent)
        self.setRange(0, 1000)
        self.setCursor(Qt.PointingHandCursor)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            opt = QStyleOptionSlider()
            self.initStyleOption(opt)
            handle = self.style().subControlRect(QStyle.CC_Slider, opt, QStyle.SC_SliderHandle, self)
            if not handle.contains(event.position().toPoint()):
                groove = self.style().subControlRect(QStyle.CC_Slider, opt, QStyle.SC_SliderGroove, self)
                span = max(1, groove.width() - handle.width())
                x = int(event.position().x()) - groove.x() - handle.width() // 2
                self.setValue(QStyle.sliderValueFromPosition(self.minimum(), self.maximum(), x, span))
        super().mousePressEvent(event)  # now on the handle: starts a drag (sliderPressed)

    def seconds(self) -> float:
        return self.value() / 1000.0

    def set_seconds(self, s):
        self.blockSignals(True)
        self.setValue(int(round(s * 1000)))
        self.blockSignals(False)

    def set_length(self, s):
        self.setMaximum(max(100, int(round(s * 1000))))


def ask_choice(parent, title, message, choices, default=None):
    """A modal question with one button per (label, value); returns the value.

    Esc and the close box give None. Enter picks *default*.
    """
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setText(message)
    box.setIcon(QMessageBox.NoIcon)
    buttons = {}
    for text, value in choices:
        role = QMessageBox.RejectRole if value is None else QMessageBox.AcceptRole
        b = box.addButton(text, role)
        buttons[b] = value
        if value == default:
            box.setDefaultButton(b)
        if value is None:
            box.setEscapeButton(b)
    box.exec()
    return buttons.get(box.clickedButton())


class Segmented(QWidget):
    """A row of joined buttons, one of which is selected: the Qt counterpart of
    CTkSegmentedButton (Active / Create voice, Mode, trim presets, gender).

    changed(str) fires when the user (or set_value) picks another value.
    """
    changed = Signal(str)

    def __init__(self, values, value=None, height=26, parent=None):
        super().__init__(parent)
        self._row = QHBoxLayout(self)
        self._row.setContentsMargins(0, 0, 0, 0)
        self._row.setSpacing(0)
        self._height = height
        self._buttons = {}
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._group.buttonClicked.connect(lambda b: self._picked(b.text()))
        self._value = None
        self.set_values(values, value)

    def _picked(self, value):
        if value != self._value:
            self._value = value
            self.changed.emit(value)

    def set_values(self, values, value=None):
        """Replace the buttons; keeps the selection when it is still offered."""
        for b in list(self._buttons.values()):
            self._group.removeButton(b)
            b.setParent(None)
            b.deleteLater()
        self._buttons = {}
        values = list(values)
        for i, v in enumerate(values):
            b = QPushButton(v)
            b.setCheckable(True)
            b.setAutoDefault(False)
            b.setCursor(Qt.PointingHandCursor)
            # The height goes in the button's own style sheet: the app sheet's
            # min-height would otherwise override setFixedHeight() when the
            # button is polished (the tabs came out 2 px tall at start).
            h = self._height - 2                    # minus the 1 px border, top and bottom
            b.setStyleSheet(f"QPushButton {{ min-height: {h}px; max-height: {h}px; }}")
            b.setProperty("role", "seg")
            b.setProperty("pos", "only" if len(values) == 1 else
                          "first" if i == 0 else "last" if i == len(values) - 1 else "mid")
            self._group.addButton(b)
            self._row.addWidget(b, 1)
            self._buttons[v] = b
        keep = value if value in self._buttons else (self._value if self._value in self._buttons else
                                                     (values[0] if values else None))
        self._value = None
        if keep is not None:
            self._buttons[keep].setChecked(True)
            self._value = keep

    def values(self):
        return list(self._buttons)

    def value(self):
        return self._value

    def set_value(self, value, emit=True):
        if value in self._buttons and value != self._value:
            self._buttons[value].setChecked(True)
            self._value = value
            if emit:
                self.changed.emit(value)

    def set_tips(self, tips):
        for v, text in tips.items():
            if v in self._buttons:
                self._buttons[v].setToolTip(text)
