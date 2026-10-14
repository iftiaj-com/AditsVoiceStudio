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
import pygame
import torch   # noqa: F401  loaded here, before the window, as before core.pipeline existed

from core.constants import COLORS, FONTS
from core import paths
from core import pipeline
from core.playback import Player
from core.synth import Synthesizer
from core.version import APP_TITLE
from ui.theme import card, SmoothScrollableFrame
from ui.panels.text_panel          import TextPanel
from ui.panels.engine_panel        import EnginePanel
from ui.panels.effects_panel       import EffectsPanel
from ui.panels.customization_panel import CustomizationPanel
from ui.panels.playback_panel      import PlaybackPanel

from engines.registry   import get_available_engines
from engines.pyttsx3_engine import Pyttsx3Engine
from effects.registry       import EFFECTS_REGISTRY
import core.voice_cloning as voice_cloning


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
        self._is_generating      = False
        self._gen_notice         = None    # warning shown instead of "success"
        self._stop_event         = threading.Event()
        self._tmp_dirs           = set()   # every mkdtemp we own, for cleanup
        self._player             = Player()
        self._play_poll          = None    # after() id of _poll_playback

        # Best-effort temp cleanup on any interpreter exit (incl. unhandled
        # exceptions). Hard crashes are covered by the startup sweep in
        # tts_app.py (core.temp_cleanup.sweep_orphan_temp_dirs).
        atexit.register(self._cleanup_tmp_dirs)

        # Engines
        self._available_engines = get_available_engines()
        # Every engine.synthesize() call (generation, Clone Voice, voice match) goes through it.
        self.synth              = Synthesizer(self._available_engines)

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
            self.set_status(f"⚠  No audio output device — playback disabled ({self._audio_error})",
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
    #  WHAT PANELS AND DIALOGS MAY USE
    #  Panels use these instead of the private members, so a new UI
    #  toolkit only has to provide the same names.
    # ──────────────────────────────────────────────────────────────

    @property
    def engine_panel(self):
        return self._engine_panel

    @property
    def text_panel(self):
        return self._text_panel

    @property
    def cust_panel(self):
        return getattr(self, "_cust_panel", None)

    @property
    def system_voices(self):
        return self._system_voices

    @property
    def is_generating(self):
        return self._is_generating

    # ──────────────────────────────────────────────────────────────
    #  STATUS
    # ──────────────────────────────────────────────────────────────

    def set_status(self, text, color=None):
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
            self.set_status("⚠  Please enter some text first", COLORS["warning"])
            return
        if preview:
            text = self._snippet_of(text)

        # Read every setting here, on the Tk thread. The worker gets plain
        # values and never touches a widget.
        try:
            req = pipeline.GenerationRequest(
                text=text,
                eng_settings=self._engine_panel.get_settings(),
                cust_settings=self._cust_panel.get_settings(),
                fx_active=self._effects_panel.get_active_effects(),
                preview=preview,
            )
        except Exception as e:
            self.set_status(f"❌  Error: {e}", COLORS["error"])
            messagebox.showerror("Generation Error", str(e))
            return

        self._temp_playback_file = None
        self._temp_save_file     = None
        self._temp_save_ext      = ".wav"
        self._temp_mp3           = None
        self._temp_srt           = None

        tmp_dir = tempfile.mkdtemp(prefix="adits_vs_")
        self._tmp_dirs.add(tmp_dir)

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
        self.set_status("Generating preview…" if preview else "Generating speech…",
                         COLORS["warning"])

        threading.Thread(target=self._generate_worker,
                         args=(req, tmp_dir), daemon=True).start()

    def _on_cancel(self):
        if self._is_generating:
            self._stop_event.set()
            self.set_status("⌛  Cancelling…", COLORS["warning"])
            self._play_panel.cancel_btn.configure(state="disabled", text="Cancelling…")

    def _on_generate_finish(self, playback_file, autoplay=False):
        self._is_generating = False
        pp = self._play_panel
        pp.generate_btn.configure(state="normal", text="⚡  Generate Speech")
        pp.set_generating(False)
        if getattr(pp, "preview_btn", None):
            pp.preview_btn.configure(state="normal")
        if playback_file is None:
            self.set_status("⏹  Generation cancelled", COLORS["warning"])
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
            self.set_status(f"⚠  Speech generated. {self._gen_notice}", COLORS["warning"])
        else:
            self.set_status("✅  Speech generated successfully!", COLORS["success"])
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
        self.set_status(f"❌  Error: {error_msg}", COLORS["error"])
        messagebox.showerror("Generation Error", error_msg)

    def _generate_worker(self, req, tmp_dir):
        """Runs core.pipeline on this thread; reports to the Tk thread with after()."""
        def progress(frac):
            self.after(0, lambda f=frac: self._play_panel.progress.set(f))

        def status(msg):
            self.after(0, lambda m=msg: self.set_status(m, COLORS["accent_primary"]))

        def warn(msg):
            self.after(0, lambda m=msg: self.set_status(m, COLORS["warning"]))

        try:
            res = pipeline.run(req, self.synth, tmp_dir, self._stop_event,
                               progress=progress, status=status, warn=warn)
        except pipeline.Cancelled:
            self.after(0, self._on_generate_finish, None)
            return
        except Exception as e:
            if self._stop_event.is_set():
                # A cancel raised inside an engine (e.g. aborted download)
                self.after(0, self._on_generate_finish, None)
            else:
                self.after(0, lambda err=str(e): self._on_generate_error(err))
            return
        self.after(0, self._on_generate_done, res, tmp_dir, req.preview)

    def _on_generate_done(self, res, tmp_dir, preview):
        """Tk thread: keep the result, drop older temp dirs, then finish."""
        self._temp_srt       = res.srt_file
        self._temp_mp3       = res.mp3_file
        self._temp_save_file = res.save_file
        self._temp_save_ext  = res.save_ext
        self._gen_notice     = res.notice
        # Clean up temp dirs from previous generations
        for old_dir in list(self._tmp_dirs):
            if old_dir != tmp_dir:
                shutil.rmtree(old_dir, ignore_errors=True)
                self._tmp_dirs.discard(old_dir)
        self._on_generate_finish(res.playback_file, autoplay=preview)

    # ──────────────────────────────────────────────────────────────
    #  PLAYBACK
    # ──────────────────────────────────────────────────────────────

    def _on_play(self):
        pf = self._temp_playback_file
        if not pf or not os.path.exists(pf):
            self.set_status("⚠  No audio to play — generate first", COLORS["warning"])
            return
        if self._audio_error:
            self.set_status("⚠  No audio output device — save the file to listen",
                             COLORS["warning"])
            return
        amb = self._cust_panel._ambiance_channel
        if amb and amb.get_busy():
            amb.pause()
        self._cancel_play_poll()
        self._player.play(pf)
        self._play_panel.play_btn.configure(text="▶  Playing…", state="disabled")
        self._play_panel.stop_btn.configure(state="normal")
        self.set_status("🎵  Playing audio…", COLORS["accent_primary"])
        self._play_poll = self.after(50, self._poll_playback)

    def _poll_playback(self):
        """Move the progress bar while the audio plays (Tk timer, no thread)."""
        self._play_poll = None
        try:
            playing = self._player.is_playing()
            if playing:
                self._play_panel.progress.set(self._player.fraction())
        except Exception:
            playing = False
        if playing:
            self._play_poll = self.after(50, self._poll_playback)
        else:
            self._on_playback_done()

    def _cancel_play_poll(self):
        if self._play_poll:
            self.after_cancel(self._play_poll)
            self._play_poll = None

    def _on_playback_done(self):
        self._play_panel.play_btn.configure(text="▶  Play", state="normal")
        self._play_panel.stop_btn.configure(state="disabled")
        self._play_panel.progress.set(0)
        self.set_status("✅  Playback complete", COLORS["success"])
        amb = self._cust_panel._ambiance_channel
        if amb:
            amb.unpause()

    def _on_stop(self):
        if self._player.stop():
            self._cancel_play_poll()
            self._play_panel.play_btn.configure(text="▶  Play", state="normal")
            self._play_panel.stop_btn.configure(state="disabled")
            self._play_panel.progress.set(0)
            self.set_status("⏹  Playback stopped", COLORS["text_muted"])
            amb = self._cust_panel._ambiance_channel
            if amb:
                amb.unpause()

    # ──────────────────────────────────────────────────────────────
    #  SAVE / EXPORT
    # ──────────────────────────────────────────────────────────────

    def _on_save(self):
        sf = self._temp_save_file
        if not sf or not os.path.exists(sf):
            self.set_status("⚠  No audio to save — generate first", COLORS["warning"])
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
                self.set_status(f"💾  Saved audio & subtitles to {os.path.basename(filepath)}", COLORS["success"])
            else:
                self.set_status(f"💾  Saved to {os.path.basename(filepath)}", COLORS["success"])

            self._final_saved_file = filepath
            self._play_panel.open_folder_btn.configure(state="normal")
        except Exception as e:
            self.set_status(f"❌  Save error: {e}", COLORS["error"])

    def _on_save_srt(self):
        sf = self._temp_srt
        if not sf or not os.path.exists(sf):
            self.set_status("⚠  No subtitle to save", COLORS["warning"])
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
            self.set_status(f"💾  Saved SRT to {os.path.basename(filepath)}", COLORS["success"])
        except Exception as e:
            self.set_status(f"❌  Save error: {e}", COLORS["error"])

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
            self.set_status(f"❌  Could not open folder: {e}", COLORS["error"])

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
