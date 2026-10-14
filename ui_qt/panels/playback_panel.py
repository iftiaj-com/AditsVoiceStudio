"""
ui_qt/panels/playback_panel.py  –  Playback & Export card (Qt)

    🎧 Playback & Export
    ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━  progress
    [ ⚡ Generate Speech                         ] [🎧 Preview]
    [▶ Play] [⏹ Stop] [💾 Save MP3] [📄 Save SRT] [📂 Folder] [✖ Cancel]

Same buttons and starting states as ui/panels/playback_panel.py. The second
row keeps the 3:3:4:4:3:3 width ratio. Each button's minimum width fits the
longest text it ever shows, and its size hint is ignored, so a text change
("Play" -> "Playing…") cannot resize or clip its neighbours.

When the card is too narrow for all six at that width (the CTk version then
clips their text), the row becomes two rows of three:

    [▶ Play]     [⏹ Stop]   [💾 Save MP3]
    [📄 Save SRT] [📂 Folder] [✖ Cancel]
"""
from PySide6.QtCore import QEvent, QObject
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QProgressBar, QSizePolicy

from ui_qt import theme

PROGRESS_STEPS = 1000


def _fix_width(btn, *texts):
    """Minimum width that fits the widest of *texts*; ignore the changing size hint."""
    # The style sheet sets the font when the button is first shown, so measure
    # with the theme font rather than btn.fontMetrics().
    fm = QFontMetrics(theme.font("button"))
    widest = max(fm.horizontalAdvance(t) for t in (btn.text(),) + texts)
    btn.setMinimumWidth(widest + 28)                # padding and border
    btn.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)


class _OnResize(QObject):
    def __init__(self, fn):
        super().__init__()
        self._fn = fn

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Resize:
            self._fn()
        return False


class PlaybackPanel:
    def __init__(self, on_generate, on_cancel, on_play, on_stop, on_save, on_save_srt,
                 on_open_folder, on_preview=None):
        self.frame, lay = theme.card("🎧  Playback & Export")

        self.progress = QProgressBar()
        self.progress.setRange(0, PROGRESS_STEPS)
        self.progress.setTextVisible(False)
        lay.addWidget(self.progress)

        row1 = QHBoxLayout()
        row1.setSpacing(6)
        self.generate_btn = theme.button("⚡  Generate Speech", theme.ROLE_PRIMARY)
        self.generate_btn.clicked.connect(on_generate)
        row1.addWidget(self.generate_btn, 1)
        self.preview_btn = None
        if on_preview is not None:
            self.preview_btn = theme.button("🎧  Preview", theme.ROLE_OUTLINE, accent="secondary")
            self.preview_btn.setFixedWidth(110)
            self.preview_btn.clicked.connect(on_preview)
            row1.addWidget(self.preview_btn)
        lay.addLayout(row1)

        row2 = QGridLayout()
        row2.setHorizontalSpacing(4)
        row2.setVerticalSpacing(6)
        self._row2 = row2
        self.play_btn = theme.button("▶  Play", theme.ROLE_OUTLINE)
        self.stop_btn = theme.button("⏹  Stop", theme.ROLE_STOP)
        self.save_btn = theme.button("💾  Save MP3", theme.ROLE_SUCCESS)
        self.save_srt_btn = theme.button("📄  Save SRT", theme.ROLE_INFO)
        self.open_folder_btn = theme.button("📂  Folder", theme.ROLE_GHOST)
        self.cancel_btn = theme.button("✖  Cancel", theme.ROLE_DANGER)
        _fix_width(self.play_btn, "▶  Playing…")
        _fix_width(self.cancel_btn, "Cancelling…")
        for b in (self.stop_btn, self.save_btn, self.save_srt_btn, self.open_folder_btn):
            _fix_width(b)
        _fix_width(self.generate_btn, "Generating…", "Previewing…")
        self.generate_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        buttons = [(self.play_btn, on_play), (self.stop_btn, on_stop), (self.save_btn, on_save),
                   (self.save_srt_btn, on_save_srt), (self.open_folder_btn, on_open_folder),
                   (self.cancel_btn, on_cancel)]
        self._row2_btns = [b for b, _ in buttons]
        for btn, cb in buttons:
            btn.clicked.connect(cb)
            btn.setEnabled(False)
        self._one_row_px = (sum(b.minimumWidth() for b in self._row2_btns)
                            + row2.horizontalSpacing() * (len(buttons) - 1))
        self._cols = None
        self._place_row2(6)
        lay.addLayout(row2)
        self._resize_filter = _OnResize(self._relayout)
        self.frame.installEventFilter(self._resize_filter)

    def _relayout(self):
        m = self.frame.layout().contentsMargins()
        width = self.frame.width() - m.left() - m.right()
        self._place_row2(6 if width >= self._one_row_px else 3)

    def _place_row2(self, cols):
        """Six buttons in one row (3:3:4:4:3:3), or two rows of three (equal widths)."""
        if cols == self._cols:
            return
        self._cols = cols
        g = self._row2
        for b in self._row2_btns:
            g.removeWidget(b)
        for c in range(6):
            g.setColumnStretch(c, 0)
        for i, b in enumerate(self._row2_btns):
            g.addWidget(b, i // cols, i % cols)
        stretches = (3, 3, 4, 4, 3, 3) if cols == 6 else (1, 1, 1)
        for c, st in enumerate(stretches):
            g.setColumnStretch(c, st)

    def set_progress(self, frac):
        self.progress.setValue(round(max(0.0, min(1.0, float(frac))) * PROGRESS_STEPS))

    def set_generating(self, is_generating: bool):
        """Toggle Cancel during generation."""
        self.cancel_btn.setEnabled(is_generating)
        self.cancel_btn.setText("✖  Cancel")
