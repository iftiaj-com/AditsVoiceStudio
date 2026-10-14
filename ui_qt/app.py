"""
ui_qt/app.py
────────────
MainWindow: the Qt main window, started with `tts_app.py --qt`.

Header, two-column layout and status bar, with Text Input and Playback &
Export on the left and TTS Engine, Customization (with Clone Voice) and Voice
Effects on the right (phases 2 to 4 of docs/pyside6-migration-plan.md).

Threads: the generation worker never touches a widget. It reports through
ui_qt.bridge.ui(), which runs the call on the GUI thread.

Start-up: the window opens at once with Text Input, Playback (Generate reads
"Starting…") and a loading card on the right. A background thread imports
the engine adapters and the pipeline and reads the system voices (about 2 s);
the right-hand panels are then built on the GUI thread, one per event-loop
turn so the window keeps painting. No TTS library or model loads here: the
adapters, librosa and torch load on first use, so the first Generate with an
engine takes longer. This module imports nothing heavy at the top.
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

from core import paths
from core.constants import COLORS
from core.playback import Player
from core.version import APP_TITLE
from ui_qt import bridge, icons, theme
from ui_qt.bridge import ui
from ui_qt.panels.playback_panel import PlaybackPanel
from ui_qt.panels.text_panel import TextPanel
from ui_qt.widgets import IconLabel

SITE_URL = "https://www.iftiaj.com"


class MainWindow(QMainWindow):

    def __init__(self, background=True):
        """background=False loads the engines before returning (self-test)."""
        super().__init__()
        import pygame

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

        self._available_engines = []
        self.synth = None
        self._system_voices = []
        self._engine_panel = self._cust_panel = self._effects_panel = None
        self._loaded = False
        self._load_error = None

        self._audio_error = None
        try:
            pygame.mixer.init()
        except Exception as e:
            self._audio_error = str(e)
            print(f"Audio output unavailable: {e}")
        self._build_ui()
        if background:
            threading.Thread(target=self._load_worker, daemon=True, name="load-engines").start()
        else:
            self._on_engines_loaded(*self._load_engines())
            while not self._loaded:
                QApplication.processEvents()

    # ── engines, loaded in the background ──────────────────────────
    @staticmethod
    def _load_engines():
        """Engine discovery and the panels' imports. Runs off the GUI thread."""
        from core import pipeline  # noqa: F401
        import core.voice_cloning  # noqa: F401  (is_training / shutdown_client at close)
        from engines.pyttsx3_engine import Pyttsx3Engine
        from engines.registry import get_available_engines
        import ui_qt.panels.customization_panel  # noqa: F401
        import ui_qt.panels.effects_panel  # noqa: F401
        import ui_qt.panels.engine_panel  # noqa: F401
        engines = get_available_engines()
        voices = Pyttsx3Engine.load_system_voices() if Pyttsx3Engine.is_available() else []
        return engines, voices

    def _load_worker(self):
        try:
            res = self._load_engines()
        except Exception as e:
            import traceback
            traceback.print_exc()
            ui(self._on_load_failed, f"{type(e).__name__}: {e}")
            return
        ui(self._on_engines_loaded, *res)

    def _on_load_failed(self, msg):
        self._load_error = msg
        self._loading_label.setText("❌  The engines could not be loaded: " + msg)
        self._loading_label.set_icon_color(COLORS["error"])
        self.set_status(f"❌  Engines failed to load: {msg}", COLORS["error"])

    def _on_engines_loaded(self, engines, voices):
        """Build the right-hand panels, one per event-loop turn."""
        from core.synth import Synthesizer
        from ui_qt.panels.customization_panel import CustomizationPanel
        from ui_qt.panels.effects_panel import EffectsPanel
        from ui_qt.panels.engine_panel import EnginePanel

        self._available_engines = engines
        self._system_voices = voices
        self.synth = Synthesizer(engines)
        self._loading_label.setText("⌛  Building the panels…")

        def engine():
            self._engine_panel = EnginePanel(self._available_engines, self._system_voices)
            self._right_l.insertWidget(0, self._engine_panel.frame)
            QTimer.singleShot(0, customization)

        def customization():
            self._cust_panel = CustomizationPanel(app=self)
            self._right_l.insertWidget(1, self._cust_panel.frame)
            QTimer.singleShot(0, effects)

        def effects():
            self._effects_panel = EffectsPanel(on_edit_clones=self._open_voice_manager)
            if self._cust_panel.clone_group is not None:
                self._cust_panel.clone_group.attach(self._effects_panel)
            self._right_l.insertWidget(2, self._effects_panel.frame)
            self._loading_card.hide()
            self._loading_card.deleteLater()
            self._loaded = True
            self._play_panel.set_loading(False)
            if self._audio_error:
                self.set_status(f"⚠  No audio output device — playback disabled ({self._audio_error})",
                                COLORS["warning"])
            else:
                self.set_status("Ready — Enter text and click Generate")

        engine()

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
            on_play=self._on_play, on_stop=self._on_stop, on_save_audio=self._on_save,
            on_save_srt=self._on_save_srt, on_open_folder=self._on_open_folder)
        self._play_panel.set_loading(True)
        left_l.addWidget(self._play_panel.frame)

        right = QScrollArea()
        right.setWidgetResizable(True)
        right.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        right.setFrameShape(QFrame.NoFrame)
        right_body = QWidget()
        self._right_l = QVBoxLayout(right_body)
        self._right_l.setContentsMargins(0, 0, 6, 0)
        self._right_l.setSpacing(12)
        # Until the engines are loaded: one card that says so.
        self._loading_card, lc = theme.card("🔊  TTS Engine")
        self._loading_label = theme.label("⌛  Loading…", secondary=True)
        self._loading_label.setWordWrap(True)
        lc.addWidget(self._loading_label)
        note = theme.label("Voice models load when you first generate with them. "
                           "You can type or paste text meanwhile.", muted=True, small=True)
        note.setWordWrap(True)
        lc.addWidget(note)
        self._right_l.addWidget(self._loading_card)
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
        self._status_label = IconLabel("Ready — Enter text and click Generate")
        self._status_label.setObjectName("statusText")
        self._status_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        sb.addWidget(self._status_label, 1)
        link = QLabel("www.iftiaj.com")
        link.setObjectName("siteLink")
        link.setCursor(Qt.PointingHandCursor)
        link.mousePressEvent = lambda e: QDesktopServices.openUrl(QUrl(SITE_URL))
        sb.addWidget(link)
        outer.addWidget(bar)
        self.set_status("⌛  Starting…", COLORS["accent_primary"])

    def refresh_cloned_voices(self, select=None):
        """Re-read the voice library into the registry, the tiles and the Clone Voice group.

        select: slug of a voice to switch on afterwards (e.g. just created).
        """
        from effects.cloned_voices import refresh_cloned_voices
        from effects.registry import EFFECTS_REGISTRY
        try:
            refresh_cloned_voices(EFFECTS_REGISTRY)
        except Exception as e:
            print(f"Cloned voice refresh failed: {e}")
        if not self._loaded:
            return
        self._effects_panel.sync()
        group = self._cust_panel.clone_group
        if group is not None:
            group.refresh()
        if select:
            self._effects_panel.set_active(f"clone:{select}", True)

    def _open_voice_manager(self):
        """✏️ in Cloned Voices: rename, re-base, tune or delete voices."""
        from ui_qt.panels.clone_voice_dialog import ManageVoicesDialog
        win = getattr(self, "_voice_manager", None)
        if win is not None and win.isVisible():
            win.raise_()
            win.activateWindow()
            return
        self._voice_manager = ManageVoicesDialog(self)
        self._voice_manager.show()

    # ── what panels may use (same names as AditsVoiceStudioApp) ────
    @property
    def engine_panel(self):
        return self._engine_panel

    @property
    def text_panel(self):
        return self._text_panel

    @property
    def cust_panel(self):
        return self._cust_panel

    @property
    def system_voices(self):
        return self._system_voices

    @property
    def is_generating(self):
        return self._is_generating

    def set_status(self, text, color=None):
        """Status bar: the message's leading emoji becomes an icon in *color*
        (a dot when it has none); the words stay in the secondary text color."""
        icon, plain = icons.split(text)
        self._status_label.setText(text if icon else "●  " + plain)
        self._status_label.set_icon_color(color or COLORS["text_muted"])
        self._status_label.setToolTip(plain)

    # ── generation ─────────────────────────────────────────────────
    def _on_generate(self):
        self._start_generation(preview=False)

    def _on_preview(self):
        """Fast preview: only the first sentence, played when done."""
        self._start_generation(preview=True)

    def _current_request(self, text, preview):
        """Settings for a run, read here on the GUI thread; the worker gets plain values."""
        from core import pipeline
        return pipeline.GenerationRequest(
            text=text,
            eng_settings=self._engine_panel.get_settings(),
            cust_settings=self._cust_panel.get_settings(),
            fx_active=self._effects_panel.get_active_effects(),
            preview=preview,
        )

    def _start_generation(self, preview=False):
        from core import pipeline
        if self._is_generating or not self._loaded:
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

        if self._player.stop():                     # Generate while the last clip plays
            self._play_timer.stop()
            self._resume_ambiance()
        self._play_panel.set_generating(True, preview=preview)
        self.set_status("Generating preview…" if preview else "Generating speech…", COLORS["warning"])

        threading.Thread(target=self._generate_worker, args=(req, tmp_dir), daemon=True,
                         name="generate").start()

    def _generate_worker(self, req, tmp_dir):
        """Runs core.pipeline on this thread; reports to the GUI thread with ui()."""
        from core import pipeline

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
            self._play_panel.set_cancelling()

    def _reset_generate_buttons(self):
        self._is_generating = False
        self._play_panel.set_generating(False)

    def _on_generate_finish(self, playback_file, autoplay=False):
        self._reset_generate_buttons()
        pp = self._play_panel
        pp.set_outputs(mp3=False, wav=False, srt=False)
        if playback_file is None:
            self.set_status("⏹  Generation cancelled", COLORS["warning"])
            return
        self._temp_playback_file = playback_file
        pp.play_btn.setEnabled(True)
        pp.set_outputs(mp3=self._has(self._temp_mp3), wav=bool(self._wav_source()),
                       srt=self._has(self._temp_srt))
        if self._gen_notice:
            self.set_status(f"⚠  Speech generated. {self._gen_notice}", COLORS["warning"])
        else:
            self.set_status("✅  Speech generated successfully!", COLORS["success"])
        if autoplay:
            self._on_play()

    def _on_generate_error(self, error_msg):
        self._reset_generate_buttons()
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
        amb = self._cust_panel.ambiance_channel
        if amb and amb.get_busy():
            amb.pause()
        self._player.play(pf)
        self._play_panel.set_playing(True)
        self.set_status("🎵  Playing audio…", COLORS["accent_primary"])
        self._play_timer.start()

    def _poll_playback(self):
        """Fill the Stop button with the playback position while the audio plays."""
        try:
            playing = self._player.is_playing()
            if playing:
                self._play_panel.set_play_progress(self._player.fraction())
        except Exception:
            playing = False
        if not playing:
            self._play_timer.stop()
            self._on_playback_done()

    def _reset_play_buttons(self):
        self._play_panel.set_playing(False)

    def _resume_ambiance(self):
        amb = self._cust_panel.ambiance_channel
        if amb:
            amb.unpause()

    def _on_playback_done(self):
        self._reset_play_buttons()
        self.set_status("✅  Playback complete", COLORS["success"])
        self._resume_ambiance()

    def _on_stop(self):
        if self._player.stop():
            self._play_timer.stop()
            self._reset_play_buttons()
            self.set_status("⏹  Playback stopped", COLORS["text_muted"])
            self._resume_ambiance()

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

    @staticmethod
    def _has(path):
        return bool(path and os.path.exists(path))

    def _wav_source(self):
        """A WAV of the last generation, or an MP3 to convert (online engines without effects)."""
        for f in (self._temp_save_file, self._temp_playback_file):
            if self._has(f) and f.lower().endswith(".wav"):
                return f
        return self._temp_save_file if self._has(self._temp_save_file) else None

    def _on_save(self, fmt="mp3"):
        """Save menu → MP3 or WAV. The SRT, when there is one, goes beside it."""
        src = self._temp_mp3 if fmt == "mp3" else self._wav_source()
        if not self._has(src):
            self.set_status("⚠  No audio to save — generate first", COLORS["warning"])
            return
        ext = "." + fmt
        filters = [f"{fmt.upper()} Audio (*{ext})", "All Files (*.*)"]
        filepath = self._ask_save_path(f"Save Speech Audio ({fmt.upper()})",
                                       self._default_name("adits_voice_studio", ext), filters, fmt)
        if not filepath:
            return
        try:
            os.makedirs(os.path.dirname(filepath), exist_ok=True)
            if fmt == "wav" and not src.lower().endswith(".wav"):
                from core.audio_io import load_segment
                load_segment(src).export(filepath, format="wav")
            else:
                shutil.copy2(src, filepath)
            if self._temp_srt and os.path.exists(self._temp_srt):
                shutil.copy2(self._temp_srt, os.path.splitext(filepath)[0] + ".srt")
                self.set_status(f"💾  Saved audio & subtitles to {os.path.basename(filepath)}",
                                COLORS["success"])
            else:
                self.set_status(f"💾  Saved to {os.path.basename(filepath)}", COLORS["success"])
            self._final_saved_file = filepath
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
        cust = self._cust_panel                 # None if closed while the engines load
        if cust is not None and cust.clone_group is not None:
            cust.clone_group.cleanup()      # stops clone tasks and previews
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
        if cust is not None:
            cust.cleanup()                  # ambiance preview temp files
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
