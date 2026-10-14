"""
ui_qt/panels/record_dialog.py
─────────────────────────────
Record a voice clip without leaving the app (Qt).

    🎙 Record a clip                                     [microphone ▾]
    ┌──────────────────── live waveform / preview ─────────────────┐
    └───────────────────────────────────────────────────────────────┘
    ▮▮▮▮▮▮▮▯▯▯ level        00:12
    [● Record] / [■ Stop]                                 tips
    after recording:  [▶] ──────●──────── 0:05 / 0:12   (click the waveform to seek)
                      [⟲ Record again] [🗑 Discard]        [✓ Add to clips]

Behavior follows ui/panels/record_dialog.py (see docs/ui-parity-checklist.md,
"Recorder dialog"). Capture uses PyAudio (PortAudio, default host API) in
callback mode; the callback only appends raw frames, and a 50 ms QTimer on
the GUI thread draws the meter and waveform. The take is saved as 16-bit
mono WAV in out_dir and handed to on_done(path, seconds). Playback uses
pygame Channel 4 from an in-memory buffer, so seeking is just "play from here".

The level meter and waveform are QWidgets drawn with QPainter. Differences
from the CTk dialog, none of which change what the user sees:

* The live waveform reads only the last 12 s of frames on each tick (the
  CTk dialog joined the whole take every 50 ms), and the waveform of a
  finished take is reduced to one min/max pair per pixel once per size, so
  a repaint during playback does not rescan the take.
* While the seek bar is dragged during playback, the time label shows the
  drag position only. The CTk tick also wrote the playing position into it,
  so the label flickered between the two.
* The dialog is not deleted when it closes, so the caller's reference stays
  valid for isVisible(). Its audio buffers are released at close instead.
* Esc does nothing, as in the CTk dialog; the close box asks as before.
* The ▶ / ⏸ button uses Segoe UI Symbol, so ⏸ is a plain glyph like in Tk
  and not Qt's color emoji fallback.
"""

import os
import time
from datetime import datetime

import numpy as np
from PySide6.QtCore import QLineF, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPen
from PySide6.QtWidgets import (QApplication, QComboBox, QDialog, QFrame, QHBoxLayout, QMessageBox,
                               QSizePolicy, QVBoxLayout, QWidget)

from core import paths
from core.constants import COLORS
from ui_qt import theme
from ui_qt.widgets import SeekSlider

# The constants below match ui/panels/record_dialog.py, which cannot be
# imported here (it loads CustomTkinter).
MAX_SECONDS = 600            # stop automatically after 10 minutes
CHUNK = 1024
LIVE_WINDOW = 12.0           # seconds of waveform shown while recording
PLAY_CHANNEL = 4             # pygame channel (1 ambiance, 2 previews, 3 timeline, 4 recorder)
TICK_MS = 50                 # meter, waveform and playhead refresh
RECORD_RED = "#CC0000"

TIP_START = "Speak normally in a quiet room. 10 to 25 s is enough for Quick; training needs 3+ minutes."


def _fmt(t):
    m, s = divmod(int(max(0, t)), 60)
    return f"{m}:{s:02d}"


