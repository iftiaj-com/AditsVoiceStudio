"""
ui_qt/app.py
────────────
MainWindow: the Qt main window, started with `tts_app.py --qt`.

Phase 2 of docs/pyside6-migration-plan.md: header, two-column layout, status
bar, the Text Input and Playback & Export panels, and the full generation,
playback and save flow through core.pipeline. The right column (TTS Engine,
Customization, Voice Effects) comes in Phase 3; until then Generate uses the
panels' starting settings from core.defaults.

Threads: the generation worker never touches a widget. It reports through
ui_qt.bridge.ui(), which runs the call on the GUI thread.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QIcon, QPixmap
from PySide6.QtWidgets import (QApplication, QFileDialog, QFrame, QHBoxLayout, QLabel,
                               QMainWindow, QMessageBox, QScrollArea, QSizePolicy, QVBoxLayout,
                               QWidget)

from core import defaults, paths, pipeline
from core.constants import COLORS
from core.playback import Player
from core.synth import Synthesizer
from core.version import APP_TITLE
from ui_qt import bridge, theme
from ui_qt.bridge import ui
from ui_qt.panels.playback_panel import PlaybackPanel
from ui_qt.panels.text_panel import TextPanel

SITE_URL = "https://www.iftiaj.com"


class MainWindow(QMainWindow):

    def __init__(self):
        super().__init__()
        import pygame
        import core.voice_cloning  # noqa: F401  (is_training / shutdown_client at close)
        from engines.pyttsx3_engine import Pyttsx3Engine
        from engines.registry import get_available_engines

        self.setWindowTitle(APP_TITLE)
        self.resize(1180, 680)
        self.setMinimumSize(1100, 600)
        ico = paths.bundle_dir() / "assets" / "adits_voice_studio.ico"
        if ico.exists():
            self.setWindowIcon(QIcon(str(ico)))

        # State (same names and meaning as AditsVoiceStudioApp)
        self._temp_playback_file = None
        self._temp_save_file = None
        self._temp_save_ext = ".wav"
        self._temp_mp3 = None
        self._temp_srt = None
        self._final_saved_file = None
        self._is_generating = False
        self._gen_notice = None
        self._stop_event = threading.Event()
        self._tmp_dirs = set()
        self._player = Player()
        self._play_timer = QTimer(self)
        self._play_timer.setInterval(50)
        self._play_timer.timeout.connect(self._poll_playback)

        self._available_engines = get_available_engines()
        self.synth = Synthesizer(self._available_engines)
        self._system_voices = Pyttsx3Engine.load_system_voices() if Pyttsx3Engine.is_available() else []

        self._audio_error = None
        try:
            pygame.mixer.init()
        except Exception as e:
            self._audio_error = str(e)
            print(f"Audio output unavailable: {e}")
        self._build_ui()
        if self._audio_error:
            self.set_status(f"⚠  No audio output device — playback disabled ({self._audio_error})",
                            COLORS["warning"])

    # ── UI ─────────────────────────────────────────────────────────
    def _build_ui(self):
        root = QWidget()
        outer = QVBoxLayout(root)
        outer.setContentsMargins(24, 18, 24, 15)
        outer.setSpacing(10)
        self.setCentralWidget(root)

        # Header
        header = QHBoxLayout()
        header.setSpacing(14)
        logo = QLabel()
        logo_path = paths.bundle_dir() / "assets" / "logo.png"
        pix = QPixmap(str(logo_path)) if logo_path.exists() else QPixmap()
        if not pix.isNull():
            dpr = self.devicePixelRatioF()
            pix = pix.scaled(int(48 * dpr), int(48 * dpr), Qt.KeepAspectRatio, Qt.SmoothTransformation)
            pix.setDevicePixelRatio(dpr)
            logo.setPixmap(pix)
        else:
            logo.setFixedSize(5, 50)
            logo.setStyleSheet(f"background: {COLORS['accent_primary']}; border-radius: 3px;")
        header.addWidget(logo)
        titles = QVBoxLayout()
        titles.setSpacing(0)
        title = QLabel(APP_TITLE)
        title.setObjectName("appTitle")
        titles.addWidget(title)
        titles.addWidget(theme.label("Text-to-Speech, Audio FX & Voice Cloning", muted=True, small=True))
        header.addLayout(titles)
        header.addStretch(1)
        outer.addLayout(header)

        # Two columns, fixed 50/50: size hints are ignored, so a button text
        # change during generation cannot shift the split.
        content = QHBoxLayout()
        content.setSpacing(10)
        left = QWidget()
        left_l = QVBoxLayout(left)
        left_l.setContentsMargins(0, 0, 0, 0)
        left_l.setSpacing(12)
        self._text_panel = TextPanel()
        left_l.addWidget(self._text_panel.frame, 1)
        self._play_panel = PlaybackPanel(
            on_generate=self._on_generate, on_preview=self._on_preview, on_cancel=self._on_cancel,
            on_play=self._on_play, on_stop=self._on_stop, on_save=self._on_save,
            on_save_srt=self._on_save_srt, on_open_folder=self._on_open_folder)
        left_l.addWidget(self._play_panel.frame)

        right = QScrollArea()
        right.setWidgetResizable(True)
        right.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        right.setFrameShape(QFrame.NoFrame)
        right_body = QWidget()
        self._right_l = QVBoxLayout(right_body)
        self._right_l.setContentsMargins(0, 0, 6, 0)
        self._right_l.setSpacing(12)
        self._right_l.addWidget(self._placeholder_card())
        self._right_l.addStretch(1)
        right.setWidget(right_body)

        for col in (left, right):
            col.setMinimumWidth(500)
            col.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
            content.addWidget(col, 1)
        outer.addLayout(content, 1)

        # Status bar
        bar = QFrame()
        bar.setObjectName("statusBar")
        sb = QHBoxLayout(bar)
        sb.setContentsMargins(14, 8, 14, 8)
        self._status_dot = QLabel("●")
        self._status_dot.setFixedWidth(14)
        sb.addWidget(self._status_dot)
        self._status_label = QLabel("Ready — Enter text and click Generate")
        self._status_label.setObjectName("statusText")
        self._status_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        sb.addWidget(self._status_label, 1)
        link = QLabel("www.iftiaj.com")
        link.setObjectName("siteLink")
        link.setCursor(Qt.PointingHandCursor)
        link.mousePressEvent = lambda e: QDesktopServices.openUrl(QUrl(SITE_URL))
        sb.addWidget(link)
        outer.addWidget(bar)
        self.set_status("Ready — Enter text and click Generate")

    def _placeholder_card(self):
        eng = defaults.engine_settings(list(self.synth.engine_map), self._system_voices)
        frame, lay = theme.card("🔊  TTS Engine · ⚙️  Customization · ✨  Voice Effects")
        voice = {"Kokoro": eng["kokoro_voice_key"], "Piper": "first Piper voice",
                 "Melo": "first MeloTTS voice", "Edge": eng["edge_voice_key"],
                 "gTTS": eng["gtts_lang_key"], "pyttsx3": "first system voice"}
        name = eng["engine_name"]
        voice_text = next((v for k, v in voice.items() if k in name), "default voice")
        text = (f"These panels move to Qt in Phase 3 of the migration.\n\n"
                f"Until then, Generate uses their starting settings:\n"
                f"• Engine: {name}, {voice_text}, speed 1.0\n"
                f"• GPU: {'on' if eng['use_gpu'] else 'off'}\n"
                f"• Effects: Normalize only. No ambiance, EQ or subtitles.\n\n"
                f"To change them, use the CustomTkinter UI (run without --qt).")
        body = theme.label(text, secondary=True)
        body.setWordWrap(True)
        lay.addWidget(body)
        return frame

    # ── what panels may use (same names as AditsVoiceStudioApp) ────
    @property
    def text_panel(self):
        return self._text_panel

    @property
    def system_voices(self):
        return self._system_voices

    @property
    def is_generating(self):
        return self._is_generating

    def set_status(self, text, color=None):
        self._status_label.setText(text)
        self._status_label.setToolTip(text)
        theme.set_color(self._status_dot, color or COLORS["text_muted"])

    # ── generation ─────────────────────────────────────────────────
    def _on_generate(self):
        self._start_generation(preview=False)

    def _on_preview(self):
        """Fast preview: only the first sentence, played when done."""
        self._start_generation(preview=True)

    def _current_request(self, text, preview):
        """Settings for a run, read on the GUI thread. Phase 3 replaces the
        defaults with the ported panels' get_settings()."""
        from effects.registry import EFFECTS_REGISTRY
        return pipeline.GenerationRequest(
            text=text,
            eng_settings=defaults.engine_settings(list(self.synth.engine_map), self._system_voices),
            cust_settings=defaults.customization_settings(),
            fx_active=defaults.active_effects(EFFECTS_REGISTRY),
            preview=preview,
        )

    def _start_generation(self, preview=False):
        if self._is_generating:
            return
        text = self._text_panel.get_text().strip()
        if not text:
            self.set_status("⚠  Please enter some text first", COLORS["warning"])
            return
        if preview:
            text = pipeline.snippet_of(text)
        try:
            req = self._current_request(text, preview)
        except Exception as e:
            self.set_status(f"❌  Error: {e}", COLORS["error"])
            QMessageBox.critical(self, "Generation Error", str(e))
            return

        self._temp_playback_file = None
        self._temp_save_file = None
        self._temp_save_ext = ".wav"
        self._temp_mp3 = None
        self._temp_srt = None
        tmp_dir = tempfile.mkdtemp(prefix="adits_vs_")
        self._tmp_dirs.add(tmp_dir)

        self._is_generating = True
        self._gen_notice = None
        self._stop_event.clear()

        pp = self._play_panel
        pp.generate_btn.setEnabled(False)
        pp.generate_btn.setText("Previewing…" if preview else "Generating…")
        pp.set_generating(True)
        for btn in (pp.play_btn, pp.stop_btn, pp.save_btn, pp.save_srt_btn, pp.open_folder_btn,
                    pp.preview_btn):
            if btn is not None:
                btn.setEnabled(False)
        pp.set_progress(0)
        self.set_status("Generating preview…" if preview else "Generating speech…", COLORS["warning"])

        threading.Thread(target=self._generate_worker, args=(req, tmp_dir), daemon=True,
                         name="generate").start()

    def _generate_worker(self, req, tmp_dir):
        """Runs core.pipeline on this thread; reports to the GUI thread with ui()."""
        def progress(frac):
            ui(self._play_panel.set_progress, frac)

        def status(msg):
            ui(self.set_status, msg, COLORS["accent_primary"])

        def warn(msg):
            ui(self.set_status, msg, COLORS["warning"])

        try:
            res = pipeline.run(req, self.synth, tmp_dir, self._stop_event,
                               progress=progress, status=status, warn=warn)
        except pipeline.Cancelled:
            ui(self._on_generate_finish, None)
            return
        except Exception as e:
            if self._stop_event.is_set():
                ui(self._on_generate_finish, None)   # a cancel raised inside an engine
            else:
                ui(self._on_generate_error, str(e))
            return
        ui(self._on_generate_done, res, tmp_dir, req.preview)

    def _on_generate_done(self, res, tmp_dir, preview):
        self._temp_srt = res.srt_file
        self._temp_mp3 = res.mp3_file
        self._temp_save_file = res.save_file
        self._temp_save_ext = res.save_ext
        self._gen_notice = res.notice
        for old_dir in list(self._tmp_dirs):          # temp dirs of previous generations
            if old_dir != tmp_dir:
                shutil.rmtree(old_dir, ignore_errors=True)
                self._tmp_dirs.discard(old_dir)
        self._on_generate_finish(res.playback_file, autoplay=preview)

    def _on_cancel(self):
        if self._is_generating:
            self._stop_event.set()
            self.set_status("⌛  Cancelling…", COLORS["warning"])
            self._play_panel.cancel_btn.setEnabled(False)
            self._play_panel.cancel_btn.setText("Cancelling…")

    def _reset_generate_buttons(self):
        self._is_generating = False
        pp = self._play_panel
        pp.generate_btn.setEnabled(True)
        pp.generate_btn.setText("⚡  Generate Speech")
        pp.set_generating(False)
        if pp.preview_btn is not None:
            pp.preview_btn.setEnabled(True)

    def _on_generate_finish(self, playback_file, autoplay=False):
        self._reset_generate_buttons()
        pp = self._play_panel
        if playback_file is None:
            self.set_status("⏹  Generation cancelled", COLORS["warning"])
            pp.set_progress(0)
            return
        self._temp_playback_file = playback_file
        pp.play_btn.setEnabled(True)
        pp.save_btn.setEnabled(True)
        pp.save_srt_btn.setEnabled(bool(self._temp_srt and os.path.exists(self._temp_srt)))
        if self._temp_mp3:
            pp.open_folder_btn.setEnabled(True)
        if self._gen_notice:
            self.set_status(f"⚠  Speech generated. {self._gen_notice}", COLORS["warning"])
        else:
            self.set_status("✅  Speech generated successfully!", COLORS["success"])
        if autoplay:
            self._on_play()

    def _on_generate_error(self, error_msg):
        self._reset_generate_buttons()
        self._play_panel.set_progress(0)
        self.set_status(f"❌  Error: {error_msg}", COLORS["error"])
        QMessageBox.critical(self, "Generation Error", error_msg)

    # ── playback ───────────────────────────────────────────────────
    def _on_play(self):
        pf = self._temp_playback_file
        if not pf or not os.path.exists(pf):
            self.set_status("⚠  No audio to play — generate first", COLORS["warning"])
            return
        if self._audio_error:
            self.set_status("⚠  No audio output device — save the file to listen", COLORS["warning"])
            return
        self._player.play(pf)
        pp = self._play_panel
        pp.play_btn.setText("▶  Playing…")
        pp.play_btn.setEnabled(False)
        pp.stop_btn.setEnabled(True)
        self.set_status("🎵  Playing audio…", COLORS["accent_primary"])
        self._play_timer.start()

    def _poll_playback(self):
        """Move the progress bar while the audio plays."""
        try:
            playing = self._player.is_playing()
            if playing:
                self._play_panel.set_progress(self._player.fraction())
        except Exception:
            playing = False
        if not playing:
            self._play_timer.stop()
            self._on_playback_done()

    def _reset_play_buttons(self):
        pp = self._play_panel
        pp.play_btn.setText("▶  Play")
        pp.play_btn.setEnabled(True)
        pp.stop_btn.setEnabled(False)
        pp.set_progress(0)

    def _on_playback_done(self):
        self._reset_play_buttons()
        self.set_status("✅  Playback complete", COLORS["success"])

    def _on_stop(self):
        if self._player.stop():
            self._play_timer.stop()
            self._reset_play_buttons()
            self.set_status("⏹  Playback stopped", COLORS["text_muted"])

    # ── save / export ──────────────────────────────────────────────
    def _default_name(self, fallback, ext):
        words = re.findall(r'\w+', self._text_panel.get_text().strip())
        prefix = "_".join(words[:3]) if words else fallback
        return f"{prefix}_{time.strftime('%Y%m%d_%H%M%S')}{ext}"

    def _ask_save_path(self, title, name, filters, suffix):
        dlg = QFileDialog(self, title, os.path.join(str(paths.output_dir()), name), ";;".join(filters))
        dlg.setAcceptMode(QFileDialog.AcceptSave)
        dlg.setDefaultSuffix(suffix)
        if not dlg.exec():
            return None
        files = dlg.selectedFiles()
        return os.path.normpath(files[0]) if files else None

    def _on_save(self):
        sf = self._temp_save_file
        if not sf or not os.path.exists(sf):
            self.set_status("⚠  No audio to save — generate first", COLORS["warning"])
            return
        has_mp3 = bool(self._temp_mp3 and os.path.exists(self._temp_mp3))
        if has_mp3:
            ext, filters = ".mp3", ["MP3 Audio (*.mp3)", "WAV Audio (*.wav)", "All Files (*.*)"]
        else:
            ext, filters = ".wav", ["WAV Audio (*.wav)", "All Files (*.*)"]
        filepath = self._ask_save_path("Save Speech Audio", self._default_name("adits_voice_studio", ext),
                                       filters, ext[1:])
        if not filepath:
            return
        try:
            os.makedirs(os.path.dirname(filepath), exist_ok=True)
            src = self._temp_mp3 if (filepath.lower().endswith(".mp3") and has_mp3) else sf
            shutil.copy2(src, filepath)
            if self._temp_srt and os.path.exists(self._temp_srt):
                shutil.copy2(self._temp_srt, os.path.splitext(filepath)[0] + ".srt")
                self.set_status(f"💾  Saved audio & subtitles to {os.path.basename(filepath)}",
                                COLORS["success"])
            else:
                self.set_status(f"💾  Saved to {os.path.basename(filepath)}", COLORS["success"])
            self._final_saved_file = filepath
            self._play_panel.open_folder_btn.setEnabled(True)
        except Exception as e:
            self.set_status(f"❌  Save error: {e}", COLORS["error"])

    def _on_save_srt(self):
        sf = self._temp_srt
        if not sf or not os.path.exists(sf):
            self.set_status("⚠  No subtitle to save", COLORS["warning"])
            return
        filepath = self._ask_save_path("Save Subtitles (SRT)", self._default_name("subtitles", ".srt"),
                                       ["SRT Subtitles (*.srt)", "All Files (*.*)"], "srt")
        if not filepath:
            return
        try:
            shutil.copy2(sf, filepath)
            self._final_saved_file = filepath
            self._play_panel.open_folder_btn.setEnabled(True)
            self.set_status(f"💾  Saved SRT to {os.path.basename(filepath)}", COLORS["success"])
        except Exception as e:
            self.set_status(f"❌  Save error: {e}", COLORS["error"])

    def _on_open_folder(self):
        # Before the first save, open the save folder rather than the temp dir.
        target = self._final_saved_file
        try:
            if target and os.path.exists(target):
                subprocess.run(['explorer', '/select,', os.path.normpath(target)])
            else:
                os.startfile(str(paths.output_dir()))
        except Exception as e:
            self.set_status(f"❌  Could not open folder: {e}", COLORS["error"])

    # ── closing ────────────────────────────────────────────────────
    def _cleanup_tmp_dirs(self):
        for d in list(self._tmp_dirs):
            shutil.rmtree(d, ignore_errors=True)
            self._tmp_dirs.discard(d)

    def closeEvent(self, event):
        import pygame
        import core.voice_cloning as voice_cloning
        if voice_cloning.is_training():
            answer = QMessageBox.warning(
                self, "Voice training in progress",
                "A cloned voice is still training.\n\n"
                "Closing Adits Voice Studio stops the training and discards it. Close anyway?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if answer != QMessageBox.Yes:
                event.ignore()
                return
        self._stop_event.set()
        self._play_timer.stop()
        self._text_panel.cleanup()          # closes the dictation mic, stops a transcription
        try:
            pygame.mixer.quit()
        except Exception:
            pass
        try:
            voice_cloning.shutdown_client()
        except Exception as e:
            print(f"Voice cloning shutdown failed: {e}")
        self._cleanup_tmp_dirs()
        event.accept()


def run(on_shown=None) -> int:
    """Start the Qt UI; returns the exit code. on_shown runs once the window is up."""
    import atexit
    app = QApplication.instance() or QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setFont(theme.font("body"))
    app.setStyleSheet(theme.stylesheet())
    bridge.install()
    win = MainWindow()
    atexit.register(win._cleanup_tmp_dirs)
    win.show()
    if on_shown:
        QTimer.singleShot(0, on_shown)
    return app.exec()
