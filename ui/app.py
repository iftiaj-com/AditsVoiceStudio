"""
ui/app.py
──────────
AditsVoiceStudioApp — the main application window.
Assembles all panels and owns the generation / playback pipeline.
"""

import os
import sys
import io
import time
import wave
import atexit
import shutil
import tempfile
import threading
import subprocess
import webbrowser
import re

# Pyinstaller windowed-mode fix. tts_app.py normally routes these to a log
# file first (core.paths.configure_runtime); this is the last-resort guard.
if sys.stdout is None:
    sys.stdout = io.StringIO()
if sys.stderr is None:
    sys.stderr = io.StringIO()

import customtkinter as ctk
from tkinter import filedialog, messagebox
from PIL import Image
import numpy as np
import pygame
import torch
from pydub import AudioSegment
from pydub import silence as pydub_silence

from core.constants import COLORS, FONTS
from core import paths
from core.audio_io import load_segment, export_mp3
from core.version import APP_TITLE
from ui.theme import card, SmoothScrollableFrame
from ui.panels.text_panel          import TextPanel
from ui.panels.engine_panel        import EnginePanel
from ui.panels.effects_panel       import EffectsPanel
from ui.panels.customization_panel import CustomizationPanel
from ui.panels.playback_panel      import PlaybackPanel

from engines.registry   import get_available_engines
from engines.pyttsx3_engine import Pyttsx3Engine
from effects.audio_effects  import AudioEffects
from effects.registry       import EFFECTS_REGISTRY
from effects.cloned_voices  import is_clone_entry, slug_of
import core.voice_cloning as voice_cloning

try:
    import librosa
    _LIBROSA = True
except ImportError:
    _LIBROSA = False