def _to_mono(raw_bytes, channels):
    """float32 mono from interleaved 16-bit PCM bytes."""
    raw = np.frombuffer(raw_bytes, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        raw = raw[: len(raw) // channels * channels].reshape(-1, channels).mean(axis=1)
    return raw


class _Meter(QWidget):
    """Input level bar, -60 to 0 dB (the CTk 260 x 10 canvas)."""

    def __init__(self):
        super().__init__()
        self.setFixedSize(260, 10)
        self._level = -90.0

    def set_level(self, db):
        self._level = db
        self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        w, h = self.width(), self.height()
        p.fillRect(0, 0, w, h, QColor(COLORS["slider_track"]))
        frac = min(1.0, max(0.0, (self._level + 60) / 60))
        color = (COLORS["error"] if self._level > -1 else
                 COLORS["warning"] if self._level > -6 else COLORS["success"])
        if frac > 0:
            p.fillRect(QRectF(0, 0, w * frac, h), QColor(color))


class _Wave(QWidget):
    """Live waveform while recording, and the take with a playhead after it.

    The dialog hands it samples; min/max per pixel column are computed here
    and cached until the samples or the size change. Mouse presses and drags
    go to the dialog's seek handler, like the canvas bindings did."""

    def __init__(self, dialog):
        super().__init__()
        self._d = dialog
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumHeight(60)
        self._y = None
        self._live = False
        self._window = 1            # samples in LIVE_WINDOW at the current rate
        self._pos = None            # playhead as a fraction of the take, or None
        self._msg = "Press ● Record to start"
        self._cache_key = None
        self._cols = None
        self._msg_font = QFont("Segoe UI")
        self._msg_font.setPointSize(11)

    def sizeHint(self):
        return QSize(400, 150)

    def show_message(self, text):
        self._y, self._pos, self._msg = None, None, text
        self._cache_key = None
        self.update()

    def show_live(self, y, window):
        self._y, self._live, self._window, self._pos = y, True, max(1, window), None
        self._cache_key = None
        self.update()

    def show_take(self, y, pos):
        if y is not self._y or self._live:
            self._cache_key = None
        self._y, self._live, self._pos = y, False, pos
        self.update()

    def set_pos(self, pos):
        self._pos = pos
        self.update()

    def _columns(self, w, h):
        key = (w, h)
        if key == self._cache_key:
            return self._cols
        y = self._y
        if self._live:
            cols = int(w * min(1.0, len(y) / self._window))
        else:
            cols = int(w)
        cols = max(1, cols)
        usable = y[: (len(y) // cols) * cols] if len(y) >= cols else np.pad(y, (0, cols - len(y)))
        block = usable.reshape(cols, -1)
        hi, lo = block.max(axis=1), block.min(axis=1)
        peak = max(0.05, float(np.max(np.abs(y))))
        amp = h / 2 - 6
        mid = h / 2
        top = mid - hi / peak * amp
        bottom = mid - lo / peak * amp + 1
        self._cols = (cols, top, bottom)
        self._cache_key = key
        return self._cols

    def paintEvent(self, _event):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(COLORS["bg_input"]))
        w, h = max(10, self.width()), max(40, self.height())
        if self._y is None or self._y.size == 0:
            p.setPen(QColor(COLORS["text_muted"]))
            p.setFont(self._msg_font)
            p.drawText(QRectF(0, 0, w, h), Qt.AlignCenter, self._msg)
            return
        cols, top, bottom = self._columns(w, h)
        if self._live:
            p.setPen(QPen(QColor(COLORS["error"]), 1))
            p.drawLines([QLineF(x + 0.5, top[x], x + 0.5, bottom[x]) for x in range(cols)])
            return
        pos_x = None if self._pos is None else self._pos * w
        played, rest = [], []
        for x in range(cols):
            (played if pos_x is not None and x <= pos_x else rest).append(
                QLineF(x + 0.5, top[x], x + 0.5, bottom[x]))
        if rest:
            p.setPen(QPen(QColor(COLORS["text_muted"]), 1))
            p.drawLines(rest)
        if played:
            p.setPen(QPen(QColor(COLORS["accent_primary"]), 1))
            p.drawLines(played)
        if pos_x is not None:
            p.setPen(QPen(QColor(COLORS["success"]), 2))
            p.drawLine(QLineF(pos_x, 0, pos_x, h))

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self._d._on_canvas_seek(ev.position().x())

    def mouseMoveEvent(self, ev):
        if ev.buttons() & Qt.LeftButton:
            self._d._on_canvas_seek(ev.position().x())


class RecordDialog(QDialog):
    def __init__(self, parent, out_dir, on_done=None):
        super().__init__(parent)
        self.setObjectName("recordDialog")
        self.setWindowTitle("Record a clip")
        ico = paths.bundle_dir() / "assets" / "adits_voice_studio.ico"
        if ico.exists():
            self.setWindowIcon(QIcon(str(ico)))
        self.resize(720, 430)
        self.setMinimumSize(600, 400)
        self.setModal(True)
        self.setStyleSheet(self._sheet())
        self.out_dir = out_dir
        self.on_done = on_done
        self._pa = None
        self._stream = None
        self._frames = []
        self._rate = 44100
        self._channels = 1
        self._rec_t0 = None
        self._level = -90.0
        self._audio = None            # float32 mono of the finished take
        self._path = None
        self._added = False
        self._seg = None
        self._sound = None
        self._play_t0 = None
        self._play_from = 0.0
        self._seeking = False
        self._devices = {}
        self._closed = False

        self._rec_timer = QTimer(self)
        self._rec_timer.setInterval(TICK_MS)
        self._rec_timer.timeout.connect(self._tick_record)
        self._play_timer = QTimer(self)
        self._play_timer.setInterval(TICK_MS)
        self._play_timer.timeout.connect(self._tick_play)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 12, 16, 12)
        root.setSpacing(0)

        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 4)
        title = theme.label("🎙  Record a clip")
        title.setObjectName("cardTitle")
        head.addWidget(title)
        head.addStretch(1)
        head.addWidget(theme.label("Microphone:", secondary=True, small=True))
        head.addSpacing(6)
        self._dev_menu = QComboBox()
        self._dev_menu.setProperty("onCard", True)
        self._dev_menu.setFont(theme.font("body_small"))
        self._dev_menu.setFixedWidth(260)
        head.addWidget(self._dev_menu)
        root.addLayout(head)
        root.addSpacing(8)

        wrap = QFrame()
        wrap.setObjectName("recWrap")
        wl = QVBoxLayout(wrap)
        wl.setContentsMargins(8, 8, 8, 8)
        wl.setSpacing(4)
        self.canvas = _Wave(self)
        wl.addWidget(self.canvas, 1)
        meter_row = QHBoxLayout()
        meter_row.setContentsMargins(0, 0, 0, 0)
        meter_row.setSpacing(10)
        self._meter = _Meter()
        meter_row.addWidget(self._meter, 0, Qt.AlignVCenter)
        self._time_lbl = theme.label("0:00")
        theme.icon_font(self._time_lbl, "Segoe UI", 15, bold=True)
        meter_row.addWidget(self._time_lbl)
        meter_row.addStretch(1)
        self._info = theme.label("", muted=True, small=True)
        meter_row.addWidget(self._info)
        wl.addLayout(meter_row)
        root.addWidget(wrap, 1)
        root.addSpacing(8)

        # record controls
        self._rec_row = QWidget()
        rr = QHBoxLayout(self._rec_row)
        rr.setContentsMargins(0, 0, 0, 4)
        rr.setSpacing(12)
        self._rec_btn = theme.button("●  Record", theme.ROLE_DANGER)
        self._rec_btn.setFixedWidth(130)
        self._rec_btn.clicked.connect(self._toggle_record)
        rr.addWidget(self._rec_btn)
        self._tip = theme.label(TIP_START, muted=True, small=True)
        self._tip.setWordWrap(True)
        self._tip.setMaximumWidth(480)
        rr.addWidget(self._tip, 10)   # wraps at 480 px, like wraplength=480
        rr.addStretch(1)              # takes what is left past 480 px
        root.addWidget(self._rec_row)

        # preview controls (shown after a take)
        self._play_row = QWidget()
        pr = QHBoxLayout(self._play_row)
        pr.setContentsMargins(0, 4, 0, 4)
        pr.setSpacing(10)
        self._play_btn = theme.button("▶", theme.ROLE_OUTLINE)
        self._play_btn.setObjectName("recPlay")
        self._play_btn.setFixedWidth(40)
        # Segoe UI Symbol draws ▶ and ⏸ as plain glyphs; the default font
        # falls back to the color emoji for ⏸.
        theme.icon_font(self._play_btn, "Segoe UI Symbol", 13)
        self._play_btn.clicked.connect(self._toggle_play)
        pr.addWidget(self._play_btn)
        self._seek = SeekSlider()
        self._seek.setFixedWidth(420)
        self._seek.sliderPressed.connect(lambda: self._on_slider(self._seek.seconds()))
        self._seek.sliderMoved.connect(lambda v: self._on_slider(v / 1000.0))
        self._seek.sliderReleased.connect(self._end_slider)
        pr.addWidget(self._seek)
        self._pos_lbl = theme.label("0:00 / 0:00", secondary=True, small=True)
        pr.addWidget(self._pos_lbl)
        pr.addStretch(1)
        root.addWidget(self._play_row)

        self._done_row = QWidget()
        dr = QHBoxLayout(self._done_row)
        dr.setContentsMargins(0, 4, 0, 0)
        dr.setSpacing(8)
        self._again_btn = theme.button("⟲  Record again", theme.ROLE_OUTLINE)
        self._again_btn.setFixedWidth(140)
        self._again_btn.clicked.connect(self._record_again)
        dr.addWidget(self._again_btn)
        self._discard_btn = theme.button("🗑  Discard", theme.ROLE_STOP)
        self._discard_btn.setFixedWidth(110)
        self._discard_btn.clicked.connect(self._discard)
        dr.addWidget(self._discard_btn)
        dr.addStretch(1)
        self._add_btn = theme.button("✓  Add to clips", theme.ROLE_PRIMARY)
        self._add_btn.setFixedWidth(150)
        self._add_btn.clicked.connect(self._add)
        dr.addWidget(self._add_btn)
        root.addWidget(self._done_row)
        self._play_row.hide()
        self._done_row.hide()

        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self._release)   # app closing with the recorder open
        self._load_devices()
        self._draw_meter()

    @staticmethod
    def _sheet():
        c = COLORS
        return f"""
QDialog#recordDialog {{ background: {c['bg_dark']}; }}
QFrame#recWrap {{
    background: {c['bg_card']}; border: 1px solid {c['border']}; border-radius: 12px;
}}
QFrame#recWrap QLabel {{ background: transparent; }}
QPushButton#recPlay {{ background: {c['bg_card']}; border: 1px solid {c['success']}; padding: 0; }}
QPushButton#recPlay:hover {{ background: {c['success']}; }}
"""

    # ── devices ────────────────────────────────────────────────────
    def _load_devices(self):
        """Input devices of the default host API (MME on Windows), default first."""
        self._devices = {}
        try:
            import pyaudio
        except ImportError:
            self._fail("Recording needs the PyAudio package. Install it with:\n"
                       ".\\venv_311\\Scripts\\python.exe -m pip install pyaudio")
            return
        pa = pyaudio.PyAudio()
        try:
            default = pa.get_default_input_device_info()
        except (IOError, OSError):
            pa.terminate()
            self._fail("No microphone was found. Connect one and open the recorder again.")
            return
        for i in range(pa.get_device_count()):
            d = pa.get_device_info_by_index(i)
            if d["maxInputChannels"] > 0 and d["hostApi"] == default["hostApi"] and "Mapper" not in d["name"]:
                self._devices[d["name"]] = (i, int(d["defaultSampleRate"]) or 44100,
                                            int(d["maxInputChannels"]))
        pa.terminate()
        if not self._devices:
            self._devices[default["name"]] = (default["index"], int(default["defaultSampleRate"]) or 44100,
                                              int(default["maxInputChannels"]))
        names = list(self._devices)
        first = default["name"] if default["name"] in self._devices else names[0]
        self._dev_menu.clear()
        self._dev_menu.addItems(names)
        self._dev_menu.setCurrentText(first)

    def _fail(self, text):
        self._set_tip(text, COLORS["error"])
        self._rec_btn.setEnabled(False)

    def _set_tip(self, text, color):
        self._tip.setText(text)
        theme.set_color(self._tip, color)

    # ── recording ──────────────────────────────────────────────────
    def _toggle_record(self):
        if self._stream is None:
            self._start()
        else:
            self._stop_recording()

    def _on_frames(self, in_data):
        """PortAudio callback thread: keep the raw block, nothing else."""
        self._frames.append(in_data)

    def _start(self):
        import pyaudio
        self._stop_play()
        name = self._dev_menu.currentText()
        if name not in self._devices:
            return
        index, rate, max_ch = self._devices[name]
        self._frames = []
        self._audio = None
        self._pa = pyaudio.PyAudio()

        def callback(in_data, frame_count, time_info, status):
            self._on_frames(in_data)
            return (None, pyaudio.paContinue)

        last_err = None
        for ch in (1, min(2, max_ch)):
            try:
                self._stream = self._pa.open(format=pyaudio.paInt16, channels=ch, rate=rate, input=True,
                                             input_device_index=index, frames_per_buffer=CHUNK,
                                             stream_callback=callback)
                self._channels = ch
                break
            except (IOError, OSError, ValueError) as e:
                last_err = e
                self._stream = None
        if self._stream is None:
            self._pa.terminate()
            self._pa = None
            QMessageBox.critical(self, "Recording failed", f"Could not open {name}:\n{last_err}")
            return
        self._rate = rate
        self._rec_t0 = time.monotonic()
        self._stream.start_stream()
        self._rec_btn.setText("■  Stop")
        self._rec_btn.setProperty("role", theme.ROLE_STOP)
        theme.repolish(self._rec_btn)
        self._dev_menu.setEnabled(False)
        self._play_row.hide()
        self._done_row.hide()
        self._set_tip("Recording… press Stop when you are done.", COLORS["error"])
        self._rec_timer.start()
        self._tick_record()

    def _samples(self):
        """float32 mono of everything recorded so far."""
        if not self._frames:
            return np.zeros(0, dtype=np.float32)
        return _to_mono(b"".join(self._frames), self._channels)

    def _live_samples(self):
        """float32 mono of about the last LIVE_WINDOW seconds (only the frames needed)."""
        frames = self._frames[:]            # the callback may append meanwhile
        need = int(LIVE_WINDOW * self._rate) * self._channels * 2
        tail, size = [], 0
        for f in reversed(frames):
            tail.append(f)
            size += len(f)
            if size >= need:
                break
        if not tail:
            return np.zeros(0, dtype=np.float32)
        y = _to_mono(b"".join(reversed(tail)), self._channels)
        return y[-int(LIVE_WINDOW * self._rate):]

    def _tick_record(self):
        if self._stream is None:
            self._rec_timer.stop()
            return
        elapsed = time.monotonic() - self._rec_t0
        if self._frames:
            last = np.frombuffer(self._frames[-1], dtype=np.int16).astype(np.float32) / 32768.0
            peak = float(np.max(np.abs(last))) if last.size else 0.0
            self._level = 20 * np.log10(peak + 1e-9)
        self._time_lbl.setText(_fmt(elapsed))
        self._draw_meter()
        self._draw()
        if elapsed >= MAX_SECONDS:
            self._stop_recording()

    def _close_stream(self):
        self._rec_timer.stop()
        if self._stream is not None:
            try:
                self._stream.stop_stream()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        if self._pa is not None:
            try:
                self._pa.terminate()
            except Exception:
                pass
            self._pa = None

    def _stop_recording(self):
        self._close_stream()
        self._level = -90.0
        self._draw_meter()
        self._rec_btn.setText("●  Record")
        self._rec_btn.setProperty("role", theme.ROLE_DANGER)
        theme.repolish(self._rec_btn)
        self._dev_menu.setEnabled(True)
        audio = self._samples()
        if audio.size < self._rate * 0.5:
            self._set_tip("That take was shorter than half a second. Try again.", COLORS["warning"])
            self._draw()
            return
        self._set_take(audio)

    def _set_take(self, audio):
        """Save the take, then switch to the preview controls."""
        import soundfile as sf
        from pydub import AudioSegment
        self._audio = audio.astype(np.float32)
        os.makedirs(self.out_dir, exist_ok=True)
        if self._path and not self._added and os.path.exists(self._path):
            os.remove(self._path)               # replace an earlier, unused take
        name = f"Recording {datetime.now().strftime('%Y-%m-%d %H%M%S')}.wav"
        self._path = os.path.join(self.out_dir, name)
        pcm = (np.clip(self._audio, -1, 1) * 32767).astype(np.int16)
        sf.write(self._path, pcm, self._rate, subtype="PCM_16")
        self._added = False
        self._seg = AudioSegment(data=pcm.tobytes(), sample_width=2, frame_rate=self._rate, channels=1)
        dur = len(self._audio) / self._rate
        peak = 20 * np.log10(float(np.max(np.abs(self._audio))) + 1e-9)
        if peak > -0.5:
            tip, color = "Some parts clipped (too loud). Move back a little or lower the mic gain.", COLORS["warning"]
        elif peak < -30:
            tip, color = "The take is very quiet. Move closer or raise the mic gain.", COLORS["warning"]
        else:
            tip, color = "Listen back, then add it to the clips or record again.", COLORS["text_secondary"]
        self._set_tip(tip, color)
        self._info.setText(f"{dur:.1f} s · peak {peak:.0f} dBFS · {self._rate // 1000} kHz")
        self._time_lbl.setText(_fmt(dur))
        self._seek.set_length(max(0.1, dur))
        self._seek.set_seconds(0.0)
        self._play_from = 0.0
        self._update_pos(0.0)
        self._play_row.show()
        self._done_row.show()
        self._draw()

    # ── preview playback with seek ─────────────────────────────────
    def _duration(self):
        return len(self._audio) / self._rate if self._audio is not None else 0.0

    def _position(self):
        if self._play_t0 is None:
            return self._play_from
        return min(self._duration(), self._play_from + time.monotonic() - self._play_t0)

    def _toggle_play(self):
        if self._play_t0 is None:
            self._play_at(self._play_from if self._play_from < self._duration() - 0.05 else 0.0)
        else:
            self._play_from = self._position()
            self._stop_play()

    def _play_at(self, pos):
        try:
            import pygame
            if self._seg is None or not pygame.mixer.get_init():
                return
            if pygame.mixer.get_num_channels() <= PLAY_CHANNEL:
                pygame.mixer.set_num_channels(PLAY_CHANNEL + 1)
            freq, _size, ch = pygame.mixer.get_init()
            part = self._seg[int(pos * 1000):].set_frame_rate(freq).set_channels(ch).set_sample_width(2)
            self._stop_play()
            self._sound = pygame.mixer.Sound(buffer=part.raw_data)
            pygame.mixer.Channel(PLAY_CHANNEL).play(self._sound)
            self._play_from, self._play_t0 = pos, time.monotonic()
            self._play_btn.setText("⏸")
            self._play_timer.start()
            self._tick_play()
        except Exception as e:
            print(f"Recording preview failed: {e}")

    def _stop_play(self):
        self._play_t0 = None
        self._play_timer.stop()
        try:
            import pygame
            if pygame.mixer.get_init() and pygame.mixer.get_num_channels() > PLAY_CHANNEL:
                pygame.mixer.Channel(PLAY_CHANNEL).stop()
        except Exception:
            pass
        self._play_btn.setText("▶")

    def _tick_play(self):
        if self._play_t0 is None:
            self._play_timer.stop()
            return
        pos = self._position()
        if pos >= self._duration() - 0.01:
            self._play_from = 0.0
            self._stop_play()
            pos = self._duration()
        if not self._seeking:               # while the bar is dragged, it shows the drag
            self._seek.set_seconds(pos)
            self._update_pos(pos)
        self._draw()

    def _update_pos(self, pos):
        self._pos_lbl.setText(f"{_fmt(pos)} / {_fmt(self._duration())}")

    def _seek_to(self, pos):
        pos = min(max(0.0, pos), self._duration())
        playing = self._play_t0 is not None
        self._play_from = pos
        self._seek.set_seconds(pos)
        self._update_pos(pos)
        if playing:
            self._play_at(pos)
        else:
            self._draw()

    def _on_slider(self, value):
        self._seeking = True
        self._update_pos(float(value))
        self._play_from = float(value)
        if self._play_t0 is None:
            self._draw()

    def _end_slider(self):
        self._seeking = False
        self._seek_to(self._seek.seconds())

    def _on_canvas_seek(self, x):
        if self._audio is None or self._stream is not None:
            return
        w = max(1, self.canvas.width())
        self._seek_to(x / w * self._duration())

    # ── drawing ────────────────────────────────────────────────────
    def _draw_meter(self):
        self._meter.set_level(self._level)

    def _draw(self):
        recording = self._stream is not None
        y = self._live_samples() if recording else self._audio
        if y is None or y.size == 0:
            self.canvas.show_message("Press ● Record to start" if not recording else "Listening…")
            return
        if recording:                         # scrolling window of the last LIVE_WINDOW seconds
            self.canvas.show_live(y, int(LIVE_WINDOW * self._rate))
        else:
            self.canvas.show_take(y, self._position() / max(1e-9, self._duration()))

    # ── finish ─────────────────────────────────────────────────────
    def _record_again(self):
        self._stop_play()
        self._start()

    def _discard(self):
        self._stop_play()
        if self._path and not self._added and os.path.exists(self._path):
            os.remove(self._path)
        self._path = None
        self._audio = None
        self._seg = None
        self._play_row.hide()
        self._done_row.hide()
        self._info.setText("")
        self._time_lbl.setText("0:00")
        self._set_tip("Take discarded. Press ● Record to try again.", COLORS["text_muted"])
        self._draw()

    def _add(self):
        if not self._path:
            return
        self._stop_play()
        self._added = True
        if self.on_done:
            self.on_done(self._path, self._duration())
        self._release()
        self.accept()

    def keyPressEvent(self, ev):
        if ev.key() == Qt.Key_Escape:       # the CTk dialog ignores Esc too
            ev.accept()
            return
        super().keyPressEvent(ev)

    def reject(self):
        """The close box (QDialog.closeEvent calls this): ask before dropping a take."""
        if self._stream is not None:
            self._stop_recording()
        if self._path and not self._added:
            answer = QMessageBox.question(self, "Discard recording?",
                                          "This take has not been added to the clips. Discard it?",
                                          QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if answer != QMessageBox.Yes:
                return
            if os.path.exists(self._path):
                os.remove(self._path)
        self._release()
        super().reject()

    def _release(self):
        """Stop audio and the microphone and free the take's buffers (the CTk destroy())."""
        if self._closed:
            return
        self._closed = True
        try:
            self._stop_play()
            self._sound = None
            self._close_stream()
            self._seg = None
            self._audio = None
            self._frames = []
            self.canvas.show_message("")
        except RuntimeError:
            pass                            # widgets already deleted at app exit
