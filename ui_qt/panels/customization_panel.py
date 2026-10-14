"""
ui_qt/panels/customization_panel.py  –  Skip silences, subtitles, EQ, ambiance (Qt)

Same controls, values and get_settings() keys as
ui/panels/customization_panel.py, in the same order:

    [Skip Silences]
    [Auto-Subtitles]   Mode: [Word-Level ▾]
    [3-Band EQ]        Bass / Mids / Treble sliders, shown while on
    Ambiance: [None ▾]   Vol: ━━●━━ 50%
      [📥 Import] [▶ Play] [⏸ Pause] [⏮ Restart] [✖ Remove]  file name

The Clone Voice group (ui_qt/panels/clone_voice_group.py) sits last, built
when the app is passed, as in the CTk panel.

One change from the CTk panel: choosing "Custom File..." again after a
built-in sound reloads the imported file. The CTk panel kept the built-in
sound loaded while showing the file's name (docs/ui-parity-checklist.md,
"Known issues").
"""
import os
import shutil
import tempfile
import time

import pygame
from PySide6.QtWidgets import QComboBox, QFileDialog, QGridLayout, QHBoxLayout, QWidget

from core.audio_io import load_segment
from core.constants import COLORS
from effects.audio_effects import AudioEffects
from ui_qt import theme
from ui_qt.widgets import ToggleSwitch, ValueSlider

AMBIANCES = ["None", "Airplane Cabin", "Nature/Forest", "Custom File..."]
AMBIANCE_CHANNEL = 1


def _switch(text, checked=False):
    s = ToggleSwitch(text, checked, on_color=COLORS["success"])
    s.set_text_color(COLORS["text_secondary"])
    return s


def _short(path):
    name = os.path.basename(path)
    return name[:17] + "..." if len(name) > 20 else name


