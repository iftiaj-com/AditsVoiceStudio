"""
ui/panels/customization_panel.py  –  Skip silences, speed, ambiance
"""
import tempfile
import customtkinter as ctk
from tkinter import filedialog
import pygame
from core.audio_io import load_segment
from core.constants import COLORS, FONTS
from ui.theme import card, styled_slider
from effects.audio_effects import AudioEffects


class CustomizationPanel:
    def __init__(self, parent, row=None, app=None):
        if row is not None:
            frame = card(parent, row, "⚙️  Customization Options")
            container = ctk.CTkFrame(frame, fg_color="transparent")
            container.pack(fill="x", padx=16, pady=(0, 14))
        else:
            frame = ctk.CTkFrame(
                parent, fg_color=COLORS["bg_card"],
                corner_radius=14, border_width=1, border_color=COLORS["border"]
            )
            frame.pack(fill="x", padx=16, pady=6)
            
            ctk.CTkLabel(
                frame, text="⚙️  Customization Options",
                font=FONTS["subtitle"],
                text_color=COLORS["text_primary"],
            ).pack(anchor="w", padx=16, pady=(14, 10))
            
            container = ctk.CTkFrame(frame, fg_color="transparent")
            container.pack(fill="x", padx=16, pady=(0, 14))

        self._row_is_none = (row is None)
        # Temp file tracking
        self._temp_dirs = []
        self._temp_files = []

        # ── Group 1: Speech Adjustments ─────────────────────────────
        speech_box = ctk.CTkFrame(container, fg_color=COLORS["bg_input"] if row is None else "transparent",
                                  corner_radius=10, border_width=1 if row is None else 0,
                                  border_color=COLORS["border"])
        speech_box.pack(fill="x", pady=4, padx=2)
        inner_speech = ctk.CTkFrame(speech_box, fg_color="transparent")
        inner_speech.pack(fill="x", padx=10, pady=8)

        self.skip_silences_var = ctk.BooleanVar(value=False)
        ctk.CTkSwitch(inner_speech, text="Skip Silences", variable=self.skip_silences_var,
                      font=FONTS["body"], text_color=COLORS["text_secondary"],
                      onvalue=True, offvalue=False,
                      button_color=COLORS["accent_primary"],
                      button_hover_color=COLORS["accent_secondary"],
                      progress_color=COLORS["success"],
                      fg_color=COLORS["switch_off"], width=80
                      ).pack(side="left", padx=(0, 20))

        # (Speed adjustments removed)

        # ── Group 2: Subtitles ─────────────────────────────────────
        sub_box = ctk.CTkFrame(container, fg_color=COLORS["bg_input"] if row is None else "transparent",
                               corner_radius=10, border_width=1 if row is None else 0,
                               border_color=COLORS["border"])
        sub_box.pack(fill="x", pady=4, padx=2)
        inner_sub = ctk.CTkFrame(sub_box, fg_color="transparent")
        inner_sub.pack(fill="x", padx=10, pady=8)

        self.auto_subtitle_var = ctk.BooleanVar(value=False)
        ctk.CTkSwitch(inner_sub, text="Auto-Subtitles", variable=self.auto_subtitle_var,
                      font=FONTS["body"], text_color=COLORS["text_secondary"],
                      onvalue=True, offvalue=False,
                      button_color=COLORS["accent_primary"],
                      button_hover_color=COLORS["accent_secondary"],
                      progress_color=COLORS["success"],
                      fg_color=COLORS["switch_off"]
                      ).pack(side="left", padx=(0, 20))

        ctk.CTkLabel(inner_sub, text="Mode:", font=FONTS["body"],
                     text_color=COLORS["text_secondary"]).pack(side="left", padx=(0, 4))
        self.sub_seg_var = ctk.StringVar(value="Word-Level")
        self._sub_seg_menu = ctk.CTkOptionMenu(
            inner_sub, variable=self.sub_seg_var,
            values=["Word-Level", "Sentence-Level"],
            font=FONTS["body"], fg_color=COLORS["bg_card"] if row is None else COLORS["bg_input"],
            button_color=COLORS["accent_primary"],
            button_hover_color=COLORS["accent_secondary"],
            dropdown_fg_color=COLORS["bg_card"],
            dropdown_hover_color=COLORS["accent_primary"],
            corner_radius=8, width=120)
        self._sub_seg_menu.pack(side="left", padx=(0, 14))

        # ── Group 3: 3-Band EQ ──────────────────────────────────────
        eq_box = ctk.CTkFrame(container, fg_color=COLORS["bg_input"] if row is None else "transparent",
                              corner_radius=10, border_width=1 if row is None else 0,
                              border_color=COLORS["border"])
        eq_box.pack(fill="x", pady=4, padx=2)
        inner_eq = ctk.CTkFrame(eq_box, fg_color="transparent")
        inner_eq.pack(fill="x", padx=10, pady=8)

        self.eq_var = ctk.BooleanVar(value=False)
        self._eq_switch = ctk.CTkSwitch(
            inner_eq, text="3-Band EQ", variable=self.eq_var,
            font=FONTS["body"], text_color=COLORS["text_secondary"],
            onvalue=True, offvalue=False,
            button_color=COLORS["accent_primary"],
            button_hover_color=COLORS["accent_secondary"],
            progress_color=COLORS["success"],
            fg_color=COLORS["switch_off"],
            command=self._on_eq_change
        )
        if row is None:
            self._eq_switch.pack(side="top", anchor="w", padx=0, pady=(0, 6))
        else:
            self._eq_switch.pack(side="left", padx=(0, 14))

        self._eq_sliders_frame = ctk.CTkFrame(inner_eq, fg_color="transparent")
        
        if self._row_is_none:
            # Stack vertically
            # Bass row
            bass_row = ctk.CTkFrame(self._eq_sliders_frame, fg_color="transparent")
            bass_row.pack(fill="x", pady=2)
            ctk.CTkLabel(bass_row, text="Bass:", font=FONTS["body_small"],
                         text_color=COLORS["text_secondary"], width=50, anchor="w").pack(side="left", padx=(10, 4))
            self.eq_bass_var = ctk.DoubleVar(value=0.0)
            self._eq_bass_lbl = ctk.CTkLabel(bass_row, text="0dB", font=FONTS["body_small"],
                                             text_color=COLORS["text_muted"], width=40, anchor="w")
            styled_slider(bass_row, self.eq_bass_var, -12, 12, width=150,
                          command=lambda v: self._eq_bass_lbl.configure(text=f"{int(float(v)):+d}dB")).pack(side="left", padx=2)
            self._eq_bass_lbl.pack(side="left", padx=(10, 0))

            # Mids row
            mids_row = ctk.CTkFrame(self._eq_sliders_frame, fg_color="transparent")
            mids_row.pack(fill="x", pady=2)
            ctk.CTkLabel(mids_row, text="Mids:", font=FONTS["body_small"],
                         text_color=COLORS["text_secondary"], width=50, anchor="w").pack(side="left", padx=(10, 4))
            self.eq_mids_var = ctk.DoubleVar(value=0.0)
            self._eq_mids_lbl = ctk.CTkLabel(mids_row, text="0dB", font=FONTS["body_small"],
                                             text_color=COLORS["text_muted"], width=40, anchor="w")
            styled_slider(mids_row, self.eq_mids_var, -12, 12, width=150,
                          command=lambda v: self._eq_mids_lbl.configure(text=f"{int(float(v)):+d}dB")).pack(side="left", padx=2)
            self._eq_mids_lbl.pack(side="left", padx=(10, 0))

            # Treble row
            treble_row = ctk.CTkFrame(self._eq_sliders_frame, fg_color="transparent")
            treble_row.pack(fill="x", pady=2)
            ctk.CTkLabel(treble_row, text="Treble:", font=FONTS["body_small"],
                         text_color=COLORS["text_secondary"], width=50, anchor="w").pack(side="left", padx=(10, 4))
            self.eq_treble_var = ctk.DoubleVar(value=0.0)
            self._eq_treble_lbl = ctk.CTkLabel(treble_row, text="0dB", font=FONTS["body_small"],
                                               text_color=COLORS["text_muted"], width=40, anchor="w")
            styled_slider(treble_row, self.eq_treble_var, -12, 12, width=150,
                          command=lambda v: self._eq_treble_lbl.configure(text=f"{int(float(v)):+d}dB")).pack(side="left", padx=2)
            self._eq_treble_lbl.pack(side="left", padx=(10, 0))
        else:
            # Keep original side-by-side packing for row is not None (horizontal layout)
            # Bass
            ctk.CTkLabel(self._eq_sliders_frame, text="Bass:", font=FONTS["body_small"],
                         text_color=COLORS["text_secondary"]).pack(side="left", padx=(10, 4))
            self.eq_bass_var = ctk.DoubleVar(value=0.0)
            self._eq_bass_lbl = ctk.CTkLabel(self._eq_sliders_frame, text="0dB", font=FONTS["body_small"],
                                             text_color=COLORS["text_muted"], width=30)
            styled_slider(self._eq_sliders_frame, self.eq_bass_var, -12, 12, width=60,
                          command=lambda v: self._eq_bass_lbl.configure(text=f"{int(float(v)):+d}dB")).pack(side="left", padx=2)
            self._eq_bass_lbl.pack(side="left", padx=(0, 10))

            # Mids
            ctk.CTkLabel(self._eq_sliders_frame, text="Mids:", font=FONTS["body_small"],
                         text_color=COLORS["text_secondary"]).pack(side="left", padx=(0, 4))
            self.eq_mids_var = ctk.DoubleVar(value=0.0)
            self._eq_mids_lbl = ctk.CTkLabel(self._eq_sliders_frame, text="0dB", font=FONTS["body_small"],
                                             text_color=COLORS["text_muted"], width=30)
            styled_slider(self._eq_sliders_frame, self.eq_mids_var, -12, 12, width=60,
                          command=lambda v: self._eq_mids_lbl.configure(text=f"{int(float(v)):+d}dB")).pack(side="left", padx=2)
            self._eq_mids_lbl.pack(side="left", padx=(0, 10))

            # Treble
            ctk.CTkLabel(self._eq_sliders_frame, text="Treble:", font=FONTS["body_small"],
                         text_color=COLORS["text_secondary"]).pack(side="left", padx=(0, 4))
            self.eq_treble_var = ctk.DoubleVar(value=0.0)
            self._eq_treble_lbl = ctk.CTkLabel(self._eq_sliders_frame, text="0dB", font=FONTS["body_small"],
                                               text_color=COLORS["text_muted"], width=30)
            styled_slider(self._eq_sliders_frame, self.eq_treble_var, -12, 12, width=60,
                          command=lambda v: self._eq_treble_lbl.configure(text=f"{int(float(v)):+d}dB")).pack(side="left", padx=2)
            self._eq_treble_lbl.pack(side="left")

        # ── Group 4: Ambiance Mix ──────────────────────────────────
        amb_box = ctk.CTkFrame(container, fg_color=COLORS["bg_input"] if row is None else "transparent",
                               corner_radius=10, border_width=1 if row is None else 0,
                               border_color=COLORS["border"])
        amb_box.pack(fill="x", pady=4, padx=2)
        inner_amb = ctk.CTkFrame(amb_box, fg_color="transparent")
        inner_amb.pack(fill="x", padx=10, pady=8)

        ctk.CTkLabel(inner_amb, text="Ambiance:", font=FONTS["body"],
                     text_color=COLORS["text_secondary"]).pack(side="left", padx=(0, 4))
        self.env_var = ctk.StringVar(value="None")
        self._env_menu = ctk.CTkOptionMenu(
            inner_amb, variable=self.env_var,
            values=["None", "Airplane Cabin", "Nature/Forest", "Custom File..."],
            font=FONTS["body"], fg_color=COLORS["bg_card"] if row is None else COLORS["bg_input"],
            button_color=COLORS["accent_primary"],
            button_hover_color=COLORS["accent_secondary"],
            dropdown_fg_color=COLORS["bg_card"],
            dropdown_hover_color=COLORS["accent_primary"],
            corner_radius=8, width=120, command=self._on_env_change)
        self._env_menu.pack(side="left", padx=(0, 14))

        ctk.CTkLabel(inner_amb, text="Vol:", font=FONTS["body_small"],
                     text_color=COLORS["text_secondary"]).pack(side="left", padx=(0, 4))
        self.env_vol_var = ctk.DoubleVar(value=50.0)
        self._env_vol_lbl = ctk.CTkLabel(inner_amb, text="50%", font=FONTS["body_small"],
                                         text_color=COLORS["text_muted"], width=25, anchor="w")
        styled_slider(inner_amb, self.env_vol_var, 0, 100, width=80,
                      command=self._on_volume_change).pack(side="left", padx=4)
        self._env_vol_lbl.pack(side="left", padx=(0, 14))

        # ── Group 5: Ambiance Player Controls ──────────────────────
        self._amb_row = ctk.CTkFrame(amb_box, fg_color="transparent")
        self.custom_amb_path = None
        self._amb_sound = None
        self._ambiance_channel = None

        def _btn(text, cmd, color, state="normal"):
            return ctk.CTkButton(self._amb_row, text=text, font=FONTS["button"],
                                 fg_color=COLORS["bg_card"] if row is None else COLORS["bg_input"],
                                 hover_color=color,
                                 border_color=color, border_width=1,
                                 corner_radius=8, height=28, width=60,
                                 command=cmd, state=state)

        self._play_amb_btn    = _btn("▶ Play",    self._play_amb,    COLORS["success"],  "disabled")
        self._pause_amb_btn   = _btn("⏸ Pause",   self._pause_amb,   COLORS["warning"],  "disabled")
        self._restart_amb_btn = _btn("⏮ Restart", self._restart_amb, COLORS["accent_secondary"], "disabled")
        self._remove_amb_btn  = _btn("✖ Remove",  self._remove_amb,  COLORS["error"],    "disabled")
        self._import_amb_btn  = ctk.CTkButton(
            self._amb_row, text="📥 Import", font=FONTS["button"],
            fg_color=COLORS["bg_card"] if row is None else COLORS["bg_input"],
            hover_color=COLORS["accent_primary"],
            border_color=COLORS["accent_primary"], border_width=1,
            corner_radius=8, height=28, width=60, command=self._import_amb)

        self._amb_label = ctk.CTkLabel(self._amb_row, text="No file selected",
                                       font=FONTS["body_small"],
                                       text_color=COLORS["text_muted"])
        
        # ── Group 6: Clone Voice ───────────────────────────────────
        # Needs the app (engine panel, effects panel, status bar).
        self.clone_group = None
        if app is not None:
            from ui.panels.clone_voice_group import CloneVoiceGroup
            self.clone_group = CloneVoiceGroup(container, app)

        # Initialize visibility state
        self._on_env_change("None")
        self._on_eq_change()

    # ── EQ Switch Handler ──────────────────────────────────────────
    def _on_eq_change(self):
        if self.eq_var.get():
            if hasattr(self, '_row_is_none') and self._row_is_none:
                self._eq_sliders_frame.pack(side="top", fill="x", pady=(4, 0))
            else:
                self._eq_sliders_frame.pack(side="left", fill="y")
        else:
            self._eq_sliders_frame.pack_forget()

    # ── Volume Change Handler ──────────────────────────────────────
    def _on_volume_change(self, v):
        self._env_vol_lbl.configure(text=f"{int(float(v))}%")
        if self._ambiance_channel:
            try:
                self._ambiance_channel.set_volume(float(v) / 100.0)
            except Exception as e:
                print(f"Error updating ambiance volume: {e}")

    # ── Env change ─────────────────────────────────────────────────
    def _on_env_change(self, sel):
        self._import_amb_btn.pack_forget()
        self._play_amb_btn.pack_forget()
        self._pause_amb_btn.pack_forget()
        self._restart_amb_btn.pack_forget()
        self._remove_amb_btn.pack_forget()
        self._amb_label.pack_forget()

        if sel == "None":
            if self._ambiance_channel:
                self._ambiance_channel.stop()
            self._amb_row.pack_forget()
            self.custom_amb_path = None
            self._amb_sound = None
            self._amb_label.configure(text="No file selected", text_color=COLORS["text_muted"])
            for b in (self._play_amb_btn, self._pause_amb_btn,
                      self._restart_amb_btn, self._remove_amb_btn):
                b.configure(state="disabled")
            self.cleanup()
        elif sel == "Custom File...":
            self._amb_row.pack(fill="x", padx=10, pady=(0, 8))
            self._import_amb_btn.pack(side="left", padx=4)
            self._play_amb_btn.pack(side="left", padx=4)
            self._pause_amb_btn.pack(side="left", padx=4)
            self._restart_amb_btn.pack(side="left", padx=4)
            self._remove_amb_btn.pack(side="left", padx=4)
            self._amb_label.pack(side="left", padx=(6, 0))

            if self.custom_amb_path:
                name = self.custom_amb_path.split("/")[-1].split("\\")[-1]
                if len(name) > 20:
                    name = name[:17] + "..."
                self._amb_label.configure(text=name, text_color=COLORS["success"])
                if not self._amb_sound:
                    self._load_custom_amb_sound(self.custom_amb_path)
                for b in (self._play_amb_btn, self._pause_amb_btn,
                          self._restart_amb_btn, self._remove_amb_btn):
                    b.configure(state="normal")
            else:
                self._amb_sound = None
                self._amb_label.configure(text="No file selected", text_color=COLORS["text_muted"])
                for b in (self._play_amb_btn, self._pause_amb_btn,
                          self._restart_amb_btn, self._remove_amb_btn):
                    b.configure(state="disabled")
        else: # "Airplane Cabin" or "Nature/Forest"
            self._amb_row.pack(fill="x", padx=10, pady=(0, 8))
            self._play_amb_btn.pack(side="left", padx=4)
            self._pause_amb_btn.pack(side="left", padx=4)
            self._restart_amb_btn.pack(side="left", padx=4)
            self._remove_amb_btn.pack(side="left", padx=4)
            self._amb_label.pack(side="left", padx=(6, 0))
            self._load_builtin_ambiance(sel)

    # ── Load Sound Helpers ─────────────────────────────────────────
    def _load_custom_amb_sound(self, fp):
        if not pygame.mixer.get_init():   # no audio device: preview unavailable
            self._amb_label.configure(text="No audio device", text_color=COLORS["warning"])
            return
        pygame.mixer.set_num_channels(8)
        if not self._ambiance_channel:
            self._ambiance_channel = pygame.mixer.Channel(1)
        try:
            self.cleanup()
            if not fp.lower().endswith(".wav"):
                tmp = tempfile.mkdtemp(prefix="amb_")
                self._temp_dirs.append(tmp)
                import os
                wav = os.path.join(tmp, "temp_amb.wav")
                load_segment(fp).export(wav, format="wav")
                self._amb_sound = pygame.mixer.Sound(wav)
            else:
                self._amb_sound = pygame.mixer.Sound(fp)
        except Exception as e:
            print(f"Could not load custom ambiance: {e}")
            self._amb_label.configure(text="Load failed", text_color=COLORS["error"])

    def _load_builtin_ambiance(self, env_name):
        import numpy as np
        import os
        import time
        
        if self._ambiance_channel:
            self._ambiance_channel.stop()
            
        self._amb_label.configure(text="Generating...", text_color=COLORS["warning"])
        self._amb_sound = None

        if not pygame.mixer.get_init():   # no audio device: preview unavailable
            self._amb_label.configure(text="No audio device", text_color=COLORS["warning"])
            return
        pygame.mixer.set_num_channels(8)
        if not self._ambiance_channel:
            self._ambiance_channel = pygame.mixer.Channel(1)
            
        try:
            self.cleanup()
            sr = 24000
            duration = 30
            silent_samples = np.zeros(sr * duration, dtype=np.float32)
            
            ambiance_samples = AudioEffects.add_environment_sound(silent_samples, sr, env_name, volume=1.0)
            
            tmp_wav = os.path.join(tempfile.gettempdir(), f"preview_{env_name.replace('/', '_').replace(' ', '_')}_{int(time.time())}.wav")
            AudioEffects.save_float_as_wav(ambiance_samples, sr, tmp_wav)
            self._temp_files.append(tmp_wav)
            
            self._amb_sound = pygame.mixer.Sound(tmp_wav)
            self._amb_label.configure(text=env_name, text_color=COLORS["success"])
            
            for b in (self._play_amb_btn, self._pause_amb_btn,
                      self._restart_amb_btn, self._remove_amb_btn):
                b.configure(state="normal")
        except Exception as e:
            print(f"Error loading built-in ambiance: {e}")
            self._amb_label.configure(text="Generation failed", text_color=COLORS["error"])

    # ── Import ─────────────────────────────────────────────────────
    def _import_amb(self):
        ftypes = [("Audio Files", "*.mp3 *.wav *.ogg *.flac"), ("All Files", "*.*")]
        fp = filedialog.askopenfilename(title="Select Ambiance Music", filetypes=ftypes)
        if not fp:
            return
        self.custom_amb_path = fp
        name = fp.split("/")[-1].split("\\")[-1]
        if len(name) > 20:
            name = name[:17] + "..."
        self._amb_label.configure(text=name, text_color=COLORS["success"])
        for b in (self._play_amb_btn, self._pause_amb_btn,
                  self._restart_amb_btn, self._remove_amb_btn):
            b.configure(state="normal")
        self._load_custom_amb_sound(fp)

    def _play_amb(self):
        if self._amb_sound and self._ambiance_channel:
            self._ambiance_channel.set_volume(self.env_vol_var.get() / 100.0)
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
            self._ambiance_channel.set_volume(self.env_vol_var.get() / 100.0)
            self._ambiance_channel.play(self._amb_sound, loops=-1)

    def _remove_amb(self):
        self.env_var.set("None")
        self._on_env_change("None")

    # ── Cleanup ────────────────────────────────────────────────────
    def cleanup(self):
        import shutil
        import os
        # Stop channel first
        if self._ambiance_channel:
            try:
                self._ambiance_channel.stop()
            except Exception:
                pass
        self._amb_sound = None
        
        # Remove files
        for fp in self._temp_files:
            try:
                if os.path.exists(fp):
                    os.remove(fp)
            except Exception as e:
                print(f"Error removing temp file {fp}: {e}")
        self._temp_files.clear()
        
        # Remove directories
        for d in self._temp_dirs:
            try:
                shutil.rmtree(d, ignore_errors=True)
            except Exception as e:
                print(f"Error removing temp dir {d}: {e}")
        self._temp_dirs.clear()

    def get_settings(self) -> dict:
        return {
            "skip_silences":   self.skip_silences_var.get(),
            "speed":           1.0,
            "env_choice":      self.env_var.get(),
            "env_volume":      self.env_vol_var.get() / 100.0,
            "custom_amb_path": self.custom_amb_path,
            "ambiance_channel": self._ambiance_channel,
            "auto_subtitle":   self.auto_subtitle_var.get(),
            "eq_enabled":      self.eq_var.get(),
            "eq_bass":         self.eq_bass_var.get(),
            "eq_mids":         self.eq_mids_var.get(),
            "eq_treble":       self.eq_treble_var.get(),
            "sub_segmentation": "sentence" if self.sub_seg_var.get() == "Sentence-Level" else "word",
            "clone_voice":     self.clone_group.get_settings() if self.clone_group else {},
        }
