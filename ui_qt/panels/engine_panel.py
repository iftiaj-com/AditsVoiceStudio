"""
ui_qt/panels/engine_panel.py
────────────────────────────
TTS engine card (Qt): engine dropdown, GPU switch, and one options box per
engine, shown for the selected engine only.

Same public methods as ui/panels/engine_panel.py (get_settings,
get_voice_selection, apply_voice_selection, settings_for_selection,
engine_names, voice_choices, plus the static helpers), so the generation
code and the Clone Voice code can use either. The selection rules live in
core.voice_selection.
"""
import torch
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QComboBox, QFrame, QGridLayout, QHBoxLayout, QSizePolicy, QWidget

from core import voice_selection
from core.constants import (COLORS, EDGE_VOICES, GTTS_LANGUAGES, KOKORO_LANGS, KOKORO_VOICES,
                            MELO_VOICES, PIPER_VOICES)
from ui_qt import theme
from ui_qt.widgets import ToggleSwitch, ValueSlider

MENU_W = 280            # CTk option menus are 280 px (gTTS 250)


def _combo(values, current=None, width=MENU_W):
    cb = QComboBox()
    cb.addItems(list(values))
    if current is not None:
        cb.setCurrentText(current)
    cb.setFixedWidth(width)
    cb.setMaxVisibleItems(15)
    return cb


