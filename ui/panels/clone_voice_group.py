"""
ui/panels/clone_voice_group.py  –  "Clone Voice" box in the Customization card

    🧬 Clone Voice                                   [status] [⬆ Update]
    [ Active voice | Create voice ]

    Active voice   ┌ Voice ──────────────┐ ┌ Sound ──────────────────┐
                   │ picker, base voice  │ │ sliders, ▶ Preview, ⏱   │
                   └─────────────────────┘ └─────────────────────────┘
    Create voice   ┌ New voice ──────────┐ ┌ Recordings / Model files ┐
                   │ name, mode, base    │ │ add, record, analyze     │
                   └─────────────────────┘ └─────────────────────────┘
                   ┌ Clean-up ───────────┐ ┌ Training (RVC only) ─────┐
                   └─────────────────────┘ └─────────────────────────┘
                   ⏱ estimate                            [🧬 Create Voice]
    ▬▬▬▬▬▬▬▬▬▬ progress ▬▬▬▬▬▬▬▬▬▬   stage text            [✖ Cancel]

Tiles sit in two columns when the box is at least WIDE_PX wide and stack in
one column below that. A tile alone in its row spans both columns, so the
box never shows an empty cell. Help text lives in hover tips (_Tip), not in
notes under the controls.

Long work (install, training, preview) runs on a daemon thread; every
widget update goes back through app.after(0, ...). One task at a time.
"""

import os
import shutil
import tempfile
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox

import customtkinter as ctk

from core.audio_io import AUDIO_EXTENSIONS
from core.constants import COLORS, FONTS
from core.voice_cloning import estimate, library, pitch, runtime
from core.voice_cloning.errors import VCCancelled, VCError, VCWorkerError
from ui.theme import styled_slider

CLONE_GROUP = "clone"
KEY_PREFIX = "clone:"
PREVIEW_TEXT = "Hello! This is a short preview of my cloned voice."
AUDIO_TYPES = [("Audio files", " ".join("*" + e for e in AUDIO_EXTENSIONS)), ("All files", "*.*")]
MODES = ["Quick", "Train (RVC)", "Import model"]
MODE_HINTS = {
    "Quick": "No training. Uses up to 25 s of your clips and is ready in seconds.",
    "Train (RVC)": "Trains a voice model on your clips. Best with 3+ minutes of clean speech. "
                   "Cloned voices can't be used while training runs.",
    "Import model": "Use an existing RVC voice model (.pth), with its .index if you have it.",
}
SR_CHOICES = {"32 kHz": 32000, "40 kHz": 40000, "48 kHz": 48000}
# "Use part of each recording" presets: the first N seconds, or Custom
# ranges. Seed-VC (Quick) only reads 25 s of reference, so Quick stops at
# 25 s; training uses everything it is given.
TRIM_PRESETS = {
    "Quick": {"10 s": 10, "20 s": 20, "25 s": 25, "Custom": None},
    "Train (RVC)": {"1 min": 60, "3 min": 180, "5 min": 300, "Custom": None},
}
WIDE_PX = 600                       # two tile columns from this box width up
TITLE_FONT = ("Segoe UI", 11, "bold")


def _btn(parent, text, command, color, width=110, filled=False, state="normal", height=30, font=None):
    """A button in the app's style: outlined in *color*, or filled with it."""
    font = font or FONTS["button"]
    if filled:
        return ctk.CTkButton(parent, text=text, font=font, fg_color=color,
                             hover_color=COLORS["accent_secondary"], corner_radius=8,
                             height=height, width=width, command=command, state=state)
    return ctk.CTkButton(parent, text=text, font=font, fg_color="transparent",
                         hover_color=color, border_color=color, border_width=1,
                         text_color=COLORS["text_primary"], corner_radius=8, height=height,
                         width=width, command=command, state=state)


def _small(parent, text, color=None, **kw):
    return ctk.CTkLabel(parent, text=text, font=FONTS["body_small"],
                        text_color=color or COLORS["text_secondary"], **kw)


def _tile(parent, title=None):
    """A sub-card of the Clone Voice box. Its .body uses grid; row 0 holds the title."""
    tile = ctk.CTkFrame(parent, fg_color=COLORS["bg_card"], corner_radius=10,
                        border_width=1, border_color=COLORS["border"])
    body = ctk.CTkFrame(tile, fg_color="transparent")
    body.pack(fill="both", expand=True, padx=10, pady=(6, 8))
    body.columnconfigure(1, weight=1)
    if title:
        ctk.CTkLabel(body, text=title, font=TITLE_FONT, text_color=COLORS["text_secondary"],
                     anchor="w").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 4))
    tile.body = body
    return tile


class _Tip:
    """Hover help for a widget: a small box under it after a short delay."""

    def __init__(self, widget, text):
        self.widget, self.text = widget, text
        self._win = self._job = None
        for seq, fn in (("<Enter>", self._schedule), ("<Leave>", self._hide), ("<ButtonPress>", self._hide)):
            try:
                widget.bind(seq, fn, add="+")
            except (TypeError, ValueError, NotImplementedError):
                pass

    def _schedule(self, _e=None):
        self._cancel_job()
        if self.text:
            self._job = self.widget.after(450, self._show)

    def _cancel_job(self):
        if self._job is not None:
            try:
                self.widget.after_cancel(self._job)
            except Exception:
                pass
            self._job = None

    def _show(self):
        self._job = None
        if self._win is not None or not self.text:
            return
        try:
            x = self.widget.winfo_rootx() + 6
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
            win = tk.Toplevel(self.widget)
            win.wm_overrideredirect(True)
            win.attributes("-topmost", True)
            win.configure(bg=COLORS["border"])
            tk.Label(win, text=self.text, justify="left", wraplength=320, bg=COLORS["bg_card"],
                     fg=COLORS["text_primary"], font=FONTS["body_small"], padx=9, pady=6
                     ).pack(padx=1, pady=1)
            win.wm_geometry(f"+{x}+{y}")
            self._win = win
        except tk.TclError:
            self._win = None

    def _hide(self, _e=None):
        self._cancel_job()
        if self._win is not None:
            try:
                self._win.destroy()
            except tk.TclError:
                pass
            self._win = None


