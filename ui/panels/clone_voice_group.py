"""
ui/panels/clone_voice_group.py  –  "Clone Voice" box in the Customization card

    🧬 Clone Voice                                   [status]
    ┌ Not installed ─────────────────────────────────────────┐
    │  one-time setup text                 [⬇ Install]       │
    └────────────────────────────────────────────────────────┘
    [ Active voice | Create voice ]
      Active voice: voice picker, pitch / index / protect (RVC) or
                    quality steps (Quick), time estimate, ▶ Preview
      Create voice: name, Quick / Train (RVC) / Import model,
                    clips, epochs / batch / sample rate, estimate,
                    🧬 Create Voice
    ▬▬▬▬▬▬▬▬▬▬ progress ▬▬▬▬▬▬▬▬▬▬   stage text          [✖ Cancel]

Long work (install, training, preview) runs on a daemon thread; every
widget update goes back through app.after(0, ...). One task at a time.
"""

import os
import shutil
import tempfile
import threading
import time
from tkinter import filedialog, messagebox

import customtkinter as ctk

from core.constants import COLORS, FONTS
from core.voice_cloning import estimate, library, pitch, runtime
from core.voice_cloning.errors import VCCancelled, VCError, VCWorkerError
from ui.theme import styled_slider

CLONE_GROUP = "clone"
KEY_PREFIX = "clone:"
PREVIEW_TEXT = "Hello! This is a short preview of my cloned voice."
from core.audio_io import AUDIO_EXTENSIONS
AUDIO_TYPES = [("Audio files", " ".join("*" + e for e in AUDIO_EXTENSIONS)), ("All files", "*.*")]
MODES = ["Quick", "Train (RVC)", "Import model"]
MODE_HINTS = {
    "Quick": "No training. Uses up to 25 s of your clips and is ready in seconds.",
    "Train (RVC)": "Trains a voice model on your clips. Best with 3+ minutes of clean speech.",
    "Import model": "Use an existing RVC voice model (.pth), with its .index if you have it.",
}
SR_CHOICES = {"32 kHz": 32000, "40 kHz": 40000, "48 kHz": 48000}
# "Use part of each recording" presets. Seed-VC (Quick) only reads 25 s of
# reference, so Quick stops at 25 s; training uses everything it is given.
TRIM_PRESETS = {
    "Quick": {"First 10 s": 10, "First 20 s": 20, "First 25 s": 25, "Custom": None},
    "Train (RVC)": {"First 1 min": 60, "First 3 min": 180, "First 5 min": 300, "Custom": None},
}


def _btn(parent, text, command, color, width=110, filled=False, state="normal"):
    """A button in the app's style: outlined (bg_input) or filled with *color*."""
    if filled:
        return ctk.CTkButton(parent, text=text, font=FONTS["button"], fg_color=color,
                             hover_color=COLORS["accent_secondary"], corner_radius=8,
                             height=30, width=width, command=command, state=state)
    return ctk.CTkButton(parent, text=text, font=FONTS["button"], fg_color=COLORS["bg_card"],
                         hover_color=color, border_color=color, border_width=1,
                         corner_radius=8, height=30, width=width, command=command, state=state)


