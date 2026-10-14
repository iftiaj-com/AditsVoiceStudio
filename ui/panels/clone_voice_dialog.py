"""
ui/panels/clone_voice_dialog.py  –  rename, tune or delete cloned voices

Opened by the ✏ button in the "Cloned Voices" header of the Voice Effects
card. One row per voice in the library (including drafts and failed
trainings, so they can be cleaned up):

    [ name                         ] [Save] [🗑 Delete]
    🧬 Quick · 15 MB                    [🔊 Voice settings ▾]
    Base  Kokoro-82M · am_onyx · 1.0x
    ┌ Engine / Language / Voice / Speed ─────────────────┐   (opens with ▾)
    │ Pitch 🎯 / Index / Protect (RVC) or Quality (Quick) │
    │ [▶ Preview]  status                                │
    └────────────────────────────────────────────────────┘

Save writes the name, the base voice and the sound settings together; they
are the same profile fields the Clone Voice box edits. ▶ Preview speaks a
short sentence with the editor's values (saved or not) and converts it into
the clone. 🎯 measures the editor's base voice against the clips and sets
Pitch. Changes go through core.voice_cloning.library, then the app refreshes
the registry, the effect tiles and the Clone Voice box.
"""

import dataclasses
import os
import subprocess
import threading
from tkinter import messagebox

import customtkinter as ctk

from core.constants import COLORS, FONTS, KOKORO_LANGS
from core.voice_cloning import library, previews, runtime
from core.voice_cloning.errors import VCCancelled, VCError
from ui.panels.clone_voice_group import CloneVoiceGroup, _Tip
from ui.theme import styled_option_menu, styled_slider, use_app_icon

# (profile field, label, low, high, steps, format, tip) of each sound slider,
# the same ranges as the Clone Voice box.
RVC_SOUND = (
    ("pitch", "Pitch", -24, 24, 48, lambda v: f"{int(round(v)):+d} st",
     "Shifts the cloned voice up or down, in semitones."),
    ("index_rate", "Index", 0, 1, 100, lambda v: f"{v:.2f}",
     "Index rate: higher pulls the accent closer to the target voice."),
    ("protect", "Protect", 0, 0.5, 50, lambda v: f"{v:.2f}",
     "Keeps breaths and consonants clean. 0.33 suits most voices."),
)
# Each sound field's value for a new voice (↺ goes back to it).
_SOUND_DEFAULTS = {f.name: f.default for f in dataclasses.fields(library.VoiceProfile)}
QUICK_SOUND = (
    ("diffusion_steps", "Quality", 4, 50, 46, lambda v: f"{int(round(v))} steps",
     "More steps sound smoother but take longer. 10 is a good balance on this GPU."),
)