class CloneVoiceGroup:
    def __init__(self, container, app):
        self.app = app
        self.effects = None             # set by attach()
        self._busy = False
        self._cancel = threading.Event()
        self._task_name = ""
        self._names = {}                # dropdown text -> slug
        self._active_slug = None
        self._active_profile = None
        self._save_job = None
        self._clips = []                # [{"path", "seconds" (full length), "start", "end"}]
        self._cleanup_preview = None    # (original_wav, cleaned_wav) from the last cleanup preview
        self._pth = None
        self._index = None
        self._tmp_dirs = []
        self._preview_sound = None
        self._width = 0                 # box width in unscaled pixels
        self._wide = True
        self._layout_key = None
        self._custom_key = None
        self._indent = 40               # sub-option indent under a clean-up switch
        self._wraps = []                # (label, width -> wraplength)

        box = ctk.CTkFrame(container, fg_color=COLORS["bg_input"], corner_radius=10,
                           border_width=1, border_color=COLORS["border"])
        box.pack(fill="x", pady=4, padx=2)
        self.box = box
        inner = ctk.CTkFrame(box, fg_color="transparent")
        inner.pack(fill="x", padx=10, pady=8)
        inner.columnconfigure(0, weight=1)
        self.inner = inner

        # header
        head = ctk.CTkFrame(inner, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew")
        ctk.CTkLabel(head, text="🧬  Clone Voice", font=FONTS["body"],
                     text_color=COLORS["text_secondary"]).pack(side="left")
        self._status = _small(head, "", COLORS["text_muted"])
        self._status.pack(side="right")
        # Shown when a newer app needs extra runtime packages (e.g. music removal).
        self._update_btn = ctk.CTkButton(head, text="⬆  Update", font=FONTS["body_small"], width=80,
                                         height=24, corner_radius=8, fg_color=COLORS["bg_card"],
                                         hover_color=COLORS["accent_primary"],
                                         border_color=COLORS["warning"], border_width=1,
                                         command=self._on_update)

        # not-installed panel: text left, button right
        self._setup = ctk.CTkFrame(inner, fg_color="transparent")
        self._setup.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        self._setup.columnconfigure(0, weight=1)
        self._setup_text = _small(self._setup, "", COLORS["text_muted"], justify="left", anchor="w")
        self._setup_text.grid(row=0, column=0, sticky="w", padx=(0, 12))
        self._wraps.append((self._setup_text, lambda w: w - 240))
        self._install_btn = _btn(self._setup, "⬇  Install voice cloning", self._on_install,
                                 COLORS["accent_primary"], width=200, filled=True)
        self._install_btn.grid(row=0, column=1, sticky="e")

        # installed: tabs
        self._tabs_var = ctk.StringVar(value="Active voice")
        self._tabs = ctk.CTkSegmentedButton(
            inner, values=["Active voice", "Create voice"], variable=self._tabs_var,
            command=self._on_tab, font=FONTS["body_small"], height=28,
            fg_color=COLORS["bg_card"], selected_color=COLORS["accent_primary"],
            selected_hover_color=COLORS["accent_primary"],
            unselected_color=COLORS["bg_card"], unselected_hover_color=COLORS["border"],
            text_color=COLORS["text_primary"])
        self._tabs.grid(row=2, column=0, sticky="ew", pady=(8, 2))
        self._active_page = self._build_active_page(inner)
        self._create_page = self._build_create_page(inner)
        self._active_page.grid(row=3, column=0, sticky="ew")
        self._create_page.grid(row=3, column=0, sticky="ew")

        # shared task row
        self._task = ctk.CTkFrame(inner, fg_color="transparent")
        self._task.grid(row=4, column=0, sticky="ew", pady=(6, 0))
        self._task.columnconfigure(0, weight=1)
        self._progress = ctk.CTkProgressBar(self._task, height=6, fg_color=COLORS["slider_track"],
                                            progress_color=COLORS["accent_primary"], corner_radius=3)
        self._progress.grid(row=0, column=0, sticky="ew", padx=(0, 10))
        self._progress.set(0)
        self._cancel_btn = ctk.CTkButton(self._task, text="✖  Cancel", font=FONTS["button"],
                                         fg_color="#CC0000", hover_color=COLORS["error"],
                                         corner_radius=8, height=28, width=96,
                                         command=self._on_cancel)
        self._cancel_btn.grid(row=0, column=1, rowspan=2)
        self._stage = _small(self._task, "", COLORS["text_secondary"], anchor="w", justify="left")
        self._stage.grid(row=1, column=0, sticky="w", pady=(2, 0))
        self._wraps.append((self._stage, lambda w: w - 120))
        self._task.grid_remove()

        inner.bind("<Configure>", self._on_resize, add="+")
        self.refresh()
        self._tick()

    # ═══════════════════════════════════════════════════════════════
    #  Page builders
    # ═══════════════════════════════════════════════════════════════
    def _seg(self, parent, values, var, command, height=26):
        return ctk.CTkSegmentedButton(
            parent, values=values, variable=var, command=command, font=FONTS["body_small"],
            height=height, fg_color=COLORS["bg_input"], selected_color=COLORS["accent_primary"],
            selected_hover_color=COLORS["accent_primary"], unselected_color=COLORS["bg_input"],
            unselected_hover_color=COLORS["border"], text_color=COLORS["text_primary"])

    @staticmethod
    def _seg_tips(seg, tips):
        """Hover tips on the buttons of a CTkSegmentedButton (private attribute, guarded)."""
        for value, text in tips.items():
            b = getattr(seg, "_buttons_dict", {}).get(value)
            if b is not None:
                _Tip(b, text)

    def _menu(self, parent, var, values, width):
        return ctk.CTkOptionMenu(parent, variable=var, values=values, command=lambda v: self._update_create(),
                                 font=FONTS["body_small"], fg_color=COLORS["bg_input"],
                                 button_color=COLORS["accent_primary"],
                                 button_hover_color=COLORS["accent_secondary"],
                                 dropdown_fg_color=COLORS["bg_card"],
                                 dropdown_hover_color=COLORS["accent_primary"],
                                 corner_radius=8, width=width, height=26)

    def _switch(self, parent, text, var, command):
        return ctk.CTkSwitch(parent, text=text, variable=var, command=command,
                             font=FONTS["body_small"], text_color=COLORS["text_primary"],
                             button_color=COLORS["accent_primary"], button_hover_color=COLORS["accent_secondary"],
                             progress_color=COLORS["success"], fg_color=COLORS["switch_off"],
                             switch_width=32, switch_height=16)

    def _slider_row(self, parent, row, label, var, lo, hi, steps, fmt, on_change, tip=None):
        """Label · slider (stretches) · value, in its own grid row of *parent*."""
        f = ctk.CTkFrame(parent, fg_color="transparent")
        f.grid(row=row, column=0, columnspan=2, sticky="ew", pady=1)
        f.columnconfigure(1, weight=1)
        lab = _small(f, label, width=64, anchor="w")
        lab.grid(row=0, column=0, sticky="w")
        if tip:
            _Tip(lab, tip)
        val = _small(f, fmt(var.get()), COLORS["text_muted"], width=52, anchor="e")

        def changed(v):
            val.configure(text=fmt(float(v)))
            on_change()
        s = styled_slider(f, var, lo, hi, width=80, command=changed)
        s.configure(number_of_steps=steps)
        s.grid(row=0, column=1, sticky="ew", padx=(0, 6))
        val.grid(row=0, column=2, sticky="e")
        f.value_label = val
        f.slider = s
        f.fmt = fmt
        return f

    def _build_active_page(self, parent):
        page = ctk.CTkFrame(parent, fg_color="transparent")
        page.columnconfigure((0, 1), weight=1, uniform="clone_cols")

        # ── Voice tile: picker, mode, base voice ──
        self._av_tile = _tile(page, "Voice")
        b = self._av_tile.body
        b.columnconfigure(1, weight=1)
        self._mode_tag = _small(b, "", COLORS["text_muted"], anchor="e")
        self._mode_tag.grid(row=0, column=1, columnspan=2, sticky="e", pady=(0, 4))
        self._voice_var = ctk.StringVar(value="None")
        self._voice_menu = ctk.CTkOptionMenu(
            b, variable=self._voice_var, values=["None"], command=self._on_voice_pick,
            font=FONTS["body"], fg_color=COLORS["bg_input"], button_color=COLORS["accent_primary"],
            button_hover_color=COLORS["accent_secondary"], dropdown_fg_color=COLORS["bg_card"],
            dropdown_hover_color=COLORS["accent_primary"], corner_radius=8, width=220, height=30)
        self._voice_menu.grid(row=1, column=0, columnspan=3, sticky="ew")
        self._empty_hint = _small(b, "Switch on a cloned voice here or in Voice Effects.",
                                  COLORS["text_muted"], anchor="w")
        self._empty_hint.grid(row=2, column=0, columnspan=3, sticky="w", pady=(6, 0))

        # Base voice: the engine voice this clone was made from. Switching the
        # clone on selects it in the TTS Engine card (conversion sounds best
        # from the same source voice).
        self._base_row = ctk.CTkFrame(b, fg_color="transparent")
        self._base_row.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        self._base_row.columnconfigure(1, weight=1)
        _small(self._base_row, "Base", width=40, anchor="w").grid(row=0, column=0, sticky="w")
        self._base_value = _small(self._base_row, "", COLORS["text_primary"], anchor="w")
        self._base_value.grid(row=0, column=1, sticky="w", padx=(0, 6))
        _Tip(self._base_value, "The TTS engine voice this clone was made from. Switching the clone "
                               "on selects it again, with its speed.")
        self._use_current_btn = _btn(self._base_row, "Use current", self._on_use_current_base,
                                     COLORS["accent_primary"], width=92, height=26)
        self._use_current_btn.grid(row=0, column=2, sticky="e")
        _Tip(self._use_current_btn, "Save the voice now selected in TTS Engine as this clone's base voice.")

        # ── Sound tile: tuning, preview, time ──
        self._snd_tile = _tile(page, "Sound")
        s = self._snd_tile.body
        self.pitch_var = ctk.DoubleVar(value=0)
        self.index_var = ctk.DoubleVar(value=0.3)
        self.protect_var = ctk.DoubleVar(value=0.33)
        self.steps_var = ctk.DoubleVar(value=10)
        self._pitch_row = self._slider_row(s, 1, "Pitch", self.pitch_var, -24, 24, 48,
                                           lambda v: f"{int(round(v)):+d} st", self._params_changed,
                                           "Shifts the cloned voice up or down, in semitones.")
        self._match_btn = _btn(self._pitch_row, "🎯", self._on_match_pitch, COLORS["accent_primary"],
                               width=30, height=24)
        self._match_btn.grid(row=0, column=3, padx=(6, 0))
        _Tip(self._match_btn, "Match pitch: measure the base voice and your clips, then set Pitch "
                              "so they line up.")
        self._index_row = self._slider_row(s, 2, "Index", self.index_var, 0, 1, 100,
                                           lambda v: f"{v:.2f}", self._params_changed,
                                           "Index rate: higher pulls the accent closer to the target voice.")
        self._protect_row = self._slider_row(s, 3, "Protect", self.protect_var, 0, 0.5, 50,
                                             lambda v: f"{v:.2f}", self._params_changed,
                                             "Keeps breaths and consonants clean. 0.33 suits most voices.")
        self._steps_row = self._slider_row(s, 4, "Quality", self.steps_var, 4, 50, 46,
                                           lambda v: f"{int(round(v))} steps", self._params_changed,
                                           "More steps sound smoother but take longer. "
                                           "10 is a good balance on this GPU.")
        # A child of the page (not a tile), so _layout() can grid it into
        # either tile with in_=; created last so it stacks above both.
        foot = ctk.CTkFrame(page, fg_color=COLORS["bg_card"], bg_color=COLORS["bg_card"], corner_radius=0)
        foot.columnconfigure(1, weight=1)
        self._preview_row = foot
        self._preview_btn = _btn(foot, "▶  Preview", self._on_preview, COLORS["success"], width=104)
        self._preview_btn.grid(row=0, column=0, sticky="w")
        self._active_est = _small(foot, "", COLORS["text_muted"], anchor="w", justify="left")
        self._active_est.grid(row=0, column=1, sticky="w", padx=(10, 0))
        self._wraps.append((self._active_est, lambda w: (w // 2 if self._wide else w) - 150))
        return page

    def _build_create_page(self, parent):
        page = ctk.CTkFrame(parent, fg_color="transparent")
        page.columnconfigure((0, 1), weight=1, uniform="clone_cols")

        # ── New voice tile: name, mode, base voice ──
        self._cv_tile = _tile(page, "New voice")
        b = self._cv_tile.body
        _small(b, "Name", width=44, anchor="w").grid(row=1, column=0, sticky="w")
        self._name_entry = ctk.CTkEntry(b, placeholder_text="e.g. My narrator", font=FONTS["body"],
                                        fg_color=COLORS["bg_input"], border_color=COLORS["border"],
                                        text_color=COLORS["text_primary"], width=120, height=30)
        self._name_entry.grid(row=1, column=1, sticky="ew")
        self._name_entry.bind("<KeyRelease>", lambda e: self._update_create())
        _small(b, "Mode", width=44, anchor="w").grid(row=2, column=0, sticky="w", pady=(6, 0))
        self._mode_var = ctk.StringVar(value="Quick")
        self._mode_seg = self._seg(b, MODES, self._mode_var, lambda v: self._update_create())
        self._mode_seg.grid(row=2, column=1, sticky="ew", pady=(6, 0))
        self._seg_tips(self._mode_seg, MODE_HINTS)
        _small(b, "Base", width=44, anchor="w").grid(row=3, column=0, sticky="w", pady=(6, 0))
        self._base_label = _small(b, "", COLORS["text_primary"], anchor="w")
        self._base_label.grid(row=3, column=1, sticky="w", pady=(6, 0))
        _Tip(self._base_label, "The voice selected in TTS Engine. It is saved with the clone and "
                               "selected again when you switch the clone on.")

        # ── Recordings tile ──
        self._rec_tile = _tile(page, "Recordings")
        r = self._rec_tile.body
        self._clips_label = _small(r, "No clips", COLORS["text_muted"], anchor="e")
        self._clips_label.grid(row=0, column=1, sticky="e", pady=(0, 4))
        self._rec_btns = ctk.CTkFrame(r, fg_color="transparent")
        self._rec_btns.grid(row=1, column=0, columnspan=2, sticky="ew")
        add = _btn(self._rec_btns, "📥  Add clips", self._on_add_clips, COLORS["accent_primary"], width=90)
        _Tip(add, "Import recordings: WAV, MP3, M4A, FLAC, OGG, Opus, WebM and more.")
        self._record_btn = _btn(self._rec_btns, "🎙  Record", self._on_record, COLORS["accent_secondary"], width=90)
        _Tip(self._record_btn, "Record with your microphone, preview it, then add it.")
        self._analyze_btn = _btn(self._rec_btns, "🔍  Analyze", self._on_analyze, COLORS["success"], width=90,
                                 state="disabled")
        _Tip(self._analyze_btn, "Find the English engine voice that clones best from these clips, "
                                "and use it as the base voice.")
        clear = _btn(self._rec_btns, "✖  Clear", self._on_clear_clips, COLORS["error"], width=90)
        self._rec_btn_list = [add, self._record_btn, self._analyze_btn, clear]
        self._clips_note = _small(r, "", COLORS["text_muted"], anchor="w", justify="left")
        self._clips_note.grid(row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self._wraps.append((self._clips_note, lambda w: (w // 2 if self._wide else w) - 40))

        # ── Model files tile (Import model) ──
        self._model_tile = _tile(page, "Model files")
        m = self._model_tile.body
        _btn(m, "📂  Model .pth", self._on_pick_pth, COLORS["accent_primary"],
             width=120).grid(row=1, column=0, sticky="w", pady=2)
        self._pth_label = _small(m, "No file", COLORS["text_muted"], anchor="w")
        self._pth_label.grid(row=1, column=1, sticky="w", padx=(10, 0))
        _btn(m, "📂  Index", self._on_pick_index, COLORS["accent_primary"],
             width=120).grid(row=2, column=0, sticky="w", pady=2)
        self._index_label = _small(m, "Optional", COLORS["text_muted"], anchor="w")
        self._index_label.grid(row=2, column=1, sticky="w", padx=(10, 0))

        self._build_prep(page)

        # ── Training tile (Train (RVC)) ──
        self._train_tile = _tile(page, "Training")
        t = self._train_tile.body
        self.epochs_var = ctk.DoubleVar(value=150)
        self._slider_row(t, 1, "Epochs", self.epochs_var, 10, 500, 49, lambda v: f"{int(round(v))}",
                         self._update_create, "More epochs learn the voice better but take longer. "
                                              "150 is typical.")
        opt = ctk.CTkFrame(t, fg_color="transparent")
        opt.grid(row=2, column=0, sticky="w", pady=(4, 0))
        lab = _small(opt, "Batch", width=64, anchor="w")
        lab.pack(side="left")
        _Tip(lab, "Clips trained at once. 2 fits a 4 GB GPU; 4 is faster but may run out of memory.")
        self.batch_var = ctk.StringVar(value="2")
        self._menu(opt, self.batch_var, ["1", "2", "4"], 60).pack(side="left", padx=(0, 12))
        lab = _small(opt, "Rate")
        lab.pack(side="left", padx=(0, 6))
        _Tip(lab, "Model sample rate. 40 kHz suits most voices.")
        self.sr_var = ctk.StringVar(value="40 kHz")
        self._menu(opt, self.sr_var, list(SR_CHOICES), 84).pack(side="left")
        self._train_opt = opt
        self.match_var = ctk.BooleanVar(value=True)
        self._match_switch = self._switch(t, "Match pitch to base voice", self.match_var, None)
        _Tip(self._match_switch, "Measures the pitch of the base voice and your clips, then sets the "
                                 "clone's Pitch so they line up.")

        # ── Footer: estimate and the main action ──
        self._footer = ctk.CTkFrame(page, fg_color="transparent")
        self._footer.columnconfigure(0, weight=1)
        self._create_est = _small(self._footer, "", COLORS["text_secondary"], anchor="w", justify="left")
        self._create_est.grid(row=0, column=0, sticky="w", padx=(2, 10))
        self._wraps.append((self._create_est, lambda w: w - 190))
        self._create_btn = _btn(self._footer, "🧬  Create Voice", self._on_create, COLORS["accent_primary"],
                                width=150, filled=True, height=32)
        self._create_btn.grid(row=0, column=1, sticky="e")
        return page

    def _build_prep(self, page):
        """Optional clip preparation, applied in order: trim, remove music, skip silences."""
        self._clean_tile = _tile(page, "Clean-up")
        body = self._clean_tile.body
        self._prep = body

        # 1. use part of each recording
        self._trim_col = ctk.CTkFrame(body, fg_color="transparent")
        self.trim_on = ctk.BooleanVar(value=False)
        sw = self._switch(self._trim_col, "Use part of each recording", self.trim_on, self._update_create)
        sw.grid(row=0, column=0, sticky="w")
        self._trim_tip = _Tip(sw, "")
        self.trim_var = ctk.StringVar(value="20 s")
        self._trim_seg = self._seg(self._trim_col, list(TRIM_PRESETS["Quick"]), self.trim_var,
                                   lambda v: self._update_create(), height=24)
        self._trim_seg.grid(row=1, column=0, sticky="w", padx=(40, 0), pady=(4, 0))
        self._custom_list = ctk.CTkFrame(body, fg_color="transparent")

        # 2. remove background music & noise
        self._clean_col = ctk.CTkFrame(body, fg_color="transparent")
        self.clean_on = ctk.BooleanVar(value=False)
        sw = self._switch(self._clean_col, "Remove music & noise", self.clean_on, self._update_create)
        sw.grid(row=0, column=0, sticky="w")
        _Tip(sw, "Separates the voice from background music with Demucs. The first use downloads "
                 "the model (about 80 MB).")
        self.denoise_var = ctk.BooleanVar(value=False)
        self._denoise_cb = ctk.CTkCheckBox(
            self._clean_col, text="Also reduce hiss & hum", variable=self.denoise_var,
            font=FONTS["body_small"], text_color=COLORS["text_secondary"],
            fg_color=COLORS["accent_primary"], hover_color=COLORS["accent_secondary"],
            border_color=COLORS["text_muted"], checkbox_width=16, checkbox_height=16,
            command=self._update_create)
        self._denoise_cb.grid(row=1, column=0, sticky="w", padx=(40, 0), pady=(6, 0))
        self._cleanup_btn = _btn(self._clean_col, "🎧  Preview cleanup", self._on_preview_cleanup,
                                 COLORS["accent_primary"], width=120, height=26, font=FONTS["body_small"])
        self._cleanup_btn.grid(row=2, column=0, sticky="w", padx=(40, 0), pady=(6, 0))
        _Tip(self._cleanup_btn, "Clean up to 30 s of the first clip so you can compare before and after.")
        self._ab_row = ctk.CTkFrame(self._clean_col, fg_color="transparent")
        self._ab_row.grid(row=3, column=0, sticky="w", padx=(40, 0), pady=(4, 0))
        self._play_orig_btn = _btn(self._ab_row, "▶ Original", lambda: self._play_preview(0),
                                   COLORS["text_muted"], width=60, height=24, font=FONTS["body_small"])
        self._play_orig_btn.pack(side="left", padx=(0, 6))
        self._play_clean_btn = _btn(self._ab_row, "▶ Cleaned", lambda: self._play_preview(1),
                                    COLORS["success"], width=60, height=24, font=FONTS["body_small"])
        self._play_clean_btn.pack(side="left")

        # 3. skip silences (applied last, to the trimmed / cleaned audio)
        self._skip_col = ctk.CTkFrame(body, fg_color="transparent")
        self.skipsil_on = ctk.BooleanVar(value=False)
        sw = self._switch(self._skip_col, "Skip silences", self.skipsil_on, self._update_create)
        sw.grid(row=0, column=0, sticky="w")
        _Tip(sw, "Applied last: removes pauses longer than 0.4 s from the trimmed and cleaned audio.")

    def _layout_prep(self, three_cols):
        """Clean-up options side by side (tile spans the box) or stacked.

        Side by side, sub-options line up with their switch so the columns
        fit; stacked, they are indented under it.
        """
        body = self._prep
        custom = self._custom_list.winfo_children() != []
        indent = 0 if three_cols else 40
        if indent != self._indent:
            self._indent = indent
            self._refresh_prep(self._mode_var.get())
        for i in range(3):
            body.columnconfigure(i, weight=1 if three_cols or i == 0 else 0, uniform="")
        if three_cols:
            for i, col in enumerate((self._trim_col, self._clean_col, self._skip_col)):
                col.grid(row=1, column=i, columnspan=1, sticky="nw", padx=(0 if i == 0 else 10, 0), pady=0)
            if custom:
                self._custom_list.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        else:
            r = 1
            self._trim_col.grid(row=r, column=0, columnspan=3, sticky="w", padx=0, pady=0)
            if custom:
                r += 1
                self._custom_list.grid(row=r, column=0, columnspan=3, sticky="ew", pady=(6, 0))
            self._clean_col.grid(row=r + 1, column=0, columnspan=3, sticky="w", padx=0, pady=(8, 0))
            self._skip_col.grid(row=r + 2, column=0, columnspan=3, sticky="w", padx=0, pady=(8, 0))
        if not custom:
            self._custom_list.grid_remove()

    def _layout_rec_buttons(self, one_row):
        for i, b in enumerate(self._rec_btn_list):
            r, c = (0, i) if one_row else divmod(i, 2)
            pad_l = 0 if c == 0 else 6
            b.grid(row=r, column=c, sticky="ew", padx=(pad_l, 0), pady=(0 if r == 0 else 6, 0))
        for c in range(4):
            self._rec_btns.columnconfigure(c, weight=1 if (one_row or c < 2) else 0,
                                           uniform="recb" if (one_row or c < 2) else "")

    @staticmethod
    def _place(page, rows, wide):
        """Grid tiles row by row: two per row when wide, otherwise one; a lone tile spans."""
        for w in page.winfo_children():
            w.grid_remove()
        r = 0
        for row in rows:
            row = [t for t in row if t is not None]
            if wide and len(row) == 2:
                row[0].grid(row=r, column=0, columnspan=1, sticky="nsew", padx=(0, 4), pady=4)
                row[1].grid(row=r, column=1, columnspan=1, sticky="nsew", padx=(4, 0), pady=4)
                r += 1
                continue
            for t in row:
                t.grid(row=r, column=0, columnspan=2, sticky="nsew", padx=0, pady=4)
                r += 1

    def _layout(self, force=False):
        """Place the tiles for the current mode, voice and box width."""
        mode = self._mode_var.get()
        has_voice = self._active_profile is not None
        custom = bool(self._custom_list.winfo_children())
        one_row = (not self._wide) and self._width >= 470
        rvc = has_voice and self._active_profile.mode == "rvc"
        key = (self._wide, one_row, mode, has_voice, rvc, custom)
        if key == self._layout_key and not force:
            return
        self._layout_key = key
        self._place(self._active_page, [[self._av_tile, self._snd_tile if has_voice else None]], self._wide)
        if mode == "Import model":
            rows = [[self._cv_tile, self._model_tile], [self._footer]]
        elif mode == "Train (RVC)":
            rows = [[self._cv_tile, self._rec_tile], [self._clean_tile], [self._train_tile], [self._footer]]
        else:
            rows = [[self._cv_tile, self._rec_tile], [self._clean_tile], [self._footer]]
        self._place(self._create_page, rows, self._wide)
        self._layout_prep(three_cols=self._wide)
        self._layout_rec_buttons(one_row)
        # match-pitch switch beside Batch / Rate when there is room, else below
        if self._wide or one_row:
            self._match_switch.grid(row=2, column=1, sticky="e", padx=(12, 0), pady=(4, 0))
        else:
            self._match_switch.grid(row=3, column=0, columnspan=2, sticky="w", padx=0, pady=(8, 0))
        # ▶ Preview row: under the base voice for RVC (three sliders on the right),
        # under the Quality slider for Quick, so both tiles end level
        rvc = has_voice and self._active_profile.mode == "rvc"
        host = self._av_tile.body if rvc else self._snd_tile.body
        self._preview_row.grid(in_=host, row=5, column=0, columnspan=3, sticky="ew", padx=0,
                               pady=(10 if rvc else 8, 0))

    def _on_resize(self, event):
        try:
            scale = self.inner._get_widget_scaling()
        except Exception:
            scale = 1.0
        width = int(event.width / max(scale, 0.1))
        if abs(width - self._width) < 8:
            return
        self._width = width
        self._wide = width >= WIDE_PX
        for label, fn in self._wraps:
            try:
                label.configure(wraplength=max(120, fn(width)))
            except Exception:
                pass
        self._layout()

    # ── clip selection logic ───────────────────────────────────────
    def _selections(self):
        """[{"path", "start", "end"}] that cloning will use, given the trim options.

        "First N s" counts recording time from the start of the first clip and
        continues into the next clips until N seconds are covered.
        """
        clips = self._clips
        mode = self._mode_var.get()
        if not self.trim_on.get() or mode not in TRIM_PRESETS:
            return [{"path": c["path"]} for c in clips]
        limit = TRIM_PRESETS[mode].get(self.trim_var.get())
        if limit is None:                                       # Custom
            return [{"path": c["path"], "start": c.get("start"), "end": c.get("end")} for c in clips]
        out, left = [], float(limit)
        for c in clips:
            if left <= 0.05:
                break
            take = min(left, c["seconds"])
            out.append({"path": c["path"]} if take >= c["seconds"] - 0.01
                       else {"path": c["path"], "start": 0.0, "end": round(take, 3)})
            left -= take
        return out

    def _sel_seconds(self, sel, full):
        start = sel.get("start") or 0.0
        end = sel.get("end") if sel.get("end") is not None else full
        return max(0.0, end - start)

    def _clip_seconds(self):
        """Seconds of audio cloning will use (after the trim options)."""
        full = {c["path"]: c["seconds"] for c in self._clips}
        return sum(self._sel_seconds(s, full[s["path"]]) for s in self._selections())

    def _prep_options(self):
        mode = self._mode_var.get()
        trim = None
        if self.trim_on.get() and mode in TRIM_PRESETS:
            limit = TRIM_PRESETS[mode].get(self.trim_var.get())
            trim = "custom" if limit is None else f"first{limit}"
        return {"trim": trim, "separate": bool(self.clean_on.get()), "denoise": bool(self.denoise_var.get()),
                "skip_silences": bool(self.skipsil_on.get())}

    def _refresh_prep(self, mode):
        """Show / hide the clean-up options for the current mode and selection."""
        if mode == "Import model":
            return
        presets = list(TRIM_PRESETS[mode])
        if self._trim_seg.cget("values") != presets:
            self._trim_seg.configure(values=presets)
        if self.trim_var.get() not in presets:
            self.trim_var.set(presets[1])
        first = "the first N seconds of speech, counted from the first clip into the next"
        self._trim_tip.text = (f"Use {first}, or Custom ranges picked on each clip's timeline."
                               + (" Quick mode reads at most 25 s." if mode == "Quick" else ""))
        pad = (self._indent, 0)
        show = [(self._trim_seg, self.trim_on.get()),
                (self._denoise_cb, self.clean_on.get()), (self._cleanup_btn, self.clean_on.get()),
                (self._ab_row, self.clean_on.get() and bool(self._cleanup_preview))]
        for w, on in show:
            if on:
                w.grid(padx=pad)
            else:
                w.grid_remove()

        custom = self.trim_on.get() and TRIM_PRESETS[mode].get(self.trim_var.get()) is None
        ckey = tuple((c["path"], c.get("start"), c.get("end"), c["seconds"]) for c in self._clips) if custom else None
        if ckey != self._custom_key:
            self._custom_key = ckey
            for w in self._custom_list.winfo_children():
                w.destroy()
            for i, c in enumerate(self._clips if custom else []):
                row = ctk.CTkFrame(self._custom_list, fg_color=COLORS["bg_input"], corner_radius=8)
                row.pack(fill="x", pady=2)
                name = os.path.basename(c["path"])
                _small(row, name if len(name) <= 32 else name[:29] + "…", COLORS["text_primary"],
                       anchor="w").pack(side="left", padx=(8, 10), pady=4)
                s0 = c.get("start") or 0.0
                e0 = c.get("end") if c.get("end") is not None else c["seconds"]
                whole = c.get("start") is None and c.get("end") is None
                txt = (f"all · {self._fmt_len(c['seconds'])}" if whole else
                       f"{self._clock(s0)}–{self._clock(e0)} · {e0 - s0:.1f} s")
                _small(row, txt, COLORS["text_secondary"] if whole else COLORS["success"]).pack(side="left")
                ctk.CTkButton(row, text="✂  Edit", font=FONTS["body_small"], width=64, height=24,
                              corner_radius=6, fg_color="transparent", hover_color=COLORS["accent_primary"],
                              border_color=COLORS["accent_primary"], border_width=1,
                              command=lambda i=i: self._edit_clip(i)).pack(side="right", padx=6, pady=3)
        self._cleanup_btn.configure(state="normal" if self._clips and not self._busy else "disabled")
        self._analyze_btn.configure(state="normal" if self._clips and not self._busy else "disabled")

    @staticmethod
    def _clock(t):
        m, s = divmod(int(round(t)), 60)
        return f"{m}:{s:02d}"

    def _edit_clip(self, i):
        from ui.panels.timeline_dialog import TimelineDialog
        c = self._clips[i]
        mode = self._mode_var.get()
        quick = (10, 20, 25) if mode == "Quick" else (30, 60, 120)

        def done(start, end, duration):
            c["start"], c["end"] = start, end
            c["seconds"] = round(duration, 2)
            self._cleanup_preview = None
            self._update_create()
        TimelineDialog(self.app, c["path"], c.get("start"), c.get("end"), on_done=done, quick_lengths=quick)

    # ═══════════════════════════════════════════════════════════════
    #  Wiring and refresh
    # ═══════════════════════════════════════════════════════════════
    def attach(self, effects_panel):
        """Link to the Voice Effects card (built after Customization)."""
        self.effects = effects_panel
        effects_panel.subscribe(CLONE_GROUP, self._on_active_changed)
        self._on_active_changed(effects_panel.active_in_group(CLONE_GROUP))

    def _ui(self, fn, *args):
        """Run fn on the Tk thread (safe from worker threads)."""
        try:
            self.app.after(0, lambda: fn(*args))
        except RuntimeError:
            pass   # window already closed

    def refresh(self):
        """Re-read runtime status and the voice list; update every visible widget."""
        st = runtime.status()
        installed = all(e["installed"] for e in st["engines"].values())
        voices = library.list_voices(ready_only=True)
        if st["frozen_unsupported"]:
            self._set_status("Not available in this build", COLORS["text_muted"])
            self._setup_text.configure(text="Voice cloning setup is available when running "
                                            "Adits Voice Studio from source. It is not bundled with the installer yet.")
            self._install_btn.grid_remove()
        elif not installed:
            self._set_status("Not installed", COLORS["text_muted"])
            missing = [e["label"] for e in st["engines"].values() if not e["installed"]]
            self._setup_text.configure(text=(
                "One-time setup of " + " and ".join(missing) + ". Downloads about 9 GB and uses about "
                f"21 GB on {os.path.splitdrive(st['root'])[0] or 'disk'}. You can keep using the app "
                "while it installs."))
        else:
            outdated = [e["label"] for e in st["engines"].values() if e.get("needs_update")]
            if outdated:
                self._set_status("Update available", COLORS["warning"])
                self._update_btn.pack(side="right", padx=(0, 8))
            else:
                self._set_status(f"Ready · {len(voices)} voice{'s' if len(voices) != 1 else ''}",
                                 COLORS["success"])
                self._update_btn.pack_forget()
        if installed:
            self._setup.grid_remove()
            self._tabs.grid()
            self._on_tab(self._tabs_var.get())
        else:
            self._setup.grid()
            self._tabs.grid_remove()
            self._active_page.grid_remove()
            self._create_page.grid_remove()

        # voice picker
        names, seen = {}, set()
        for v in voices:
            label = v.name if v.name.lower() not in seen else f"{v.name} ({v.slug})"
            seen.add(v.name.lower())
            names[label] = v.slug
        self._names = names
        self._voice_menu.configure(values=["None"] + list(names))
        self._sync_picker()
        self._update_create()

    def _set_status(self, text, color):
        self._status.configure(text=text, text_color=color)

    def _on_tab(self, value):
        if value == "Create voice":
            self._active_page.grid_remove()
            self._create_page.grid()
            self._update_create()
        else:
            self._create_page.grid_remove()
            self._active_page.grid()
            self._update_active_est()

    # ── active voice ───────────────────────────────────────────────
    def _sync_picker(self):
        slug = self._active_slug
        label = next((n for n, s in self._names.items() if s == slug), "None")
        self._voice_var.set(label)

    def _on_voice_pick(self, label):
        if self.effects is None:
            return
        slug = self._names.get(label)
        if slug:
            self.effects.set_active(KEY_PREFIX + slug, True)
        else:
            self.effects.clear_group(CLONE_GROUP)

    def _on_active_changed(self, key):
        slug = key[len(KEY_PREFIX):] if key else None
        newly_on = slug is not None and slug != self._active_slug
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
        self._show_base(p)
        if p is not None and newly_on:
            self._apply_base(p)
        rows_rvc = (self._pitch_row, self._index_row, self._protect_row)
        if p is None:
            self._mode_tag.configure(text="")
            self._empty_hint.grid()
            self._preview_btn.configure(state="disabled")
        else:
            self._empty_hint.grid_remove()
            self._preview_btn.configure(state="normal" if not self._busy else "disabled")
            if p.mode == "rvc":
                self._mode_tag.configure(text="RVC model" + (" · imported" if p.imported else ""))
                for var, val in ((self.pitch_var, p.pitch), (self.index_var, p.index_rate),
                                 (self.protect_var, p.protect)):
                    var.set(val)
                for r in rows_rvc:
                    r.grid()
                    r.value_label.configure(text=r.fmt(r.slider.get()))
                self._steps_row.grid_remove()
                self._match_btn.configure(state="normal" if p.clips and not self._busy else "disabled")
            else:
                self._mode_tag.configure(text="Quick · zero-shot")
                self.steps_var.set(p.diffusion_steps)
                for r in rows_rvc:
                    r.grid_remove()
                self._steps_row.grid()
                self._steps_row.value_label.configure(text=self._steps_row.fmt(self.steps_var.get()))
        self._layout()
        self._update_active_est()

    def _params_changed(self):
        self._update_active_est()
        if self._save_job is not None:
            self.app.after_cancel(self._save_job)
        self._save_job = self.app.after(600, self._flush_params)

    def _flush_params(self):
        """Persist slider values to the active voice's profile."""
        if self._save_job is not None:
            try:
                self.app.after_cancel(self._save_job)
            except Exception:
                pass
            self._save_job = None
        p = self._active_profile
        if p is None:
            return
        try:
            if p.mode == "rvc":
                p = library.update_params(p.slug, pitch=int(round(self.pitch_var.get())),
                                          index_rate=round(float(self.index_var.get()), 2),
                                          protect=round(float(self.protect_var.get()), 2))
            else:
                p = library.update_params(p.slug, diffusion_steps=int(round(self.steps_var.get())))
            self._active_profile = p
        except (OSError, ValueError) as e:
            print(f"Could not save voice settings: {e}")

    def get_settings(self) -> dict:
        p = self._active_profile
        if p is None:
            return {}
        return {"slug": p.slug,
                "pitch": int(round(self.pitch_var.get())),
                "index_rate": round(float(self.index_var.get()), 2),
                "protect": round(float(self.protect_var.get()), 2),
                "diffusion_steps": int(round(self.steps_var.get()))}

    def _update_active_est(self):
        p = self._active_profile
        if p is None:
            self._active_est.configure(text="")
            return
        try:
            text = self.app._text_panel.get_text()
        except Exception:
            text = ""
        speech = estimate.speech_seconds(text)
        warm = False
        try:
            from core.voice_cloning import get_client
            warm = get_client().is_warm(p.mode)
        except Exception:
            pass
        steps = int(round(self.steps_var.get()))
        if speech < 0.5:
            per10 = estimate.conversion_seconds(p.mode, 10, steps)
            msg = f"⏱  Adds {estimate.fmt(per10)} per 10 s of speech"
        else:
            t = estimate.conversion_seconds(p.mode, speech, steps)
            msg = f"⏱  Adds {estimate.fmt(t)} to this text"
        if not warm:
            first = estimate.conversion_seconds(p.mode, 0, steps, first_use=True)
            msg += f", +{estimate.fmt(first).replace('about ', '')} the first time"
        self._active_est.configure(text=msg)

    def _tick(self):
        """Keep the time estimate in step with the text box."""
        try:
            if self._tabs_var.get() == "Active voice" and self._active_profile is not None:
                self._update_active_est()
            self.app.after(2000, self._tick)
        except Exception:
            pass

    # ── create page state ──────────────────────────────────────────
    def _engine_selection(self):
        try:
            return self.app._engine_panel.get_voice_selection()
        except Exception:
            return None

    @staticmethod
    def _selection_for(p):
        """The profile's base selection; older voices only stored engine + voice."""
        if p is None:
            return None
        if p.base_selection:
            return p.base_selection
        if p.base_engine:
            from ui.panels.engine_panel import EnginePanel
            return {"engine": p.base_engine, "kind": EnginePanel.engine_kind(p.base_engine),
                    "voice": p.base_voice or None}
        return None

    def _show_base(self, p):
        from ui.panels.engine_panel import EnginePanel
        sel = self._selection_for(p)
        if p is None:
            self._base_row.grid_remove()
            return
        self._base_row.grid()
        if sel:
            self._base_value.configure(text=EnginePanel.describe_selection(sel),
                                       text_color=COLORS["text_primary"])
        else:
            self._base_value.configure(text="not set", text_color=COLORS["text_muted"])

    def _apply_base(self, p):
        """Select the voice's base engine voice in the TTS Engine card."""
        from ui.panels.engine_panel import EnginePanel
        sel = self._selection_for(p)
        if not sel:
            self.app._set_status(f"ℹ  '{p.name}' has no base voice yet. Pick an engine voice and press "
                                 "Use current in Clone Voice.", COLORS["text_secondary"])
            return
        err = self.app._engine_panel.apply_voice_selection(sel)
        if err:
            self.app._set_status(f"⚠  Base voice for '{p.name}' not selected: {err}", COLORS["warning"])
        else:
            self.app._set_status(f"🔁  TTS Engine set to {EnginePanel.describe_selection(sel)}, "
                                 f"the base voice of '{p.name}'", COLORS["accent_primary"])
        self._update_create()

    def _on_use_current_base(self):
        from ui.panels.engine_panel import EnginePanel
        p = self._active_profile
        sel = self._engine_selection()
        if p is None or not sel:
            return
        p = library.update_params(p.slug, base_selection=sel, base_engine=sel["engine"],
                                  base_voice=str(sel.get("voice") or ""))
        self._active_profile = p
        self._show_base(p)
        hint = " Press 🎯 Match to re-measure the pitch." if p.mode == "rvc" and p.clips else ""
        self.app._set_status(f"✅  Base voice of '{p.name}' set to "
                             f"{EnginePanel.describe_selection(sel)}.{hint}", COLORS["success"])

    def _base_voice(self):
        try:
            s = self.app._engine_panel.get_settings()
        except Exception:
            return "", "", None
        name = s["engine_name"]
        voice = (s["edge_voice_key"] if "Edge" in name else
                 s["kokoro_voice_key"] if "Kokoro" in name else
                 s["piper_voice_key"] if "Piper" in name else
                 (s["melo_voice_cfg"] or {}).get("speaker", "") if "Melo" in name else
                 s["pyttsx3_voice_id"] if "pyttsx3" in name else "")
        return name, str(voice), s

    def _update_create(self):
        mode = self._mode_var.get()
        from ui.panels.engine_panel import EnginePanel
        sel = self._engine_selection()
        self._base_label.configure(text=EnginePanel.describe_selection(sel) if sel else "not set")
        self._refresh_prep(mode)
        self._layout()

        secs = self._clip_seconds()
        n = len(self._clips)
        ok, color, note = True, COLORS["success"], ""
        if mode == "Quick":
            if secs < 3:
                ok, color, note = False, COLORS["error"], "need at least 3 s"
            elif secs < 8:
                color, note = COLORS["warning"], "10–25 s works best"
        elif mode == "Train (RVC)":
            if secs < 30:
                ok, color, note = False, COLORS["error"], "need at least 30 s"
            elif secs < 60:
                color, note = COLORS["warning"], "under 1 min: Quick mode is recommended"
            elif secs < 180:
                color, note = COLORS["warning"], "under 3 min: expect rough results"
        if mode != "Import model":
            total = sum(c["seconds"] for c in self._clips)
            self._clips_label.configure(
                text=f"{n} clip{'s' if n != 1 else ''} · {self._fmt_len(total)}" if n else "No clips",
                text_color=color if n else COLORS["text_muted"])
            parts = []
            if n and abs(total - secs) >= 0.05:
                parts.append(f"Using {self._fmt_len(secs)}")
            if n and note:
                parts.append(note[0].upper() + note[1:])
            self._clips_note.configure(text=" · ".join(parts), text_color=color)
            (self._clips_note.grid if parts else self._clips_note.grid_remove)()
        else:
            ok = bool(self._pth)

        # estimate
        if mode == "Quick":
            est = f"⏱  Ready in {estimate.fmt(estimate.quick_create_seconds(secs))}" if n else ""
        elif mode == "Train (RVC)":
            if n:
                t = estimate.training_seconds(secs, int(round(self.epochs_var.get())),
                                              int(self.batch_var.get()), SR_CHOICES[self.sr_var.get()])
                est = (f"⏱  {estimate.fmt(t['total'])[0].upper()}{estimate.fmt(t['total'])[1:]}: "
                       f"prepare {estimate.fmt(t['slice'] + t['features']).replace('about ', '')}, "
                       f"then {estimate.fmt(t['per_epoch']).replace('about ', '')} per epoch")
                if self.batch_var.get() == "4":
                    est += " · batch 4 may not fit a 4 GB GPU"
            else:
                est = ""
        else:
            est = "⏱  Ready in about 15 s" if self._pth else ""
        if est and mode != "Import model" and n and self.clean_on.get():
            warm = False
            try:
                from core.voice_cloning import get_client
                warm = get_client().is_warm("rvc")
            except Exception:
                pass
            extra = estimate.separation_seconds(secs, first_use=not warm)
            est += f" · music removal +{estimate.fmt(extra).replace('about ', '')}"
        if not est:
            est = ("Pick a model file to import." if mode == "Import model"
                   else "Add or record clips of the voice to clone.")
        self._create_est.configure(text=est, text_color=COLORS["text_secondary"] if n or self._pth
                                   else COLORS["text_muted"])
        name_ok = bool(self._name_entry.get().strip())
        self._create_btn.configure(state="normal" if ok and name_ok and not self._busy else "disabled")

    @staticmethod
    def _fmt_len(secs):
        m, s = divmod(int(round(secs)), 60)
        return f"{m} min {s:02d} s" if m else f"{s} s"

    def _on_add_clips(self):
        paths = filedialog.askopenfilenames(title="Choose recordings of the target voice",
                                            filetypes=AUDIO_TYPES)
        if not paths:
            return
        known = {c["path"] for c in self._clips}
        for p in paths:
            if p in known:
                continue
            try:
                self._clips.append({"path": p, "seconds": round(library.audio_seconds(p), 2),
                                    "start": None, "end": None})
            except Exception as e:
                messagebox.showwarning("Clip skipped", f"Could not read {os.path.basename(p)}:\n{e}",
                                       parent=self.app)
        if not self._name_entry.get().strip() and self._clips:
            self._name_entry.insert(0, os.path.splitext(os.path.basename(self._clips[0]["path"]))[0][:40])
        self._update_create()

    def _on_record(self):
        """Record a clip with the microphone; the take is added like an imported file."""
        from ui.panels.record_dialog import RecordDialog
        win = getattr(self, "_recorder", None)
        if win is not None and win.winfo_exists():
            win.lift()
            return

        def done(path, seconds):
            self._clips.append({"path": path, "seconds": round(seconds, 2), "start": None, "end": None})
            if not self._name_entry.get().strip():
                self._name_entry.insert(0, "My voice")
            self._cleanup_preview = None
            self._update_create()
        self._recorder = RecordDialog(self.app, str(runtime.runtime_root() / "recordings"), on_done=done)

    def _on_analyze(self):
        """Voice match: rank engine voices by how close they sound to the selected clip audio."""
        from ui.panels.analyzer_dialog import AnalyzerDialog
        win = getattr(self, "_analyzer", None)
        if win is not None and win.winfo_exists():
            win.lift()
            return
        if not self._clips:
            return
        self._analyzer = AnalyzerDialog(self.app, self._selections(), clean=bool(self.clean_on.get()),
                                        denoise=bool(self.denoise_var.get()),
                                        on_applied=lambda sel: self._update_create())

    def _on_clear_clips(self):
        self._clips = []
        self._cleanup_preview = None
        self._update_create()

    def _on_pick_pth(self):
        p = filedialog.askopenfilename(title="Choose an RVC voice model",
                                       filetypes=[("RVC model", "*.pth"), ("All files", "*.*")])
        if p:
            self._pth = p
            self._pth_label.configure(text=os.path.basename(p), text_color=COLORS["text_primary"])
            if not self._name_entry.get().strip():
                self._name_entry.insert(0, os.path.splitext(os.path.basename(p))[0][:40])
            self._update_create()

    def _on_pick_index(self):
        p = filedialog.askopenfilename(title="Choose the model's index file (optional)",
                                       filetypes=[("Index", "*.index"), ("All files", "*.*")])
        if p:
            self._index = p
            self._index_label.configure(text=os.path.basename(p), text_color=COLORS["text_primary"])

    # ═══════════════════════════════════════════════════════════════
    #  Task plumbing
    # ═══════════════════════════════════════════════════════════════
    def _begin(self, name, stage):
        self._busy = True
        self._task_name = name
        self._cancel.clear()
        self._progress.set(0)
        self._progress.configure(progress_color=COLORS["accent_primary"])
        self._stage.configure(text=stage, text_color=COLORS["text_secondary"])
        self._cancel_btn.configure(state="normal", text="✖  Cancel")
        self._task.grid()
        for b in (self._create_btn, self._install_btn, self._preview_btn, self._match_btn):
            b.configure(state="disabled")

    def _end(self, message, color, keep_seconds=8):
        self._busy = False
        self._progress.set(1.0 if color == COLORS["success"] else 0)
        self._stage.configure(text=message, text_color=color)
        self._cancel_btn.configure(state="disabled")
        self._install_btn.configure(state="normal")
        self.refresh()
        self._on_active_changed(self.effects.active_in_group(CLONE_GROUP) if self.effects else None)
        self.app.after(keep_seconds * 1000, lambda: (not self._busy) and self._task.grid_remove())

    def _set_progress(self, frac, text=None):
        if frac is not None:
            self._progress.set(max(0.0, min(1.0, float(frac))))
        if text:
            self._stage.configure(text=text)

    def _on_cancel(self):
        if self._busy:
            self._cancel.set()
            self._cancel_btn.configure(state="disabled", text="Cancelling…")
            self._stage.configure(text="Cancelling…", text_color=COLORS["warning"])

    def _run(self, name, stage, work):
        """Start *work* (a callable) on a daemon thread with standard error handling."""
        if self._busy:
            return
        self._begin(name, stage)

        def runner():
            try:
                msg = work()
                self._ui(self._end, msg or "Done", COLORS["success"])
            except VCCancelled:
                self._ui(self._end, f"{name} cancelled", COLORS["warning"])
            except VCError as e:
                self._ui(self._fail, name, e.message, e.hint)
            except Exception as e:   # noqa: BLE001
                self._ui(self._fail, name, f"{type(e).__name__}: {e}", None)
        threading.Thread(target=runner, daemon=True, name=f"clone-{name}").start()

    def _fail(self, name, message, hint):
        self._end(f"❌  {name} failed: {message}", COLORS["error"], keep_seconds=30)
        self.app._set_status(f"❌  {name} failed: {message}", COLORS["error"])
        messagebox.showerror(f"{name} failed", message + (f"\n\n{hint}" if hint else ""), parent=self.app)

    def _check_cancel(self):
        if self._cancel.is_set():
            raise VCCancelled()

    def _synthesize(self, text, eng_settings, tmp):
        """Synthesize *text* with the selected engine; returns (float32 samples, sr)."""
        import numpy as np
        import torch
        from core.audio_io import load_segment
        app = self.app
        engine = app._engine_map.get(eng_settings["engine_name"])
        if engine is None:
            raise VCError("The selected TTS engine is not available.")
        use_gpu = eng_settings["use_gpu"] and torch.cuda.is_available()
        kwargs = app._engine_kwargs(eng_settings, "cuda" if use_gpu else "cpu",
                                    lambda m: None, self._cancel.is_set)
        with app._engine_lock:
            res = engine.synthesize(text, tmp, **kwargs)
        self._check_cancel()
        seg = load_segment(res["playback_file"]).set_channels(1)
        peak = float(2 ** (8 * seg.sample_width - 1))
        return np.array(seg.get_array_of_samples(), dtype=np.float32) / peak, seg.frame_rate

    def _new_tmp(self):
        d = tempfile.mkdtemp(prefix="adits_vs_clone_")
        self._tmp_dirs.append(d)
        return d

    # ═══════════════════════════════════════════════════════════════
    #  Tasks
    # ═══════════════════════════════════════════════════════════════
    def _on_install(self):
        if not messagebox.askyesno(
                "Install voice cloning",
                "This downloads about 9 GB (two Python environments, PyTorch, and voice models) "
                f"into:\n{runtime.runtime_root()}\n\nIt needs Python 3.11 and 3.12 and Git. "
                "Continue?", parent=self.app):
            return

        def work():
            def prog(frac, msg):
                self._ui(self._set_progress, frac, msg)
            runtime.install(progress_cb=prog, log_cb=lambda line: None, cancel_event=self._cancel)
            return "✅  Voice cloning is installed"
        self._run("Install", "Starting install…", work)

    def _on_update(self):
        st = runtime.status()
        outdated = [n for n, e in st["engines"].items() if e.get("needs_update")]
        if not outdated or not messagebox.askyesno(
                "Update voice cloning",
                "This version adds packages to the voice-cloning runtime (for example music "
                "removal). The update installs only what is missing, usually a small download, then "
                "re-checks the engines. Continue?", parent=self.app):
            return

        def work():
            runtime.install(engines=tuple(outdated),
                            progress_cb=lambda f, m: self._ui(self._set_progress, f, m),
                            log_cb=lambda line: None, cancel_event=self._cancel)
            return "✅  Voice cloning is up to date"
        self._run("Update", "Updating…", work)

    def _on_create(self):
        name = self._name_entry.get().strip()[:40]
        mode = self._mode_var.get()
        existing = {v.name.lower() for v in library.list_voices()}
        if name.lower() in existing:
            messagebox.showwarning("Name in use", f"A voice named '{name}' already exists.", parent=self.app)
            return
        if mode == "Import model":
            pth, index = self._pth, self._index
            self._run("Import", "Checking the model…", lambda: self._do_import(name, pth, index))
            return
        clips = self._selections()
        prep = self._prep_options()
        eng, voice, eng_settings = self._base_voice()
        base_sel = self._engine_selection()      # restored when the voice is switched on
        if mode == "Quick":
            self._run("Create voice", "Preparing the reference voice…",
                      lambda: self._do_quick(name, clips, eng, voice, base_sel, prep))
        else:
            if self.app._is_generating:
                messagebox.showinfo("Busy", "Wait for the current generation to finish, then start training.",
                                    parent=self.app)
                return
            opts = dict(epochs=int(round(self.epochs_var.get())), batch_size=int(self.batch_var.get()),
                        sample_rate=SR_CHOICES[self.sr_var.get()], match=bool(self.match_var.get()))
            self._run("Training", "Starting…",
                      lambda: self._do_train(name, clips, eng, voice, eng_settings, opts, base_sel, prep))

    def _finish_new_voice(self, p, msg):
        def ui():
            self.app.refresh_cloned_voices(select=p.slug)
            self._clips, self._pth, self._index = [], None, None
            self._cleanup_preview = None
            self._name_entry.delete(0, "end")
            self._pth_label.configure(text="No file", text_color=COLORS["text_muted"])
            self._index_label.configure(text="Optional", text_color=COLORS["text_muted"])
            self._tabs_var.set("Active voice")
            self._on_tab("Active voice")
            self.app._set_status(f"✅  {msg}", COLORS["success"])
        self._ui(ui)
        return f"✅  {msg}"

    def _prepare(self, p, prep, lo=0.0, hi=0.3):
        """Trim and/or clean the clips before cloning (no-op when both are off)."""
        if not prep or not (prep.get("trim") or prep.get("separate") or prep.get("skip_silences")):
            return p
        separate_fn = None
        if prep.get("separate"):
            from core.voice_cloning import get_client
            client = get_client()

            def separate_fn(src, dst, denoise):
                client.separate(src, dst, denoise=denoise, cancel_event=self._cancel,
                                status_cb=lambda m: self._ui(self._set_progress, None, m))
        t0 = time.monotonic()
        est = estimate.separation_seconds(p.total_seconds) if separate_fn else 2

        def progress(frac, msg):
            left = max(0.0, est - (time.monotonic() - t0))
            self._ui(self._set_progress, lo + (hi - lo) * frac,
                     f"{msg} · {estimate.fmt(left)} left" if separate_fn and frac < 1 else msg)
        return library.prepare_clips(p, separate_fn=separate_fn, denoise=prep.get("denoise", False),
                                     progress_cb=progress, check_cancel=self._check_cancel,
                                     skip_silences=prep.get("skip_silences", False))

    def _do_quick(self, name, clips, eng, voice, base_sel=None, prep=None):
        p = library.create_voice(name, "seedvc", clips, eng, voice, base_selection=base_sel, prep=prep)
        try:
            p = self._prepare(p, prep, 0.0, 0.8)
            self._ui(self._set_progress, 0.3, "Trimming silence and building the reference…")
            library.build_seedvc_reference(p)
            self._check_cancel()
            p = library.update_params(p.slug, status="ready")
        except BaseException:
            library.delete_voice(p.slug)
            raise
        return self._finish_new_voice(p, f"Voice '{name}' is ready. It is switched on in Voice Effects.")

    def _do_import(self, name, pth, index):
        from core.voice_cloning import get_client
        info = get_client().inspect_rvc(pth)
        self._check_cancel()
        p = library.import_rvc_model(name, pth, index, info)
        return self._finish_new_voice(p, f"Voice '{name}' imported ({info['sample_rate'] // 1000} kHz model).")

    def _do_train(self, name, clips, eng, voice, eng_settings, opts, base_sel=None, prep=None):
        from core.voice_cloning import get_client
        p = library.create_voice(name, "rvc", clips, eng, voice, base_selection=base_sel, prep=prep)
        t0 = time.monotonic()
        est = estimate.training_seconds(p.total_seconds, opts["epochs"], opts["batch_size"], opts["sample_rate"])
        try:
            p = self._prepare(p, prep, 0.0, 0.02)
            if opts["match"] and eng_settings:
                self._ui(self._set_progress, 0.01, "Measuring pitch of the base and target voices…")
                tmp = self._new_tmp()
                y, sr = self._synthesize(pitch.BASE_SENTENCE, eng_settings, tmp)
                base_wav = os.path.join(p.dir, "samples", "base.wav")
                os.makedirs(os.path.dirname(base_wav), exist_ok=True)
                import soundfile as sf
                sf.write(base_wav, y, sr)
                r = pitch.estimate_offset(base_wav, library.clip_audio_paths(p))
                self._check_cancel()
                p = library.update_params(p.slug, pitch=r["offset"], base_f0_hz=r["base_f0"],
                                          target_f0_hz=r["target_f0"])
            self._ui(self._set_progress, 0.02, "Converting clips for training…")
            ds = library.build_dataset(p)
            self._check_cancel()
            p = library.update_params(p.slug, status="training", batch_size=opts["batch_size"])

            def on_progress(ev):
                frac = ev.get("fraction") or 0
                stage = ev.get("stage")
                if stage == "train" and ev.get("epoch") is not None:
                    left = ev.get("eta_seconds")
                    if left is None:
                        left = (opts["epochs"] - ev["epoch"]) * est["per_epoch"] + 75
                    text = (f"Training · epoch {ev['epoch']}/{ev['total_epoch']} · "
                            f"{estimate.fmt_clock(left)} left")
                else:
                    elapsed = time.monotonic() - t0
                    text = f"{ev.get('message') or stage} · {estimate.fmt(max(0, est['total'] - elapsed))} left"
                self._ui(self._set_progress, frac, text)

            res = get_client().train_rvc(p, ds, epochs=opts["epochs"], batch_size=opts["batch_size"],
                                         sample_rate=opts["sample_rate"],
                                         save_every_epoch=max(10, opts["epochs"] // 10),
                                         progress_cb=on_progress, cancel_event=self._cancel)
            p = library.update_params(p.slug, status="ready", pth=f"model/{p.slug}.pth",
                                      index=f"model/{p.slug}.index" if res.get("index") else None,
                                      sample_rate=res["sample_rate"], epochs=res["epochs"])
            shutil.rmtree(p.dir / "dataset", ignore_errors=True)
        except VCWorkerError as e:
            library.delete_voice(p.slug)
            if e.kind == "oom" and opts["batch_size"] > 1:
                e.hint = (e.hint or "") + f"\nTry again with batch size {opts['batch_size'] // 2}."
            raise
        except BaseException:
            library.delete_voice(p.slug)
            raise
        took = estimate.fmt(time.monotonic() - t0).replace("about ", "")
        return self._finish_new_voice(p, f"Voice '{name}' trained in {took} and switched on.")

    def _on_match_pitch(self):
        p = self._active_profile
        if p is None or not p.clips:
            return
        _, _, eng_settings = self._base_voice()

        def work():
            tmp = self._new_tmp()
            y, sr = self._synthesize(pitch.BASE_SENTENCE, eng_settings, tmp)
            base_wav = os.path.join(tmp, "base.wav")
            import soundfile as sf
            sf.write(base_wav, y, sr)
            self._ui(self._set_progress, 0.4, "Measuring pitch…")
            r = pitch.estimate_offset(base_wav, library.clip_audio_paths(p))
            self._check_cancel()
            library.update_params(p.slug, pitch=r["offset"], base_f0_hz=r["base_f0"],
                                  target_f0_hz=r["target_f0"])
            self._ui(self.pitch_var.set, r["offset"])
            return (f"✅  Pitch set to {r['offset']:+d} st "
                    f"(base {r['base_f0'] or 0:.0f} Hz → target {r['target_f0'] or 0:.0f} Hz)")
        self._run("Match pitch", "Synthesizing a sample with the current engine voice…", work)

    def _on_preview(self):
        p = self._active_profile
        if p is None:
            return
        self._flush_params()
        settings = self.get_settings()
        _, _, eng_settings = self._base_voice()

        def work():
            import soundfile as sf
            from effects.cloned_voices import convert_samples
            tmp = self._new_tmp()
            self._ui(self._set_progress, 0.1, "Synthesizing the preview sentence…")
            y, sr = self._synthesize(PREVIEW_TEXT, eng_settings, tmp)
            self._ui(self._set_progress, 0.35, "Converting to the cloned voice…")
            out = convert_samples(p.slug, y, sr, pitch=settings["pitch"],
                                  index_rate=settings["index_rate"], protect=settings["protect"],
                                  diffusion_steps=settings["diffusion_steps"],
                                  cancel_event=self._cancel,
                                  status_cb=lambda m: self._ui(self._set_progress, 0.5, m))
            path = os.path.join(tmp, "preview.wav")
            sf.write(path, out, sr)
            self._ui(self._play, path)
            return f"▶  Playing preview of '{p.name}'"
        self._run("Preview", "Starting…", work)

    def _on_preview_cleanup(self):
        sels = self._selections()
        if not sels:
            return
        first = dict(sels[0])
        full = next(c["seconds"] for c in self._clips if c["path"] == first["path"])
        start = first.get("start") or 0.0
        end = first.get("end") if first.get("end") is not None else full
        end = min(end, start + 30.0)                 # preview at most 30 s
        denoise = bool(self.denoise_var.get())

        def work():
            from core.audio_io import load_segment
            from core.voice_cloning import get_client
            tmp = self._new_tmp()
            orig = os.path.join(tmp, "original.wav")
            load_segment(first["path"])[int(start * 1000):int(end * 1000)].export(orig, format="wav")
            self._check_cancel()
            res = get_client().separate(orig, os.path.join(tmp, "cleaned.wav"), denoise=denoise,
                                        cancel_event=self._cancel,
                                        status_cb=lambda m: self._ui(self._set_progress, 0.3, m))
            self._cleanup_preview = (orig, res["out"])

            def ui():
                self._update_create()          # shows ▶ Original / ▶ Cleaned
                self._play(res["out"])
            self._ui(ui)
            return (f"▶  Playing the cleaned audio ({end - start:.0f} s, music removed in "
                    f"{res['elapsed']:.0f} s). Compare with ▶ Original.")
        self._run("Cleanup preview", "Removing background music from a sample…", work)

    def _play_preview(self, which):
        if self._cleanup_preview:
            self._play(self._cleanup_preview[which])

    def _play(self, path):
        try:
            import pygame
            if not pygame.mixer.get_init():
                return
            self._preview_sound = pygame.mixer.Sound(path)
            pygame.mixer.Channel(2).play(self._preview_sound)
        except Exception as e:
            print(f"Preview playback failed: {e}")

    def cleanup(self):
        self._cancel.set()
        try:
            import pygame
            if pygame.mixer.get_init():
                pygame.mixer.Channel(2).stop()
        except Exception:
            pass
        self._preview_sound = None
        for d in self._tmp_dirs:
            shutil.rmtree(d, ignore_errors=True)
        self._tmp_dirs.clear()
