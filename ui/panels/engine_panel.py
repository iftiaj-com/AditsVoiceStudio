"""
ui/panels/engine_panel.py
──────────────────────────
TTS engine selector card with per-engine option sub-frames.
"""
import customtkinter as ctk
import torch

from core.constants import (COLORS, FONTS, GTTS_LANGUAGES, EDGE_VOICES, KOKORO_LANGS, KOKORO_VOICES, 
                            PIPER_VOICES, MELO_VOICES)
from ui.theme import card, styled_option_menu, styled_slider


class EnginePanel:
    """
    Builds the engine selector card and all per-engine option sub-frames.
    Exposes get_settings() so the generation pipeline can read current values.
    """

    def __init__(self, parent, available_engines: list, system_voices: list, row=None):
        if row is not None:
            self._frame = card(parent, row, "🔊  TTS Engine")
        else:
            self._frame = ctk.CTkFrame(
                parent, fg_color=COLORS["bg_card"],
                corner_radius=14, border_width=1, border_color=COLORS["border"]
            )
            self._frame.pack(fill="x", padx=16, pady=6)
            
            ctk.CTkLabel(
                self._frame, text="🔊  TTS Engine",
                font=FONTS["subtitle"],
                text_color=COLORS["text_primary"],
            ).pack(anchor="w", padx=16, pady=(14, 10))
        self._system_voices = system_voices

        # ── Engine selector, GPU toggle on the right edge of the same row ──
        # The dropdown stretches between the label and the toggle, so the row
        # also fits the narrowest window (500 px column).
        engine_row = ctk.CTkFrame(self._frame, fg_color="transparent")
        engine_row.pack(fill="x", padx=16, pady=(0, 10))
        engine_row.columnconfigure(1, weight=1)

        engine_names = [e.name for e in available_engines] or ["No TTS engines available"]

        ctk.CTkLabel(engine_row, text="Engine", font=FONTS["body"],
                     text_color=COLORS["text_secondary"]).grid(row=0, column=0, sticky="w", padx=(0, 10))

        # Default to Kokoro if present
        default_val = engine_names[0]
        for n in engine_names:
            if "Kokoro" in n:
                default_val = n
                break

        self.engine_var = ctk.StringVar(value=default_val)
        self._engine_menu = ctk.CTkOptionMenu(
            engine_row, variable=self.engine_var, values=engine_names,
            font=FONTS["body"], fg_color=COLORS["bg_input"],
            button_color=COLORS["accent_primary"],
            button_hover_color=COLORS["accent_secondary"],
            dropdown_fg_color=COLORS["bg_card"],
            dropdown_hover_color=COLORS["accent_primary"],
            corner_radius=8, width=160,
            command=self._on_engine_change,
        )
        self._engine_menu.grid(row=0, column=1, sticky="ew")

        self.gpu_var = ctk.BooleanVar(value=torch.cuda.is_available())
        ctk.CTkSwitch(
            engine_row, text="GPU Acceleration (PyTorch)", variable=self.gpu_var,
            font=FONTS["body_small"], text_color=COLORS["text_secondary"],
            onvalue=True, offvalue=False,
            button_color=COLORS["accent_primary"],
            button_hover_color=COLORS["accent_secondary"],
            progress_color=COLORS["success"],
            fg_color=COLORS["switch_off"],
        ).grid(row=0, column=2, sticky="e", padx=(16, 0))

        # ── Per-engine sub-frames ──────────────────────────────────
        self._sub_frames: dict[str, ctk.CTkFrame] = {}
        self._build_gtts_frame()
        self._build_edge_frame()
        self._build_kokoro_frame()
        self._build_piper_frame()
        self._build_melo_frame()
        self._build_pyttsx3_frame(system_voices)

        # Show the default engine frame after a short delay
        self._frame.after(50, lambda: self._on_engine_change(self.engine_var.get()))

    # ── Sub-frame builders ─────────────────────────────────────────

    def _sub_inner(self, key: str) -> ctk.CTkFrame:
        outer = ctk.CTkFrame(self._frame, fg_color="transparent")
        self._sub_frames[key] = outer
        inner = ctk.CTkFrame(outer, fg_color=COLORS["bg_input"],
                             corner_radius=10, border_width=1,
                             border_color=COLORS["border"])
        inner.pack(fill="x")
        inner.columnconfigure(1, weight=1)
        return inner

    def _build_gtts_frame(self):
        inner = self._sub_inner("gtts")
        ctk.CTkLabel(inner, text="Language / Accent", font=FONTS["body"],
                     text_color=COLORS["text_secondary"]).grid(
            row=0, column=0, padx=14, pady=12, sticky="w")
        self.gtts_lang_var = ctk.StringVar(value="English (US)")
        styled_option_menu(inner, self.gtts_lang_var,
                           list(GTTS_LANGUAGES.keys()), width=250).grid(
            row=0, column=1, padx=14, pady=12, sticky="e")

    def _build_edge_frame(self):
        inner = self._sub_inner("edge")
        ctk.CTkLabel(inner, text="Neural Voice", font=FONTS["body"],
                     text_color=COLORS["text_secondary"]).grid(
            row=0, column=0, padx=14, pady=(12, 6), sticky="w")
        self.edge_voice_var = ctk.StringVar(value=list(EDGE_VOICES.keys())[0])
        styled_option_menu(inner, self.edge_voice_var,
                           list(EDGE_VOICES.keys()), width=280).grid(
            row=0, column=1, padx=14, pady=(12, 6), sticky="e")

        ctk.CTkLabel(inner, text="Speed (+/- %)", font=FONTS["body"],
                     text_color=COLORS["text_secondary"]).grid(
            row=1, column=0, padx=14, pady=6, sticky="w")
        rate_row = ctk.CTkFrame(inner, fg_color="transparent")
        rate_row.grid(row=1, column=1, padx=14, pady=6, sticky="e")
        self.edge_rate_var = ctk.IntVar(value=0)
        self._edge_rate_lbl = ctk.CTkLabel(rate_row, text="+0%",
                                           font=FONTS["body_small"],
                                           text_color=COLORS["text_muted"], width=45)
        styled_slider(rate_row, self.edge_rate_var, -50, 50, width=200,
                      command=lambda v: self._edge_rate_lbl.configure(
                          text=f"{int(v):+d}%")).pack(side="left", padx=(0, 8))
        self._edge_rate_lbl.pack(side="left")

    def _build_kokoro_frame(self):
        inner = self._sub_inner("kokoro")
        inner.columnconfigure(1, weight=1)

        # Language selection
        ctk.CTkLabel(inner, text="Language", font=FONTS["body"],
                     text_color=COLORS["text_secondary"]).grid(
            row=0, column=0, padx=14, pady=(12, 6), sticky="w")
        
        self.kokoro_lang_var = ctk.StringVar(value="American English")
        self._kokoro_lang_menu = styled_option_menu(
            inner, self.kokoro_lang_var, list(KOKORO_LANGS.keys()), width=280,
            command=self._update_kokoro_voices
        )
        self._kokoro_lang_menu.grid(row=0, column=1, padx=14, pady=(12, 6), sticky="e")

        # Voice selection
        ctk.CTkLabel(inner, text="Voice", font=FONTS["body"],
                     text_color=COLORS["text_secondary"]).grid(
            row=1, column=0, padx=14, pady=6, sticky="w")
        
        self.kokoro_voice_var = ctk.StringVar()
        self._kokoro_voice_menu = styled_option_menu(inner, self.kokoro_voice_var, [], width=280)
        self._kokoro_voice_menu.grid(row=1, column=1, padx=14, pady=6, sticky="e")
        
        # Initial voice list update
        self._update_kokoro_voices("American English")

        # Speed slider
        ctk.CTkLabel(inner, text="Speed (0.5x - 2x)", font=FONTS["body"],
                     text_color=COLORS["text_secondary"]).grid(
            row=2, column=0, padx=14, pady=(6, 12), sticky="w")
        
        speed_row = ctk.CTkFrame(inner, fg_color="transparent")
        speed_row.grid(row=2, column=1, padx=14, pady=(6, 12), sticky="e")
        self.kokoro_speed_var = ctk.DoubleVar(value=1.0)
        self._kokoro_speed_lbl = ctk.CTkLabel(speed_row, text="1.0x",
                                            font=FONTS["body_small"],
                                            text_color=COLORS["text_muted"], width=45)
        styled_slider(speed_row, self.kokoro_speed_var, 0.5, 2.0, width=150,
                      command=lambda v: self._kokoro_speed_lbl.configure(
                          text=f"{float(v):.1f}x")).pack(side="left", padx=(0, 4))
        self._kokoro_speed_lbl.pack(side="left", padx=(0, 4))

        def _reset_kokoro_speed():
            self.kokoro_speed_var.set(1.0)
            self._kokoro_speed_lbl.configure(text="1.0x")

        ctk.CTkButton(
            speed_row, text="↺ 1x", font=FONTS["body_small"],
            fg_color=COLORS["bg_input"],
            hover_color=COLORS["accent_primary"],
            border_color=COLORS["border"], border_width=1,
            corner_radius=6, height=24, width=46,
            command=_reset_kokoro_speed,
        ).pack(side="left")



    def _update_kokoro_voices(self, lang_name):
        lang_code = KOKORO_LANGS[lang_name]
        voices = list(KOKORO_VOICES[lang_code].keys())
        self._kokoro_voice_menu.configure(values=voices)
        self.kokoro_voice_var.set(voices[0])

    def _build_piper_frame(self):
        inner = self._sub_inner("piper")
        ctk.CTkLabel(inner, text="Voice", font=FONTS["body"],
                     text_color=COLORS["text_secondary"]).grid(
            row=0, column=0, padx=14, pady=12, sticky="w")
        self.piper_voice_var = ctk.StringVar(value=list(PIPER_VOICES.keys())[0])
        styled_option_menu(inner, self.piper_voice_var,
                           list(PIPER_VOICES.keys()), width=280).grid(
            row=0, column=1, padx=14, pady=12, sticky="e")

    def _build_melo_frame(self):
        inner = self._sub_inner("melo")
        ctk.CTkLabel(inner, text="Language / Accent", font=FONTS["body"],
                     text_color=COLORS["text_secondary"]).grid(
            row=0, column=0, padx=14, pady=(12, 6), sticky="w")
        self.melo_voice_var = ctk.StringVar(value=list(MELO_VOICES.keys())[0])
        styled_option_menu(inner, self.melo_voice_var,
                           list(MELO_VOICES.keys()), width=280).grid(
            row=0, column=1, padx=14, pady=(12, 6), sticky="e")

        ctk.CTkLabel(inner, text="Speed (0.5x - 2x)", font=FONTS["body"],
                     text_color=COLORS["text_secondary"]).grid(
            row=1, column=0, padx=14, pady=6, sticky="w")
        
        speed_row = ctk.CTkFrame(inner, fg_color="transparent")
        speed_row.grid(row=1, column=1, padx=14, pady=(6, 12), sticky="e")
        self.melo_speed_var = ctk.DoubleVar(value=1.0)
        self._melo_speed_lbl = ctk.CTkLabel(speed_row, text="1.0x",
                                            font=FONTS["body_small"],
                                            text_color=COLORS["text_muted"], width=45)
        styled_slider(speed_row, self.melo_speed_var, 0.5, 2.0, width=150,
                      command=lambda v: self._melo_speed_lbl.configure(
                          text=f"{float(v):.1f}x")).pack(side="left", padx=(0, 4))
        self._melo_speed_lbl.pack(side="left", padx=(0, 4))

        def _reset_melo_speed():
            self.melo_speed_var.set(1.0)
            self._melo_speed_lbl.configure(text="1.0x")

        ctk.CTkButton(
            speed_row, text="↺ 1x", font=FONTS["body_small"],
            fg_color=COLORS["bg_input"],
            hover_color=COLORS["accent_primary"],
            border_color=COLORS["border"], border_width=1,
            corner_radius=6, height=24, width=46,
            command=_reset_melo_speed,
        ).pack(side="left")

    def _build_pyttsx3_frame(self, system_voices):
        inner = self._sub_inner("pyttsx3")
        inner.columnconfigure(1, weight=1)
        voice_names = [v[0] for v in system_voices] if system_voices else ["Default"]

        ctk.CTkLabel(inner, text="Voice", font=FONTS["body"],
                     text_color=COLORS["text_secondary"]).grid(
            row=0, column=0, padx=14, pady=(12, 6), sticky="w")
        self.pyttsx3_voice_var = ctk.StringVar(value=voice_names[0])
        styled_option_menu(inner, self.pyttsx3_voice_var,
                           voice_names, width=280).grid(
            row=0, column=1, padx=14, pady=(12, 6), sticky="e")

        ctk.CTkLabel(inner, text="Speed (WPM)", font=FONTS["body"],
                     text_color=COLORS["text_secondary"]).grid(
            row=1, column=0, padx=14, pady=6, sticky="w")
        rate_row = ctk.CTkFrame(inner, fg_color="transparent")
        rate_row.grid(row=1, column=1, padx=14, pady=6, sticky="e")
        self.rate_var = ctk.IntVar(value=175)
        self._rate_lbl = ctk.CTkLabel(rate_row, text="175 WPM",
                                      font=FONTS["body_small"],
                                      text_color=COLORS["text_muted"], width=65)
        styled_slider(rate_row, self.rate_var, 50, 350, width=200,
                      command=lambda v: self._rate_lbl.configure(
                          text=f"{int(v)} WPM")).pack(side="left", padx=(0, 8))
        self._rate_lbl.pack(side="left")

        ctk.CTkLabel(inner, text="Volume", font=FONTS["body"],
                     text_color=COLORS["text_secondary"]).grid(
            row=2, column=0, padx=14, pady=(6, 12), sticky="w")
        vol_row = ctk.CTkFrame(inner, fg_color="transparent")
        vol_row.grid(row=2, column=1, padx=14, pady=(6, 12), sticky="e")
        self.volume_var = ctk.DoubleVar(value=0.9)
        self._vol_lbl = ctk.CTkLabel(vol_row, text="90%",
                                     font=FONTS["body_small"],
                                     text_color=COLORS["text_muted"], width=45)
        styled_slider(vol_row, self.volume_var, 0.0, 1.0, width=200,
                      command=lambda v: self._vol_lbl.configure(
                          text=f"{int(float(v)*100)}%")).pack(side="left", padx=(0, 8))
        self._vol_lbl.pack(side="left")

    # ── Engine change handler ──────────────────────────────────────

    def _on_engine_change(self, selection: str):
        """Hide all sub-frames then show the relevant one."""
        for f in self._sub_frames.values():
            f.pack_forget()

        # One entry per registered engine, keyed by a word from its display name.
        key_map = {
            "gTTS":    "gtts",
            "Edge":    "edge",
            "Kokoro":  "kokoro",
            "Piper":   "piper",
            "Melo":    "melo",
            "pyttsx3": "pyttsx3",
        }
        for keyword, key in key_map.items():
            if keyword in selection and key in self._sub_frames:
                self._sub_frames[key].pack(fill="x", padx=16, pady=(0, 12))
                break

    # ── Voice selection (used by cloned voices) ────────────────────
    # A cloned voice sounds best when converted from the engine voice it was
    # made with, so each voice stores the selection below and switching the
    # voice on restores it. Values are the dropdown display strings.

    _KIND_BY_KEYWORD = {"gTTS": "gtts", "Edge": "edge", "Kokoro": "kokoro",
                        "Piper": "piper", "Melo": "melo", "pyttsx3": "pyttsx3"}

    @classmethod
    def engine_kind(cls, engine_name):
        for keyword, kind in cls._KIND_BY_KEYWORD.items():
            if keyword in (engine_name or ""):
                return kind
        return None

    @classmethod
    def describe_selection(cls, sel) -> str:
        """'Kokoro-82M · am_onyx · 1.2x' for labels; '' when sel is empty."""
        if not sel or not sel.get("engine"):
            return ""
        parts = [sel["engine"].split(" (")[0]]
        if sel.get("voice"):
            parts.append(str(sel["voice"]))
        speed = sel.get("speed")
        if speed is not None:
            kind = sel.get("kind") or cls.engine_kind(sel["engine"])
            parts.append(f"{int(speed):+d}%" if kind == "edge" else
                         f"{int(speed)} WPM" if kind == "pyttsx3" else f"{float(speed):.1f}x")
        return " · ".join(parts)

    def get_voice_selection(self) -> dict:
        """The engine and voice currently picked, as dropdown values."""
        name = self.engine_var.get()
        kind = self.engine_kind(name)
        sel = {"engine": name, "kind": kind}
        if kind == "kokoro":
            sel.update(lang=self.kokoro_lang_var.get(), voice=self.kokoro_voice_var.get())
        elif kind == "edge":
            sel["voice"] = self.edge_voice_var.get()
        elif kind == "piper":
            sel["voice"] = self.piper_voice_var.get()
        elif kind == "melo":
            sel["voice"] = self.melo_voice_var.get()
        elif kind == "pyttsx3":
            sel["voice"] = self.pyttsx3_voice_var.get()
        elif kind == "gtts":
            sel["voice"] = self.gtts_lang_var.get()
        speed = self._speed_var(kind)
        if speed is not None:
            sel["speed"] = round(float(speed.get()), 2)
        return sel

    def _speed_var(self, kind):
        """The speed control of an engine kind (None for Piper and gTTS)."""
        return {"kokoro": self.kokoro_speed_var, "melo": self.melo_speed_var,
                "edge": self.edge_rate_var, "pyttsx3": self.rate_var}.get(kind)

    def _apply_speed(self, kind, value):
        """Set an engine's speed control and its value label. Out-of-range values are clamped."""
        var = self._speed_var(kind)
        if var is None or value is None:
            return
        lo, hi, label, fmt = {
            "kokoro":  (0.5, 2.0, self._kokoro_speed_lbl, lambda v: f"{v:.1f}x"),
            "melo":    (0.5, 2.0, self._melo_speed_lbl, lambda v: f"{v:.1f}x"),
            "edge":    (-50, 50, self._edge_rate_lbl, lambda v: f"{int(v):+d}%"),
            "pyttsx3": (50, 350, self._rate_lbl, lambda v: f"{int(v)} WPM"),
        }[kind]
        v = max(lo, min(hi, float(value)))
        var.set(int(round(v)) if isinstance(var, ctk.IntVar) else v)
        label.configure(text=fmt(v))

    def _voice_options(self, kind) -> dict:
        """{dropdown value: underlying value} for an engine kind."""
        if kind == "edge":
            return dict(EDGE_VOICES)
        if kind == "piper":
            return dict(PIPER_VOICES)
        if kind == "melo":
            return {k: v.get("speaker") for k, v in MELO_VOICES.items()}
        if kind == "pyttsx3":
            return {n: vid for n, vid in self._system_voices} or {"Default": ""}
        if kind == "gtts":
            return dict(GTTS_LANGUAGES)
        return {}

    def apply_voice_selection(self, sel):
        """Select sel's engine and voice. Returns None on success, else why not.

        Nothing changes unless the whole selection can be applied. A voice
        may be given as the dropdown value or the underlying value (older
        voices stored e.g. a Piper model key or a Melo speaker id).
        """
        if not sel or not sel.get("engine"):
            return "no base voice is recorded"
        engines = list(self._engine_menu.cget("values"))
        kind = sel.get("kind") or self.engine_kind(sel["engine"])
        name = sel["engine"] if sel["engine"] in engines else next(
            (e for e in engines if kind and self.engine_kind(e) == kind), None)
        if name is None:
            return f"{sel['engine'].split(' (')[0]} is not available"
        voice = sel.get("voice")

        if kind == "kokoro":
            lang = sel.get("lang")
            if lang not in KOKORO_LANGS or voice not in KOKORO_VOICES[KOKORO_LANGS[lang]]:
                lang = next((ln for ln, code in KOKORO_LANGS.items()
                             if voice in KOKORO_VOICES.get(code, {})), None)
            if lang is None:
                return f"Kokoro voice '{voice}' is not available"
            self.engine_var.set(name)
            self._on_engine_change(name)
            self.kokoro_lang_var.set(lang)
            self._update_kokoro_voices(lang)
            self.kokoro_voice_var.set(voice)
            self._apply_speed(kind, sel.get("speed"))
            return None

        target = None
        if voice is not None:
            options = self._voice_options(kind)
            target = voice if voice in options else next(
                (k for k, v in options.items() if v == voice), None)
            if target is None:
                return f"voice '{voice}' is not available in {name.split(' (')[0]}"
        self.engine_var.set(name)
        self._on_engine_change(name)
        var = {"edge": self.edge_voice_var, "piper": self.piper_voice_var, "melo": self.melo_voice_var,
               "pyttsx3": self.pyttsx3_voice_var, "gtts": self.gtts_lang_var}.get(kind)
        if var is not None and target is not None:
            var.set(target)
        self._apply_speed(kind, sel.get("speed"))   # older voices have none: speed is left alone
        return None

    # ── Settings accessor ──────────────────────────────────────────

    def get_settings(self) -> dict:
        """Return a dict of all current engine settings for the generator."""
        voice_id = ""
        for name, vid in self._system_voices:
            if name == self.pyttsx3_voice_var.get():
                voice_id = vid
                break
        return {
            "engine_name":   self.engine_var.get(),
            "use_gpu":       self.gpu_var.get(),
            "gtts_lang_key": self.gtts_lang_var.get(),
            "edge_voice_key": self.edge_voice_var.get(),
            "edge_rate":     self.edge_rate_var.get(),
            "kokoro_lang_code":  KOKORO_LANGS[self.kokoro_lang_var.get()],
            "kokoro_voice_key": self.kokoro_voice_var.get(),
            "kokoro_speed":     self.kokoro_speed_var.get(),
            "piper_voice_key": PIPER_VOICES[self.piper_voice_var.get()],
            "melo_voice_cfg": MELO_VOICES[self.melo_voice_var.get()],
            "melo_speed":      self.melo_speed_var.get(),
            "pyttsx3_voice_id": voice_id,
            "pyttsx3_rate":  self.rate_var.get(),
            "pyttsx3_volume": self.volume_var.get(),
        }