class AditsVoiceStudioApp(ctk.CTk):

    def __init__(self):
        super().__init__()

        self.title(APP_TITLE)
        self.geometry("1180x680")
        self.minsize(1100, 600)
        self.configure(fg_color=COLORS["bg_dark"])
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        # Window & taskbar icon
        ico_file = paths.bundle_dir() / "assets" / "adits_voice_studio.ico"
        if ico_file.exists():
            try:
                self.iconbitmap(default=str(ico_file))
            except Exception:
                try:
                    self.iconbitmap(str(ico_file))
                except Exception:
                    pass

        # State
        self._temp_playback_file = None
        self._temp_save_file     = None
        self._temp_save_ext      = ".wav"
        self._temp_mp3           = None
        self._temp_srt           = None
        self._final_saved_file   = None
        self._is_playing         = False
        self._is_generating      = False
        self._gen_notice         = None    # warning shown instead of "success"
        self._playback_thread    = None
        self._stop_event         = threading.Event()
        self._tmp_dirs           = set()   # every mkdtemp we own, for cleanup
        self._engine_lock        = threading.Lock()   # one engine.synthesize() at a time

        # Best-effort temp cleanup on any interpreter exit (incl. unhandled
        # exceptions). Hard crashes are covered by the startup sweep in
        # tts_app.py (core.temp_cleanup.sweep_orphan_temp_dirs).
        atexit.register(self._cleanup_tmp_dirs)

        # Engines
        self._available_engines = get_available_engines()
        self._engine_map        = {e.name: e for e in self._available_engines}

        # System voices for pyttsx3
        self._system_voices = []
        if Pyttsx3Engine.is_available():
            self._system_voices = Pyttsx3Engine.load_system_voices()

        # No audio device (some VMs, RDP sessions) must not stop the window
        # from opening; generation and saving still work without playback.
        self._audio_error = None
        try:
            pygame.mixer.init()
        except Exception as e:
            self._audio_error = str(e)
            print(f"Audio output unavailable: {e}")
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        if self._audio_error:
            self._set_status(f"⚠  No audio output device — playback disabled ({self._audio_error})",
                             COLORS["warning"])

    # ──────────────────────────────────────────────────────────────
    #  UI
    # ──────────────────────────────────────────────────────────────

    def _build_ui(self):
        # Main container filling the window
        main_container = ctk.CTkFrame(self, fg_color=COLORS["bg_dark"])
        main_container.pack(fill="both", expand=True)

        # Header (Top, full width)
        header = ctk.CTkFrame(main_container, fg_color="transparent")
        header.pack(fill="x", padx=24, pady=(18, 4))
        header.columnconfigure(1, weight=1)

        logo_path = paths.bundle_dir() / "assets" / "logo.png"
        self._logo_image = None
        if logo_path.exists():
            try:
                logo_pil = Image.open(logo_path)
                self._logo_image = ctk.CTkImage(light_image=logo_pil, dark_image=logo_pil, size=(48, 48))
                logo_label = ctk.CTkLabel(header, text="", image=self._logo_image)
                logo_label.grid(row=0, column=0, rowspan=2, padx=(0, 14), sticky="w")
            except Exception:
                bar = ctk.CTkFrame(header, width=5, height=50,
                                   fg_color=COLORS["accent_primary"], corner_radius=3)
                bar.grid(row=0, column=0, rowspan=2, padx=(0, 14), sticky="ns")
        else:
            bar = ctk.CTkFrame(header, width=5, height=50,
                               fg_color=COLORS["accent_primary"], corner_radius=3)
            bar.grid(row=0, column=0, rowspan=2, padx=(0, 14), sticky="ns")

        ctk.CTkLabel(header, text=APP_TITLE, font=FONTS["title"],
                     text_color=COLORS["text_primary"]).grid(row=0, column=1, sticky="w")
        engine_names = " + ".join(e.name.split("(")[0].split(" ")[0]
                                  for e in self._available_engines)
        ctk.CTkLabel(header,
                     text=f"Text-to-Speech, Audio FX & Voice Cloning",
                     font=FONTS["body_small"],
                     text_color=COLORS["text_muted"]).grid(row=1, column=1, sticky="w")

        # Split content frame (Middle)
        content_frame = ctk.CTkFrame(main_container, fg_color="transparent")
        content_frame.pack(fill="both", expand=True, padx=24, pady=(10, 10))
        # "uniform" locks both columns to a fixed 50/50 split so button
        # text/state changes during generation can't re-balance the layout.
        content_frame.columnconfigure(0, weight=1, minsize=500, uniform="content")
        content_frame.columnconfigure(1, weight=1, minsize=500, uniform="content")
        content_frame.rowconfigure(0, weight=1)

        # Left Column (Text Input + Playback)
        left_col = ctk.CTkFrame(content_frame, fg_color="transparent")
        left_col.grid(row=0, column=0, sticky="nsew", padx=(0, 5))

        # Right Column (Scrollable list of panels: TTS Engine, Customization, Voice Effects)
        right_scroll = SmoothScrollableFrame(
            content_frame, fg_color="transparent",
            scrollbar_button_color=COLORS["border"],
            scrollbar_button_hover_color=COLORS["accent_primary"]
        )
        right_scroll.grid(row=0, column=1, sticky="nsew", padx=(5, 0))

        # add="+" preserves CTkScrollableFrame's own <Configure> handler
        # (_fit_frame_dimensions_to_canvas), which stretches the inner frame
        # to the full canvas width. Binding without it replaced that handler
        # and left the right-side cards at their narrow natural width.
        def _reset_xview(event):
            right_scroll._parent_canvas.xview_moveto(0)
        right_scroll._parent_canvas.bind("<FocusIn>", _reset_xview, add="+")
        right_scroll._parent_canvas.bind("<Configure>", _reset_xview, add="+")

        # Panels in Left Column
        self._text_panel = TextPanel(left_col, row=None)
        self._play_panel = PlaybackPanel(
            left_col,
            on_generate=self._on_generate,
            on_preview=self._on_preview,
            on_cancel=self._on_cancel,
            on_play=self._on_play,
            on_stop=self._on_stop,
            on_save=self._on_save,
            on_save_srt=self._on_save_srt,
            on_open_folder=self._on_open_folder,
            row=None
        )

        # Panels in Right Scrollable Column
        self._engine_panel = EnginePanel(right_scroll, self._available_engines, self._system_voices, row=None)
        self._cust_panel = CustomizationPanel(right_scroll, row=None, app=self)
        self._effects_panel = EffectsPanel(right_scroll, row=None,
                                           on_edit_clones=self._open_voice_manager)
        if self._cust_panel.clone_group is not None:
            self._cust_panel.clone_group.attach(self._effects_panel)

        # Status Bar (Bottom)
        status_frame = ctk.CTkFrame(main_container, fg_color=COLORS["bg_card"],
                                    corner_radius=10, border_width=1,
                                    border_color=COLORS["border"])
        status_frame.pack(fill="x", padx=24, pady=(5, 15))

        si = ctk.CTkFrame(status_frame, fg_color="transparent")
        si.pack(fill="x", padx=14, pady=8)
        self._status_dot = ctk.CTkLabel(si, text="●", font=("Segoe UI", 10),
                                        text_color=COLORS["text_muted"])
        self._status_dot.pack(side="left", padx=(0, 8))
        self._status_label = ctk.CTkLabel(si,
                                          text="Ready — Enter text and click Generate",
                                          font=FONTS["status"],
                                          text_color=COLORS["text_secondary"])
        self._status_label.pack(side="left")
        self._site_link = ctk.CTkLabel(si, text="www.iftiaj.com",
                                       font=FONTS["body_small"],
                                       text_color=COLORS["accent_primary"],
                                       fg_color=COLORS["bg_input"],
                                       corner_radius=6, width=60, height=22, cursor="hand2")
        self._site_link.pack(side="right", padx=(8, 0))
        self._site_link.bind("<Button-1>", lambda e: webbrowser.open("https://www.iftiaj.com"))

    # ──────────────────────────────────────────────────────────────
    #  STATUS
    # ──────────────────────────────────────────────────────────────

    def _set_status(self, text, color=None):
        self._status_label.configure(text=text)
        self._status_dot.configure(text_color=color or COLORS["text_muted"])

    # ──────────────────────────────────────────────────────────────
    #  GENERATION
    # ──────────────────────────────────────────────────────────────

    def _on_generate(self):
        self._start_generation(preview=False)

    def _on_preview(self):
        """Fast snippet preview: synthesize only the first sentence with the
        currently active voice + effects so the user can audition instantly."""
        self._start_generation(preview=True)

    @staticmethod
    def _snippet_of(text, max_chars=150):
        """First sentence of *text*, capped at *max_chars* on a word boundary."""
        parts = re.split(r'(?<=[.!?])\s+', text, maxsplit=1)
        snippet = parts[0] if parts else text
        if len(snippet) > max_chars:
            cut = snippet[:max_chars].rsplit(" ", 1)[0]
            snippet = cut or snippet[:max_chars]
        return snippet

    def _start_generation(self, preview=False):
        if self._is_generating:
            return
        text = self._text_panel.get_text().strip()
        if not text:
            self._set_status("⚠  Please enter some text first", COLORS["warning"])
            return
        if preview:
            text = self._snippet_of(text)

        self._temp_playback_file = None
        self._temp_save_file     = None
        self._temp_save_ext      = ".wav"
        self._temp_mp3           = None
        self._temp_srt           = None

        self._is_generating = True
        self._gen_notice    = None
        self._stop_event.clear()

        pp = self._play_panel
        pp.generate_btn.configure(state="disabled",
                                  text="Previewing…" if preview else "Generating…")
        pp.cancel_btn.configure(state="normal", text="✖  Cancel")
        pp.set_generating(True)
        buttons = [pp.play_btn, pp.stop_btn, pp.save_btn, pp.save_srt_btn, pp.open_folder_btn]
        if getattr(pp, "preview_btn", None):
            buttons.append(pp.preview_btn)
        for btn in buttons:
            btn.configure(state="disabled")
        pp.progress.set(0)
        self._set_status("Generating preview…" if preview else "Generating speech…",
                         COLORS["warning"])

        threading.Thread(target=self._generate_worker,
                         args=(text, preview), daemon=True).start()

    def _on_cancel(self):
        if self._is_generating:
            self._stop_event.set()
            self._set_status("⌛  Cancelling…", COLORS["warning"])
            self._play_panel.cancel_btn.configure(state="disabled", text="Cancelling…")

    def _on_generate_finish(self, playback_file, autoplay=False):
        self._is_generating = False
        pp = self._play_panel
        pp.generate_btn.configure(state="normal", text="⚡  Generate Speech")
        pp.set_generating(False)
        if getattr(pp, "preview_btn", None):
            pp.preview_btn.configure(state="normal")
        if playback_file is None:
            self._set_status("⏹  Generation cancelled", COLORS["warning"])
            pp.progress.set(0)
            return
        self._temp_playback_file = playback_file
        pp.play_btn.configure(state="normal")
        pp.save_btn.configure(state="normal")
        if self._temp_srt and os.path.exists(self._temp_srt):
            pp.save_srt_btn.configure(state="normal")
        else:
            pp.save_srt_btn.configure(state="disabled")

        if self._temp_mp3:
            pp.open_folder_btn.configure(state="normal")
        if self._gen_notice:
            # A step was skipped (e.g. the cloned voice); don't hide that behind "success".
            self._set_status(f"⚠  Speech generated. {self._gen_notice}", COLORS["warning"])
        else:
            self._set_status("✅  Speech generated successfully!", COLORS["success"])
        if autoplay:
            self._on_play()

    def _on_generate_error(self, error_msg):
        self._is_generating = False
        pp = self._play_panel
        pp.generate_btn.configure(state="normal", text="⚡  Generate Speech")
        pp.set_generating(False)
        if getattr(pp, "preview_btn", None):
            pp.preview_btn.configure(state="normal")
        pp.progress.set(0)
        self._set_status(f"❌  Error: {error_msg}", COLORS["error"])
        messagebox.showerror("Generation Error", error_msg)

    @staticmethod
    def _engine_kwargs(eng_settings, device_str, status_cb, check_cancel) -> dict:
        """Keyword arguments for engine.synthesize() from EnginePanel.get_settings().

        Shared by generation and the Clone Voice dialog (base-voice sample).
        """
        engine_name = eng_settings["engine_name"]
        kwargs = dict(
            device=device_str,
            status_cb=status_cb,
            check_cancel=check_cancel,
            lang_key=eng_settings["gtts_lang_key"],
            voice_key=(
                eng_settings["edge_voice_key"]   if "Edge"    in engine_name else
                eng_settings["kokoro_voice_key"] if "Kokoro"  in engine_name else
                eng_settings["piper_voice_key"]  if "Piper"   in engine_name else
                eng_settings["kokoro_voice_key"]  # fallback
            ),
            melo_voice_cfg=eng_settings["melo_voice_cfg"],
            melo_speed=eng_settings["melo_speed"],
            rate=eng_settings["edge_rate"],
            kokoro_speed=eng_settings["kokoro_speed"],
            kokoro_lang_code=eng_settings.get("kokoro_lang_code", "a"),
            kokoro_subtitle=eng_settings.get("kokoro_subtitle", False),
            voice_id=eng_settings["pyttsx3_voice_id"],
            volume=eng_settings["pyttsx3_volume"],
        )
        if "pyttsx3" in engine_name:
            kwargs["rate"] = eng_settings["pyttsx3_rate"]
        return kwargs

    def _generate_worker(self, text, preview=False):
        def check_cancel():
            if self._stop_event.is_set():
                self.after(0, self._on_generate_finish, None)
                return True
            return False

        def progress(frac):
            self.after(0, lambda f=frac: self._play_panel.progress.set(f))

        def status(msg):
            self.after(0, lambda m=msg: self._set_status(m, COLORS["accent_primary"]))

        def warn(msg):
            self.after(0, lambda m=msg: self._set_status(m, COLORS["warning"]))

        try:
            import gc
            gc.collect()
            if torch.cuda.is_available():
                torch.cuda.empty_cache()

            if check_cancel():
                return

            tmp_dir = tempfile.mkdtemp(prefix="adits_vs_")
            self._tmp_dirs.add(tmp_dir)
            progress(0.05)

            eng_settings  = self._engine_panel.get_settings()
            cust_settings = self._cust_panel.get_settings()
            fx_active     = self._effects_panel.get_active_effects()
            # Per-voice overrides from the Clone Voice group; empty means
            # "use the values saved in the voice's profile".
            clone_cfg     = cust_settings.get("clone_voice") or {}

            engine_name = eng_settings["engine_name"]
            use_gpu     = eng_settings["use_gpu"] and torch.cuda.is_available()
            device_str  = "cuda" if use_gpu else "cpu"

            # Locate the engine instance
            engine = self._engine_map.get(engine_name)
            if engine is None:
                raise RuntimeError(f"Engine '{engine_name}' not found.")

            kwargs = self._engine_kwargs(eng_settings, device_str, status, check_cancel)

            progress(0.10)
            # The Clone Voice tools also synthesize (base-voice sample, preview);
            # engines are not known to be thread-safe, so one call at a time.
            with self._engine_lock:
                result = engine.synthesize(text, tmp_dir, **kwargs)
            if check_cancel():
                return

            playback_file = result["playback_file"]
            save_file     = result["save_file"]
            save_ext      = result["save_ext"]
            self._temp_srt = result.get("srt_file")

            progress(0.45)

            # ── Post-processing plan ───────────────────────────────
            # Everything below runs on ONE in-memory float32 array: the
            # engine output is decoded once, every enabled DSP stage runs
            # in RAM, and the result hits the disk exactly once at the end
            # (plus one MP3 encode from the same in-memory buffer).
            speed         = cust_settings["speed"]
            needs_silence = cust_settings["skip_silences"]
            needs_speed   = abs(speed - 1.0) > 0.01
            active_fx     = [e for e in EFFECTS_REGISTRY
                             if e["key"] != "normalize"
                             and fx_active.get(e["key"], False)]
            eq_enabled    = cust_settings.get("eq_enabled", False)
            env_choice    = cust_settings["env_choice"]
            needs_env     = env_choice != "None"
            needs_norm    = fx_active.get("normalize", False)

            dsp_stages = (int(needs_silence) + int(needs_speed) + len(active_fx)
                          + int(eq_enabled) + int(needs_env) + int(needs_norm))
            processed_seg = None   # in-memory AudioSegment for the final export

            if dsp_stages and playback_file:
                stages_done = 0

                def step():
                    # Advance the bar proportionally through the 0.45–0.80 window
                    nonlocal stages_done
                    stages_done += 1
                    progress(0.45 + 0.35 * stages_done / dsp_stages)

                try:
                    seg = load_segment(playback_file)   # single decode

                    # ── Skip silences (pydub, in memory) ───────────
                    if needs_silence:
                        if check_cancel():
                            return
                        status("Removing silences…")
                        chunks = pydub_silence.split_on_silence(
                            seg, min_silence_len=200, silence_thresh=-40)
                        if chunks:
                            joined = chunks[0]
                            for c in chunks[1:]:
                                joined += c
                            seg = joined
                        step()

                    # ── Decode once to float32 mono ────────────────
                    seg  = seg.set_channels(1)
                    sr   = seg.frame_rate
                    peak = float(2 ** (8 * seg.sample_width - 1))
                    samples = np.array(seg.get_array_of_samples(),
                                       dtype=np.float32) / peak

                    # ── Speed adjustment ───────────────────────────
                    if needs_speed:
                        if check_cancel():
                            return
                        status(f"Adjusting speed to {speed:.2f}x…")
                        try:
                            samples = AudioEffects.change_speed(samples, speed)
                        except Exception as e:
                            warn(f"⚠ Speed adjustment failed: {e}")
                        step()

                    # ── Voice effects ──────────────────────────────
                    for effect in active_fx:
                        if check_cancel():
                            return
                        status(f"Applying {effect['label'].split('  ')[-1]}…")
                        fn        = effect["fn"]
                        kwargs_fx = effect.get("kwargs", {})
                        if is_clone_entry(effect):
                            # Cloned voice: runs in a worker process; can take
                            # seconds to a minute, and must honour Cancel.
                            from core.voice_cloning.errors import VCCancelled, VCError
                            kwargs_fx = dict(kwargs_fx, cancel_event=self._stop_event,
                                             status_cb=status)
                            cfg = clone_cfg if clone_cfg.get("slug") == slug_of(effect["key"]) else {}
                            for name in ("pitch", "index_rate", "protect", "diffusion_steps"):
                                if cfg.get(name) is not None:
                                    kwargs_fx[name] = cfg[name]
                            try:
                                samples = fn(samples, sr, **kwargs_fx)
                            except VCCancelled:
                                if check_cancel():
                                    return
                                raise
                            except VCError as vc_err:
                                # Keep going in the engine's own voice; say why.
                                self._gen_notice = (f"Cloned voice not applied: "
                                                    f"{vc_err.message}")
                                warn(f"⚠ {self._gen_notice}")
                            step()
                            continue
                        try:
                            samples = fn(samples, sr, **kwargs_fx)
                        except TypeError:
                            samples = fn(samples, **kwargs_fx)
                        step()

                    # ── Equalizer ──────────────────────────────────
                    if eq_enabled:
                        if check_cancel():
                            return
                        status("Applying equalizer…")
                        try:
                            low_factor  = 10 ** (cust_settings["eq_bass"] / 20.0)
                            mid_factor  = 10 ** (cust_settings["eq_mids"] / 20.0)
                            high_factor = 10 ** (cust_settings["eq_treble"] / 20.0)
                            samples = AudioEffects.three_band_eq(
                                samples, sr, low_factor, mid_factor, high_factor)
                        except Exception as eq_err:
                            warn(f"⚠ EQ failed: {eq_err}")
                        step()

                    # ── Ambiance mix ───────────────────────────────
                    if needs_env:
                        if check_cancel():
                            return
                        status("Adding ambiance noise…")
                        amb_path = (cust_settings["custom_amb_path"]
                                    if env_choice == "Custom File..."
                                    else env_choice)
                        if amb_path:
                            samples = AudioEffects.add_environment_sound(
                                samples, sr, amb_path,
                                volume=cust_settings["env_volume"])
                        step()

                    # ── Post-mix normalization (always last) ───────
                    if needs_norm:
                        if check_cancel():
                            return
                        samples = AudioEffects.normalize(samples)
                        step()

                    # ── Single disk write ──────────────────────────
                    int16 = (np.clip(samples, -1.0, 1.0) * 32767.0).astype(np.int16)
                    processed_seg = AudioSegment(
                        data=int16.tobytes(), sample_width=2,
                        frame_rate=sr, channels=1)
                    out = os.path.join(tmp_dir, "processed.wav")
                    processed_seg.export(out, format="wav")
                    playback_file = save_file = out
                    save_ext = ".wav"

                except Exception as fx_err:
                    # Fall back to the raw engine output
                    processed_seg = None
                    warn(f"⚠ Effects failed: {fx_err}")

            progress(0.80)

            # ── Subtitles generation ───────────────────────────────
            if not preview and cust_settings.get("auto_subtitle", False) and playback_file:
                if check_cancel():
                    return
                from core.subtitles import generate_subtitles, SubtitleCancelled
                try:
                    srt_out = os.path.join(tmp_dir, "final.srt")
                    seg_mode = cust_settings.get("sub_segmentation", "word")
                    generate_subtitles(playback_file, srt_out, status_cb=status,
                                       segmentation=seg_mode,
                                       check_cancel=self._stop_event.is_set)
                    self._temp_srt = srt_out
                except SubtitleCancelled:
                    self.after(0, self._on_generate_finish, None)
                    return
                except Exception as e:
                    print(f"Subtitle generation failed: {e}")
                    warn(f"⚠ Subtitles failed: {e}")

            progress(0.92)

            # ── MP3 export for save (from memory when possible) ────
            self._temp_mp3 = None
            if preview:
                pass   # previews are playback-only; skip the MP3 encode
            elif save_file and save_file.endswith(".mp3"):
                self._temp_mp3 = save_file
            elif save_file:
                try:
                    mp3_out = os.path.join(tmp_dir, "final.mp3")
                    src_seg = (processed_seg if processed_seg is not None
                               else load_segment(save_file))
                    export_mp3(src_seg, mp3_out)
                    self._temp_mp3 = mp3_out
                except Exception as mp3_err:
                    # WAV is still saved; say why MP3 is missing from the dialog.
                    print(f"MP3 export failed: {mp3_err}")
                    warn(f"⚠ MP3 export failed, WAV only: {mp3_err}")

            # Clean up temp dirs from previous generations
            for old_dir in list(self._tmp_dirs):
                if old_dir != tmp_dir:
                    shutil.rmtree(old_dir, ignore_errors=True)
                    self._tmp_dirs.discard(old_dir)

            self._temp_save_file = save_file
            self._temp_save_ext  = save_ext
            progress(1.0)
            self.after(0, lambda: self._on_generate_finish(playback_file,
                                                           autoplay=preview))

        except Exception as e:
            if self._stop_event.is_set():
                # A cancel raised inside an engine (e.g. aborted download)
                self.after(0, self._on_generate_finish, None)
            else:
                self.after(0, lambda err=str(e): self._on_generate_error(err))

    # ──────────────────────────────────────────────────────────────
    #  PLAYBACK
    # ──────────────────────────────────────────────────────────────

    def _on_play(self):
        pf = self._temp_playback_file
        if not pf or not os.path.exists(pf):
            self._set_status("⚠  No audio to play — generate first", COLORS["warning"])
            return
        if self._audio_error:
            self._set_status("⚠  No audio output device — save the file to listen",
                             COLORS["warning"])
            return
        if self._is_playing:
            pygame.mixer.music.stop()
        amb = self._cust_panel._ambiance_channel
        if amb and amb.get_busy():
            amb.pause()
        pygame.mixer.music.load(pf)
        pygame.mixer.music.play()
        self._is_playing = True
        self._play_panel.play_btn.configure(text="▶  Playing…", state="disabled")
        self._play_panel.stop_btn.configure(state="normal")
        self._set_status("🎵  Playing audio…", COLORS["accent_primary"])
        threading.Thread(target=self._monitor_playback, daemon=True).start()

    def _monitor_playback(self):
        try:
            pf = self._temp_playback_file
            try:
                if pf.endswith(".wav"):
                    with wave.open(pf, 'r') as wf:
                        duration_ms = (wf.getnframes() / wf.getframerate()) * 1000
                else:
                    duration_ms = (os.path.getsize(pf) / 4000) * 1000
            except Exception:
                duration_ms = 10000
            start = time.time()
            while pygame.mixer.music.get_busy() and self._is_playing:
                elapsed  = (time.time() - start) * 1000
                progress = min(elapsed / max(duration_ms, 1), 1.0)
                self.after(0, lambda p=progress: self._play_panel.progress.set(p))
                time.sleep(0.05)
            self.after(0, self._on_playback_done)
        except Exception:
            self.after(0, self._on_playback_done)

    def _on_playback_done(self):
        self._is_playing = False
        self._play_panel.play_btn.configure(text="▶  Play", state="normal")
        self._play_panel.stop_btn.configure(state="disabled")
        self._play_panel.progress.set(0)
        self._set_status("✅  Playback complete", COLORS["success"])
        amb = self._cust_panel._ambiance_channel
        if amb:
            amb.unpause()

    def _on_stop(self):
        if self._is_playing:
            self._is_playing = False
            pygame.mixer.music.stop()
            self._play_panel.play_btn.configure(text="▶  Play", state="normal")
            self._play_panel.stop_btn.configure(state="disabled")
            self._play_panel.progress.set(0)
            self._set_status("⏹  Playback stopped", COLORS["text_muted"])
            amb = self._cust_panel._ambiance_channel
            if amb:
                amb.unpause()

    # ──────────────────────────────────────────────────────────────
    #  SAVE / EXPORT
    # ──────────────────────────────────────────────────────────────

    def _on_save(self):
        sf = self._temp_save_file
        if not sf or not os.path.exists(sf):
            self._set_status("⚠  No audio to save — generate first", COLORS["warning"])
            return
        has_mp3 = bool(self._temp_mp3 and os.path.exists(self._temp_mp3))
        if has_mp3:
            default_ext = ".mp3"
            ftypes = [("MP3 Audio", "*.mp3"), ("WAV Audio", "*.wav"), ("All Files", "*.*")]
        else:
            default_ext = ".wav"
            ftypes = [("WAV Audio", "*.wav"), ("All Files", "*.*")]

        # New filename convention: First_three_word_of_input_timestamp
        text = self._text_panel.get_text().strip()
        words = re.findall(r'\w+', text)
        prefix = "_".join(words[:3]) if words else "adits_voice_studio"
        ts = time.strftime('%Y%m%d_%H%M%S')
        initial_name = f"{prefix}_{ts}{default_ext}"

        output_dir = str(paths.output_dir())
        filepath = filedialog.asksaveasfilename(
            title="Save Speech Audio",
            defaultextension=default_ext,
            filetypes=ftypes,
            initialdir=output_dir,
            initialfile=initial_name,
        )
        if not filepath:
            return
        try:
            os.makedirs(os.path.dirname(filepath), exist_ok=True)
            src = self._temp_mp3 if (filepath.lower().endswith(".mp3") and has_mp3) else sf
            shutil.copy2(src, filepath)
            
            # Save SRT if it exists
            if self._temp_srt and os.path.exists(self._temp_srt):
                srt_path = os.path.splitext(filepath)[0] + ".srt"
                shutil.copy2(self._temp_srt, srt_path)
                self._set_status(f"💾  Saved audio & subtitles to {os.path.basename(filepath)}", COLORS["success"])
            else:
                self._set_status(f"💾  Saved to {os.path.basename(filepath)}", COLORS["success"])

            self._final_saved_file = filepath
            self._play_panel.open_folder_btn.configure(state="normal")
        except Exception as e:
            self._set_status(f"❌  Save error: {e}", COLORS["error"])

    def _on_save_srt(self):
        sf = self._temp_srt
        if not sf or not os.path.exists(sf):
            self._set_status("⚠  No subtitle to save", COLORS["warning"])
            return

        text = self._text_panel.get_text().strip()
        words = re.findall(r'\w+', text)
        prefix = "_".join(words[:3]) if words else "subtitles"
        ts = time.strftime('%Y%m%d_%H%M%S')
        initial_name = f"{prefix}_{ts}.srt"

        output_dir = str(paths.output_dir())
        filepath = filedialog.asksaveasfilename(
            title="Save Subtitles (SRT)",
            defaultextension=".srt",
            filetypes=[("SRT Subtitles", "*.srt"), ("All Files", "*.*")],
            initialdir=output_dir,
            initialfile=initial_name,
        )
        if not filepath:
            return
        try:
            shutil.copy2(sf, filepath)
            self._final_saved_file = filepath
            self._play_panel.open_folder_btn.configure(state="normal")
            self._set_status(f"💾  Saved SRT to {os.path.basename(filepath)}", COLORS["success"])
        except Exception as e:
            self._set_status(f"❌  Save error: {e}", COLORS["error"])

    def _on_open_folder(self):
        # Before the first save, open the save folder rather than the temp
        # dir, which is deleted on the next generation.
        target = self._final_saved_file
        try:
            if target and os.path.exists(target):
                subprocess.run(['explorer', '/select,', os.path.normpath(target)])
            else:
                os.startfile(str(paths.output_dir()))
        except Exception as e:
            self._set_status(f"❌  Could not open folder: {e}", COLORS["error"])

    # ──────────────────────────────────────────────────────────────
    #  CLEANUP
    # ──────────────────────────────────────────────────────────────

    def _cleanup_tmp_dirs(self):
        """Delete every temp dir this session created (also runs via atexit)."""
        for d in list(self._tmp_dirs):
            shutil.rmtree(d, ignore_errors=True)
            self._tmp_dirs.discard(d)
        pf = self._temp_playback_file
        if pf:
            shutil.rmtree(os.path.dirname(pf), ignore_errors=True)

    # ──────────────────────────────────────────────────────────────
    #  CLONED VOICES
    # ──────────────────────────────────────────────────────────────

    def refresh_cloned_voices(self, select=None):
        """Re-read the voice library into the registry, the tiles and the Clone Voice group.

        select: slug of a voice to switch on afterwards (e.g. just created).
        """
        from effects.cloned_voices import refresh_cloned_voices
        try:
            refresh_cloned_voices(EFFECTS_REGISTRY)
        except Exception as e:
            print(f"Cloned voice refresh failed: {e}")
        self._effects_panel.sync()
        group = self._cust_panel.clone_group
        if group is not None:
            group.refresh()
        if select:
            self._effects_panel.set_active(f"clone:{select}", True)

    def _open_voice_manager(self):
        from ui.panels.clone_voice_dialog import ManageVoicesDialog
        win = getattr(self, "_voice_manager", None)
        if win is not None and win.winfo_exists():
            win.lift()
            win.focus_force()
            return
        self._voice_manager = ManageVoicesDialog(self)

    def _on_close(self):
        if voice_cloning.is_training():
            if not messagebox.askyesno(
                    "Voice training in progress",
                    "A cloned voice is still training.\n\n"
                    "Closing Adits Voice Studio stops the training and discards it. Close anyway?",
                    icon="warning", parent=self):
                return
        self._stop_event.set()
        self.destroy()

    def destroy(self):
        try:
            pygame.mixer.quit()
        except Exception:
            pass
        # Stop clone tasks/preview, then the worker processes (no-op if none started).
        group = getattr(getattr(self, "_cust_panel", None), "clone_group", None)
        if group is not None:
            group.cleanup()
        text_panel = getattr(self, "_text_panel", None)
        if text_panel is not None:
            text_panel.cleanup()            # closes the dictation mic, stops a transcription
        try:
            voice_cloning.shutdown_client()
        except Exception as e:
            print(f"Voice cloning shutdown failed: {e}")
        self._cleanup_tmp_dirs()
        if hasattr(self, '_cust_panel') and hasattr(self._cust_panel, 'cleanup'):
            self._cust_panel.cleanup()
        super().destroy()
