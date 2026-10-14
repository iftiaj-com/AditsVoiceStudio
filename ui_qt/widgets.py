"""
ui_qt/widgets.py
────────────────
Small widgets shared by the Qt panels.
"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox, QSlider, QStyle, QStyleOptionSlider


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
