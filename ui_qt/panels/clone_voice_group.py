"""
ui_qt/panels/clone_voice_group.py  –  "Clone Voice" box in the Customization card (Qt)

    🧬 Clone Voice                                   [status] [⬆ Update]
    [ Active voice | Create voice ]

    Active voice   ┌ Voice ──────────────┐ ┌ Sound ──────────────────┐
                   │ picker, base voice  │ │ sliders, ▶ Preview, ⏱   │
                   └─────────────────────┘ └─────────────────────────┘
    Create voice   ┌ New voice ──────────┐ ┌ Recordings / Model files ┐
                   └─────────────────────┘ └─────────────────────────┘
                   ┌ Clean-up ───────────┐ ┌ Training (RVC only) ─────┐
                   └─────────────────────┘ └─────────────────────────┘
                   ⏱ estimate                            [🧬 Create Voice]
    ▬▬▬▬▬▬▬▬▬▬ progress ▬▬▬▬▬▬▬▬▬▬   stage text            [✖ Cancel]

Same controls, rules and public methods as ui/panels/clone_voice_group.py
(docs/ui-parity-checklist.md, section 3). The jobs and rules themselves live
in core.voice_cloning.tasks and are shared with the CTk group; this file is
the Qt layout and wiring.

Tiles sit in two columns when the box is at least WIDE_PX wide and stack
below that; a tile alone in its row spans both columns. Long work runs on a
daemon thread and reaches the widgets only through ui_qt.bridge.ui().

Differences from the CTk group, both forced by Qt:
* The Active voice picker is a QComboBox (Qt's list scrolls, 12 rows); a ♥
  after the name marks favourites, drawn by a delegate on the right.
* Hover help uses Qt tooltips (the CTk _Tip shows after 450 ms; Qt's delay
  is the system's, about 700 ms on Windows).
"""

import os
import shutil
import threading
import time

from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtWidgets import (QComboBox, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QLabel,
                               QLineEdit, QMessageBox, QProgressBar, QCheckBox, QSizePolicy,
                               QStyledItemDelegate, QVBoxLayout, QWidget)

from core import voice_selection
from core.audio_io import AUDIO_EXTENSIONS
from core.constants import COLORS
from core.voice_cloning import library, previews, runtime, tasks
from core.voice_cloning.errors import VCCancelled, VCError
from ui_qt import icons, theme
from ui_qt.bridge import ui
from ui_qt.widgets import IconLabel, SeekSlider, Segmented, ToggleSwitch, ValueSlider

CLONE_GROUP = "clone"
KEY_PREFIX = "clone:"
HEART_OFF, HEART_ON = "♡", "♥"
MODES = tasks.MODES
MODE_HINTS = {
    "Quick": "No training. Uses up to 25 s of your clips and is ready in seconds.",
    "Train (RVC)": "Trains a voice model on your clips. Best with 3+ minutes of clean speech. "
                   "Cloned voices can't be used while training runs.",
    "Import model": "Use an existing RVC voice model (.pth), with its .index if you have it.",
}
SR_CHOICES = tasks.SR_CHOICES
TRIM_PRESETS = tasks.TRIM_PRESETS
WIDE_PX = 600                       # two tile columns from this box width up
CLIP_PLAY_CHANNEL = 6               # pygame channel of the Recordings clip player
PREVIEW_CHANNEL = 2                 # ▶ Preview, ▶ Original / ▶ Cleaned, Cloned Voices dialog
AUDIO_FILTER = ("Audio files (" + " ".join("*" + e for e in AUDIO_EXTENSIONS) + ");;All files (*.*)")


def _small(text="", color=None, wrap=False):
    lab = IconLabel(text)
    lab.setProperty("small", True)
    theme.set_color(lab, color or COLORS["text_secondary"])
    if wrap:
        lab.setWordWrap(True)
    return lab


def _btn(text, command, color, filled=False, height=30, small=False, tip=None):
    """The group's button: outlined in *color*, or filled with it (Create Voice, Install)."""
    if filled:
        b = theme.button(text, theme.ROLE_PRIMARY, tip)
        b.setStyleSheet(f"QPushButton {{ min-height: {height}px; max-height: {height}px; border-radius: 8px; }}")
    else:
        b = theme.line_button(text, color, tip, height=height, small=small)
    b.setAutoDefault(False)
    b.clicked.connect(command)
    return b


class _Tile(QFrame):
    """A sub-card of the Clone Voice box: optional title, then a grid body."""

    def __init__(self, title=None):
        super().__init__()
        self.setObjectName("cvTile")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 6, 10, 8)
        outer.setSpacing(4)
        self.head = QHBoxLayout()
        if title:
            t = QLabel(title)
            t.setObjectName("tileTitle")
            self.head.addWidget(t)
        self.head.addStretch(1)
        outer.addLayout(self.head)
        self.body = QGridLayout()
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.setHorizontalSpacing(6)
        self.body.setVerticalSpacing(4)
        self.body.setColumnStretch(1, 1)
        outer.addLayout(self.body)
        outer.addStretch(1)


class _SliderRow(QWidget):
    """Label · slider (stretches) · value, with an optional button after it."""

    def __init__(self, label, lo, hi, step, value, fmt, on_change, tip=None):
        super().__init__()
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 1, 0, 1)
        h.setSpacing(6)
        lab = _small(label)
        lab.setFixedWidth(64)
        if tip:
            lab.setToolTip(tip)
        h.addWidget(lab)
        self.slider = ValueSlider(lo, hi, value, step=step)
        self.slider.setMinimumWidth(80)
        h.addWidget(self.slider, 1)
        self.fmt = fmt
        self.value_label = _small(fmt(value), COLORS["text_muted"])
        self.value_label.setFixedWidth(52)
        self.value_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        h.addWidget(self.value_label)
        self._layout = h

        def changed(v):
            self.value_label.setText(fmt(v))
            on_change()
        self.slider.valueChangedF.connect(changed)

    def add(self, widget):
        self._layout.addWidget(widget)

    def get(self):
        return self.slider.value_f()

    def set(self, value):
        self.slider.set_value(value)
        self.value_label.setText(self.fmt(self.slider.value_f()))


class _HeartDelegate(QStyledItemDelegate):
    """The voice picker's list: a ♥ on the right of each favourite."""

    def __init__(self, is_favourite, parent=None):
        super().__init__(parent)
        self._is_favourite = is_favourite

    def paint(self, painter, option, index):
        super().paint(painter, option, index)
        if self._is_favourite(index.data()):
            size = 14
            r = option.rect
            painter.drawPixmap(r.right() - 8 - size, r.top() + (r.height() - size) // 2,
                               icons.pixmap("heart-filled", COLORS["accent_primary"], size))


