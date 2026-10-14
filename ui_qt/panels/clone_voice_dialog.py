"""
ui_qt/panels/clone_voice_dialog.py  –  rename, tune or delete cloned voices (Qt)

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

Same rows, rules and messages as ui/panels/clone_voice_dialog.py
(docs/ui-parity-checklist.md, "Cloned Voices dialog"). Previews and pitch
matching run the shared jobs in core.voice_cloning.tasks through the Clone
Voice group, and reach the widgets only through ui_qt.bridge.ui().
"""

import dataclasses
import os
import subprocess
import threading

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QComboBox, QDialog, QFrame, QGridLayout, QHBoxLayout,
                               QLineEdit, QMessageBox, QScrollArea, QVBoxLayout, QWidget)

from core import voice_selection
from core.constants import COLORS, KOKORO_LANGS
from core.voice_cloning import library, previews, runtime, tasks
from core.voice_cloning.errors import VCCancelled, VCError
from ui_qt import theme
from ui_qt.bridge import ui
from ui_qt.widgets import IconLabel, ValueSlider

# (profile field, label, low, high, step, format, tip) of each sound slider,
# the same ranges as the Clone Voice box.
RVC_SOUND = (
    ("pitch", "Pitch", -24, 24, 1, lambda v: f"{int(round(v)):+d} st",
     "Shifts the cloned voice up or down, in semitones."),
    ("index_rate", "Index", 0, 1, 0.01, lambda v: f"{v:.2f}",
     "Index rate: higher pulls the accent closer to the target voice."),
    ("protect", "Protect", 0, 0.5, 0.01, lambda v: f"{v:.2f}",
     "Keeps breaths and consonants clean. 0.33 suits most voices."),
)
# Each sound field's value for a new voice (↺ goes back to it).
_SOUND_DEFAULTS = {f.name: f.default for f in dataclasses.fields(library.VoiceProfile)}
QUICK_SOUND = (
    ("diffusion_steps", "Quality", 4, 50, 1, lambda v: f"{int(round(v))} steps",
     "More steps sound smoother but take longer. 10 is a good balance on this GPU."),
)
PREVIEW_CHANNEL = 2


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


def _small(text="", color=None):
    lab = IconLabel(text)
    lab.setProperty("small", True)
    theme.set_color(lab, color or COLORS["text_secondary"])
    return lab


def _side_button(text, tip, command=None):
    b = theme.line_button(text, COLORS["accent_primary"], tip, height=24, small=True)
    b.setFixedWidth(28)
    b.setAutoDefault(False)
    if command:
        b.clicked.connect(command)
    return b