class EnginePanel:
    SPEED_RANGES = voice_selection.SPEED_RANGES
    engine_kind = staticmethod(voice_selection.engine_kind)
    describe_selection = staticmethod(voice_selection.describe_selection)
    format_speed = staticmethod(voice_selection.format_speed)
    synthesis_key = staticmethod(voice_selection.synthesis_key)

    def __init__(self, available_engines: list, system_voices: list):
        self.frame, lay = theme.card("🔊  TTS Engine")
        self._system_voices = system_voices

        # Engine row: label, dropdown stretching, GPU switch on the right.
        row = QHBoxLayout()
        row.setSpacing(10)
        row.addWidget(theme.label("Engine", secondary=True))
        self._engine_names = [e.name for e in available_engines] or ["No TTS engines available"]
        from core.defaults import default_engine_name
        self._engine_menu = QComboBox()
        self._engine_menu.addItems(self._engine_names)
        self._engine_menu.setCurrentText(default_engine_name(self._engine_names))
        self._engine_menu.setMinimumWidth(160)
        self._engine_menu.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        row.addWidget(self._engine_menu, 1)
        row.addSpacing(6)
        self._gpu = ToggleSwitch("GPU Acceleration (PyTorch)", torch.cuda.is_available(),
                                 on_color=COLORS["success"])
        self._gpu.setFont(theme.font("body_small"))
        self._gpu.set_text_color(COLORS["text_secondary"])
        row.addWidget(self._gpu)
        lay.addLayout(row)

        self._sub = {}                  # kind -> options box
        self._build_gtts(lay)
        self._build_edge(lay)
        self._build_kokoro(lay)
        self._build_piper(lay)
        self._build_melo(lay)
        self._build_pyttsx3(lay)
        self._engine_menu.currentTextChanged.connect(self._on_engine_change)
        self._on_engine_change(self._engine_menu.currentText())

    # ── options boxes ──────────────────────────────────────────────
    def _box(self, lay, kind):
        frame = QFrame()
        frame.setObjectName("inset")
        g = QGridLayout(frame)
        g.setContentsMargins(14, 10, 14, 10)
        g.setHorizontalSpacing(12)
        g.setVerticalSpacing(8)
        g.setColumnStretch(1, 1)
        frame.hide()
        lay.addWidget(frame)
        self._sub[kind] = frame
        return g

    @staticmethod
    def _row(g, r, text, widget):
        g.addWidget(theme.label(text, secondary=True), r, 0)
        g.addWidget(widget, r, 1, Qt.AlignRight)

    @staticmethod
    def _slider_row(slider, label, reset=None):
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(6)
        h.addWidget(slider)
        h.addWidget(label)
        if reset is not None:
            h.addWidget(reset)
        return w

    @staticmethod
    def _value_label(text, width):
        lab = theme.label(text, muted=True, small=True)
        lab.setFixedWidth(width)
        return lab

    def _reset_button(self, slider, label, value, text):
        b = theme.button("↺ 1x", theme.ROLE_GHOST, small=True)
        b.setFixedWidth(46)

        def reset():
            slider.set_value(value)
            label.setText(text)
        b.clicked.connect(reset)
        return b

    def _build_gtts(self, lay):
        g = self._box(lay, "gtts")
        self._gtts_lang = _combo(GTTS_LANGUAGES, "English (US)", width=250)
        self._row(g, 0, "Language / Accent", self._gtts_lang)

    def _build_edge(self, lay):
        g = self._box(lay, "edge")
        self._edge_voice = _combo(EDGE_VOICES)
        self._row(g, 0, "Neural Voice", self._edge_voice)
        self._edge_rate = ValueSlider(-50, 50, 0, step=1, width=200)
        self._edge_rate_lbl = self._value_label("+0%", 45)
        self._edge_rate.valueChangedF.connect(lambda v: self._edge_rate_lbl.setText(f"{int(v):+d}%"))
        self._row(g, 1, "Speed (+/- %)", self._slider_row(self._edge_rate, self._edge_rate_lbl))

    def _build_kokoro(self, lay):
        g = self._box(lay, "kokoro")
        self._kokoro_lang = _combo(KOKORO_LANGS, "American English")
        self._row(g, 0, "Language", self._kokoro_lang)
        self._kokoro_voice = _combo([])
        self._row(g, 1, "Voice", self._kokoro_voice)
        self._update_kokoro_voices("American English")
        self._kokoro_lang.currentTextChanged.connect(self._update_kokoro_voices)
        self._kokoro_speed = ValueSlider(0.5, 2.0, 1.0, step=0.01, width=150)
        self._kokoro_speed_lbl = self._value_label("1.0x", 45)
        self._kokoro_speed.valueChangedF.connect(lambda v: self._kokoro_speed_lbl.setText(f"{v:.1f}x"))
        reset = self._reset_button(self._kokoro_speed, self._kokoro_speed_lbl, 1.0, "1.0x")
        self._row(g, 2, "Speed (0.5x - 2x)", self._slider_row(self._kokoro_speed, self._kokoro_speed_lbl, reset))

    def _update_kokoro_voices(self, lang_name):
        voices = list(KOKORO_VOICES[KOKORO_LANGS[lang_name]].keys())
        self._kokoro_voice.blockSignals(True)
        self._kokoro_voice.clear()
        self._kokoro_voice.addItems(voices)
        self._kokoro_voice.setCurrentIndex(0)
        self._kokoro_voice.blockSignals(False)

    def _build_piper(self, lay):
        g = self._box(lay, "piper")
        self._piper_voice = _combo(PIPER_VOICES)
        self._row(g, 0, "Voice", self._piper_voice)

    def _build_melo(self, lay):
        g = self._box(lay, "melo")
        self._melo_voice = _combo(MELO_VOICES)
        self._row(g, 0, "Language / Accent", self._melo_voice)
        self._melo_speed = ValueSlider(0.5, 2.0, 1.0, step=0.01, width=150)
        self._melo_speed_lbl = self._value_label("1.0x", 45)
        self._melo_speed.valueChangedF.connect(lambda v: self._melo_speed_lbl.setText(f"{v:.1f}x"))
        reset = self._reset_button(self._melo_speed, self._melo_speed_lbl, 1.0, "1.0x")
        self._row(g, 1, "Speed (0.5x - 2x)", self._slider_row(self._melo_speed, self._melo_speed_lbl, reset))

    def _build_pyttsx3(self, lay):
        g = self._box(lay, "pyttsx3")
        names = [v[0] for v in self._system_voices] if self._system_voices else ["Default"]
        self._pyttsx3_voice = _combo(names)
        self._row(g, 0, "Voice", self._pyttsx3_voice)
        self._rate = ValueSlider(50, 350, 175, step=1, width=200)
        self._rate_lbl = self._value_label("175 WPM", 65)
        self._rate.valueChangedF.connect(lambda v: self._rate_lbl.setText(f"{int(v)} WPM"))
        self._row(g, 1, "Speed (WPM)", self._slider_row(self._rate, self._rate_lbl))
        self._volume = ValueSlider(0.0, 1.0, 0.9, step=0.01, width=200)
        self._vol_lbl = self._value_label("90%", 45)
        self._volume.valueChangedF.connect(lambda v: self._vol_lbl.setText(f"{int(v * 100)}%"))
        self._row(g, 2, "Volume", self._slider_row(self._volume, self._vol_lbl))

    def _on_engine_change(self, selection: str):
        """Show only the options box of the selected engine."""
        kind = self.engine_kind(selection)
        for k, frame in self._sub.items():
            frame.setVisible(k == kind)

    # ── voice selection (used by cloned voices) ────────────────────
    def engine_names(self) -> list:
        """The engines offered in the Engine dropdown."""
        return list(self._engine_names)

    def voice_choices(self, kind, lang=None) -> list:
        return voice_selection.voice_choices(kind, self._system_voices, lang)

    def _speed_control(self, kind):
        """(slider, value label) of an engine kind's speed (None for Piper and gTTS)."""
        return {"kokoro": (self._kokoro_speed, self._kokoro_speed_lbl),
                "melo": (self._melo_speed, self._melo_speed_lbl),
                "edge": (self._edge_rate, self._edge_rate_lbl),
                "pyttsx3": (self._rate, self._rate_lbl)}.get(kind)

    def get_voice_selection(self) -> dict:
        """The engine and voice currently picked, as dropdown values."""
        name = self._engine_menu.currentText()
        kind = self.engine_kind(name)
        sel = {"engine": name, "kind": kind}
        if kind == "kokoro":
            sel.update(lang=self._kokoro_lang.currentText(), voice=self._kokoro_voice.currentText())
        elif kind in ("edge", "piper", "melo", "pyttsx3", "gtts"):
            sel["voice"] = self._voice_menu(kind).currentText()
        ctl = self._speed_control(kind)
        if ctl is not None:
            sel["speed"] = round(float(ctl[0].value_f()), 2)
        return sel

    def _voice_menu(self, kind):
        return {"edge": self._edge_voice, "piper": self._piper_voice, "melo": self._melo_voice,
                "pyttsx3": self._pyttsx3_voice, "gtts": self._gtts_lang}.get(kind)

    def _apply_speed(self, kind, value):
        """Set an engine's speed control and its label. Out-of-range values are clamped."""
        ctl = self._speed_control(kind)
        if ctl is None or value is None:
            return
        lo, hi, _default = self.SPEED_RANGES[kind]
        v = max(lo, min(hi, float(value)))
        ctl[0].set_value(v)
        ctl[1].setText(self.format_speed(kind, v))

    def _resolve_selection(self, sel):
        return voice_selection.resolve_selection(sel, self.engine_names(), self._system_voices)

    def apply_voice_selection(self, sel):
        """Select sel's engine and voice. Returns None on success, else why not.

        Nothing changes unless the whole selection can be applied.
        """
        r = self._resolve_selection(sel)
        if isinstance(r, str):
            return r
        name, kind, lang, voice = r
        self._engine_menu.setCurrentText(name)
        self._on_engine_change(name)
        if kind == "kokoro":
            self._kokoro_lang.blockSignals(True)
            self._kokoro_lang.setCurrentText(lang)
            self._kokoro_lang.blockSignals(False)
            self._update_kokoro_voices(lang)
            self._kokoro_voice.setCurrentText(voice)
        else:
            menu = self._voice_menu(kind)
            if menu is not None and voice is not None:
                menu.setCurrentText(voice)
        self._apply_speed(kind, sel.get("speed"))   # older voices have none: speed is left alone
        return None

    def settings_for_selection(self, sel):
        """get_settings() with sel's engine, voice and speed in place of the dropdowns."""
        return voice_selection.settings_for_selection(self.get_settings(), sel, self.engine_names(),
                                                      self._system_voices)

    # ── settings ───────────────────────────────────────────────────
    def get_settings(self) -> dict:
        """All current engine settings for the generator (same keys as the CTk panel)."""
        voice_id = ""
        for name, vid in self._system_voices:
            if name == self._pyttsx3_voice.currentText():
                voice_id = vid
                break
        return {
            "engine_name":      self._engine_menu.currentText(),
            "use_gpu":          self._gpu.isChecked(),
            "gtts_lang_key":    self._gtts_lang.currentText(),
            "edge_voice_key":   self._edge_voice.currentText(),
            "edge_rate":        int(self._edge_rate.value_f()),
            "kokoro_lang_code": KOKORO_LANGS[self._kokoro_lang.currentText()],
            "kokoro_voice_key": self._kokoro_voice.currentText(),
            "kokoro_speed":     self._kokoro_speed.value_f(),
            "piper_voice_key":  PIPER_VOICES[self._piper_voice.currentText()],
            "melo_voice_cfg":   MELO_VOICES[self._melo_voice.currentText()],
            "melo_speed":       self._melo_speed.value_f(),
            "pyttsx3_voice_id": voice_id,
            "pyttsx3_rate":     int(self._rate.value_f()),
            "pyttsx3_volume":   self._volume.value_f(),
        }