class CloneVoiceGroup:
    def __init__(self, app):
        self.app = app
        self.effects = None             # set by attach()
        self._busy = False
        self._cancel = threading.Event()
        self._task_name = ""
        self._names = {}                # dropdown text -> slug
        self._active_slug = None
        self._active_profile = None
        self._clips = []                # [{"path", "seconds" (full length), "start", "end"}]
        self._cleanup_preview = None    # (original_wav, cleaned_wav) from the last cleanup preview
        self._pth = None
        self._index = None
        self._tmp_dirs = []
        self._preview_sound = None
        self._preview_playing = None    # 0 (▶ Original) or 1 (▶ Cleaned) while it plays
        self._active_sound = None       # the Active voice ▶ Preview while it plays (button shows ■ Stop)
        self._width = 0
        self._wide = True
        self._layout_key = None
        self._custom_key = None
        self._indent = 40
        self._match_result = None
        self._recorder = None
        self._analyzer = None
        # clip player
        self._pl_labels = {}
        self._pl_path = None
        self._pl_seg = None
        self._pl_sound = None
        self._pl_from = 0.0
        self._pl_t0 = None
        self._pl_seeking = False
        self._pl_loading = False

        self._save_timer = QTimer()
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(600)
        self._save_timer.timeout.connect(self._flush_params)
        self._pl_timer = QTimer()
        self._pl_timer.setInterval(100)
        self._pl_timer.timeout.connect(self._clip_tick)
        self._hide_timer = QTimer()
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(lambda: (not self._busy) and self._task.hide())

        box = QFrame()
        box.setObjectName("inset")
        self.box = box
        lay = QVBoxLayout(box)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(6)

        # header
        head = QHBoxLayout()
        title = theme.label("🧬  Clone Voice", secondary=True)
        head.addWidget(title)
        head.addStretch(1)
        self._update_btn = theme.line_button("⬆  Update", COLORS["warning"], height=24, small=True)
        self._update_btn.clicked.connect(self._on_update)
        self._update_btn.hide()
        head.addWidget(self._update_btn)
        self._status = _small("", COLORS["text_muted"])
        head.addWidget(self._status)
        lay.addLayout(head)

        # not-installed panel: text left, button right
        self._setup = QWidget()
        sl = QHBoxLayout(self._setup)
        sl.setContentsMargins(0, 0, 0, 0)
        self._setup_text = _small("", COLORS["text_muted"], wrap=True)
        sl.addWidget(self._setup_text, 1)
        self._install_btn = _btn("⬇  Install voice cloning", self._on_install, COLORS["accent_primary"], filled=True)
        self._install_btn.setFixedWidth(200)
        sl.addWidget(self._install_btn, 0, Qt.AlignTop)
        lay.addWidget(self._setup)

        # installed: tabs and pages
        self._tabs = Segmented(["Active voice", "Create voice"], "Active voice", height=28)
        self._tabs.changed.connect(self._on_tab)
        lay.addWidget(self._tabs)
        self._active_page = self._build_active_page()
        self._create_page = self._build_create_page()
        lay.addWidget(self._active_page)
        lay.addWidget(self._create_page)

        # shared task row
        self._task = QWidget()
        tg = QGridLayout(self._task)
        tg.setContentsMargins(0, 0, 0, 0)
        tg.setHorizontalSpacing(10)
        self._progress = QProgressBar()
        self._progress.setRange(0, 1000)
        self._progress.setTextVisible(False)
        tg.addWidget(self._progress, 0, 0)
        self._cancel_btn = theme.button("✖  Cancel", theme.ROLE_DANGER)
        self._cancel_btn.setFixedWidth(100)
        self._cancel_btn.setAutoDefault(False)
        self._cancel_btn.clicked.connect(self._on_cancel)
        tg.addWidget(self._cancel_btn, 0, 1, 2, 1)
        self._stage = _small("", COLORS["text_secondary"], wrap=True)
        tg.addWidget(self._stage, 1, 0)
        tg.setColumnStretch(0, 1)
        self._task.hide()
        self._box_l = lay
        lay.addWidget(self._task)
        self._dock_task()

        self._resize_filter = _ResizeWatch(self._on_resize)
        box.installEventFilter(self._resize_filter)
        self._tick_timer = QTimer()
        self._tick_timer.setInterval(2000)
        self._tick_timer.timeout.connect(self._tick)
        self._tick_timer.start()
        self.refresh()

    # ═══════════════════════════════════════════════════════════════
    #  Page builders
    # ═══════════════════════════════════════════════════════════════
    def _switch(self, text, command=None):
        s = ToggleSwitch(text, on_color=COLORS["success"])
        s.setFont(theme.font("body_small"))
        if command:
            s.toggled.connect(lambda _on: command())
        return s

    def _build_active_page(self):
        page = QWidget()
        self._active_grid = QGridLayout(page)
        self._active_grid.setContentsMargins(0, 0, 0, 0)
        self._active_grid.setHorizontalSpacing(8)
        self._active_grid.setVerticalSpacing(8)

        # ── Voice tile: picker, mode, base voice ──
        self._av_tile = _Tile("Voice")
        self._mode_tag = _small("", COLORS["text_muted"])
        self._av_tile.head.addWidget(self._mode_tag)
        b = self._av_tile.body
        pick = QHBoxLayout()
        self._voice_menu = QComboBox()
        self._voice_menu.setMaxVisibleItems(12)
        self._voice_menu.setMinimumWidth(160)
        self._voice_menu.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._voice_menu.setItemDelegate(_HeartDelegate(self._label_is_favourite, self._voice_menu))
        self._voice_menu.activated.connect(lambda i: self._on_voice_pick(self._voice_menu.itemText(i)))
        pick.addWidget(self._voice_menu, 1)
        self._fav_btn = theme.button(HEART_OFF, "heart", "Add this voice to Favourites in Voice Effects, or remove it.")
        self._fav_btn.setProperty("on", False)
        self._fav_btn.clicked.connect(self._on_fav_click)
        self._fav_btn.setEnabled(False)
        pick.addWidget(self._fav_btn)
        b.addLayout(pick, 1, 0, 1, 3)
        self._empty_hint = _small("Switch on a cloned voice here or in Voice Effects.", COLORS["text_muted"])
        b.addWidget(self._empty_hint, 2, 0, 1, 3)
        # Base voice: the engine voice this clone was made from.
        self._base_row = QWidget()
        br = QHBoxLayout(self._base_row)
        br.setContentsMargins(0, 6, 0, 0)
        lab = _small("Base")
        lab.setFixedWidth(40)
        br.addWidget(lab)
        self._base_value = _small("", COLORS["text_primary"])
        self._base_value.setToolTip("The TTS engine voice this clone was made from. Switching the clone "
                                    "on selects it again, with its speed.")
        self._base_value.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        br.addWidget(self._base_value, 1)
        self._use_current_btn = _btn("Use current", self._on_use_current_base, COLORS["accent_primary"],
                                     height=26, small=True,
                                     tip="Save the voice now selected in TTS Engine as this clone's base voice.")
        br.addWidget(self._use_current_btn)
        b.addWidget(self._base_row, 3, 0, 1, 3)

        # ── Sound tile: tuning, preview, time ──
        self._snd_tile = _Tile("Sound")
        s = self._snd_tile.body
        self._pitch_row = _SliderRow("Pitch", -24, 24, 1, 0, lambda v: f"{int(round(v)):+d} st",
                                     self._params_changed, "Shifts the cloned voice up or down, in semitones.")
        self._match_btn = _btn("🎯", self._on_match_pitch, COLORS["accent_primary"], height=24,
                               tip="Match pitch: measure the base voice and your clips, then set Pitch "
                                   "so they line up.")
        self._match_btn.setFixedWidth(30)
        self._pitch_row.add(self._match_btn)
        self._index_row = _SliderRow("Index", 0, 1, 0.01, 0.3, lambda v: f"{v:.2f}", self._params_changed,
                                     "Index rate: higher pulls the accent closer to the target voice.")
        self._protect_row = _SliderRow("Protect", 0, 0.5, 0.01, 0.33, lambda v: f"{v:.2f}", self._params_changed,
                                       "Keeps breaths and consonants clean. 0.33 suits most voices.")
        self._steps_row = _SliderRow("Quality", 4, 50, 1, 10, lambda v: f"{int(round(v))} steps",
                                     self._params_changed,
                                     "More steps sound smoother but take longer. 10 is a good balance on this GPU.")
        for r, row in enumerate((self._pitch_row, self._index_row, self._protect_row, self._steps_row), 1):
            s.addWidget(row, r, 0, 1, 3)
        # ▶ Preview row: moved into the Voice tile (RVC) or the Sound tile (Quick) by _layout().
        self._preview_row = QWidget()
        pr = QHBoxLayout(self._preview_row)
        pr.setContentsMargins(0, 8, 0, 0)
        self._preview_btn = _btn("▶  Preview", self._on_preview, COLORS["success"])
        self._preview_btn.setFixedWidth(104)
        pr.addWidget(self._preview_btn)
        self._active_est = _small("", COLORS["text_muted"], wrap=True)
        pr.addWidget(self._active_est, 1)
        return page

    def _build_create_page(self):
        page = QWidget()
        self._create_grid = QGridLayout(page)
        self._create_grid.setContentsMargins(0, 0, 0, 0)
        self._create_grid.setHorizontalSpacing(8)
        self._create_grid.setVerticalSpacing(8)

        # ── New voice tile: name, mode, base voice ──
        self._cv_tile = _Tile("New voice")
        b = self._cv_tile.body
        lab = _small("Name")
        lab.setFixedWidth(44)
        b.addWidget(lab, 1, 0)
        self._name_entry = QLineEdit()
        self._name_entry.setPlaceholderText("e.g. My narrator")
        self._name_entry.textChanged.connect(lambda _t: self._update_create())
        b.addWidget(self._name_entry, 1, 1)
        b.addWidget(_small("Mode"), 2, 0)
        self._mode_seg = Segmented(MODES, "Quick")
        self._mode_seg.set_tips(MODE_HINTS)
        self._mode_seg.changed.connect(lambda _v: self._update_create())
        b.addWidget(self._mode_seg, 2, 1)
        b.addWidget(_small("Base"), 3, 0)
        self._base_label = _small("", COLORS["text_primary"])
        self._base_label.setToolTip("The voice selected in TTS Engine. It is saved with the clone and "
                                    "selected again when you switch the clone on.")
        self._base_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        b.addWidget(self._base_label, 3, 1)

        # ── Recordings tile ──
        self._rec_tile = _Tile("Recordings")
        self._clips_label = _small("No clips", COLORS["text_muted"])
        self._rec_tile.head.addWidget(self._clips_label)
        r = self._rec_tile.body
        self._rec_btns = QGridLayout()
        self._rec_btns.setHorizontalSpacing(6)
        self._rec_btns.setVerticalSpacing(6)
        add = _btn("📥  Add clips", self._on_add_clips, COLORS["accent_primary"],
                   tip="Import recordings: WAV, MP3, M4A, FLAC, OGG, Opus, WebM and more.")
        self._record_btn = _btn("🎙  Record", self._on_record, COLORS["accent_secondary"],
                                tip="Record with your microphone, preview it, then add it.")
        self._analyze_btn = _btn("🔍  Analyze", self._on_analyze, COLORS["success"],
                                 tip="Find the English engine voice that clones best from these clips, "
                                     "and use it as the base voice.")
        self._analyze_btn.setEnabled(False)
        clear = _btn("✖  Clear", self._on_clear_clips, COLORS["error"])
        self._rec_btn_list = [add, self._record_btn, self._analyze_btn, clear]
        for x in self._rec_btn_list:
            x.setMinimumWidth(90)
            x.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        r.addLayout(self._rec_btns, 1, 0, 1, 2)
        self._match_gender_seg = Segmented(["Auto", "Male", "Female"], "Auto", height=30)
        self._match_gender_seg.set_tips({
            "Auto": "Analyze decides from the clip whether to compare male or female voices.",
            "Male": "Analyze compares the clip with male engine voices only.",
            "Female": "Analyze compares the clip with female engine voices only."})
        r.addWidget(self._match_gender_seg, 2, 0, 1, 2)
        # clip player: ▶/❚❚, which clip, time; seek bar under them. Shown once a clip is added.
        pl = QFrame()
        pl.setObjectName("clipPlayer")
        pg = QGridLayout(pl)
        pg.setContentsMargins(6, 6, 8, 8)
        pg.setHorizontalSpacing(6)
        self._player = pl
        self._pl_btn = theme.button("▶", theme.ROLE_PRIMARY, "Play or pause the clip. Drag the bar below to jump.")
        self._pl_btn.setFixedWidth(38)
        self._pl_btn.setAutoDefault(False)
        self._pl_btn.clicked.connect(self._on_clip_play)
        pg.addWidget(self._pl_btn, 0, 0)
        self._pl_menu = QComboBox()
        self._pl_menu.setProperty("onCard", True)
        self._pl_menu.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self._pl_menu.activated.connect(lambda i: self._on_clip_pick(self._pl_menu.itemText(i)))
        pg.addWidget(self._pl_menu, 0, 1)
        self._pl_time = _small("0:00 / 0:00", COLORS["text_muted"])
        pg.addWidget(self._pl_time, 0, 2)
        self._pl_seek = SeekSlider()
        self._pl_seek.sliderPressed.connect(lambda: self._on_clip_slide(self._pl_seek.seconds()))
        self._pl_seek.sliderMoved.connect(lambda v: self._on_clip_slide(v / 1000.0))
        self._pl_seek.sliderReleased.connect(lambda: self._clip_seek(self._pl_seek.seconds()))
        pg.addWidget(self._pl_seek, 1, 0, 1, 3)
        pg.setColumnStretch(1, 1)
        pl.hide()
        r.addWidget(pl, 3, 0, 1, 2)
        self._clips_note = _small("", COLORS["text_muted"], wrap=True)
        r.addWidget(self._clips_note, 4, 0, 1, 2)

        # ── Model files tile (Import model) ──
        self._model_tile = _Tile("Model files")
        m = self._model_tile.body
        pth = _btn("📂  Model .pth", self._on_pick_pth, COLORS["accent_primary"])
        pth.setFixedWidth(120)
        m.addWidget(pth, 1, 0)
        self._pth_label = _small("No file", COLORS["text_muted"])
        m.addWidget(self._pth_label, 1, 1)
        idx = _btn("📂  Index", self._on_pick_index, COLORS["accent_primary"])
        idx.setFixedWidth(120)
        m.addWidget(idx, 2, 0)
        self._index_label = _small("Optional", COLORS["text_muted"])
        m.addWidget(self._index_label, 2, 1)

        self._build_prep()

        # ── Training tile (Train (RVC)) ──
        self._train_tile = _Tile("Training")
        t = self._train_tile.body
        self._epochs_row = _SliderRow("Epochs", 10, 500, 10, 150, lambda v: f"{int(round(v))}", self._update_create,
                                      "More epochs learn the voice better but take longer. 150 is typical.")
        t.addWidget(self._epochs_row, 1, 0, 1, 2)
        opt = QWidget()
        oh = QHBoxLayout(opt)
        oh.setContentsMargins(0, 4, 0, 0)
        lab = _small("Batch")
        lab.setFixedWidth(64)
        lab.setToolTip("Clips trained at once. 2 fits a 4 GB GPU; 4 is faster but may run out of memory.")
        oh.addWidget(lab)
        self._batch_menu = QComboBox()
        self._batch_menu.addItems(["1", "2", "4"])
        self._batch_menu.setCurrentText("2")
        self._batch_menu.setFixedWidth(60)
        self._batch_menu.currentTextChanged.connect(lambda _t: self._update_create())
        oh.addWidget(self._batch_menu)
        oh.addSpacing(12)
        lab = _small("Rate")
        lab.setToolTip("Model sample rate. 40 kHz suits most voices.")
        oh.addWidget(lab)
        self._sr_menu = QComboBox()
        self._sr_menu.addItems(list(SR_CHOICES))
        self._sr_menu.setCurrentText("40 kHz")
        self._sr_menu.setFixedWidth(84)
        self._sr_menu.currentTextChanged.connect(lambda _t: self._update_create())
        oh.addWidget(self._sr_menu)
        oh.addStretch(1)
        self._train_opt = opt
        self._match_switch = self._switch("Match pitch to base voice")
        self._match_switch.setChecked(True)
        self._match_switch.setToolTip("Measures the pitch of the base voice and your clips, then sets the "
                                      "clone's Pitch so they line up.")

        # ── Footer: the task row (while a job runs, see _dock_task), then
        #    the estimate and the main action ──
        self._footer = QWidget()
        fv = QVBoxLayout(self._footer)
        fv.setContentsMargins(0, 0, 0, 0)
        fv.setSpacing(8)
        self._footer_l = fv
        fh = QHBoxLayout()
        fh.setContentsMargins(2, 0, 0, 0)
        fv.addLayout(fh)
        self._create_est = _small("", COLORS["text_secondary"], wrap=True)
        fh.addWidget(self._create_est, 1)
        self._create_btn = _btn("🧬  Create Voice", self._on_create, COLORS["accent_primary"], filled=True, height=32)
        self._create_btn.setFixedWidth(150)
        fh.addWidget(self._create_btn)
        return page

    def _build_prep(self):
        """Optional clip preparation, applied in order: trim, then remove music.

        Three columns when wide: Use part | Remove music & noise | Preview
        cleanup with ▶ Original / ▶ Cleaned. The third shows only while music
        removal is on, and the first two then share the width. (Skip silences
        was dropped from this window: the Seed-VC reference and Applio's
        preprocessing already cut long pauses.)"""
        self._clean_tile = _Tile("Clean-up")
        # 1. use part of each recording
        self._trim_col = QWidget()
        tl = QVBoxLayout(self._trim_col)
        tl.setContentsMargins(0, 0, 0, 0)
        self._trim_switch = self._switch("Use part of each recording", self._update_create)
        tl.addWidget(self._trim_switch)
        self._trim_seg = Segmented(list(TRIM_PRESETS["Quick"]), "20 s", height=24)
        self._trim_seg.changed.connect(lambda _v: self._update_create())
        self._trim_seg_wrap = self._indented(self._trim_seg)
        tl.addWidget(self._trim_seg_wrap)
        self._custom_list = QWidget()
        self._custom_l = QVBoxLayout(self._custom_list)
        self._custom_l.setContentsMargins(0, 0, 0, 0)
        self._custom_l.setSpacing(2)
        # 2. remove background music & noise
        self._clean_col = QWidget()
        cl = QVBoxLayout(self._clean_col)
        cl.setContentsMargins(0, 0, 0, 0)
        self._clean_switch = self._switch("Remove music & noise", self._update_create)
        self._clean_switch.setToolTip("Separates the voice from background music with Demucs. The first use "
                                      "downloads the model (about 80 MB).")
        cl.addWidget(self._clean_switch)
        self._denoise_cb = QCheckBox("Also reduce hiss && hum")
        self._denoise_cb.toggled.connect(lambda _on: self._update_create())
        self._denoise_wrap = self._indented(self._denoise_cb)
        cl.addWidget(self._denoise_wrap)
        self._cleanup_btn = _btn("🎧  Preview cleanup", self._on_preview_cleanup, COLORS["accent_primary"],
                                 height=26, small=True,
                                 tip="Clean up to 30 s of the first clip so you can compare before and after.")
        self._cleanup_wrap = self._indented(self._cleanup_btn)
        # 3. compare before / after (beside the switch when wide, under it when narrow)
        self._ab_col = QWidget()
        al = QVBoxLayout(self._ab_col)
        al.setContentsMargins(0, 0, 0, 0)
        al.addWidget(self._cleanup_wrap)
        ab = QWidget()
        abh = QHBoxLayout(ab)
        abh.setContentsMargins(0, 0, 0, 0)
        self._play_orig_btn = _btn("▶ Original", lambda: self._play_preview(0), COLORS["text_muted"],
                                   height=24, small=True)
        self._play_clean_btn = _btn("▶ Cleaned", lambda: self._play_preview(1), COLORS["success"],
                                    height=24, small=True)
        abh.addWidget(self._play_orig_btn)
        abh.addWidget(self._play_clean_btn)
        abh.addStretch(1)
        self._ab_wrap = self._indented(ab)
        al.addWidget(self._ab_wrap)
        for col in (self._trim_col, self._clean_col, self._ab_col):
            col.layout().addStretch(1)

    @staticmethod
    def _indented(widget):
        """widget inside a row whose left margin is the sub-option indent (set by _layout_prep)."""
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(40, 4, 0, 0)
        h.addWidget(widget)
        h.addStretch(1)
        return w

    # ═══════════════════════════════════════════════════════════════
    #  Layout
    # ═══════════════════════════════════════════════════════════════
    @staticmethod
    def _clear_grid(grid):
        while grid.count():
            item = grid.takeAt(0)
            w = item.widget()
            if w is not None:
                w.hide()

    @classmethod
    def _place(cls, grid, rows, wide):
        """Grid tiles row by row: two per row when wide, otherwise one; a lone tile spans."""
        cls._clear_grid(grid)
        r = 0
        for row in rows:
            row = [t for t in row if t is not None]
            if wide and len(row) == 2:
                grid.addWidget(row[0], r, 0)
                grid.addWidget(row[1], r, 1)
                row[0].show()
                row[1].show()
                r += 1
                continue
            for t in row:
                grid.addWidget(t, r, 0, 1, 2)
                t.show()
                r += 1
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)

    def _layout_prep(self, three_cols):
        """Clean-up options side by side (tile spans the box) or stacked, indented."""
        body = self._clean_tile.body
        self._clear_grid(body)
        custom = self._custom_l.count() > 0
        indent = 0 if three_cols else 40
        for wrap in (self._trim_seg_wrap, self._denoise_wrap, self._cleanup_wrap, self._ab_wrap):
            m = wrap.layout().contentsMargins()
            wrap.layout().setContentsMargins(indent, m.top(), 0, 0)
        self._three_cols = three_cols
        if three_cols:
            for i, col in enumerate((self._trim_col, self._clean_col, self._ab_col)):
                body.addWidget(col, 1, i)
                col.show()
            if custom:
                body.addWidget(self._custom_list, 2, 0, 1, 3)
                self._custom_list.show()
        else:
            r = 1
            body.addWidget(self._trim_col, r, 0, 1, 3)
            if custom:
                r += 1
                body.addWidget(self._custom_list, r, 0, 1, 3)
                self._custom_list.show()
            body.addWidget(self._clean_col, r + 1, 0, 1, 3)
            body.addWidget(self._ab_col, r + 2, 0, 1, 3)
            for col in (self._trim_col, self._clean_col, self._ab_col):
                col.show()
        if not custom:
            self._custom_list.hide()
        self._prep_columns()

    def _prep_columns(self):
        """The compare column only while music removal is on; no empty third column."""
        clean = self._clean_switch.isChecked()
        self._ab_col.setVisible(clean)
        three = getattr(self, "_three_cols", False)
        body = self._clean_tile.body
        for c, st in enumerate((1, 1, 1 if clean else 0) if three else (1, 0, 0)):
            body.setColumnStretch(c, st)

    def _layout_rec_buttons(self, one_row):
        g = self._rec_btns
        while g.count():
            g.takeAt(0)
        for i, b in enumerate(self._rec_btn_list):
            r, c = (0, i) if one_row else divmod(i, 2)
            g.addWidget(b, r, c)
        for c in range(4):
            g.setColumnStretch(c, 1 if (one_row or c < 2) else 0)

    def _layout(self, force=False):
        """Place the tiles for the current mode, voice and box width."""
        mode = self._mode_seg.value()
        has_voice = self._active_profile is not None
        custom = self._custom_l.count() > 0
        one_row = (not self._wide) and self._width >= 470
        rvc = has_voice and self._active_profile.mode == "rvc"
        key = (self._wide, one_row, mode, has_voice, rvc, custom)
        if key == self._layout_key and not force:
            return
        self._layout_key = key
        self._place(self._active_grid, [[self._av_tile, self._snd_tile if has_voice else None]], self._wide)
        if not has_voice:
            self._snd_tile.hide()
        if mode == "Import model":
            rows = [[self._cv_tile, self._model_tile], [self._footer]]
        elif mode == "Train (RVC)":
            rows = [[self._cv_tile, self._rec_tile], [self._clean_tile], [self._train_tile], [self._footer]]
        else:
            rows = [[self._cv_tile, self._rec_tile], [self._clean_tile], [self._footer]]
        for t in (self._model_tile, self._rec_tile, self._clean_tile, self._train_tile):
            t.hide()
        self._place(self._create_grid, rows, self._wide)
        self._layout_prep(three_cols=self._wide)
        self._layout_rec_buttons(one_row)
        # match-pitch switch beside Batch / Rate when there is room, else below
        t = self._train_tile.body
        t.removeWidget(self._train_opt)
        t.removeWidget(self._match_switch)
        if self._wide or one_row:
            t.addWidget(self._train_opt, 2, 0)
            t.addWidget(self._match_switch, 2, 1, Qt.AlignRight)
        else:
            t.addWidget(self._train_opt, 2, 0, 1, 2)
            t.addWidget(self._match_switch, 3, 0, 1, 2)
        # ▶ Preview row: under the base voice for RVC, under the Quality slider for Quick
        for tile in (self._av_tile, self._snd_tile):
            tile.body.removeWidget(self._preview_row)
        host = self._av_tile.body if rvc else self._snd_tile.body
        host.addWidget(self._preview_row, 5, 0, 1, 3)
        self._preview_row.show()

    def _on_resize(self, width):
        if abs(width - self._width) < 8:
            return
        self._width = width
        self._wide = width >= WIDE_PX
        self._layout()

    # ═══════════════════════════════════════════════════════════════
    #  Clip selection
    # ═══════════════════════════════════════════════════════════════
    def _mode(self):
        return self._mode_seg.value()

    def _selections(self):
        return tasks.selections(self._clips, self._mode(), self._trim_switch.isChecked(), self._trim_seg.value())

    def _clip_seconds(self):
        return tasks.clip_seconds(self._clips, self._selections())

    def _prep_options(self):
        return tasks.prep_options(self._mode(), self._trim_switch.isChecked(), self._trim_seg.value(),
                                  self._clean_switch.isChecked(), self._denoise_cb.isChecked(),
                                  False)

    def _refresh_prep(self, mode):
        """Show / hide the clean-up options for the current mode and selection."""
        if mode == "Import model":
            return
        presets = list(TRIM_PRESETS[mode])
        if self._trim_seg.values() != presets:
            self._trim_seg.set_values(presets, presets[1])
        if self._trim_seg.value() not in presets:
            self._trim_seg.set_value(presets[1], emit=False)
        first = "the first N seconds of speech, counted from the first clip into the next"
        self._trim_switch.setToolTip(f"Use {first}, or Custom ranges picked on each clip's timeline."
                                     + (" Quick mode reads at most 25 s." if mode == "Quick" else ""))
        clean = self._clean_switch.isChecked()
        self._trim_seg_wrap.setVisible(self._trim_switch.isChecked())
        self._denoise_wrap.setVisible(clean)
        self._cleanup_wrap.setVisible(clean)
        self._ab_wrap.setVisible(clean and bool(self._cleanup_preview))
        self._prep_columns()

        custom = self._trim_switch.isChecked() and TRIM_PRESETS[mode].get(self._trim_seg.value()) is None
        ckey = tuple((c["path"], c.get("start"), c.get("end"), c["seconds"]) for c in self._clips) if custom else None
        if ckey != self._custom_key:
            self._custom_key = ckey
            while self._custom_l.count():
                w = self._custom_l.takeAt(0).widget()
                if w is not None:
                    w.setParent(None)
                    w.deleteLater()
            for i, c in enumerate(self._clips if custom else []):
                row = QFrame()
                row.setObjectName("clipRow")
                h = QHBoxLayout(row)
                h.setContentsMargins(8, 3, 6, 3)
                name = os.path.basename(c["path"])
                h.addWidget(_small(name if len(name) <= 32 else name[:29] + "…", COLORS["text_primary"]))
                s0 = c.get("start") or 0.0
                e0 = c.get("end") if c.get("end") is not None else c["seconds"]
                whole = c.get("start") is None and c.get("end") is None
                txt = (f"all · {tasks.fmt_len(c['seconds'])}" if whole else
                       f"{tasks.clock(s0)}–{tasks.clock(e0)} · {e0 - s0:.1f} s")
                h.addSpacing(10)
                h.addWidget(_small(txt, COLORS["text_secondary"] if whole else COLORS["success"]))
                h.addStretch(1)
                edit = _btn("✂  Edit", lambda _=False, i=i: self._edit_clip(i), COLORS["accent_primary"],
                            height=24, small=True)
                edit.setFixedWidth(64)
                h.addWidget(edit)
                self._custom_l.addWidget(row)
            self._layout(force=True)
        self._cleanup_btn.setEnabled(bool(self._clips) and not self._busy)
        self._analyze_btn.setEnabled(bool(self._clips) and not self._busy)

    def _edit_clip(self, i):
        from ui_qt.panels.timeline_dialog import TimelineDialog
        c = self._clips[i]
        quick = (10, 20, 25) if self._mode() == "Quick" else (30, 60, 120)

        def done(start, end, duration):
            c["start"], c["end"] = start, end
            c["seconds"] = round(duration, 2)
            self._cleanup_preview = None
            self._update_create()
        self._timeline = TimelineDialog(self.app, c["path"], c.get("start"), c.get("end"), on_done=done,
                                        quick_lengths=quick)
        self._timeline.show()

    # ═══════════════════════════════════════════════════════════════
    #  Wiring and refresh
    # ═══════════════════════════════════════════════════════════════
    def attach(self, effects_panel):
        """Link to the Voice Effects card (built after Customization)."""
        self.effects = effects_panel
        effects_panel.subscribe(CLONE_GROUP, self._on_active_changed)
        effects_panel.subscribe_favourites(self._paint_fav)
        self._on_active_changed(effects_panel.active_in_group(CLONE_GROUP))

    def refresh(self):
        """Re-read runtime status and the voice list; update every visible widget."""
        st = runtime.status()
        installed = all(e["installed"] for e in st["engines"].values())
        voices = library.list_voices(ready_only=True)
        if st["frozen_unsupported"]:
            self._set_status("Not available in this build", COLORS["text_muted"])
            self._setup_text.setText("Voice cloning setup requires Python 3.11 & 3.12 with the 'py' launcher "
                                     "(from python.org) to install the isolated AI runtimes.")
            self._install_btn.hide()
        elif not installed:
            self._set_status("Not installed", COLORS["text_muted"])
            missing = [e["label"] for e in st["engines"].values() if not e["installed"]]
            self._setup_text.setText(
                "One-time setup of " + " and ".join(missing) + ". Downloads about 9 GB and uses about "
                f"21 GB on {os.path.splitdrive(st['root'])[0] or 'disk'}. You can keep using the app "
                "while it installs.")
        else:
            outdated = [e["label"] for e in st["engines"].values() if e.get("needs_update")]
            if outdated:
                self._set_status("Update available", COLORS["warning"])
                self._update_btn.show()
            else:
                self._set_status(f"Ready · {len(voices)} voice{'s' if len(voices) != 1 else ''}",
                                 COLORS["success"])
                self._update_btn.hide()
        if installed:
            self._setup.hide()
            self._tabs.show()
            self._on_tab(self._tabs.value())
        else:
            self._setup.show()
            self._tabs.hide()
            self._active_page.hide()
            self._create_page.hide()

        names, seen = {}, set()
        for v in voices:
            label = v.name if v.name.lower() not in seen else f"{v.name} ({v.slug})"
            seen.add(v.name.lower())
            names[label] = v.slug
        self._names = names
        self._voice_menu.blockSignals(True)
        self._voice_menu.clear()
        self._voice_menu.addItems(["None"] + list(names))
        self._voice_menu.blockSignals(False)
        self._sync_picker()
        self._update_create()

    def _set_status(self, text, color):
        self._status.setText(text)
        theme.set_color(self._status, color)

    def _on_tab(self, value):
        self._clip_pause()
        if value == "Create voice":
            self._active_page.hide()
            self._create_page.show()
            self._update_create()
        else:
            self._create_page.hide()
            self._active_page.show()
            self._update_active_est()
        self._dock_task()

    def _dock_task(self):
        """The progress row with Cancel: above the estimate and 🧬 Create Voice on
        the Create voice tab, at the bottom of the box everywhere else."""
        if not hasattr(self, "_box_l"):
            return
        target = self._footer_l if self._tabs.value() == "Create voice" else self._box_l
        if self._task.parentWidget() is not (self._footer if target is self._footer_l else self.box):
            shown = self._task.isVisible() or self._busy
            self._task.parentWidget().layout().removeWidget(self._task)
            if target is self._footer_l:
                target.insertWidget(0, self._task)
            else:
                target.addWidget(self._task)
            self._task.setVisible(shown)

    # ── active voice ───────────────────────────────────────────────
    def _sync_picker(self):
        label = next((n for n, s in self._names.items() if s == self._active_slug), "None")
        self._voice_menu.blockSignals(True)
        self._voice_menu.setCurrentText(label)
        self._voice_menu.blockSignals(False)

    def _on_voice_pick(self, label):
        if self.effects is None:
            return
        slug = self._names.get(label)
        if slug:
            self.effects.set_active(KEY_PREFIX + slug, True)
        else:
            self.effects.clear_group(CLONE_GROUP)

    def _label_is_favourite(self, label):
        slug = self._names.get(label)
        return bool(slug and self.effects and self.effects.is_favourite(KEY_PREFIX + slug))

    def _on_fav_click(self):
        if self.effects is not None and self._active_slug:
            self.effects.toggle_favourite(KEY_PREFIX + self._active_slug)

    def _paint_fav(self):
        """Fill the picker's heart when the voice shown is a favourite."""
        on = bool(self._active_slug and self.effects
                  and self.effects.is_favourite(KEY_PREFIX + self._active_slug))
        self._fav_btn.setText(HEART_ON if on else HEART_OFF)
        if self._fav_btn.property("on") != on:
            self._fav_btn.setProperty("on", on)
            theme.repolish(self._fav_btn)
        self._fav_btn.setEnabled(bool(self._active_slug))
        self._voice_menu.view().viewport().update()

    def _on_active_changed(self, key):
        slug = key[len(KEY_PREFIX):] if key else None
        newly_on = slug is not None and slug != self._active_slug
        if slug != self._active_slug:
            self._stop_active_preview()     # it was the other voice's preview
        self._flush_params()
        self._active_slug = slug
        self._active_profile = None
        if slug:
            try:
                self._active_profile = library.load_voice(slug)
            except (OSError, ValueError):
                self._active_profile = None
        p = self._active_profile
        self._sync_picker()
        self._paint_fav()
        self._show_base(p)
        if p is not None and newly_on:
            self._apply_base(p)
        rows_rvc = (self._pitch_row, self._index_row, self._protect_row)
        if p is None:
            self._mode_tag.setText("")
            self._empty_hint.show()
            self._preview_btn.setEnabled(False)
        else:
            self._empty_hint.hide()
            self._preview_btn.setEnabled(not self._busy)
            if p.mode == "rvc":
                self._mode_tag.setText("RVC model" + (" · imported" if p.imported else ""))
                for row, val in ((self._pitch_row, p.pitch), (self._index_row, p.index_rate),
                                 (self._protect_row, p.protect)):
                    row.set(val)
                for r in rows_rvc:
                    r.show()
                self._steps_row.hide()
                self._match_btn.setEnabled(bool(p.clips) and not self._busy)
            else:
                self._mode_tag.setText("Quick · zero-shot")
                self._steps_row.set(p.diffusion_steps)
                for r in rows_rvc:
                    r.hide()
                self._steps_row.show()
        self._layout()
        self._update_active_est()

    def _params_changed(self):
        self._update_active_est()
        self._save_timer.start()            # restarts: saves 600 ms after the last move

    def _flush_params(self):
        """Persist slider values to the active voice's profile."""
        self._save_timer.stop()
        p = self._active_profile
        if p is None:
            return
        try:
            if p.mode == "rvc":
                p = library.update_params(p.slug, pitch=int(round(self._pitch_row.get())),
                                          index_rate=round(float(self._index_row.get()), 2),
                                          protect=round(float(self._protect_row.get()), 2))
            else:
                p = library.update_params(p.slug, diffusion_steps=int(round(self._steps_row.get())))
            self._active_profile = p
        except (OSError, ValueError) as e:
            print(f"Could not save voice settings: {e}")

    def reload_active(self, p):
        """Show *p*, the active voice, after it was saved elsewhere (the Cloned Voices window)."""
        self._save_timer.stop()
        self._active_profile = p
        for row, value in ((self._pitch_row, p.pitch), (self._index_row, p.index_rate),
                           (self._protect_row, p.protect), (self._steps_row, p.diffusion_steps)):
            row.set(value)
        self._show_base(p)
        self._update_active_est()

    def get_settings(self) -> dict:
        p = self._active_profile
        if p is None:
            return {}
        return {"slug": p.slug,
                "pitch": int(round(self._pitch_row.get())),
                "index_rate": round(float(self._index_row.get()), 2),
                "protect": round(float(self._protect_row.get()), 2),
                "diffusion_steps": int(round(self._steps_row.get()))}

    def _update_active_est(self):
        p = self._active_profile
        if p is None:
            self._active_est.setText("")
            return
        try:
            text = self.app.text_panel.get_text()
        except Exception:
            text = ""
        warm = False
        try:
            from core.voice_cloning import get_client
            warm = get_client().is_warm(p.mode)
        except Exception:
            pass
        self._active_est.setText(tasks.active_estimate(p, text, int(round(self._steps_row.get())), warm))

    def _tick(self):
        """Keep the time estimate in step with the text box."""
        if self._tabs.value() == "Active voice" and self._active_profile is not None:
            self._update_active_est()

    # ── base voice ─────────────────────────────────────────────────
    def _engine_selection(self):
        try:
            return self.app.engine_panel.get_voice_selection()
        except Exception:
            return None

    _selection_for = staticmethod(tasks.selection_for)

    def _show_base(self, p):
        sel = self._selection_for(p)
        if p is None:
            self._base_row.hide()
            return
        self._base_row.show()
        if sel:
            self._base_value.setText(voice_selection.describe_selection(sel))
            theme.set_color(self._base_value, COLORS["text_primary"])
        else:
            self._base_value.setText("not set")
            theme.set_color(self._base_value, COLORS["text_muted"])

    def _apply_base(self, p):
        """Select the voice's base engine voice in the TTS Engine card."""
        sel = self._selection_for(p)
        if not sel:
            self.app.set_status(f"ℹ  '{p.name}' has no base voice yet. Pick an engine voice and press "
                                "Use current in Clone Voice.", COLORS["text_secondary"])
            return
        err = self.app.engine_panel.apply_voice_selection(sel)
        if err:
            self.app.set_status(f"⚠  Base voice for '{p.name}' not selected: {err}", COLORS["warning"])
        else:
            self.app.set_status(f"🔁  TTS Engine set to {voice_selection.describe_selection(sel)}, "
                                f"the base voice of '{p.name}'", COLORS["accent_primary"])
        self._update_create()

    def _on_use_current_base(self):
        p = self._active_profile
        sel = self._engine_selection()
        if p is None or not sel:
            return
        p = library.update_params(p.slug, base_selection=sel, base_engine=sel["engine"],
                                  base_voice=str(sel.get("voice") or ""))
        self._active_profile = p
        self._show_base(p)
        hint = " Press Match pitch to re-measure it." if p.mode == "rvc" and p.clips else ""
        self.app.set_status(f"✅  Base voice of '{p.name}' set to "
                            f"{voice_selection.describe_selection(sel)}.{hint}", COLORS["success"])

    def _base_voice(self):
        try:
            s = self.app.engine_panel.get_settings()
        except Exception:
            return "", "", None
        name, voice = tasks.base_voice(s)
        return name, voice, s

    # ── create page state ──────────────────────────────────────────
    def _update_create(self):
        if not self._cleanup_preview and self._preview_playing is not None:
            self._stop_preview()            # the preview was discarded and its buttons hide
        self._refresh_player()
        mode = self._mode()
        sel = self._engine_selection()
        self._base_label.setText(voice_selection.describe_selection(sel) if sel else "not set")
        self._refresh_prep(mode)
        self._layout()

        secs = self._clip_seconds()
        n = len(self._clips)
        ok, level, note = tasks.length_rule(mode, secs)
        color = {"ok": COLORS["success"], "warning": COLORS["warning"], "error": COLORS["error"]}[level]
        if mode != "Import model":
            total = sum(c["seconds"] for c in self._clips)
            self._clips_label.setText(f"{n} clip{'s' if n != 1 else ''} · {tasks.fmt_len(total)}" if n else "No clips")
            theme.set_color(self._clips_label, color if n else COLORS["text_muted"])
            parts = []
            if n and abs(total - secs) >= 0.05:
                parts.append(f"Using {tasks.fmt_len(secs)}")
            if n and note:
                parts.append(note[0].upper() + note[1:])
            self._clips_note.setText(" · ".join(parts))
            theme.set_color(self._clips_note, color)
            self._clips_note.setVisible(bool(parts))
        else:
            ok = bool(self._pth)

        warm = False
        if mode != "Import model" and n and self._clean_switch.isChecked():
            try:
                from core.voice_cloning import get_client
                warm = get_client().is_warm("rvc")
            except Exception:
                pass
        est = tasks.create_estimate(mode, n, secs, int(round(self._epochs_row.get())), self._batch_menu.currentText(),
                                    self._sr_menu.currentText(), bool(self._pth), self._clean_switch.isChecked(), warm)
        self._create_est.setText(est)
        theme.set_color(self._create_est, COLORS["text_secondary"] if n or self._pth else COLORS["text_muted"])
        name_ok = bool(self._name_entry.text().strip())
        self._create_btn.setEnabled(ok and name_ok and not self._busy)

    def _on_add_clips(self):
        paths, _ = QFileDialog.getOpenFileNames(self.app, "Choose recordings of the target voice", "", AUDIO_FILTER)
        if not paths:
            return
        known = {c["path"] for c in self._clips}
        for p in (os.path.normpath(x) for x in paths):
            if p in known:
                continue
            try:
                self._clips.append({"path": p, "seconds": round(library.audio_seconds(p), 2),
                                    "start": None, "end": None})
            except Exception as e:
                QMessageBox.warning(self.app, "Clip skipped", f"Could not read {os.path.basename(p)}:\n{e}")
        if not self._name_entry.text().strip() and self._clips:
            self._name_entry.setText(os.path.splitext(os.path.basename(self._clips[0]["path"]))[0][:40])
        self._update_create()

    # ── Recordings clip player ─────────────────────────────────────
    def _refresh_player(self):
        labels = {}
        for i, c in enumerate(self._clips, 1):
            name = os.path.basename(c["path"])
            labels[f"{i}. {name if len(name) <= 34 else name[:31] + '…'}"] = c["path"]
        self._pl_labels = labels
        if not labels:
            self._clip_stop()
            self._pl_path, self._pl_seg = None, None
            self._player.hide()
            return
        if [self._pl_menu.itemText(i) for i in range(self._pl_menu.count())] != list(labels):
            self._pl_menu.blockSignals(True)
            self._pl_menu.clear()
            self._pl_menu.addItems(list(labels))
            self._pl_menu.blockSignals(False)
        if self._pl_path not in labels.values():
            self._select_clip(next(iter(labels)))
        else:
            self._pl_menu.setCurrentText(next(k for k, v in labels.items() if v == self._pl_path))
        self._player.show()

    def _select_clip(self, label):
        self._clip_stop()
        self._pl_menu.setCurrentText(label)
        self._pl_path = self._pl_labels.get(label)
        self._pl_seg, self._pl_from = None, 0.0
        self._pl_seek.set_length(max(0.1, self._clip_duration()))
        self._pl_seek.set_seconds(0.0)
        self._clip_show_pos(0.0)

    def _on_clip_pick(self, label):
        if self._pl_labels.get(label) != self._pl_path:
            self._select_clip(label)

    def _clip_duration(self):
        if self._pl_seg is not None:
            return len(self._pl_seg) / 1000.0
        return next((c["seconds"] for c in self._clips if c["path"] == self._pl_path), 0.0)

    def _clip_position(self):
        if self._pl_t0 is None:
            return self._pl_from
        return min(self._clip_duration(), self._pl_from + time.monotonic() - self._pl_t0)

    def _clip_show_pos(self, pos):
        self._pl_time.setText(f"{tasks.clock(pos)} / {tasks.clock(self._clip_duration())}")

    def _on_clip_play(self):
        if self._pl_loading or not self._pl_path:
            return
        if self._pl_t0 is not None:
            self._clip_pause()
            return
        if self._pl_seg is not None:
            start = self._pl_from if self._pl_from < self._clip_duration() - 0.05 else 0.0
            self._clip_play_at(start)
            return
        try:
            import pygame
            fmt = pygame.mixer.get_init()
        except Exception:
            fmt = None
        if not fmt:
            return
        path, (freq, _size, ch) = self._pl_path, fmt
        self._pl_loading = True
        self._pl_btn.setText("…")
        self._pl_btn.setEnabled(False)

        def load():
            try:
                from core.audio_io import load_segment
                seg = load_segment(path).set_frame_rate(freq).set_channels(ch).set_sample_width(2)
                ui(self._clip_loaded, path, seg, None)
            except Exception as e:   # noqa: BLE001
                ui(self._clip_loaded, path, None, e)
        threading.Thread(target=load, daemon=True, name="clip-player-load").start()

    def _clip_loaded(self, path, seg, error):
        self._pl_loading = False
        self._pl_btn.setText("▶")
        self._pl_btn.setEnabled(True)
        if path != self._pl_path:
            return
        if error is not None:
            self.app.set_status(f"⚠  Could not play {os.path.basename(path)}: {error}", COLORS["warning"])
            return
        self._pl_seg = seg
        self._pl_seek.set_length(max(0.1, self._clip_duration()))
        self._clip_play_at(min(self._pl_from, max(0.0, self._clip_duration() - 0.05)))

    def _clip_play_at(self, pos):
        try:
            import pygame
            part = self._pl_seg[int(pos * 1000):]
            self._clip_stop()
            self._pl_sound = pygame.mixer.Sound(buffer=part.raw_data)
            pygame.mixer.Channel(CLIP_PLAY_CHANNEL).play(self._pl_sound)
        except Exception as e:   # noqa: BLE001
            print(f"Clip playback failed: {e}")
            return
        self._pl_from, self._pl_t0 = pos, time.monotonic()
        self._pl_btn.setText("❚❚")
        self._pl_timer.start()
        self._clip_tick()

    def _clip_pause(self):
        if self._pl_t0 is not None:
            self._pl_from = self._clip_position()
            self._clip_stop()

    def _clip_stop(self):
        self._pl_t0 = None
        self._pl_timer.stop()
        try:
            import pygame
            if pygame.mixer.get_init():
                pygame.mixer.Channel(CLIP_PLAY_CHANNEL).stop()
        except Exception:
            pass
        self._pl_sound = None
        self._pl_btn.setText("▶")

    def _clip_tick(self):
        if self._pl_t0 is None:
            return
        pos = self._clip_position()
        if pos >= self._clip_duration() - 0.01:
            self._clip_stop()
            self._pl_from = 0.0
            pos = self._clip_duration()
        if not self._pl_seeking:
            self._pl_seek.set_seconds(pos)
            self._clip_show_pos(pos)

    def _on_clip_slide(self, value):
        self._pl_seeking = True
        if self._pl_t0 is None:
            self._pl_from = float(value)
        self._clip_show_pos(float(value))

    def _clip_seek(self, pos):
        self._pl_seeking = False
        pos = min(max(0.0, float(pos)), self._clip_duration())
        self._pl_from = pos
        self._clip_show_pos(pos)
        if self._pl_t0 is not None:
            self._clip_play_at(pos)

    # ── Record, Analyze, files ─────────────────────────────────────
    def _on_record(self):
        from ui_qt.panels.record_dialog import RecordDialog
        if self._recorder is not None and self._recorder.isVisible():
            self._recorder.raise_()
            self._recorder.activateWindow()
            return

        def done(path, seconds):
            self._clips.append({"path": path, "seconds": round(seconds, 2), "start": None, "end": None})
            if not self._name_entry.text().strip():
                self._name_entry.setText("My voice")
            self._cleanup_preview = None
            self._update_create()
        self._recorder = RecordDialog(self.app, str(runtime.runtime_root() / "recordings"), on_done=done)
        self._recorder.show()

    def _on_analyze(self):
        from ui_qt.panels.analyzer_dialog import AnalyzerDialog
        if self._analyzer is not None and self._analyzer.isVisible():
            self._analyzer.raise_()
            self._analyzer.activateWindow()
            return
        if not self._clips:
            return
        self._analyzer = AnalyzerDialog(self.app, self._selections(), clean=self._clean_switch.isChecked(),
                                        denoise=self._denoise_cb.isChecked(),
                                        on_applied=self._on_match_applied,
                                        gender=self._match_gender_seg.value().lower())
        self._analyzer.show()

    def _on_match_applied(self, selection, result=None):
        """Voice match applied a base voice. Keep its ▶ Result for the voice created next."""
        self._match_result = None
        if result and os.path.isfile(result["wav"]):
            wav = os.path.join(self._new_tmp(), "match_result.wav")
            shutil.copyfile(result["wav"], wav)
            self._match_result = dict(result, wav=wav)
        self._update_create()

    def _usable_match(self, eng_settings, clips, prep):
        return tasks.usable_match(self._match_result, eng_settings, clips, prep)

    def _on_clear_clips(self):
        self._clips = []
        self._cleanup_preview = None
        self._update_create()

    def _on_pick_pth(self):
        p, _ = QFileDialog.getOpenFileName(self.app, "Choose an RVC voice model", "", "RVC model (*.pth);;All files (*.*)")
        if p:
            self._pth = os.path.normpath(p)
            self._pth_label.setText(os.path.basename(p))
            theme.set_color(self._pth_label, COLORS["text_primary"])
            if not self._name_entry.text().strip():
                self._name_entry.setText(os.path.splitext(os.path.basename(p))[0][:40])
            self._update_create()

    def _on_pick_index(self):
        p, _ = QFileDialog.getOpenFileName(self.app, "Choose the model's index file (optional)", "",
                                           "Index (*.index);;All files (*.*)")
        if p:
            self._index = os.path.normpath(p)
            self._index_label.setText(os.path.basename(p))
            theme.set_color(self._index_label, COLORS["text_primary"])

    # ═══════════════════════════════════════════════════════════════
    #  Task plumbing
    # ═══════════════════════════════════════════════════════════════
    def _begin(self, name, stage):
        self._busy = True
        self._task_name = name
        self._cancel.clear()
        self._hide_timer.stop()
        self._progress.setValue(0)
        self._stage.setText(stage)
        theme.set_color(self._stage, COLORS["text_secondary"])
        self._cancel_btn.setEnabled(True)
        self._cancel_btn.setText("✖  Cancel")
        self._dock_task()
        self._task.show()
        for b in (self._create_btn, self._install_btn, self._preview_btn, self._match_btn):
            b.setEnabled(False)

    def _end(self, message, color, keep_seconds=8):
        self._busy = False
        self._progress.setValue(1000 if color == COLORS["success"] else 0)
        self._stage.setText(message)
        theme.set_color(self._stage, color)
        self._cancel_btn.setEnabled(False)
        self._install_btn.setEnabled(True)
        self.refresh()
        self._on_active_changed(self.effects.active_in_group(CLONE_GROUP) if self.effects else None)
        self._hide_timer.start(keep_seconds * 1000)

    def _set_progress(self, frac, text=None):
        if frac is not None:
            self._progress.setValue(round(max(0.0, min(1.0, float(frac))) * 1000))
        if text:
            self._stage.setText(text)

    def _on_cancel(self):
        if self._busy:
            self._cancel.set()
            self._cancel_btn.setEnabled(False)
            self._cancel_btn.setText("Cancelling…")
            self._stage.setText("Cancelling…")
            theme.set_color(self._stage, COLORS["warning"])

    def _run(self, name, stage, work):
        """Start *work* (a callable) on a daemon thread with standard error handling."""
        if self._busy:
            return
        self._begin(name, stage)

        def runner():
            try:
                msg = work()
                msg, color = msg if isinstance(msg, tuple) else (msg, COLORS["success"])
                ui(self._end, msg or "Done", color)
            except VCCancelled:
                ui(self._end, f"{name} cancelled", COLORS["warning"])
            except VCError as e:
                ui(self._fail, name, e.message, e.hint)
            except Exception as e:   # noqa: BLE001
                ui(self._fail, name, f"{type(e).__name__}: {e}", None)
        threading.Thread(target=runner, daemon=True, name=f"clone-{name}").start()

    def _fail(self, name, message, hint):
        self._end(f"❌  {name} failed: {message}", COLORS["error"], keep_seconds=30)
        self.app.set_status(f"❌  {name} failed: {message}", COLORS["error"])
        QMessageBox.critical(self.app, f"{name} failed", message + (f"\n\n{hint}" if hint else ""))

    def _check_cancel(self):
        if self._cancel.is_set():
            raise VCCancelled()

    def _ctx(self):
        return tasks.TaskContext(self.app.synth, self._cancel,
                                 lambda frac, msg: ui(self._set_progress, frac, msg), self._tmp_dirs)

    def _synthesize(self, text, eng_settings, tmp, cancel=None):
        """Synthesize *text* with the selected engine; returns (float32 samples, sr)."""
        return tasks.synthesize(self.app.synth, text, eng_settings, tmp, cancel or self._cancel)

    def _new_tmp(self):
        return self._ctx().new_tmp()

    # ═══════════════════════════════════════════════════════════════
    #  Tasks
    # ═══════════════════════════════════════════════════════════════
    def _on_install(self):
        if QMessageBox.question(
                self.app, "Install voice cloning",
                "This downloads about 9 GB (two Python environments, PyTorch, and voice models) "
                f"into:\n{runtime.runtime_root()}\n\nIt needs Python 3.11 and 3.12 and Git. "
                "Continue?") != QMessageBox.Yes:
            return

        def work():
            runtime.install(progress_cb=lambda f, m: ui(self._set_progress, f, m),
                            log_cb=lambda line: None, cancel_event=self._cancel)
            return "✅  Voice cloning is installed"
        self._run("Install", "Starting install…", work)

    def _on_update(self):
        st = runtime.status()
        outdated = [n for n, e in st["engines"].items() if e.get("needs_update")]
        if not outdated or QMessageBox.question(
                self.app, "Update voice cloning",
                "This version adds packages to the voice-cloning runtime (for example music "
                "removal). The update installs only what is missing, usually a small download, then "
                "re-checks the engines. Continue?") != QMessageBox.Yes:
            return

        def work():
            runtime.install(engines=tuple(outdated),
                            progress_cb=lambda f, m: ui(self._set_progress, f, m),
                            log_cb=lambda line: None, cancel_event=self._cancel)
            return "✅  Voice cloning is up to date"
        self._run("Update", "Updating…", work)

    def _on_create(self):
        name = self._name_entry.text().strip()[:40]
        mode = self._mode()
        existing = {v.name.lower() for v in library.list_voices()}
        if name.lower() in existing:
            QMessageBox.warning(self.app, "Name in use", f"A voice named '{name}' already exists.")
            return
        ctx = self._ctx()
        if mode == "Import model":
            pth, index = self._pth, self._index

            def work():
                p, info = tasks.import_model(ctx, name, pth, index)
                return self._finish_new_voice(p, f"Voice '{name}' imported ({info['sample_rate'] // 1000} kHz model).")
            self._run("Import", "Checking the model…", work)
            return
        clips = self._selections()
        prep = self._prep_options()
        eng, voice, eng_settings = self._base_voice()
        base_sel = self._engine_selection()
        match = self._usable_match(eng_settings, clips, prep)
        if mode == "Quick":
            def work():
                p = tasks.create_quick(ctx, name, clips, eng, voice, base_sel, prep, eng_settings, match)
                return self._finish_new_voice(p, f"Voice '{name}' is ready. It is switched on in Voice Effects.")
            self._run("Create voice", "Preparing the reference voice…", work)
        else:
            if self.app.is_generating:
                QMessageBox.information(self.app, "Busy",
                                        "Wait for the current generation to finish, then start training.")
                return
            opts = dict(epochs=int(round(self._epochs_row.get())), batch_size=int(self._batch_menu.currentText()),
                        sample_rate=SR_CHOICES[self._sr_menu.currentText()], match=self._match_switch.isChecked())

            def work():
                p, seconds = tasks.train(ctx, name, clips, eng, voice, eng_settings, opts, base_sel, prep, match)
                took = tasks.estimate.fmt(seconds).replace("about ", "")
                return self._finish_new_voice(p, f"Voice '{name}' trained in {took} and switched on.")
            self._run("Training", "Starting…", work)

    def _finish_new_voice(self, p, msg):
        """Worker thread: the voice is ready; update the window, then report msg."""
        def done():
            self.app.refresh_cloned_voices(select=p.slug)
            self._clips, self._pth, self._index = [], None, None
            self._cleanup_preview = None
            self._name_entry.clear()
            self._pth_label.setText("No file")
            theme.set_color(self._pth_label, COLORS["text_muted"])
            self._index_label.setText("Optional")
            theme.set_color(self._index_label, COLORS["text_muted"])
            self._tabs.set_value("Active voice")
            self.app.set_status(f"✅  {msg}", COLORS["success"])
        ui(done)
        return f"✅  {msg}"

    def _on_match_pitch(self):
        p = self._active_profile
        if p is None or not p.clips:
            return
        _, _, eng_settings = self._base_voice()
        ctx = self._ctx()

        def work():
            r = tasks.match_pitch(ctx, p, eng_settings)
            ui(self._pitch_row.set, r["offset"])
            return (f"✅  Pitch set to {r['offset']:+d} st "
                    f"(base {r['base_f0'] or 0:.0f} Hz → target {r['target_f0'] or 0:.0f} Hz)")
        self._run("Match pitch", "Synthesizing a sample with the current engine voice…", work)

    def make_preview(self, p, eng_settings, sound, cancel=None, say=None):
        """Speak *p*'s preview text, convert it into voice *p*; returns a WAV path (worker thread)."""
        return tasks.make_preview(self._ctx(), p, eng_settings, sound, cancel=cancel, say=say)

    def saved_fingerprint(self, p):
        """Fingerprint of *p*'s preview with its saved base and sound, or None without a usable base."""
        eng, err = self.app.engine_panel.settings_for_selection(self._selection_for(p))
        if err:
            return None
        return previews.fingerprint(p, voice_selection.synthesis_key(eng), previews.sound_of(p))

    def _on_preview(self):
        """▶ Preview plays the voice's preview (saved, or made now); ■ Stop stops it."""
        if self._active_sound is not None:
            self._stop_active_preview()
            return
        p = self._active_profile
        if p is None:
            return
        self._flush_params()
        p = self._active_profile
        _, _, eng_settings = self._base_voice()
        sound = previews.sound_of(p)
        fp = previews.fingerprint(p, voice_selection.synthesis_key(eng_settings), sound)
        saved = previews.cached(p.slug, fp)
        if saved:
            if self._play_active_preview(saved):
                self.app.set_status(f"▶  Playing the saved preview of '{p.name}'", COLORS["success"])
            return
        keep = fp == self.saved_fingerprint(p)
        ctx = self._ctx()

        def work():
            path = tasks.make_preview(ctx, p, eng_settings, sound, say=lambda m: ui(self._set_progress, 0.4, m))
            if keep:
                previews.store(p.slug, path, fp)
            ui(self._play_active_preview, path)
            return f"▶  Playing preview of '{p.name}'" + (" (saved for next time)" if keep else "")
        self._run("Preview", "Starting…", work)

    def refresh_preview(self, slug):
        """Make the saved preview of *slug* again, in the background, when it is out of date."""
        try:
            p = library.load_voice(slug)
        except (OSError, ValueError):
            return
        if not p.is_ready or self._busy:
            return
        eng, err = self.app.engine_panel.settings_for_selection(self._selection_for(p))
        if err:
            return
        sound = previews.sound_of(p)
        fp = previews.fingerprint(p, voice_selection.synthesis_key(eng), sound)
        if previews.cached(slug, fp):
            return
        ctx = self._ctx()

        def work():
            try:
                path = tasks.make_preview(ctx, p, eng, sound, say=lambda m: ui(self._set_progress, 0.4, m))
            except VCCancelled:
                raise
            except Exception as e:   # noqa: BLE001
                msg = e.message if isinstance(e, VCError) else f"{type(e).__name__}: {e}"
                return f"⚠  Preview of '{p.name}' not updated: {msg}", COLORS["warning"]
            previews.store(slug, path, fp)
            return f"✅  Saved a new preview of '{p.name}'"
        self._run("Preview update", f"Updating the saved preview of '{p.name}'…", work)

    def _on_preview_cleanup(self):
        sels = self._selections()
        if not sels:
            return
        first = dict(sels[0])
        full = next(c["seconds"] for c in self._clips if c["path"] == first["path"])
        denoise = self._denoise_cb.isChecked()
        ctx = self._ctx()

        def work():
            orig, cleaned, seconds, elapsed = tasks.cleanup_preview(ctx, first, full, denoise)

            def done():
                self._cleanup_preview = (orig, cleaned)
                self._update_create()          # shows ▶ Original / ▶ Cleaned
                self._play_preview(1)
            ui(done)
            return (f"▶  Playing the cleaned audio ({seconds:.0f} s, music removed in "
                    f"{elapsed:.0f} s). Compare with ▶ Original.")
        self._run("Cleanup preview", "Removing background music from a sample…", work)

    # ── preview playback (pygame channel 2) ────────────────────────
    _PREVIEW_IDLE = ("▶ Original", "▶ Cleaned")

    def _preview_btns(self):
        return (self._play_orig_btn, self._play_clean_btn)

    def _play_preview(self, which):
        """▶ Original / ▶ Cleaned are play / stop toggles on one channel."""
        same = self._preview_playing == which
        self._stop_preview()
        if same or not self._cleanup_preview:
            return
        if not self._play(self._cleanup_preview[which]):
            return
        self._preview_playing = which
        self._preview_btns()[which].setText("■ Stop")
        QTimer.singleShot(200, self._watch_preview)

    def _watch_preview(self):
        if self._preview_playing is None:
            return
        try:
            import pygame
            busy = pygame.mixer.get_init() and pygame.mixer.Channel(PREVIEW_CHANNEL).get_busy()
        except Exception:
            busy = False
        if busy:
            QTimer.singleShot(200, self._watch_preview)
        else:
            self._stop_preview()

    def _stop_preview(self):
        try:
            import pygame
            if pygame.mixer.get_init():
                pygame.mixer.Channel(PREVIEW_CHANNEL).stop()
        except Exception:
            pass
        self._preview_playing = None
        for b, text in zip(self._preview_btns(), self._PREVIEW_IDLE):
            b.setText(text)

    def _play_active_preview(self, path):
        """Play the Active voice preview; its button turns into ■ Stop until it ends."""
        self._stop_preview()
        if not self._play(path):
            return False
        self._active_sound = self._preview_sound
        self._preview_btn.setText("■  Stop")
        QTimer.singleShot(200, self._watch_active_preview)
        return True

    def _watch_active_preview(self):
        if self._active_sound is None:
            return
        try:
            import pygame
            ch = pygame.mixer.Channel(PREVIEW_CHANNEL)
            mine = bool(pygame.mixer.get_init()) and ch.get_busy() and ch.get_sound() is self._active_sound
        except Exception:
            mine = False
        if mine:
            QTimer.singleShot(200, self._watch_active_preview)
        else:
            self._active_sound = None
            self._preview_btn.setText("▶  Preview")

    def _stop_active_preview(self):
        if self._active_sound is None:
            return
        try:
            import pygame
            ch = pygame.mixer.Channel(PREVIEW_CHANNEL)
            if pygame.mixer.get_init() and ch.get_sound() is self._active_sound:
                ch.stop()
        except Exception:
            pass
        self._active_sound = None
        self._preview_btn.setText("▶  Preview")

    def _play(self, path):
        try:
            import pygame
            if not pygame.mixer.get_init():
                return False
            self._preview_sound = pygame.mixer.Sound(path)
            pygame.mixer.Channel(PREVIEW_CHANNEL).play(self._preview_sound)
            return True
        except Exception as e:
            print(f"Preview playback failed: {e}")
            return False

    def cleanup(self):
        """App closing: cancel the task, stop playback, delete temp folders."""
        self._cancel.set()
        self._tick_timer.stop()
        self._clip_stop()
        try:
            import pygame
            if pygame.mixer.get_init():
                pygame.mixer.Channel(PREVIEW_CHANNEL).stop()
        except Exception:
            pass
        self._preview_sound = None
        for d in self._tmp_dirs:
            shutil.rmtree(d, ignore_errors=True)
        self._tmp_dirs.clear()


class _ResizeWatch(QObject):
    """Calls fn(width) when the watched widget is resized."""

    def __init__(self, fn):
        super().__init__()
        self._fn = fn

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Resize:
            self._fn(obj.width())
        return False