class CustomizationPanel:
    def __init__(self, app=None):
        self.frame, lay = theme.card("⚙️  Customization Options")
        self._temp_dirs = []
        self._temp_files = []
        self.custom_amb_path = None
        self._amb_sound = None
        self._amb_sound_source = None       # what _amb_sound was made from (a file path or a built-in name)
        self._ambiance_channel = None
        self.clone_group = None

        # ── Speech ─────────────────────────────────────────────────
        box, bl = theme.inset()
        self._skip = _switch("Skip Silences")
        bl.addWidget(self._skip)
        lay.addWidget(box)

        # ── Subtitles ──────────────────────────────────────────────
        box, bl = theme.inset()
        row = QHBoxLayout()
        self._auto_sub = _switch("Auto-Subtitles")
        row.addWidget(self._auto_sub)
        row.addSpacing(20)
        row.addWidget(theme.label("Mode:", secondary=True))
        self._sub_mode = QComboBox()
        self._sub_mode.addItems(["Word-Level", "Sentence-Level"])
        self._sub_mode.setProperty("onCard", True)
        self._sub_mode.setFixedWidth(140)
        row.addWidget(self._sub_mode)
        row.addStretch(1)
        bl.addLayout(row)
        lay.addWidget(box)

        # ── 3-Band EQ ──────────────────────────────────────────────
        box, bl = theme.inset()
        self._eq = _switch("3-Band EQ")
        self._eq.toggled.connect(self._on_eq_change)
        bl.addWidget(self._eq)
        self._eq_sliders = QWidget()
        g = QGridLayout(self._eq_sliders)
        g.setContentsMargins(10, 4, 0, 0)
        g.setHorizontalSpacing(8)
        self._eq_bands = {}
        for r, name in enumerate(("Bass", "Mids", "Treble")):
            lab = theme.label(f"{name}:", secondary=True, small=True)
            lab.setFixedWidth(50)
            slider = ValueSlider(-12, 12, 0.0, step=0.1, width=150)
            val = theme.label("0dB", muted=True, small=True)
            val.setFixedWidth(40)
            slider.valueChangedF.connect(lambda v, l=val: l.setText(f"{int(v):+d}dB"))
            g.addWidget(lab, r, 0)
            g.addWidget(slider, r, 1)
            g.addWidget(val, r, 2)
            self._eq_bands[name.lower()] = slider
        g.setColumnStretch(3, 1)
        bl.addWidget(self._eq_sliders)
        lay.addWidget(box)

        # ── Ambiance ───────────────────────────────────────────────
        box, bl = theme.inset()
        row = QHBoxLayout()
        row.addWidget(theme.label("Ambiance:", secondary=True))
        self._env = QComboBox()
        self._env.addItems(AMBIANCES)
        self._env.setProperty("onCard", True)
        self._env.setFixedWidth(150)
        row.addWidget(self._env)
        row.addSpacing(14)
        row.addWidget(theme.label("Vol:", secondary=True, small=True))
        self._vol = ValueSlider(0, 100, 50.0, step=1, width=80)
        self._vol_lbl = theme.label("50%", muted=True, small=True)
        self._vol_lbl.setFixedWidth(34)
        self._vol.valueChangedF.connect(self._on_volume_change)
        row.addWidget(self._vol)
        row.addWidget(self._vol_lbl)
        row.addStretch(1)
        bl.addLayout(row)

        self._amb_row = QWidget()
        ar = QHBoxLayout(self._amb_row)
        ar.setContentsMargins(0, 2, 0, 0)
        ar.setSpacing(6)

        def btn(text, cmd, color):
            b = theme.button(text, theme.ROLE_GHOST, small=True)
            b.setStyleSheet(f"QPushButton:hover {{ background: {color}; border-color: {color}; }}"
                            f"QPushButton {{ border-color: {color}; }}")
            b.clicked.connect(cmd)
            ar.addWidget(b)
            return b
        self._import_btn = btn("📥 Import", self._import_amb, COLORS["accent_primary"])
        self._play_btn = btn("▶ Play", self._play_amb, COLORS["success"])
        self._pause_btn = btn("⏸ Pause", self._pause_amb, COLORS["warning"])
        self._restart_btn = btn("⏮ Restart", self._restart_amb, COLORS["accent_secondary"])
        self._remove_btn = btn("✖ Remove", self._remove_amb, COLORS["error"])
        self._amb_label = theme.label("No file selected", small=True)
        ar.addWidget(self._amb_label)
        ar.addStretch(1)
        bl.addWidget(self._amb_row)
        lay.addWidget(box)
        self._env.currentTextChanged.connect(self._on_env_change)

        # ── Clone Voice: needs the app (engine panel, effects panel, status bar) ──
        if app is not None:
            from ui_qt.panels.clone_voice_group import CloneVoiceGroup
            self.clone_group = CloneVoiceGroup(app)
            lay.addWidget(self.clone_group.box)

        self._on_env_change("None")
        self._on_eq_change(False)

    # ── helpers ────────────────────────────────────────────────────
    @property
    def ambiance_channel(self):
        """The pygame channel the ambiance preview plays on (None until used)."""
        return self._ambiance_channel

    def _set_label(self, text, color):
        self._amb_label.setText(text)
        theme.set_color(self._amb_label, color)

    def _player_buttons(self, enabled):
        for b in (self._play_btn, self._pause_btn, self._restart_btn, self._remove_btn):
            b.setEnabled(enabled)

    def _on_eq_change(self, on):
        self._eq_sliders.setVisible(bool(on))

    def _on_volume_change(self, v):
        self._vol_lbl.setText(f"{int(v)}%")
        if self._ambiance_channel:
            try:
                self._ambiance_channel.set_volume(float(v) / 100.0)
            except Exception as e:
                print(f"Error updating ambiance volume: {e}")

    # ── ambiance choice ────────────────────────────────────────────
    def _on_env_change(self, sel):
        if sel == "None":
            if self._ambiance_channel:
                self._ambiance_channel.stop()
            self._amb_row.hide()
            self.custom_amb_path = None
            self._amb_sound = self._amb_sound_source = None
            self._set_label("No file selected", COLORS["text_muted"])
            self._player_buttons(False)
            self.cleanup()
        elif sel == "Custom File...":
            self._amb_row.show()
            self._import_btn.show()
            if self.custom_amb_path:
                self._set_label(_short(self.custom_amb_path), COLORS["success"])
                if self._amb_sound_source != self.custom_amb_path:
                    self._load_custom_amb_sound(self.custom_amb_path)
                self._player_buttons(True)
            else:
                self._amb_sound = self._amb_sound_source = None
                self._set_label("No file selected", COLORS["text_muted"])
                self._player_buttons(False)
        else:                               # "Airplane Cabin" or "Nature/Forest"
            self._amb_row.show()
            self._import_btn.hide()
            self._load_builtin_ambiance(sel)

    def _ensure_channel(self):
        pygame.mixer.set_num_channels(8)
        if not self._ambiance_channel:
            self._ambiance_channel = pygame.mixer.Channel(AMBIANCE_CHANNEL)

    def _load_custom_amb_sound(self, fp):
        if not pygame.mixer.get_init():     # no audio device: preview unavailable
            self._set_label("No audio device", COLORS["warning"])
            return
        self._ensure_channel()
        try:
            self.cleanup()
            if not fp.lower().endswith(".wav"):
                tmp = tempfile.mkdtemp(prefix="amb_")
                self._temp_dirs.append(tmp)
                wav = os.path.join(tmp, "temp_amb.wav")
                load_segment(fp).export(wav, format="wav")
                self._amb_sound = pygame.mixer.Sound(wav)
            else:
                self._amb_sound = pygame.mixer.Sound(fp)
            self._amb_sound_source = fp
        except Exception as e:
            print(f"Could not load custom ambiance: {e}")
            self._set_label("Load failed", COLORS["error"])

    def _load_builtin_ambiance(self, env_name):
        import numpy as np
        if self._ambiance_channel:
            self._ambiance_channel.stop()
        self._set_label("Generating...", COLORS["warning"])
        self._amb_sound = self._amb_sound_source = None
        if not pygame.mixer.get_init():
            self._set_label("No audio device", COLORS["warning"])
            return
        self._ensure_channel()
        self._amb_label.repaint()           # show "Generating..." before the work below
        try:
            self.cleanup()
            sr, duration = 24000, 30
            silent = np.zeros(sr * duration, dtype=np.float32)
            samples = AudioEffects.add_environment_sound(silent, sr, env_name, volume=1.0)
            tmp_wav = os.path.join(tempfile.gettempdir(),
                                   f"preview_{env_name.replace('/', '_').replace(' ', '_')}_{int(time.time())}.wav")
            AudioEffects.save_float_as_wav(samples, sr, tmp_wav)
            self._temp_files.append(tmp_wav)
            self._amb_sound = pygame.mixer.Sound(tmp_wav)
            self._amb_sound_source = env_name
            self._set_label(env_name, COLORS["success"])
            self._player_buttons(True)
        except Exception as e:
            print(f"Error loading built-in ambiance: {e}")
            self._set_label("Generation failed", COLORS["error"])

    def _import_amb(self):
        fp, _ = QFileDialog.getOpenFileName(self.frame.window(), "Select Ambiance Music", "",
                                            "Audio Files (*.mp3 *.wav *.ogg *.flac);;All Files (*.*)")
        if not fp:
            return
        fp = os.path.normpath(fp)
        self.custom_amb_path = fp
        self._set_label(_short(fp), COLORS["success"])
        self._player_buttons(True)
        self._load_custom_amb_sound(fp)

    # ── player ─────────────────────────────────────────────────────
    def _play_amb(self):
        if self._amb_sound and self._ambiance_channel:
            self._ambiance_channel.set_volume(self._vol.value_f() / 100.0)
            if not self._ambiance_channel.get_busy():
                self._ambiance_channel.play(self._amb_sound, loops=-1)
            else:
                self._ambiance_channel.unpause()

    def _pause_amb(self):
        if self._ambiance_channel:
            self._ambiance_channel.pause()

    def _restart_amb(self):
        if self._amb_sound and self._ambiance_channel:
            self._ambiance_channel.stop()
            self._ambiance_channel.set_volume(self._vol.value_f() / 100.0)
            self._ambiance_channel.play(self._amb_sound, loops=-1)

    def _remove_amb(self):
        self._env.setCurrentText("None")    # runs _on_env_change("None")

    # ── cleanup and settings ───────────────────────────────────────
    def cleanup(self):
        """Stop the preview and delete its temp files (also on each new load and on "None")."""
        if self._ambiance_channel:
            try:
                self._ambiance_channel.stop()
            except Exception:
                pass
        self._amb_sound = None
        self._amb_sound_source = None
        for fp in self._temp_files:
            try:
                if os.path.exists(fp):
                    os.remove(fp)
            except Exception as e:
                print(f"Error removing temp file {fp}: {e}")
        self._temp_files.clear()
        for d in self._temp_dirs:
            shutil.rmtree(d, ignore_errors=True)
        self._temp_dirs.clear()

    def get_settings(self) -> dict:
        return {
            "skip_silences":    self._skip.isChecked(),
            "speed":            1.0,
            "env_choice":       self._env.currentText(),
            "env_volume":       self._vol.value_f() / 100.0,
            "custom_amb_path":  self.custom_amb_path,
            "ambiance_channel": self._ambiance_channel,
            "auto_subtitle":    self._auto_sub.isChecked(),
            "eq_enabled":       self._eq.isChecked(),
            "eq_bass":          self._eq_bands["bass"].value_f(),
            "eq_mids":          self._eq_bands["mids"].value_f(),
            "eq_treble":        self._eq_bands["treble"].value_f(),
            "sub_segmentation": "sentence" if self._sub_mode.currentText() == "Sentence-Level" else "word",
            "clone_voice":      self.clone_group.get_settings() if self.clone_group else {},
        }