def _folder_mb(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total / (1024 * 1024)


def _round_sound(field, value):
    return int(round(value)) if field in ("pitch", "diffusion_steps") else round(float(value), 2)


class _VoiceEditor:
    """Base voice (engine, language, voice, speed) and sound settings of one voice, in its row.

    Built the first time the row is opened. The base starts from the saved
    one, or from the TTS Engine card when the saved base cannot be shown here.
    """

    def __init__(self, parent, panel, v, on_change):
        self.panel = panel
        self.mode = v.mode
        self._on_change = on_change
        self.f0 = None                  # (base Hz, target Hz) from the last 🎯 measurement
        self.frame = ctk.CTkFrame(parent, fg_color=COLORS["bg_card"], corner_radius=8,
                                  border_width=1, border_color=COLORS["border"])
        f = self.frame
        f.columnconfigure(1, weight=1)

        sel = CloneVoiceGroup._selection_for(v)
        self.note = None
        self._unsaved = False           # True when the editor does not show the saved base
        r = panel._resolve_selection(sel)
        if isinstance(r, str):
            self._unsaved = True
            if sel:
                self.note = f"The saved base can't be used here: {r}. Showing the TTS Engine voice."
            sel = panel.get_voice_selection()
            r = panel._resolve_selection(sel)
        engine, kind, lang, voice = r
        self._saved = {"engine": engine, "lang": lang, "voice": voice, "speed": sel.get("speed")}

        self._row = 0
        self.engine_var = ctk.StringVar(value=engine)
        self.lang_var = ctk.StringVar(value=lang or "American English")
        self.voice_var = ctk.StringVar(value=voice or "")
        self._menu_row("Engine", self.engine_var, panel.engine_names(), self._on_engine)
        self._lang_lbl, self._lang_menu = self._menu_row("Language", self.lang_var, list(KOKORO_LANGS),
                                                         self._on_lang)
        self._voice_lbl, self._voice_menu = self._menu_row("Voice", self.voice_var, [""],
                                                           lambda _v: self._on_change())

        self.speed_var = ctk.DoubleVar(value=1.0)
        sp, self._slider, self._speed_lbl = self._slider_row(
            "Speed", self.speed_var, 0.5, 2.0, 150, lambda _v: self._speed_moved(), None)
        self._reset_btn = self._side_button(sp, "↺", self._reset_speed,
                                            "Back to the engine's normal speed.")
        self._no_speed = ctk.CTkLabel(sp, text="This engine has no speed setting.",
                                      font=FONTS["body_small"], text_color=COLORS["text_muted"],
                                      anchor="w")

        # Sound: the conversion settings, saved in the profile next to the base.
        self._sound = {}                # field -> (var, value label, format)
        self._sound_saved = {}
        self.match_btn = None
        for field, label, lo, hi, steps, fmt, tip in (RVC_SOUND if v.mode == "rvc" else QUICK_SOUND):
            value = getattr(v, field)
            self._sound_saved[field] = _round_sound(field, value)
            var = ctk.DoubleVar(value=value)
            line, _s, val = self._slider_row(label, var, lo, hi, steps,
                                             lambda _v, fd=field: self._sound_moved(fd), tip)
            val.configure(text=fmt(value))
            self._sound[field] = (var, val, fmt)
            if field == "pitch":
                self.match_btn = self._side_button(line, "🎯", None,
                                                   "Match pitch: measure the base voice above and "
                                                   "your clips, then set Pitch so they line up.")
            elif field in ("index_rate", "protect"):
                default = _SOUND_DEFAULTS[field]
                self._side_button(line, "↺", lambda fd=field, d=default: self.set_sound(fd, d),
                                  f"Back to {fmt(default)}, the default.")

        foot = ctk.CTkFrame(f, fg_color="transparent")
        foot.grid(row=self._row, column=0, columnspan=2, sticky="ew", padx=10, pady=(8, 8))
        foot.columnconfigure(1, weight=1)
        self.foot = foot
        self.status = ctk.CTkLabel(foot, text=self.note or "", font=FONTS["body_small"],
                                   text_color=COLORS["warning"] if self.note else COLORS["text_muted"],
                                   anchor="w", justify="left", wraplength=330)
        self.status.grid(row=0, column=1, sticky="w", padx=(10, 0))

        self._show_kind(kind, lang, voice, sel.get("speed"))

    # ── building ────────────────────────────────────────────────────
    def _label(self, text, tip=None):
        lbl = ctk.CTkLabel(self.frame, text=text, font=FONTS["body_small"],
                           text_color=COLORS["text_secondary"], width=64, anchor="w")
        lbl.grid(row=self._row, column=0, sticky="w", padx=(10, 6), pady=(6, 0))
        if tip:
            _Tip(lbl, tip)
        return lbl

    def _menu_row(self, text, var, values, command):
        lbl = self._label(text)
        menu = styled_option_menu(self.frame, var, values, width=220, height=28, command=command)
        menu.grid(row=self._row, column=1, sticky="ew", padx=(0, 10), pady=(8 if self._row == 0 else 6, 0))
        self._row += 1
        return lbl, menu

    def _slider_row(self, text, var, lo, hi, steps, command, tip):
        """Label · slider (stretches) · value · [side button], in the next row."""
        self._label(text, tip)
        line = ctk.CTkFrame(self.frame, fg_color="transparent")
        line.grid(row=self._row, column=1, sticky="ew", padx=(0, 10), pady=(6, 0))
        line.columnconfigure(0, weight=1)
        slider = styled_slider(line, var, lo, hi, width=150, command=command)
        slider.configure(number_of_steps=steps)
        slider.grid(row=0, column=0, sticky="ew")
        val = ctk.CTkLabel(line, text="", font=FONTS["body_small"], width=62,
                           text_color=COLORS["text_muted"])
        val.grid(row=0, column=1, padx=(6, 4))
        # Holds the side button's place (↺, 🎯) so every slider ends at the same x.
        ctk.CTkLabel(line, text="", width=28, height=24).grid(row=0, column=2)
        self._row += 1
        return line, slider, val

    @staticmethod
    def _side_button(line, text, command, tip):
        btn = ctk.CTkButton(line, text=text, font=FONTS["body_small"], width=28, height=24,
                            corner_radius=6, fg_color=COLORS["bg_input"],
                            hover_color=COLORS["accent_primary"],
                            border_color=COLORS["border"], border_width=1, command=command)
        btn.grid(row=0, column=2)
        _Tip(btn, tip)
        return btn

    # ── base voice ──────────────────────────────────────────────────
    @property
    def kind(self):
        return self.panel.engine_kind(self.engine_var.get())

    def _show_kind(self, kind, lang=None, voice=None, speed=None):
        """Lay out the rows *kind* needs and fill them (first voice / normal speed by default)."""
        if kind == "kokoro":
            self._lang_lbl.grid()
            self._lang_menu.grid()
            if lang:
                self.lang_var.set(lang)
            choices = self.panel.voice_choices(kind, self.lang_var.get())
        else:
            self._lang_lbl.grid_remove()
            self._lang_menu.grid_remove()
            choices = self.panel.voice_choices(kind)
        if choices:
            self._voice_lbl.grid()
            self._voice_menu.grid()
            self._voice_menu.configure(values=choices)
            self.voice_var.set(voice if voice in choices else choices[0])
        else:
            self._voice_lbl.grid_remove()
            self._voice_menu.grid_remove()
            self.voice_var.set("")

        rng = self.panel.SPEED_RANGES.get(kind)
        if rng is None:
            for w in (self._slider, self._speed_lbl, self._reset_btn):
                w.grid_remove()
            self._no_speed.grid(row=0, column=0, columnspan=3, sticky="w")
            return
        self._no_speed.grid_remove()
        for w in (self._slider, self._speed_lbl, self._reset_btn):
            w.grid()
        lo, hi, default = rng
        # 0.01x steps for Kokoro / Melo (the TTS Engine slider is continuous and the saved
        # speed is rounded to 2 decimals), 1 % for Edge, 1 WPM for pyttsx3
        steps = int(round((hi - lo) * 100)) if kind in ("kokoro", "melo") else int(hi - lo)
        self._slider.configure(from_=lo, to=hi, number_of_steps=steps)
        v = default if speed is None else max(lo, min(hi, float(speed)))
        self.speed_var.set(v)
        self._slider.set(v)
        self._speed_lbl.configure(text=self.panel.format_speed(kind, v))

    def _on_engine(self, name):
        s = self._saved
        if name == s["engine"]:          # back to the saved engine: restore its voice and speed
            self._show_kind(self.kind, s["lang"], s["voice"], s["speed"])
        else:
            self._show_kind(self.kind)
        self._on_change()

    def _on_lang(self, lang):
        self._show_kind("kokoro", lang)
        self._on_change()

    def _speed_moved(self):
        self._speed_lbl.configure(text=self.panel.format_speed(self.kind, self.speed_var.get()))
        self._on_change()

    def _reset_speed(self):
        rng = self.panel.SPEED_RANGES.get(self.kind)
        if rng:
            self.speed_var.set(rng[2])
            self._slider.set(rng[2])
            self._speed_moved()

    def selection(self) -> dict:
        """The editor's base in the format of EnginePanel.get_voice_selection()."""
        kind = self.kind
        sel = {"engine": self.engine_var.get(), "kind": kind}
        if kind == "kokoro":
            sel["lang"] = self.lang_var.get()
        if self.voice_var.get():
            sel["voice"] = self.voice_var.get()
        if kind in self.panel.SPEED_RANGES:
            v = float(self.speed_var.get())
            sel["speed"] = float(int(round(v))) if kind in ("edge", "pyttsx3") else round(v, 2)
        return sel

    def base_changed(self) -> bool:
        """True when the base differs from the saved one."""
        if self._unsaved:
            return True
        s, now = self._saved, self.selection()
        if now["engine"] != s["engine"] or now.get("voice") != s["voice"]:
            return True
        if now["kind"] == "kokoro" and now.get("lang") != s["lang"]:
            return True
        if "speed" in now:
            rng = self.panel.SPEED_RANGES[now["kind"]]
            saved = rng[2] if s["speed"] is None else s["speed"]
            return abs(now["speed"] - float(saved)) > 1e-6
        return False

    # ── sound ───────────────────────────────────────────────────────
    def _sound_moved(self, field):
        var, val, fmt = self._sound[field]
        val.configure(text=fmt(var.get()))
        self._on_change()

    def set_sound(self, field, value):
        var, val, fmt = self._sound[field]
        var.set(value)
        val.configure(text=fmt(value))
        self._on_change()

    def sound(self) -> dict:
        """The sound sliders as profile fields, e.g. {"pitch": -6, "index_rate": 0.3, ...}."""
        return {fd: _round_sound(fd, var.get()) for fd, (var, _v, _f) in self._sound.items()}

    def sound_changed(self) -> bool:
        return self.sound() != self._sound_saved

    def changed(self) -> bool:
        return self.base_changed() or self.sound_changed()


class ManageVoicesDialog(ctk.CTkToplevel):
    def __init__(self, app):
        super().__init__(app)
        use_app_icon(self)
        self.app = app
        self.title("Cloned Voices")
        self.configure(fg_color=COLORS["bg_dark"])
        self.geometry("620x600")
        self.minsize(520, 300)
        self.transient(app)
        self.resizable(True, True)

        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(fill="x", padx=16, pady=(14, 6))
        ctk.CTkLabel(head, text="✏  Cloned Voices", font=FONTS["subtitle"],
                     text_color=COLORS["text_primary"]).pack(side="left")
        ctk.CTkLabel(self, text="Rename a voice, or open its settings to change the base voice, speed "
                                "and sound, then press Save. ▶ Preview tries the settings before you "
                                "save them. Deleting removes the voice's recordings and model from disk.",
                     font=FONTS["body_small"], text_color=COLORS["text_muted"],
                     wraplength=580, justify="left").pack(anchor="w", padx=16, pady=(0, 8))

        self._list = ctk.CTkScrollableFrame(self, fg_color=COLORS["bg_card"], corner_radius=12,
                                            border_width=1, border_color=COLORS["border"],
                                            scrollbar_button_color=COLORS["border"],
                                            scrollbar_button_hover_color=COLORS["accent_primary"])
        self._list.pack(fill="both", expand=True, padx=16, pady=(0, 10))
        self._list.columnconfigure(0, weight=1)

        foot = ctk.CTkFrame(self, fg_color="transparent")
        foot.pack(fill="x", padx=16, pady=(0, 14))
        ctk.CTkButton(foot, text="📂  Open folder", font=FONTS["button"], fg_color=COLORS["bg_input"],
                      hover_color=COLORS["accent_primary"], border_color=COLORS["accent_primary"],
                      border_width=1, corner_radius=8, height=32, width=130,
                      command=self._open_folder).pack(side="left")
        ctk.CTkButton(foot, text="Close", font=FONTS["button"], fg_color=COLORS["accent_primary"],
                      hover_color=COLORS["accent_secondary"], corner_radius=8, height=32, width=90,
                      command=self.destroy).pack(side="right")

        self._rows = []
        self._open = set()              # slugs whose settings editor is showing
        self._btns = {}                 # slug -> {"preview": button, "match": button or None}
        self._editors = {}              # slug -> _VoiceEditor
        self._ready = set()             # slugs that can be previewed
        self._matchable = set()         # RVC slugs with clips to measure
        self._job = None                # (slug, cancel Event, "preview" | "match") while one runs
        self._playing = None            # slug whose preview is playing
        self._last = {}                 # slug -> (fingerprint, wav) of the last preview made here
        self._build_rows()
        self.after(100, self._focus)

    def _focus(self):
        try:
            self.lift()
            self.focus_force()
            self.grab_set()
        except Exception:
            pass

    def destroy(self):
        if self._job is not None:
            self._job[1].set()
        self._stop_playback()
        super().destroy()

    @property
    def _group(self):
        return getattr(self.app.cust_panel, "clone_group", None)

    def _build_rows(self):
        for r in self._rows:
            r.destroy()
        self._rows = []
        self._btns = {}
        self._editors = {}
        group = self._group
        if group is not None:
            group._flush_params()       # the Clone Voice box's slider moves not yet written
        voices = library.list_voices()
        self._ready = {v.slug for v in voices if v.is_ready}
        self._matchable = {v.slug for v in voices if v.mode == "rvc" and v.clips}
        if not voices:
            lbl = ctk.CTkLabel(self._list, text="No cloned voices yet. Create one in "
                                                "Customization → Clone Voice.",
                               font=FONTS["body"], text_color=COLORS["text_muted"])
            lbl.grid(row=0, column=0, padx=12, pady=20, sticky="w")
            self._rows.append(lbl)
            return
        self._open &= {v.slug for v in voices}
        for i, v in enumerate(voices):
            self._rows.append(self._row(i, v))

    def _row(self, i, v):
        from ui.panels.engine_panel import EnginePanel
        panel = self.app.engine_panel
        row = ctk.CTkFrame(self._list, fg_color=COLORS["bg_input"], corner_radius=10,
                           border_width=1, border_color=COLORS["border"])
        row.grid(row=i, column=0, sticky="ew", padx=6, pady=4)
        row.columnconfigure(0, weight=1)

        name_var = ctk.StringVar(value=v.name)
        entry = ctk.CTkEntry(row, textvariable=name_var, font=FONTS["body"], height=30,
                             fg_color=COLORS["bg_card"], border_color=COLORS["border"],
                             text_color=COLORS["text_primary"])
        entry.grid(row=0, column=0, sticky="ew", padx=(10, 8), pady=(8, 2))

        kind = "RVC" + (" · imported" if v.imported else "") if v.mode == "rvc" else "Quick"
        state = {"ready": "", "training": " · training", "draft": " · unfinished",
                 "failed": " · failed"}.get(v.status, f" · {v.status}")
        size = _folder_mb(v.dir)
        meta = ctk.CTkLabel(row, text=f"🧬 {kind}{state} · {size:.0f} MB", font=FONTS["body_small"],
                            text_color=COLORS["warning"] if state else COLORS["text_muted"])
        meta.grid(row=1, column=0, sticky="w", padx=12, pady=(0, 0))

        sel = CloneVoiceGroup._selection_for(v)
        base_text = EnginePanel.describe_selection(sel) or "not set"
        base = ctk.CTkLabel(row, text=f"Base  {base_text}", font=FONTS["body_small"], anchor="w",
                            text_color=COLORS["text_secondary"] if sel else COLORS["text_muted"])
        base.grid(row=2, column=0, sticky="w", padx=12, pady=(0, 8))
        _Tip(base, "The TTS engine voice and speed this clone converts from. Switching the clone on "
                   "selects them in TTS Engine.")

        save = ctk.CTkButton(row, text="Save", font=FONTS["button"], width=70, height=30,
                             corner_radius=8, fg_color=COLORS["accent_primary"],
                             hover_color=COLORS["accent_secondary"], state="disabled")
        save.grid(row=0, column=1, padx=(0, 6), pady=(8, 2))
        delete = ctk.CTkButton(row, text="🗑  Delete", font=FONTS["button"], width=90, height=30,
                               corner_radius=8, fg_color=COLORS["bg_card"], hover_color=COLORS["error"],
                               border_color=COLORS["error"], border_width=1,
                               command=lambda: self._delete(v))
        delete.grid(row=0, column=2, padx=(0, 10), pady=(8, 2))
        toggle = ctk.CTkButton(row, text="🔊  Voice settings ▾", font=FONTS["body_small"], width=166,
                               height=26, corner_radius=8, fg_color="transparent",
                               hover_color=COLORS["accent_primary"],
                               border_color=COLORS["accent_primary"], border_width=1,
                               text_color=COLORS["text_primary"])
        toggle.grid(row=1, column=1, rowspan=2, columnspan=2, sticky="e", padx=(0, 10), pady=(0, 8))
        _Tip(toggle, "Change the engine voice and speed this clone is made from, and how the "
                     "clone sounds. Preview before you save.")

        editor = {"ed": None}

        def on_edit(*_):
            new = name_var.get().strip()
            changed = (new and new != v.name) or (editor["ed"] is not None and editor["ed"].changed())
            save.configure(state="normal" if changed else "disabled")

        def open_editor(show):
            if show and editor["ed"] is None:
                ed = _VoiceEditor(row, panel, v, on_edit)
                btn = ctk.CTkButton(ed.foot, text="▶  Preview", font=FONTS["button"], width=104,
                                    height=28, corner_radius=8, fg_color="transparent",
                                    hover_color=COLORS["success"], border_color=COLORS["success"],
                                    border_width=1, text_color=COLORS["text_primary"],
                                    command=lambda: self._on_preview(v, ed))
                btn.grid(row=0, column=0, sticky="w")
                if ed.match_btn is not None:
                    ed.match_btn.configure(command=lambda: self._on_match(v, ed))
                if not v.is_ready and not ed.note:
                    ed.status.configure(text="Preview works once the voice is ready.")
                self._btns[v.slug] = {"preview": btn, "match": ed.match_btn}
                self._editors[v.slug] = ed
                editor["ed"] = ed
                self._refresh_buttons()
                on_edit()
            if show:
                editor["ed"].frame.grid(row=3, column=0, columnspan=3, sticky="ew",
                                        padx=10, pady=(0, 10))
                self._open.add(v.slug)
            elif editor["ed"] is not None:
                editor["ed"].frame.grid_remove()
                self._open.discard(v.slug)
            toggle.configure(text="🔊  Voice settings ▴" if show else "🔊  Voice settings ▾")

        toggle.configure(command=lambda: open_editor(v.slug not in self._open))
        save.configure(command=lambda: self._save(v, name_var.get(), editor["ed"]))
        name_var.trace_add("write", on_edit)
        entry.bind("<Return>", lambda e: save.cget("state") == "normal"
                   and self._save(v, name_var.get(), editor["ed"]))
        if v.slug in self._open:
            open_editor(True)
        return row

    # ── save / delete ───────────────────────────────────────────────
    def _busy_with(self, v, action):
        from core.voice_cloning import is_training
        if v.status == "training" and is_training():
            messagebox.showinfo("Voice is training", f"'{v.name}' is training. Cancel the training "
                                f"first, then {action} it.", parent=self)
            return True
        if self.app.is_generating:
            messagebox.showinfo("Generating", f"Wait for the current generation to finish, then {action} "
                                "the voice.", parent=self)
            return True
        if self._job is not None and self._job[0] == v.slug:
            what = "preview" if self._job[2] == "preview" else "pitch match"
            messagebox.showinfo("Busy", f"Wait for the {what} of '{v.name}' to finish, or cancel "
                                f"it, then {action} the voice.", parent=self)
            return True
        return False

    def _save(self, v, new_name, editor):
        from ui.panels.engine_panel import EnginePanel
        new_name = new_name.strip()[:40]
        changes = {}
        if new_name and new_name != v.name:
            if any(o.name.lower() == new_name.lower() and o.slug != v.slug
                   for o in library.list_voices()):
                messagebox.showwarning("Name in use", f"A voice named '{new_name}' already exists.",
                                       parent=self)
                return
            changes["name"] = new_name
        sel = editor.selection() if editor is not None and editor.base_changed() else None
        if sel is not None:
            changes.update(base_selection=sel, base_engine=sel["engine"],
                           base_voice=str(sel.get("voice") or ""))
        sound = editor.sound() if editor is not None and editor.sound_changed() else None
        if sound is not None:
            changes.update(sound)
            if editor.f0 is not None and "pitch" in sound:
                changes.update(base_f0_hz=editor.f0[0], target_f0_hz=editor.f0[1])
        if not changes or self._busy_with(v, "change"):
            return
        p = library.update_params(v.slug, **changes)
        if "name" in changes:
            self.app.refresh_cloned_voices()

        notes = []
        if "name" in changes:
            notes.append(f"renamed '{v.name}' to '{p.name}'")
        if sel is not None:
            notes.append(f"base voice of '{p.name}' set to {EnginePanel.describe_selection(sel)}")
        if sound is not None:
            notes.append(f"sound settings of '{p.name}' saved")
        text = "; ".join(notes)
        msg = "✅  " + text[0].upper() + text[1:]
        group = self._group
        if group is not None and group._active_slug == v.slug:
            group.reload_active(p)              # keep the Clone Voice box in step
            if sel is not None:                 # the voice is on: use the new base now
                err = self.app.engine_panel.apply_voice_selection(sel)
                msg += f" (TTS Engine not changed: {err})" if err else ", selected in TTS Engine"
        if sel is not None and editor.f0 is None and v.slug in self._matchable:
            msg += ". Press 🎯 next to Pitch to re-measure it for the new base."
        remake = (sel is not None or sound is not None) and self._update_saved_preview(p)
        self.app.set_status(msg, COLORS["success"])
        self._build_rows()
        if remake and group is not None:
            group.refresh_preview(p.slug)       # runs in the Clone Voice box, after this status

    def _update_saved_preview(self, p):
        """After a save: keep the voice's saved preview in step. True when it must be made again.

        A preview made here with exactly the saved settings becomes the saved
        one; otherwise the old one is removed and made again in the background.
        """
        group = self._group
        fp = group.saved_fingerprint(p) if group is not None else None
        if fp is None or previews.cached(p.slug, fp):
            return False
        last = self._last.get(p.slug)
        if last and last[0] == fp and os.path.isfile(last[1]):
            previews.store(p.slug, last[1], fp)
            return False
        previews.delete(p.slug)
        return p.is_ready

    def _delete(self, v):
        if self._busy_with(v, "delete"):
            return
        if not messagebox.askyesno("Delete voice",
                                   f"Delete '{v.name}'?\n\nIts recordings and model are removed "
                                   "from disk. This cannot be undone.",
                                   icon="warning", parent=self):
            return
        if self._playing == v.slug:
            self._stop_playback()
        library.delete_voice(v.slug)
        self._open.discard(v.slug)
        self.app.refresh_cloned_voices()
        self.app.set_status(f"🗑  Deleted voice '{v.name}'", COLORS["warning"])
        self._build_rows()

    # ── preview and pitch match ─────────────────────────────────────
    def _refresh_buttons(self):
        """▶ Preview and 🎯 of every open row, from what is running or playing.

        While a job runs or a preview plays, only its own button works (as
        Cancel or Stop); otherwise each works when its voice allows it.
        """
        active = None
        if self._job is not None:
            slug, _cancel, kind = self._job
            active = (slug, kind, "✖  Cancel" if kind == "preview" else "✖")
        elif self._playing is not None:
            active = (self._playing, "preview", "■  Stop")
        for slug, btns in self._btns.items():
            for kind, btn in btns.items():
                if btn is None:
                    continue
                idle_text = "▶  Preview" if kind == "preview" else "🎯"
                allowed = slug in (self._ready if kind == "preview" else self._matchable)
                try:
                    if not btn.winfo_exists():
                        continue
                    if active is None:
                        btn.configure(text=idle_text, state="normal" if allowed else "disabled")
                    elif (slug, kind) == active[:2]:
                        btn.configure(text=active[2], state="normal")
                    else:
                        btn.configure(text=idle_text, state="disabled")
                except Exception:
                    pass

    def _start_job(self, v, editor, kind, sentence, verb):
        """Shared checks for ▶ Preview and 🎯: returns (engine settings, profile) or None.

        A second click on the running job's button cancels it; a click while a
        preview plays stops it.
        """
        if self._job is not None:
            if self._job[0] == v.slug and self._job[2] == kind:
                self._job[1].set()
                editor.status.configure(text="Cancelling…", text_color=COLORS["warning"])
            return None
        if self._playing is not None:
            was = self._playing
            self._stop_playback()
            if kind == "preview" and was == v.slug:
                return None                             # the click meant Stop
        group = self._group
        if group is None:
            return None
        if group._busy:
            editor.status.configure(text=f"Clone Voice is busy ({group._task_name}). Try again when "
                                         "it finishes.", text_color=COLORS["warning"])
            return None
        if self.app.is_generating:
            editor.status.configure(text="Wait for the current generation to finish.",
                                    text_color=COLORS["warning"])
            return None
        eng, err = self.app.engine_panel.settings_for_selection(editor.selection())
        if err:
            editor.status.configure(text=f"Can't {verb}: {err}.", text_color=COLORS["error"])
            return None
        try:
            p = library.load_voice(v.slug)
        except (OSError, ValueError) as e:
            editor.status.configure(text=f"Can't load the voice: {e}", text_color=COLORS["error"])
            return None
        self._job = (v.slug, threading.Event(), kind)
        self._refresh_buttons()
        editor.status.configure(text=sentence, text_color=COLORS["text_secondary"])
        return eng, p

    def _fingerprint(self, v, editor):
        """(fingerprint, engine settings, profile) of a preview with the editor's values, or None."""
        from ui.panels.engine_panel import EnginePanel
        eng, err = self.app.engine_panel.settings_for_selection(editor.selection())
        if err:
            return None
        try:
            p = library.load_voice(v.slug)
        except (OSError, ValueError):
            return None
        sound = previews.sound_of(p, editor.sound())
        return previews.fingerprint(p, EnginePanel.synthesis_key(eng), sound), eng, p

    def _on_preview(self, v, editor):
        if self._job is None and self._playing is None and v.is_ready:
            made = self._fingerprint(v, editor)
            saved = made and previews.cached(v.slug, made[0])
            if saved:                                   # these exact settings were heard before
                self._preview_done(v.slug, saved, None, saved=True)
                return
        started = self._start_job(v, editor, "preview", "Speaking the preview sentence…", "preview")
        if started is None:
            return
        eng, p = started
        cancel, group, slug = self._job[1], self._group, v.slug
        sound = previews.sound_of(p, editor.sound())    # the sliders, saved or not
        made = self._fingerprint(v, editor)
        fp = made[0] if made else None
        keep = fp is not None and fp == group.saved_fingerprint(p)

        def work():
            try:
                path = group.make_preview(p, eng, sound, cancel=cancel,
                                          say=lambda m: self._ui(self._say, slug, m,
                                                                 COLORS["text_secondary"]))
                if keep:
                    previews.store(slug, path, fp)
                if fp is not None:
                    self._last[slug] = (fp, path)
                self._ui(self._preview_done, slug, path, None)
            except VCCancelled:
                self._ui(self._preview_done, slug, None, "Preview cancelled.")
            except VCError as e:
                self._ui(self._preview_done, slug, None, f"Preview failed: {e.message}")
            except Exception as e:   # noqa: BLE001
                self._ui(self._preview_done, slug, None, f"Preview failed: {type(e).__name__}: {e}")
        threading.Thread(target=work, daemon=True, name="clone-settings-preview").start()

    def _on_match(self, v, editor):
        started = self._start_job(v, editor, "match", "Speaking a sample with the base voice…",
                                  "match pitch")
        if started is None:
            return
        eng, p = started
        cancel, group, slug = self._job[1], self._group, v.slug

        def work():
            import soundfile as sf
            from core.voice_cloning import pitch
            try:
                tmp = group._new_tmp()
                y, sr = group._synthesize(pitch.BASE_SENTENCE, eng, tmp, cancel=cancel)
                base_wav = os.path.join(tmp, "base.wav")
                sf.write(base_wav, y, sr)
                r = pitch.estimate_offset(base_wav, library.clip_audio_paths(p),
                                          status_cb=lambda m: self._ui(self._say, slug, m,
                                                                       COLORS["text_secondary"]))
                if cancel.is_set():
                    raise VCCancelled()
                self._ui(self._match_done, slug, r, None)
            except VCCancelled:
                self._ui(self._match_done, slug, None, "Pitch match cancelled.")
            except VCError as e:
                self._ui(self._match_done, slug, None, f"Pitch match failed: {e.message}")
            except Exception as e:   # noqa: BLE001
                self._ui(self._match_done, slug, None,
                         f"Pitch match failed: {type(e).__name__}: {e}")
        threading.Thread(target=work, daemon=True, name="clone-settings-match").start()

    def _match_done(self, slug, r, error):
        self._job = None
        self._refresh_buttons()
        if error:
            self._say(slug, error, COLORS["warning"] if "cancel" in error else COLORS["error"])
            return
        if not r["base_f0"] or not r["target_f0"]:
            which = "the base voice" if not r["base_f0"] else "the clips"
            self._say(slug, f"Couldn't measure the pitch of {which} (too little voiced speech). "
                            "Pitch was not changed.", COLORS["warning"])
            return
        ed = self._editors.get(slug)
        if ed is None:
            return
        ed.f0 = (r["base_f0"], r["target_f0"])
        ed.set_sound("pitch", r["offset"])
        self._say(slug, f"Pitch set to {r['offset']:+d} st (base {r['base_f0']:.0f} Hz → target "
                        f"{r['target_f0']:.0f} Hz). Preview it, then Save.", COLORS["success"])

    def _ui(self, fn, *args):
        """Run fn on the Tk thread, unless this window has closed."""
        def call():
            try:
                if self.winfo_exists():
                    fn(*args)
            except Exception:
                pass
        try:
            self.app.after(0, call)
        except RuntimeError:
            pass

    def _say(self, slug, text, color):
        """Show *text* under the voice's editor, if it is still there."""
        ed = self._editors.get(slug)
        try:
            if ed is not None and ed.status.winfo_exists():
                ed.status.configure(text=text, text_color=color)
        except Exception:
            pass

    def _preview_done(self, slug, path, error, saved=False):
        self._job = None
        if error:
            self._refresh_buttons()
            self._say(slug, error, COLORS["warning"] if "cancel" in error else COLORS["error"])
            return
        group = self._group
        if group is not None:
            group._stop_preview()                       # frees channel 2 from a cleanup preview
        if group is None or not group._play(path):
            self._refresh_buttons()
            self._say(slug, "The preview was made but could not be played.", COLORS["error"])
            return
        self._playing = slug
        self._refresh_buttons()
        self._say(slug, "Playing the saved preview." if saved else "Playing. Save keeps these settings.",
                  COLORS["success"])
        self.after(200, self._watch_playback)

    def _watch_playback(self):
        if self._playing is None:
            return
        try:
            import pygame
            busy = pygame.mixer.get_init() and pygame.mixer.Channel(2).get_busy()
        except Exception:
            busy = False
        if busy:
            self.after(200, self._watch_playback)
        else:
            self._stop_playback()

    def _stop_playback(self):
        if self._playing is None:
            return
        self._playing = None
        try:
            import pygame
            if pygame.mixer.get_init():
                pygame.mixer.Channel(2).stop()
        except Exception:
            pass
        self._refresh_buttons()

    def _open_folder(self):
        d = runtime.voices_dir()
        d.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(str(d))
        except AttributeError:
            subprocess.Popen(["xdg-open", str(d)])
