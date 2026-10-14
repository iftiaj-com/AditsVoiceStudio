"""
ui_qt/panels/playback_panel.py  –  Playback & Export card (Qt)

    🎧 Playback & Export
    [ ⚡ Generate Speech                          ] [🎧 Preview]
    [ ▶ Play               ] [ 💾 Save ▾          ] [📂 Folder ]

Generate is the only filled (primary) button. While a generation runs it
turns into Cancel, and the progress fills it from the left; there is no
separate progress bar. Play and Stop are one toggle, and the playback
position fills it the same way. Save opens a menu: MP3, WAV or SRT. Items
that have nothing to save are greyed out.

Until the panels are built (MainWindow finds the engines in the background,
about 2 s), Generate reads "Starting…" and is disabled.
"""
from PySide6.QtCore import QPoint
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import QHBoxLayout, QMenu, QSizePolicy

from core.constants import COLORS
from ui_qt import theme
from ui_qt.widgets import ProgressButton

GENERATE_TEXT = "⚡  Generate Speech"
LOADING_TEXT = "⌛  Starting…"
PLAY_TEXT = "▶  Play"
STOP_TEXT = "■  Stop"


def _fix_width(btn, *texts):
    """Minimum width that fits the widest of *texts*; ignore the changing size hint."""
    # The style sheet sets the font when the button is first shown, so measure
    # with the theme font rather than btn.fontMetrics().
    fm = QFontMetrics(theme.font("button"))
    widest = max(fm.horizontalAdvance(" " + t) for t in (btn.text(),) + texts)
    icon_w = fm.height() + 2 if btn.icon_name() else 0     # IconButton's icon, about one line tall
    btn.setMinimumWidth(widest + icon_w + 28)       # padding and border
    btn.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)


def _set_role(btn, role):
    if btn.property("role") != role:
        btn.setProperty("role", role)
        theme.repolish(btn)