def _small(parent, text, color=None, **kw):
    return ctk.CTkLabel(parent, text=text, font=FONTS["body_small"],
                        text_color=color or COLORS["text_secondary"], **kw)


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

        # not-installed panel
        self._setup = ctk.CTkFrame(inner, fg_color="transparent")
        self._setup.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        self._setup_text = _small(self._setup, "", COLORS["text_muted"], wraplength=420,
                                  justify="left", anchor="w")
        self._setup_text.pack(fill="x", anchor="w")
        self._install_btn = _btn(self._setup, "⬇  Install voice cloning", self._on_install,
                                 COLORS["accent_primary"], width=200, filled=True)
        self._install_btn.pack(anchor="w", pady=(6, 0))

        # installed: tabs
        self._tabs_var = ctk.StringVar(value="Active voice")
        self._tabs = ctk.CTkSegmentedButton(
            inner, values=["Active voice", "Create voice"], variable=self._tabs_var,
            command=self._on_tab, font=FONTS["body_small"], height=28,
            fg_color=COLORS["bg_card"], selected_color=COLORS["accent_primary"],
            selected_hover_color=COLORS["accent_primary"],
            unselected_color=COLORS["bg_card"], unselected_hover_color=COLORS["border"],
            text_color=COLORS["text_primary"])
        self._tabs.grid(row=2, column=0, sticky="ew", pady=(8, 6))
        self._active_page = self._build_active_page(inner)
        self._create_page = self._build_create_page(inner)
        self._active_page.grid(row=3, column=0, sticky="ew")
        self._create_page.grid(row=3, column=0, sticky="ew")

        # shared task row
        self._task = ctk.CTkFrame(inner, fg_color="transparent")
        self._task.grid(row=4, column=0, sticky="ew", pady=(8, 0))
        self._task.columnconfigure(0, weight=1)
        self._progress = ctk.CTkProgressBar(self._task, height=6, fg_color=COLORS["slider_track"],
                                            progress_color=COLORS["accent_primary"], corner_radius=3)
        self._progress.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self._progress.set(0)
        self._cancel_btn = ctk.CTkButton(self._task, text="✖  Cancel", font=FONTS["button"],
                                         fg_color="#CC0000", hover_color=COLORS["error"],
                                         corner_radius=8, height=28, width=96,
                                         command=self._on_cancel)
        self._cancel_btn.grid(row=0, column=1, rowspan=2)
        self._stage = _small(self._task, "", COLORS["text_secondary"], anchor="w",
                             justify="left", wraplength=360)
        self._stage.grid(row=1, column=0, sticky="w", pady=(2, 0))
        self._task.grid_remove()

        self.refresh()
        self._tick()

    # ═══════════════════════════════════════════════════════════════
    #  Page builders
    # ═══════════════════════════════════════════════════════════════
    def _slider_row(self, parent, row, label, var, lo, hi, steps, fmt, on_change):
        f = ctk.CTkFrame(parent, fg_color="transparent")
        f.grid(row=row, column=0, sticky="ew", pady=2)
        _small(f, label, width=78, anchor="w").pack(side="left")
        val = _small(f, fmt(var.get()), COLORS["text_muted"], width=60, anchor="w")

        def changed(v):
            val.configure(text=fmt(float(v)))
            on_change()
        s = styled_slider(f, var, lo, hi, width=170, command=changed)
        s.configure(number_of_steps=steps)
        s.pack(side="left", padx=(0, 8))
        val.pack(side="left")
        f.value_label = val
        f.slider = s
        f.fmt = fmt
        return f

    def _build_active_page(self, parent):
        page = ctk.CTkFrame(parent, fg_color="transparent")
        page.columnconfigure(0, weight=1)

        top = ctk.CTkFrame(page, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        _small(top, "Voice:", width=78, anchor="w").pack(side="left")
        self._voice_var = ctk.StringVar(value="None")
        self._voice_menu = ctk.CTkOptionMenu(
            top, variable=self._voice_var, values=["None"], command=self._on_voice_pick,
            font=FONTS["body"], fg_color=COLORS["bg_card"], button_color=COLORS["accent_primary"],
            button_hover_color=COLORS["accent_secondary"], dropdown_fg_color=COLORS["bg_card"],
            dropdown_hover_color=COLORS["accent_primary"], corner_radius=8, width=200)
        self._voice_menu.pack(side="left", padx=(0, 8))
        self._mode_tag = _small(top, "", COLORS["text_muted"])
        self._mode_tag.pack(side="left")

        # Base voice: the engine voice this clone was made from. Switching the
        # clone on selects it in the TTS Engine card (conversion sounds best
        # from the same source voice).
        self._base_row = ctk.CTkFrame(page, fg_color="transparent")
        self._base_row.grid(row=1, column=0, sticky="ew", pady=(0, 4))
        _small(self._base_row, "Base voice:", width=78, anchor="w").pack(side="left")
        self._base_value = _small(self._base_row, "", COLORS["text_primary"])
        self._base_value.pack(side="left", padx=(0, 8))
        self._use_current_btn = _btn(self._base_row, "Use current", self._on_use_current_base,
                                     COLORS["accent_primary"], width=96)
        self._use_current_btn.pack(side="left")

        self.pitch_var = ctk.DoubleVar(value=0)
        self.index_var = ctk.DoubleVar(value=0.3)
        self.protect_var = ctk.DoubleVar(value=0.33)
        self.steps_var = ctk.DoubleVar(value=10)
        self._pitch_row = self._slider_row(page, 2, "Pitch:", self.pitch_var, -24, 24, 48,
                                           lambda v: f"{int(round(v)):+d} st", self._params_changed)
        self._match_btn = _btn(self._pitch_row, "🎯 Match", self._on_match_pitch,
                               COLORS["accent_primary"], width=80)
        self._match_btn.pack(side="left", padx=(6, 0))
        self._index_row = self._slider_row(page, 3, "Index rate:", self.index_var, 0, 1, 100,
                                           lambda v: f"{v:.2f}", self._params_changed)
        self._protect_row = self._slider_row(page, 4, "Protect:", self.protect_var, 0, 0.5, 50,
                                             lambda v: f"{v:.2f}", self._params_changed)
        self._steps_row = self._slider_row(page, 5, "Quality:", self.steps_var, 4, 50, 46,
                                           lambda v: f"{int(round(v))} steps", self._params_changed)
        self._active_hint = _small(page, "", COLORS["text_muted"], anchor="w", justify="left",
                                   wraplength=420)
        self._active_hint.grid(row=6, column=0, sticky="w", pady=(2, 0))
        self._active_est = _small(page, "", COLORS["text_secondary"], anchor="w", justify="left",
                                  wraplength=420)
        self._active_est.grid(row=7, column=0, sticky="w", pady=(4, 0))
        self._preview_btn = _btn(page, "▶  Preview", self._on_preview, COLORS["success"], width=120)
        self._preview_btn.grid(row=8, column=0, sticky="w", pady=(6, 0))
        return page

    def _build_create_page(self, parent):
        page = ctk.CTkFrame(parent, fg_color="transparent")
        page.columnconfigure(0, weight=1)

        r0 = ctk.CTkFrame(page, fg_color="transparent")
        r0.grid(row=0, column=0, sticky="ew", pady=2)
        _small(r0, "Name:", width=78, anchor="w").pack(side="left")
        self._name_entry = ctk.CTkEntry(r0, placeholder_text="e.g. My narrator", font=FONTS["body"],
                                        fg_color=COLORS["bg_card"], border_color=COLORS["border"],
                                        text_color=COLORS["text_primary"], width=240, height=30)
        self._name_entry.pack(side="left")
        self._name_entry.bind("<KeyRelease>", lambda e: self._update_create())

        self._mode_var = ctk.StringVar(value="Quick")
        ctk.CTkSegmentedButton(
            page, values=MODES, variable=self._mode_var, command=lambda v: self._update_create(),
            font=FONTS["body_small"], height=26, fg_color=COLORS["bg_card"],
            selected_color=COLORS["accent_primary"], selected_hover_color=COLORS["accent_primary"],
            unselected_color=COLORS["bg_card"], unselected_hover_color=COLORS["border"],
            text_color=COLORS["text_primary"]).grid(row=1, column=0, sticky="w", pady=(6, 2))
        self._mode_hint = _small(page, "", COLORS["text_muted"], anchor="w", justify="left",
                                 wraplength=420)
        self._mode_hint.grid(row=2, column=0, sticky="w")

        # clips
        self._clips_row = ctk.CTkFrame(page, fg_color="transparent")
        self._clips_row.grid(row=3, column=0, sticky="ew", pady=(6, 2))
        _btn(self._clips_row, "📥  Add clips", self._on_add_clips, COLORS["accent_primary"],
             width=110).pack(side="left", padx=(0, 6))
        _btn(self._clips_row, "✖  Clear", self._on_clear_clips, COLORS["error"],
             width=80).pack(side="left", padx=(0, 8))
        self._clips_label = _small(self._clips_row, "No clips", COLORS["text_muted"])
        self._clips_label.pack(side="left")

        self._build_prep(page)

        # import
        self._import_row = ctk.CTkFrame(page, fg_color="transparent")
        self._import_row.grid(row=5, column=0, sticky="ew", pady=(6, 2))
        imp1 = ctk.CTkFrame(self._import_row, fg_color="transparent")
        imp1.pack(fill="x", pady=1)
        _btn(imp1, "📂  Model .pth", self._on_pick_pth, COLORS["accent_primary"], width=120).pack(side="left", padx=(0, 8))
        self._pth_label = _small(imp1, "No file", COLORS["text_muted"])
        self._pth_label.pack(side="left")
        imp2 = ctk.CTkFrame(self._import_row, fg_color="transparent")
        imp2.pack(fill="x", pady=1)
        _btn(imp2, "📂  Index", self._on_pick_index, COLORS["accent_primary"], width=120).pack(side="left", padx=(0, 8))
        self._index_label = _small(imp2, "Optional", COLORS["text_muted"])
        self._index_label.pack(side="left")

        # training options
        self._train_opts = ctk.CTkFrame(page, fg_color="transparent")
        self._train_opts.grid(row=6, column=0, sticky="ew", pady=(4, 0))
        self._train_opts.columnconfigure(0, weight=1)
        self.epochs_var = ctk.DoubleVar(value=150)
        self._slider_row(self._train_opts, 0, "Epochs:", self.epochs_var, 10, 500, 49,
                         lambda v: f"{int(round(v))}", self._update_create)
        opt = ctk.CTkFrame(self._train_opts, fg_color="transparent")
        opt.grid(row=1, column=0, sticky="ew", pady=2)
        _small(opt, "Batch:", width=78, anchor="w").pack(side="left")
        self.batch_var = ctk.StringVar(value="2")
        self._menu(opt, self.batch_var, ["1", "2", "4"], 64).pack(side="left", padx=(0, 14))
        _small(opt, "Sample rate:").pack(side="left", padx=(0, 6))
        self.sr_var = ctk.StringVar(value="40 kHz")
        self._menu(opt, self.sr_var, list(SR_CHOICES), 90).pack(side="left")
        pm = ctk.CTkFrame(self._train_opts, fg_color="transparent")
        pm.grid(row=2, column=0, sticky="ew", pady=(4, 0))
        self.match_var = ctk.BooleanVar(value=True)
        ctk.CTkSwitch(pm, text="Match pitch to base voice", variable=self.match_var,
                      font=FONTS["body_small"], text_color=COLORS["text_secondary"],
                      button_color=COLORS["accent_primary"], button_hover_color=COLORS["accent_secondary"],
                      progress_color=COLORS["success"], fg_color=COLORS["switch_off"],
                      switch_width=32, switch_height=16).pack(side="left")
        self._base_label = _small(pm, "", COLORS["text_muted"])
        self._base_label.pack(side="left", padx=(10, 0))

        self._create_est = _small(page, "", COLORS["text_secondary"], anchor="w", justify="left",
                                  wraplength=420)
        self._create_est.grid(row=7, column=0, sticky="w", pady=(6, 0))
        self._create_btn = _btn(page, "🧬  Create Voice", self._on_create, COLORS["accent_primary"],
                                width=150, filled=True)
        self._create_btn.grid(row=8, column=0, sticky="w", pady=(6, 0))
        return page

    def _switch(self, parent, text, var, command):
        return ctk.CTkSwitch(parent, text=text, variable=var, command=command,
                             font=FONTS["body_small"], text_color=COLORS["text_secondary"],
                             button_color=COLORS["accent_primary"], button_hover_color=COLORS["accent_secondary"],
                             progress_color=COLORS["success"], fg_color=COLORS["switch_off"],
                             switch_width=32, switch_height=16)

    def _build_prep(self, page):
        """Optional clip preparation: use part of each recording, remove background music."""
        prep = ctk.CTkFrame(page, fg_color="transparent")
        prep.grid(row=4, column=0, sticky="ew", pady=(4, 0))
        prep.columnconfigure(0, weight=1)
        self._prep = prep

        # 1. use part of each recording
        self.trim_on = ctk.BooleanVar(value=False)
        self._switch(prep, "Use part of each recording", self.trim_on,
                     self._update_create).grid(row=0, column=0, sticky="w", pady=(2, 0))
        self._trim_box = ctk.CTkFrame(prep, fg_color="transparent")
        self._trim_box.grid(row=1, column=0, sticky="ew", padx=(42, 0))
        self.trim_var = ctk.StringVar(value="First 20 s")
        self._trim_seg = ctk.CTkSegmentedButton(
            self._trim_box, values=list(TRIM_PRESETS["Quick"]), variable=self.trim_var,
            command=lambda v: self._update_create(), font=FONTS["body_small"], height=24,
            fg_color=COLORS["bg_card"], selected_color=COLORS["accent_primary"],
            selected_hover_color=COLORS["accent_primary"], unselected_color=COLORS["bg_card"],
            unselected_hover_color=COLORS["border"], text_color=COLORS["text_primary"])
        self._trim_seg.pack(anchor="w", pady=(4, 2))
        self._trim_hint = _small(self._trim_box, "", COLORS["text_muted"], anchor="w", justify="left",
                                 wraplength=380)
        self._trim_hint.pack(anchor="w")
        self._custom_list = ctk.CTkFrame(self._trim_box, fg_color="transparent")
        self._custom_list.pack(fill="x", pady=(2, 0))

        # 2. remove background music & noise
        self.clean_on = ctk.BooleanVar(value=False)
        self._switch(prep, "Remove background music & noise", self.clean_on,
                     self._update_create).grid(row=2, column=0, sticky="w", pady=(6, 0))
        self._clean_box = ctk.CTkFrame(prep, fg_color="transparent")
        self._clean_box.grid(row=3, column=0, sticky="ew", padx=(42, 0))
        self.denoise_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(self._clean_box, text="Also reduce steady noise (hiss, hum)", variable=self.denoise_var,
                        font=FONTS["body_small"], text_color=COLORS["text_secondary"],
                        fg_color=COLORS["accent_primary"], hover_color=COLORS["accent_secondary"],
                        border_color=COLORS["border"], checkbox_width=16, checkbox_height=16,
                        command=self._update_create).pack(anchor="w", pady=(4, 2))
        row = ctk.CTkFrame(self._clean_box, fg_color="transparent")
        row.pack(anchor="w", pady=(2, 0))
        self._cleanup_btn = _btn(row, "🎧  Preview cleanup", self._on_preview_cleanup,
                                 COLORS["accent_primary"], width=150)
        self._cleanup_btn.pack(side="left", padx=(0, 6))
        self._play_orig_btn = _btn(row, "▶ Original", lambda: self._play_preview(0), COLORS["text_muted"],
                                   width=90, state="disabled")
        self._play_orig_btn.pack(side="left", padx=(0, 6))
        self._play_clean_btn = _btn(row, "▶ Cleaned", lambda: self._play_preview(1), COLORS["success"],
                                    width=90, state="disabled")
        self._play_clean_btn.pack(side="left")
        _small(self._clean_box, "Separates the voice from music with Demucs. The first use downloads "
                                "the model (about 80 MB).", COLORS["text_muted"], anchor="w", justify="left",
               wraplength=380).pack(anchor="w", pady=(4, 0))

        # 3. skip silences (applied last, to the trimmed / cleaned audio)
        self.skipsil_on = ctk.BooleanVar(value=False)
        self._switch(prep, "Skip silences", self.skipsil_on,
                     self._update_create).grid(row=4, column=0, sticky="w", pady=(6, 0))
        self._skipsil_hint = _small(prep, "Applied last: removes pauses longer than 0.4 s from the "
                                          "trimmed and cleaned audio.", COLORS["text_muted"],
                                    anchor="w", justify="left", wraplength=380)
        self._skipsil_hint.grid(row=5, column=0, sticky="w", padx=(42, 0))

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
        """Show / hide the prep options for the current mode and selection."""
        if mode == "Import model":
            self._prep.grid_remove()
            return
        self._prep.grid()
        presets = list(TRIM_PRESETS[mode])
        if self._trim_seg.cget("values") != presets:
            self._trim_seg.configure(values=presets)
        if self.trim_var.get() not in presets:
            self.trim_var.set(presets[1])
        (self._trim_box.grid if self.trim_on.get() else self._trim_box.grid_remove)()
        (self._clean_box.grid if self.clean_on.get() else self._clean_box.grid_remove)()
        (self._skipsil_hint.grid if self.skipsil_on.get() else self._skipsil_hint.grid_remove)()
        custom = self.trim_on.get() and TRIM_PRESETS[mode].get(self.trim_var.get()) is None
        if custom:
            self._trim_hint.configure(text="Pick the part of each recording to use. Press ✂ Edit to open "
                                           "its timeline." + (" Quick mode reads at most 25 s of speech, "
                                                              "so keep the selections short."
                                                              if mode == "Quick" else ""))
        else:
            self._trim_hint.configure(text="Uses the start of the recordings, continuing into the next "
                                           "clip if one is shorter." + (" Quick mode reads at most 25 s."
                                                                        if mode == "Quick" else ""))
        for w in self._custom_list.winfo_children():
            w.destroy()
        if custom:
            for i, c in enumerate(self._clips):
                r = ctk.CTkFrame(self._custom_list, fg_color=COLORS["bg_card"], corner_radius=8)
                r.pack(fill="x", pady=2)
                name = os.path.basename(c["path"])
                _small(r, name if len(name) <= 28 else name[:25] + "…", COLORS["text_primary"],
                       anchor="w", width=190).pack(side="left", padx=(8, 4), pady=4)
                s0 = c.get("start") or 0.0
                e0 = c.get("end") if c.get("end") is not None else c["seconds"]
                whole = c.get("start") is None and c.get("end") is None
                txt = (f"all · {self._fmt_len(c['seconds'])}" if whole else
                       f"{self._clock(s0)}–{self._clock(e0)} · {e0 - s0:.1f} s")
                _small(r, txt, COLORS["text_secondary"] if whole else COLORS["success"]).pack(side="left")
                ctk.CTkButton(r, text="✂  Edit", font=FONTS["body_small"], width=70, height=24,
                              corner_radius=6, fg_color=COLORS["bg_input"], hover_color=COLORS["accent_primary"],
                              border_color=COLORS["accent_primary"], border_width=1,
                              command=lambda i=i: self._edit_clip(i)).pack(side="right", padx=6, pady=3)
        self._cleanup_btn.configure(state="normal" if self._clips and not self._busy else "disabled")

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

    def _menu(self, parent, var, values, width):
        return ctk.CTkOptionMenu(parent, variable=var, values=values, command=lambda v: self._update_create(),
                                 font=FONTS["body_small"], fg_color=COLORS["bg_card"],
                                 button_color=COLORS["accent_primary"],
                                 button_hover_color=COLORS["accent_secondary"],
                                 dropdown_fg_color=COLORS["bg_card"],
                                 dropdown_hover_color=COLORS["accent_primary"],
                                 corner_radius=8, width=width, height=26)

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
                                            "TTS Studio from source. It is not bundled with the installer yet.")
            self._install_btn.pack_forget()
        elif not installed:
            self._set_status("Not installed", COLORS["text_muted"])
            missing = [e["label"] for e in st["engines"].values() if not e["installed"]]
            self._setup_text.configure(text=(
                "One-time setup of " + " and ".join(missing) + ". Downloads about 9 GB and uses about "
                f"21 GB on {os.path.splitdrive(st['root'])[0] or 'disk'}. Large downloads can take an hour; "
                "you can keep using the app meanwhile."))
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
            for r in rows_rvc + (self._steps_row,):
                r.grid_remove()
            self._mode_tag.configure(text="")
            self._active_hint.configure(text="Pick a cloned voice here or switch one on in Voice Effects.")
            self._preview_btn.configure(state="disabled")
        else:
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
                self._match_btn.configure(state="normal" if p.clips else "disabled")
                self._active_hint.configure(text="Pitch shifts the cloned voice in semitones. Index rate "
                                                 "pulls the accent toward the target. Protect keeps breaths "
                                                 "and consonants clean.")
            else:
                self._mode_tag.configure(text="Quick (zero-shot)")
                self.steps_var.set(p.diffusion_steps)
                for r in rows_rvc:
                    r.grid_remove()
                self._steps_row.grid()
                self._steps_row.value_label.configure(text=self._steps_row.fmt(self.steps_var.get()))
                self._active_hint.configure(text="More quality steps sound smoother but take longer. "
                                                 "10 is a good balance on this GPU.")
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
            msg = f"⏱  This text (≈{int(round(speech))} s of speech): adds {estimate.fmt(t)}"
        if not warm:
            first = estimate.conversion_seconds(p.mode, 0, steps, first_use=True)
            msg += f", plus {estimate.fmt(first)} to load the voice the first time"
        self._active_est.configure(text=msg + ".")

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
        self._mode_hint.configure(text=MODE_HINTS[mode])
        (self._clips_row.grid if mode != "Import model" else self._clips_row.grid_remove)()
        (self._import_row.grid if mode == "Import model" else self._import_row.grid_remove)()
        (self._train_opts.grid if mode == "Train (RVC)" else self._train_opts.grid_remove)()
        from ui.panels.engine_panel import EnginePanel
        sel = self._engine_selection()
        self._base_label.configure(text=f"Base: {EnginePanel.describe_selection(sel)}" if sel else "")
        self._refresh_prep(mode)

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
            used = "" if abs(total - secs) < 0.05 else f" (using {self._fmt_len(secs)})"
            txt = ("No clips" if not n else
                   f"{n} clip{'s' if n != 1 else ''} · {self._fmt_len(total)}{used}")
            self._clips_label.configure(text=txt + (f"  ({note})" if note and n else ""),
                                        text_color=color if n else COLORS["text_muted"])
        else:
            ok = bool(self._pth)

        # estimate
        if mode == "Quick":
            est = f"⏱  Estimated time: {estimate.fmt(estimate.quick_create_seconds(secs))}." if n else ""
        elif mode == "Train (RVC)":
            if n:
                t = estimate.training_seconds(secs, int(round(self.epochs_var.get())),
                                              int(self.batch_var.get()), SR_CHOICES[self.sr_var.get()])
                est = (f"⏱  Estimated time: {estimate.fmt(t['total'])} "
                       f"(prepare {estimate.fmt(t['slice'] + t['features'])}, "
                       f"then {estimate.fmt(t['per_epoch'])} per epoch). "
                       "Cloned voices can't be used while training runs.")
                if self.batch_var.get() == "4":
                    est += " Batch 4 may run out of GPU memory on 4 GB cards."
            else:
                est = "Add clips to see an estimated time."
        else:
            est = "⏱  Estimated time: about 15 s." if self._pth else ""
        if est and mode != "Import model" and n and self.clean_on.get():
            warm = False
            try:
                from core.voice_cloning import get_client
                warm = get_client().is_warm("rvc")
            except Exception:
                pass
            extra = estimate.separation_seconds(secs, first_use=not warm)
            est += f" Music removal adds {estimate.fmt(extra)}."
        self._create_est.configure(text=est)
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
        d = tempfile.mkdtemp(prefix="tts_studio_clone_")
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
                self._play_orig_btn.configure(state="normal")
                self._play_clean_btn.configure(state="normal")
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