class _VoiceEditor:
    """Base voice (engine, language, voice, speed) and sound settings of one voice, in its row.

    Built the first time the row is opened. The base starts from the saved
    one, or from the TTS Engine card when the saved base cannot be shown here.
    """

    def __init__(self, panel, v, on_change):
        self.panel = panel
        self.mode = v.mode
        self._on_change = on_change
        self.f0 = None                  # (base Hz, target Hz) from the last 🎯 measurement
        self.frame = QFrame()
        self.frame.setObjectName("cvTile")
        g = QGridLayout(self.frame)
        g.setContentsMargins(10, 8, 10, 8)
        g.setHorizontalSpacing(6)
        g.setVerticalSpacing(6)
        g.setColumnStretch(1, 1)
        self._g = g

        sel = tasks.selection_for(v)
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
        self._engine = self._menu_row("Engine", panel.engine_names(), engine)[1]
        self._lang_lbl, self._lang = self._menu_row("Language", list(KOKORO_LANGS), lang or "American English")
        self._voice_lbl, self._voice = self._menu_row("Voice", [""], voice or "")
        self._engine.currentTextChanged.connect(self._on_engine)
        self._lang.currentTextChanged.connect(self._on_lang)
        self._voice.currentTextChanged.connect(lambda _t: self._on_change())

        self._label("Speed")
        sp = QWidget()
        sh = QHBoxLayout(sp)
        sh.setContentsMargins(0, 0, 0, 0)
        sh.setSpacing(6)
        self._slider = ValueSlider(0.5, 2.0, 1.0, step=0.01)
        self._slider.valueChangedF.connect(lambda _v: self._speed_moved())
        sh.addWidget(self._slider, 1)
        self._speed_lbl = _small("", COLORS["text_muted"])
        self._speed_lbl.setFixedWidth(62)
        self._speed_lbl.setAlignment(Qt.AlignCenter)
        sh.addWidget(self._speed_lbl)
        self._reset_btn = _side_button("↺", "Back to the engine's normal speed.", self._reset_speed)
        sh.addWidget(self._reset_btn)
        self._no_speed = _small("This engine has no speed setting.", COLORS["text_muted"])
        sh.addWidget(self._no_speed)
        g.addWidget(sp, self._row, 1)
        self._row += 1

        # Sound: the conversion settings, saved in the profile next to the base.
        self._sound = {}                # field -> (slider, value label, format)
        self._sound_saved = {}
        self.match_btn = None
        for field, label, lo, hi, step, fmt, tip in (RVC_SOUND if v.mode == "rvc" else QUICK_SOUND):
            value = getattr(v, field)
            self._sound_saved[field] = _round_sound(field, value)
            self._label(label, tip)
            line = QWidget()
            lh = QHBoxLayout(line)
            lh.setContentsMargins(0, 0, 0, 0)
            lh.setSpacing(6)
            s = ValueSlider(lo, hi, value, step=step)
            lh.addWidget(s, 1)
            val = _small(fmt(value), COLORS["text_muted"])
            val.setFixedWidth(62)
            val.setAlignment(Qt.AlignCenter)
            lh.addWidget(val)
            s.valueChangedF.connect(lambda _v, fd=field: self._sound_moved(fd))
            self._sound[field] = (s, val, fmt)
            if field == "pitch":
                self.match_btn = _side_button("🎯", "Match pitch: measure the base voice above and "
                                                    "your clips, then set Pitch so they line up.")
                lh.addWidget(self.match_btn)
            elif field in ("index_rate", "protect"):
                default = _SOUND_DEFAULTS[field]
                lh.addWidget(_side_button("↺", f"Back to {fmt(default)}, the default.",
                                          lambda _=False, fd=field, d=default: self.set_sound(fd, d)))
            else:
                spacer = QWidget()
                spacer.setFixedWidth(28)
                lh.addWidget(spacer)    # every slider ends at the same x
            g.addWidget(line, self._row, 1)
            self._row += 1

        self.foot = QHBoxLayout()
        self.foot.setContentsMargins(0, 4, 0, 0)
        self.status = _small(self.note or "", COLORS["warning"] if self.note else COLORS["text_muted"])
        self.status.setWordWrap(True)
        g.addLayout(self.foot, self._row, 0, 1, 2)
        self._show_kind(kind, lang, voice, sel.get("speed"))

    # ── building ────────────────────────────────────────────────────
    def _label(self, text, tip=None):
        lbl = _small(text)
        lbl.setFixedWidth(64)
        if tip:
            lbl.setToolTip(tip)
        self._g.addWidget(lbl, self._row, 0)
        return lbl

    def _menu_row(self, text, values, current):
        lbl = self._label(text)
        menu = QComboBox()
        menu.addItems(values)
        menu.setCurrentText(current)
        menu.setProperty("onCard", True)
        menu.setMaxVisibleItems(15)
        self._g.addWidget(menu, self._row, 1)
        self._row += 1
        return lbl, menu

    @staticmethod
    def _fill(menu, values, current):
        menu.blockSignals(True)
        menu.clear()
        menu.addItems(values)
        menu.setCurrentText(current)
        menu.blockSignals(False)

    # ── base voice ──────────────────────────────────────────────────
    @property
    def kind(self):
        return self.panel.engine_kind(self._engine.currentText())

    def _show_kind(self, kind, lang=None, voice=None, speed=None):
        """Show the rows *kind* needs and fill them (first voice / normal speed by default)."""
        if kind == "kokoro":
            self._lang_lbl.show()
            self._lang.show()
            if lang:
                self._lang.blockSignals(True)
                self._lang.setCurrentText(lang)
                self._lang.blockSignals(False)
            choices = self.panel.voice_choices(kind, self._lang.currentText())
        else:
            self._lang_lbl.hide()
            self._lang.hide()
            choices = self.panel.voice_choices(kind)
        if choices:
            self._voice_lbl.show()
            self._voice.show()
            self._fill(self._voice, choices, voice if voice in choices else choices[0])
        else:
            self._voice_lbl.hide()
            self._voice.hide()
            self._fill(self._voice, [], "")

        rng = self.panel.SPEED_RANGES.get(kind)
        if rng is None:
            for w in (self._slider, self._speed_lbl, self._reset_btn):
                w.hide()
            self._no_speed.show()
            return
        self._no_speed.hide()
        for w in (self._slider, self._speed_lbl, self._reset_btn):
            w.show()
        lo, hi, default = rng
        # 0.01x steps for Kokoro / Melo, 1 % for Edge, 1 WPM for pyttsx3
        self._slider.set_range(lo, hi, 0.01 if kind in ("kokoro", "melo") else 1)
        v = default if speed is None else max(lo, min(hi, float(speed)))
        self._slider.set_value(v)
        self._speed_lbl.setText(self.panel.format_speed(kind, v))

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
        self._speed_lbl.setText(self.panel.format_speed(self.kind, self._slider.value_f()))
        self._on_change()

    def _reset_speed(self):
        rng = self.panel.SPEED_RANGES.get(self.kind)
        if rng:
            self._slider.set_value(rng[2])
            self._speed_moved()

    def selection(self) -> dict:
        """The editor's base in the format of EnginePanel.get_voice_selection()."""
        kind = self.kind
        sel = {"engine": self._engine.currentText(), "kind": kind}
        if kind == "kokoro":
            sel["lang"] = self._lang.currentText()
        if self._voice.currentText():
            sel["voice"] = self._voice.currentText()
        if kind in self.panel.SPEED_RANGES:
            v = float(self._slider.value_f())
            sel["speed"] = float(int(round(v))) if kind in ("edge", "pyttsx3") else round(v, 2)
        return sel

    def base_changed(self) -> bool:
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
        s, val, fmt = self._sound[field]
        val.setText(fmt(s.value_f()))
        self._on_change()

    def set_sound(self, field, value):
        s, val, fmt = self._sound[field]
        s.set_value(value)
        val.setText(fmt(s.value_f()))
        self._on_change()

    def sound(self) -> dict:
        return {fd: _round_sound(fd, s.value_f()) for fd, (s, _v, _f) in self._sound.items()}

    def sound_changed(self) -> bool:
        return self.sound() != self._sound_saved

    def changed(self) -> bool:
        return self.base_changed() or self.sound_changed()

    def say(self, text, color):
        self.status.setText(text)
        theme.set_color(self.status, color)