class PlaybackPanel:
    def __init__(self, on_generate, on_cancel, on_play, on_stop, on_save_audio, on_save_srt,
                 on_open_folder, on_preview=None):
        """on_save_audio(fmt) is called with "mp3" or "wav"."""
        self._on_generate = on_generate
        self._on_cancel = on_cancel
        self._on_play = on_play
        self._on_stop = on_stop
        self._generating = False
        self._playing = False
        self._loading = False

        self.frame, lay = theme.card("🎧  Playback & Export")

        row1 = QHBoxLayout()
        row1.setSpacing(6)
        self.generate_btn = theme.button(GENERATE_TEXT, theme.ROLE_PRIMARY, cls=ProgressButton)
        self.generate_btn.clicked.connect(self._main_clicked)
        _fix_width(self.generate_btn, LOADING_TEXT, "✖  Cancel preview  100%", "⌛  Cancelling…")
        self.generate_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        row1.addWidget(self.generate_btn, 1)
        self.preview_btn = None
        if on_preview is not None:
            self.preview_btn = theme.button("🎧  Preview", theme.ROLE_OUTLINE, accent="secondary",
                                            tip="Speak only the first sentence, then play it")
            self.preview_btn.setFixedWidth(118)
            self.preview_btn.clicked.connect(on_preview)
            row1.addWidget(self.preview_btn)
        lay.addLayout(row1)

        row2 = QHBoxLayout()
        row2.setSpacing(6)
        self.play_btn = theme.button(PLAY_TEXT, theme.ROLE_OUTLINE, cls=ProgressButton)
        self.play_btn.clicked.connect(self._play_clicked)
        _fix_width(self.play_btn, STOP_TEXT)

        self.save_btn = theme.button("💾  Save  ▾", theme.ROLE_OUTLINE,
                                     tip="Save the audio as MP3 or WAV, or the subtitles as SRT")
        _fix_width(self.save_btn)
        self.save_menu = QMenu(self.save_btn)
        self.save_mp3_act = self.save_menu.addAction("MP3 audio (.mp3)")
        self.save_wav_act = self.save_menu.addAction("WAV audio (.wav)")
        self.save_menu.addSeparator()
        self.save_srt_act = self.save_menu.addAction("Subtitles (.srt)")
        self.save_mp3_act.triggered.connect(lambda: on_save_audio("mp3"))
        self.save_wav_act.triggered.connect(lambda: on_save_audio("wav"))
        self.save_srt_act.triggered.connect(on_save_srt)
        self.save_btn.clicked.connect(self._show_save_menu)

        self.open_folder_btn = theme.button("📂  Folder", theme.ROLE_GHOST,
                                            tip="Open the folder of the last saved file, or the save folder")
        _fix_width(self.open_folder_btn)
        self.open_folder_btn.clicked.connect(on_open_folder)

        row2.addWidget(self.play_btn, 1)
        row2.addWidget(self.save_btn, 1)
        row2.addWidget(self.open_folder_btn)
        self.open_folder_btn.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.open_folder_btn.setFixedWidth(self.open_folder_btn.minimumWidth())
        lay.addLayout(row2)

        self.play_btn.setEnabled(False)
        self.set_outputs(mp3=False, wav=False, srt=False)

    # ── clicks ─────────────────────────────────────────────────────
    def _main_clicked(self):
        (self._on_cancel if self._generating else self._on_generate)()

    def _play_clicked(self):
        (self._on_stop if self._playing else self._on_play)()

    def _show_save_menu(self):
        self.save_menu.setMinimumWidth(self.save_btn.width())
        self.save_menu.exec(self.save_btn.mapToGlobal(QPoint(0, self.save_btn.height() + 2)))

    # ── states ─────────────────────────────────────────────────────
    def set_loading(self, loading: bool):
        """Before the engines are ready: Generate and Preview wait."""
        self._loading = loading
        if self._generating:
            return
        self.generate_btn.setText(LOADING_TEXT if loading else GENERATE_TEXT)
        self.generate_btn.setEnabled(not loading)
        if self.preview_btn is not None:
            self.preview_btn.setEnabled(not loading)

    def set_generating(self, is_generating: bool, preview=False):
        """Generate becomes Cancel (with the progress inside) while a run is on."""
        self._generating = is_generating
        self._preview = preview
        btn = self.generate_btn
        if is_generating:
            _set_role(btn, theme.ROLE_STOP)
            btn.set_fill_color(COLORS["error"], 70)
            btn.set_progress(0)
            btn.setEnabled(True)
            self._cancel_text(0)
            self.set_playing(False)
            for b in (self.play_btn, self.save_btn, self.preview_btn):
                if b is not None:
                    b.setEnabled(False)
        else:
            _set_role(btn, theme.ROLE_PRIMARY)
            btn.set_progress(None)
            btn.setText(LOADING_TEXT if self._loading else GENERATE_TEXT)
            btn.setEnabled(not self._loading)
            if self.preview_btn is not None:
                self.preview_btn.setEnabled(not self._loading)

    def _cancel_text(self, frac):
        what = "Cancel preview" if self._preview else "Cancel"
        self.generate_btn.setText(f"✖  {what}  {round(frac * 100)}%")

    def set_cancelling(self):
        self.generate_btn.setText("⌛  Cancelling…")
        self.generate_btn.setEnabled(False)

    def set_progress(self, frac):
        """Generation progress, 0 to 1, shown inside the Cancel button."""
        if not self._generating or not self.generate_btn.isEnabled():
            return
        frac = max(0.0, min(1.0, float(frac)))
        self.generate_btn.set_progress(frac)
        self._cancel_text(frac)

    def set_playing(self, playing: bool):
        """Play ↔ Stop. The playback position fills the button while it plays."""
        self._playing = playing
        btn = self.play_btn
        _set_role(btn, theme.ROLE_STOP if playing else theme.ROLE_OUTLINE)
        btn.setText(STOP_TEXT if playing else PLAY_TEXT)
        btn.set_fill_color(COLORS["error"] if playing else COLORS["accent_primary"], 70)
        btn.set_progress(0 if playing else None)

    def set_play_progress(self, frac):
        if self._playing:
            self.play_btn.set_progress(frac)

    def set_outputs(self, mp3, wav, srt):
        """Which Save menu items have something to save."""
        self.save_mp3_act.setEnabled(bool(mp3))
        self.save_wav_act.setEnabled(bool(wav))
        self.save_srt_act.setEnabled(bool(srt))
        self.save_btn.setEnabled(bool(mp3 or wav or srt))