class ManageVoicesDialog(QDialog):
    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.setWindowTitle("Cloned Voices")
        self.resize(620, 600)
        self.setMinimumSize(520, 300)
        self.setModal(True)             # the CTk window grabs input after 100 ms
        self.setAttribute(Qt.WA_DeleteOnClose)
        self._closed = False

        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        title = theme.label("✏  Cloned Voices")
        title.setObjectName("cardTitle")
        lay.addWidget(title)
        help_ = theme.label("Rename a voice, or open its settings to change the base voice, speed "
                            "and sound, then press Save. Preview tries the settings before you "
                            "save them. Deleting removes the voice's recordings and model from disk.",
                            muted=True, small=True)
        help_.setWordWrap(True)
        lay.addWidget(help_)

        frame = QFrame()
        frame.setObjectName("card")
        fl = QVBoxLayout(frame)
        fl.setContentsMargins(4, 4, 4, 4)
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.NoFrame)
        self._list_w = QWidget()
        self._list = QVBoxLayout(self._list_w)
        self._list.setContentsMargins(6, 6, 6, 6)
        self._list.setSpacing(8)
        self._scroll.setWidget(self._list_w)
        fl.addWidget(self._scroll)
        lay.addWidget(frame, 1)

        foot = QHBoxLayout()
        folder = theme.line_button("📂  Open folder", COLORS["accent_primary"], height=32)
        folder.setFixedWidth(130)
        folder.setAutoDefault(False)
        folder.clicked.connect(self._open_folder)
        foot.addWidget(folder)
        foot.addStretch(1)
        close = theme.button("Close", theme.ROLE_PRIMARY)
        close.setFixedWidth(90)
        close.setAutoDefault(False)
        close.clicked.connect(self.close)
        foot.addWidget(close)
        lay.addLayout(foot)

        self._rows = []
        self._open = set()              # slugs whose settings editor is showing
        self._btns = {}                 # slug -> {"preview": button, "match": button or None}
        self._editors = {}              # slug -> _VoiceEditor
        self._ready = set()
        self._matchable = set()
        self._job = None                # (slug, cancel Event, "preview" | "match") while one runs
        self._playing = None            # slug whose preview is playing
        self._last = {}                 # slug -> (fingerprint, wav) of the last preview made here
        self._build_rows()

    def closeEvent(self, event):
        self._closed = True
        if self._job is not None:
            self._job[1].set()
        self._stop_playback()
        super().closeEvent(event)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            return                      # Enter belongs to the name entries, not to Close
        super().keyPressEvent(event)

    @property
    def _group(self):
        return getattr(self.app.cust_panel, "clone_group", None)

    def _build_rows(self):
        for r in self._rows:
            r.setParent(None)
            r.deleteLater()
        self._rows = []
        self._btns = {}
        self._editors = {}
        while self._list.count():
            self._list.takeAt(0)
        group = self._group
        if group is not None:
            group._flush_params()       # the Clone Voice box's slider moves not yet written
        voices = library.list_voices()
        self._ready = {v.slug for v in voices if v.is_ready}
        self._matchable = {v.slug for v in voices if v.mode == "rvc" and v.clips}
        if not voices:
            lbl = theme.label("No cloned voices yet. Create one in Customization → Clone Voice.", muted=True)
            self._list.addWidget(lbl)
            self._rows.append(lbl)
        else:
            self._open &= {v.slug for v in voices}
            for v in voices:
                row = self._row(v)
                self._list.addWidget(row)
                self._rows.append(row)
        self._list.addStretch(1)

    def _row(self, v):
        panel = self.app.engine_panel
        row = QFrame()
        row.setObjectName("inset")
        g = QGridLayout(row)
        g.setContentsMargins(10, 8, 10, 8)
        g.setHorizontalSpacing(6)
        g.setVerticalSpacing(2)
        g.setColumnStretch(0, 1)

        entry = QLineEdit(v.name)
        g.addWidget(entry, 0, 0)
        kind = "RVC" + (" · imported" if v.imported else "") if v.mode == "rvc" else "Quick"
        state = {"ready": "", "training": " · training", "draft": " · unfinished",
                 "failed": " · failed"}.get(v.status, f" · {v.status}")
        meta = _small(f"🧬 {kind}{state} · {_folder_mb(v.dir):.0f} MB",
                      COLORS["warning"] if state else COLORS["text_muted"])
        g.addWidget(meta, 1, 0)
        sel = tasks.selection_for(v)
        base = _small(f"Base  {voice_selection.describe_selection(sel) or 'not set'}",
                      COLORS["text_secondary"] if sel else COLORS["text_muted"])
        base.setToolTip("The TTS engine voice and speed this clone converts from. Switching the clone on "
                        "selects them in TTS Engine.")
        g.addWidget(base, 2, 0)

        save = theme.button("Save", theme.ROLE_PRIMARY)
        save.setFixedWidth(70)
        save.setAutoDefault(False)
        save.setEnabled(False)
        g.addWidget(save, 0, 1)
        delete = theme.line_button("🗑  Delete", COLORS["error"], height=30)
        delete.setFixedWidth(90)
        delete.setAutoDefault(False)
        delete.clicked.connect(lambda: self._delete(v))
        g.addWidget(delete, 0, 2)
        toggle = theme.line_button("▾  Voice settings", COLORS["accent_primary"], height=26, small=True,
                                   tip="Change the engine voice and speed this clone is made from, and how "
                                       "the clone sounds. Preview before you save.")
        toggle.setFixedWidth(166)
        toggle.setAutoDefault(False)
        g.addWidget(toggle, 1, 1, 2, 2, Qt.AlignRight)

        editor = {"ed": None}

        def on_edit(*_):
            new = entry.text().strip()
            changed = (bool(new) and new != v.name) or (editor["ed"] is not None and editor["ed"].changed())
            save.setEnabled(bool(changed))

        def open_editor(show):
            if show and editor["ed"] is None:
                ed = _VoiceEditor(panel, v, on_edit)
                btn = theme.line_button("▶  Preview", COLORS["success"], height=28)
                btn.setFixedWidth(104)
                btn.setAutoDefault(False)
                btn.clicked.connect(lambda: self._on_preview(v, ed))
                ed.foot.addWidget(btn, 0, Qt.AlignTop)
                ed.foot.addWidget(ed.status, 1)
                if ed.match_btn is not None:
                    ed.match_btn.clicked.connect(lambda: self._on_match(v, ed))
                if not v.is_ready and not ed.note:
                    ed.say("Preview works once the voice is ready.", COLORS["text_muted"])
                self._btns[v.slug] = {"preview": btn, "match": ed.match_btn}
                self._editors[v.slug] = ed
                editor["ed"] = ed
                g.addWidget(ed.frame, 3, 0, 1, 3)
                self._refresh_buttons()
                on_edit()
            if show:
                editor["ed"].frame.show()
                self._open.add(v.slug)
            elif editor["ed"] is not None:
                editor["ed"].frame.hide()
                self._open.discard(v.slug)
            toggle.setText("▴  Voice settings" if show else "▾  Voice settings")

        toggle.clicked.connect(lambda: open_editor(v.slug not in self._open))
        save.clicked.connect(lambda: self._save(v, entry.text(), editor["ed"]))
        entry.textChanged.connect(on_edit)
        entry.returnPressed.connect(lambda: save.isEnabled() and self._save(v, entry.text(), editor["ed"]))
        if v.slug in self._open:
            open_editor(True)
        return row

    # ── save / delete ───────────────────────────────────────────────
    def _busy_with(self, v, action):
        from core.voice_cloning import is_training
        if v.status == "training" and is_training():
            QMessageBox.information(self, "Voice is training", f"'{v.name}' is training. Cancel the training "
                                    f"first, then {action} it.")
            return True
        if self.app.is_generating:
            QMessageBox.information(self, "Generating", f"Wait for the current generation to finish, then "
                                    f"{action} the voice.")
            return True
        if self._job is not None and self._job[0] == v.slug:
            what = "preview" if self._job[2] == "preview" else "pitch match"
            QMessageBox.information(self, "Busy", f"Wait for the {what} of '{v.name}' to finish, or cancel "
                                    f"it, then {action} the voice.")
            return True
        return False

    def _save(self, v, new_name, editor):
        new_name = new_name.strip()[:40]
        changes = {}
        if new_name and new_name != v.name:
            if any(o.name.lower() == new_name.lower() and o.slug != v.slug for o in library.list_voices()):
                QMessageBox.warning(self, "Name in use", f"A voice named '{new_name}' already exists.")
                return
            changes["name"] = new_name
        sel = editor.selection() if editor is not None and editor.base_changed() else None
        if sel is not None:
            changes.update(base_selection=sel, base_engine=sel["engine"], base_voice=str(sel.get("voice") or ""))
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
            notes.append(f"base voice of '{p.name}' set to {voice_selection.describe_selection(sel)}")
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
            msg += ". Press Match pitch next to Pitch to re-measure it for the new base."
        remake = (sel is not None or sound is not None) and self._update_saved_preview(p)
        self.app.set_status(msg, COLORS["success"])
        self._build_rows()
        if remake and group is not None:
            group.refresh_preview(p.slug)       # runs in the Clone Voice box, after this status

    def _update_saved_preview(self, p):
        """After a save: keep the voice's saved preview in step. True when it must be made again."""
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
        if QMessageBox.warning(self, "Delete voice",
                               f"Delete '{v.name}'?\n\nIts recordings and model are removed "
                               "from disk. This cannot be undone.",
                               QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
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
        """▶ Preview and 🎯 of every open row, from what is running or playing."""
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
                if active is None:
                    btn.setText(idle_text)
                    btn.setEnabled(allowed)
                elif (slug, kind) == active[:2]:
                    btn.setText(active[2])
                    btn.setEnabled(True)
                else:
                    btn.setText(idle_text)
                    btn.setEnabled(False)

    def _start_job(self, v, editor, kind, sentence, verb):
        """Shared checks for ▶ Preview and 🎯: returns (engine settings, profile) or None."""
        if self._job is not None:
            if self._job[0] == v.slug and self._job[2] == kind:
                self._job[1].set()
                editor.say("Cancelling…", COLORS["warning"])
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
            editor.say(f"Clone Voice is busy ({group._task_name}). Try again when it finishes.", COLORS["warning"])
            return None
        if self.app.is_generating:
            editor.say("Wait for the current generation to finish.", COLORS["warning"])
            return None
        eng, err = self.app.engine_panel.settings_for_selection(editor.selection())
        if err:
            editor.say(f"Can't {verb}: {err}.", COLORS["error"])
            return None
        try:
            p = library.load_voice(v.slug)
        except (OSError, ValueError) as e:
            editor.say(f"Can't load the voice: {e}", COLORS["error"])
            return None
        self._job = (v.slug, threading.Event(), kind)
        self._refresh_buttons()
        editor.say(sentence, COLORS["text_secondary"])
        return eng, p

    def _fingerprint(self, v, editor):
        """(fingerprint, engine settings, profile) of a preview with the editor's values, or None."""
        eng, err = self.app.engine_panel.settings_for_selection(editor.selection())
        if err:
            return None
        try:
            p = library.load_voice(v.slug)
        except (OSError, ValueError):
            return None
        sound = previews.sound_of(p, editor.sound())
        return previews.fingerprint(p, voice_selection.synthesis_key(eng), sound), eng, p

    def _ui(self, fn, *args):
        """Run fn on the GUI thread, unless this window has closed."""
        ui(lambda: None if self._closed else fn(*args))

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
        sound = previews.sound_of(p, editor.sound())
        made = self._fingerprint(v, editor)
        fp = made[0] if made else None
        keep = fp is not None and fp == group.saved_fingerprint(p)
        ctx = group._ctx()

        def work():
            try:
                path = tasks.make_preview(ctx, p, eng, sound, cancel=cancel,
                                          say=lambda m: self._ui(self._say, slug, m, COLORS["text_secondary"]))
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
        started = self._start_job(v, editor, "match", "Speaking a sample with the base voice…", "match pitch")
        if started is None:
            return
        eng, p = started
        cancel, group, slug = self._job[1], self._group, v.slug
        ctx = group._ctx()

        def work():
            try:
                r = tasks.measure_pitch(ctx, p, eng, cancel=cancel,
                                        say=lambda m: self._ui(self._say, slug, m, COLORS["text_secondary"]))
                self._ui(self._match_done, slug, r, None)
            except VCCancelled:
                self._ui(self._match_done, slug, None, "Pitch match cancelled.")
            except VCError as e:
                self._ui(self._match_done, slug, None, f"Pitch match failed: {e.message}")
            except Exception as e:   # noqa: BLE001
                self._ui(self._match_done, slug, None, f"Pitch match failed: {type(e).__name__}: {e}")
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

    def _say(self, slug, text, color):
        ed = self._editors.get(slug)
        if ed is not None:
            ed.say(text, color)

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
        QTimer.singleShot(200, self._watch_playback)

    def _watch_playback(self):
        if self._playing is None or self._closed:
            return
        try:
            import pygame
            busy = pygame.mixer.get_init() and pygame.mixer.Channel(PREVIEW_CHANNEL).get_busy()
        except Exception:
            busy = False
        if busy:
            QTimer.singleShot(200, self._watch_playback)
        else:
            self._stop_playback()

    def _stop_playback(self):
        if self._playing is None:
            return
        self._playing = None
        try:
            import pygame
            if pygame.mixer.get_init():
                pygame.mixer.Channel(PREVIEW_CHANNEL).stop()
        except Exception:
            pass
        if not self._closed:
            self._refresh_buttons()

    def _open_folder(self):
        d = runtime.voices_dir()
        d.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(str(d))
        except AttributeError:
            subprocess.Popen(["xdg-open", str(d)])
