"""
ui/panels/record_dialog.py  –  record a voice clip without leaving the app

    🎙 Record a clip                                     [microphone ▾]
    ┌──────────────────── live waveform / preview ─────────────────┐
    └───────────────────────────────────────────────────────────────┘
    ▮▮▮▮▮▮▮▯▯▯ level        00:12
    [● Record] / [■ Stop]                                 tips
    after recording:  [▶] ──────●──────── 0:05 / 0:12   (click the waveform to seek)
                      [⟲ Record again] [🗑 Discard]        [✓ Add to clips]

Capture uses PyAudio (PortAudio, default host API) in callback mode; the
callback only appends raw frames, and a 50 ms Tk timer draws the meter and
waveform. The take is saved as 16-bit mono WAV in <runtime root>/recordings/
and handed to on_done(path, seconds). Playback uses pygame Channel 4 from
an in-memory buffer, so seeking is just "play from here".
"""

import os
import time
from datetime import datetime
from tkinter import messagebox

import customtkinter as ctk
import numpy as np

from core.constants import COLORS, FONTS

MAX_SECONDS = 600            # stop automatically after 10 minutes
CHUNK = 1024
LIVE_WINDOW = 12.0           # seconds of waveform shown while recording


def _fmt(t):
    m, s = divmod(int(max(0, t)), 60)
    return f"{m}:{s:02d}"


class RecordDialog(ctk.CTkToplevel):
    def __init__(self, master, out_dir, on_done=None):
        super().__init__(master)
        self.title("Record a clip")
        self.configure(fg_color=COLORS["bg_dark"])
        self.geometry("720x430")
        self.minsize(600, 400)
        self.transient(master)
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

        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(fill="x", padx=16, pady=(12, 4))
        ctk.CTkLabel(head, text="🎙  Record a clip", font=FONTS["subtitle"],
                     text_color=COLORS["text_primary"]).pack(side="left")
        self._dev_var = ctk.StringVar(value="")
        self._dev_menu = ctk.CTkOptionMenu(
            head, variable=self._dev_var, values=[""], font=FONTS["body_small"], width=260, height=28,
            fg_color=COLORS["bg_card"], button_color=COLORS["accent_primary"],
            button_hover_color=COLORS["accent_secondary"], dropdown_fg_color=COLORS["bg_card"],
            dropdown_hover_color=COLORS["accent_primary"], corner_radius=8)
        self._dev_menu.pack(side="right")
        ctk.CTkLabel(head, text="Microphone:", font=FONTS["body_small"],
                     text_color=COLORS["text_secondary"]).pack(side="right", padx=(0, 6))

        wrap = ctk.CTkFrame(self, fg_color=COLORS["bg_card"], corner_radius=12, border_width=1,
                            border_color=COLORS["border"])
        wrap.pack(fill="both", expand=True, padx=16, pady=8)
        self.canvas = ctk.CTkCanvas(wrap, bg=COLORS["bg_input"], highlightthickness=0, height=150)
        self.canvas.pack(fill="both", expand=True, padx=8, pady=(8, 4))
        self.canvas.bind("<ButtonPress-1>", self._on_canvas_seek)
        self.canvas.bind("<B1-Motion>", self._on_canvas_seek)
        self.canvas.bind("<Configure>", lambda e: self._draw())

        meter_row = ctk.CTkFrame(wrap, fg_color="transparent")
        meter_row.pack(fill="x", padx=8, pady=(0, 8))
        self._meter = ctk.CTkCanvas(meter_row, bg=COLORS["bg_input"], highlightthickness=0, height=10, width=260)
        self._meter.pack(side="left", padx=(0, 10))
        self._time_lbl = ctk.CTkLabel(meter_row, text="0:00", font=("Segoe UI", 15, "bold"),
                                      text_color=COLORS["text_primary"])
        self._time_lbl.pack(side="left")
        self._info = ctk.CTkLabel(meter_row, text="", font=FONTS["body_small"], text_color=COLORS["text_muted"])
        self._info.pack(side="right")

        # record controls
        self._rec_row = ctk.CTkFrame(self, fg_color="transparent")
        self._rec_row.pack(fill="x", padx=16, pady=(0, 4))
        self._rec_btn = ctk.CTkButton(self._rec_row, text="●  Record", font=FONTS["button"], width=130,
                                      height=34, corner_radius=10, fg_color="#CC0000",
                                      hover_color=COLORS["error"], command=self._toggle_record)
        self._rec_btn.pack(side="left")
        self._tip = ctk.CTkLabel(self._rec_row, text="Speak normally in a quiet room. 10 to 25 s is enough for "
                                                     "Quick; training needs 3+ minutes.",
                                 font=FONTS["body_small"], text_color=COLORS["text_muted"],
                                 wraplength=480, justify="left")
        self._tip.pack(side="left", padx=12)

        # preview controls (shown after a take)
        self._play_row = ctk.CTkFrame(self, fg_color="transparent")
        self._play_btn = ctk.CTkButton(self._play_row, text="▶", font=FONTS["button"], width=40, height=30,
                                       corner_radius=8, fg_color=COLORS["bg_card"], hover_color=COLORS["success"],
                                       border_color=COLORS["success"], border_width=1, command=self._toggle_play)
        self._play_btn.pack(side="left")
        self._seek_var = ctk.DoubleVar(value=0.0)
        self._seek = ctk.CTkSlider(self._play_row, from_=0, to=1, variable=self._seek_var, width=420,
                                   button_color=COLORS["accent_primary"], button_hover_color=COLORS["accent_secondary"],
                                   progress_color=COLORS["accent_primary"], fg_color=COLORS["slider_track"],
                                   command=self._on_slider)
        self._seek.pack(side="left", padx=10)
        self._seek.bind("<ButtonRelease-1>", lambda e: self._end_slider())
        self._pos_lbl = ctk.CTkLabel(self._play_row, text="0:00 / 0:00", font=FONTS["body_small"],
                                     text_color=COLORS["text_secondary"])
        self._pos_lbl.pack(side="left")

        self._done_row = ctk.CTkFrame(self, fg_color="transparent")
        self._again_btn = self._outline(self._done_row, "⟲  Record again", self._record_again,
                                        COLORS["accent_primary"], 140)
        self._again_btn.pack(side="left")
        self._outline(self._done_row, "🗑  Discard", self._discard, COLORS["error"], 110).pack(side="left", padx=8)
        ctk.CTkButton(self._done_row, text="✓  Add to clips", font=FONTS["button"], width=150, height=32,
                      corner_radius=8, fg_color=COLORS["accent_primary"], hover_color=COLORS["accent_secondary"],
                      command=self._add).pack(side="right")

        self.protocol("WM_DELETE_WINDOW", self._close)
        self._load_devices()
        self._draw_meter()
        self.after(150, self._grab)

    @staticmethod
    def _outline(parent, text, cmd, color, width):
        return ctk.CTkButton(parent, text=text, font=FONTS["button"], width=width, height=32, corner_radius=8,
                             fg_color=COLORS["bg_card"], hover_color=color, border_color=color, border_width=1,
                             command=cmd)

    def _grab(self):
        try:
            self.lift()
            self.focus_force()
            self.grab_set()
        except Exception:
            pass

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
        self._dev_menu.configure(values=names)
        self._dev_var.set(first)

    def _fail(self, text):
        self._tip.configure(text=text, text_color=COLORS["error"])
        self._rec_btn.configure(state="disabled")

    # ── recording ──────────────────────────────────────────────────
    def _toggle_record(self):
        if self._stream is None:
            self._start()
        else:
            self._stop_recording()

    def _start(self):
        import pyaudio
        self._stop_play()
        name = self._dev_var.get()
        if name not in self._devices:
            return
        index, rate, max_ch = self._devices[name]
        self._frames = []
        self._audio = None
        self._pa = pyaudio.PyAudio()

        def callback(in_data, frame_count, time_info, status):
            self._frames.append(in_data)
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
            messagebox.showerror("Recording failed", f"Could not open {name}:\n{last_err}", parent=self)
            return
        self._rate = rate
        self._rec_t0 = time.monotonic()
        self._stream.start_stream()
        self._rec_btn.configure(text="■  Stop", fg_color=COLORS["bg_card"], hover_color=COLORS["border"],
                                border_color=COLORS["error"], border_width=2)
        self._dev_menu.configure(state="disabled")
        self._play_row.pack_forget()
        self._done_row.pack_forget()
        self._tip.configure(text="Recording… press Stop when you are done.", text_color=COLORS["error"])
        self._tick_record()

    def _samples(self):
        """float32 mono of everything recorded so far."""
        if not self._frames:
            return np.zeros(0, dtype=np.float32)
        raw = np.frombuffer(b"".join(self._frames), dtype=np.int16).astype(np.float32) / 32768.0
        if self._channels > 1:
            raw = raw[: len(raw) // self._channels * self._channels].reshape(-1, self._channels).mean(axis=1)
        return raw

    def _tick_record(self):
        if self._stream is None or not self.winfo_exists():
            return
        elapsed = time.monotonic() - self._rec_t0
        if self._frames:
            last = np.frombuffer(self._frames[-1], dtype=np.int16).astype(np.float32) / 32768.0
            peak = float(np.max(np.abs(last))) if last.size else 0.0
            self._level = 20 * np.log10(peak + 1e-9)
        self._time_lbl.configure(text=_fmt(elapsed))
        self._draw_meter()
        self._draw()
        if elapsed >= MAX_SECONDS:
            self._stop_recording()
            return
        self.after(50, self._tick_record)

    def _stop_recording(self):
        if self._stream is not None:
            try:
                self._stream.stop_stream()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        if self._pa is not None:
            self._pa.terminate()
            self._pa = None
        self._level = -90.0
        self._draw_meter()
        self._rec_btn.configure(text="●  Record", fg_color="#CC0000", hover_color=COLORS["error"], border_width=0)
        self._dev_menu.configure(state="normal")
        audio = self._samples()
        if audio.size < self._rate * 0.5:
            self._tip.configure(text="That take was shorter than half a second. Try again.",
                                text_color=COLORS["warning"])
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
        self._tip.configure(text=tip, text_color=color)
        self._info.configure(text=f"{dur:.1f} s · peak {peak:.0f} dBFS · {self._rate // 1000} kHz")
        self._time_lbl.configure(text=_fmt(dur))
        self._seek.configure(to=max(0.1, dur))
        self._seek_var.set(0.0)
        self._play_from = 0.0
        self._update_pos(0.0)
        self._play_row.pack(fill="x", padx=16, pady=(4, 4))
        self._done_row.pack(fill="x", padx=16, pady=(4, 12))
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
            freq, _size, ch = pygame.mixer.get_init()
            part = self._seg[int(pos * 1000):].set_frame_rate(freq).set_channels(ch).set_sample_width(2)
            self._stop_play()
            self._sound = pygame.mixer.Sound(buffer=part.raw_data)
            pygame.mixer.Channel(4).play(self._sound)
            self._play_from, self._play_t0 = pos, time.monotonic()
            self._play_btn.configure(text="⏸")
            self._tick_play()
        except Exception as e:
            print(f"Recording preview failed: {e}")

    def _stop_play(self):
        self._play_t0 = None
        try:
            import pygame
            if pygame.mixer.get_init():
                pygame.mixer.Channel(4).stop()
        except Exception:
            pass
        if hasattr(self, "_play_btn"):
            self._play_btn.configure(text="▶")

    def _tick_play(self):
        if self._play_t0 is None or not self.winfo_exists():
            return
        pos = self._position()
        if pos >= self._duration() - 0.01:
            self._play_from = 0.0
            self._stop_play()
            pos = self._duration()
        if not self._seeking:
            self._seek_var.set(pos)
        self._update_pos(pos)
        self._draw()
        if self._play_t0 is not None:
            self.after(50, self._tick_play)

    def _update_pos(self, pos):
        self._pos_lbl.configure(text=f"{_fmt(pos)} / {_fmt(self._duration())}")

    def _seek_to(self, pos):
        pos = min(max(0.0, pos), self._duration())
        playing = self._play_t0 is not None
        self._play_from = pos
        self._seek_var.set(pos)
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
        self._seek_to(float(self._seek_var.get()))

    def _on_canvas_seek(self, ev):
        if self._audio is None or self._stream is not None:
            return
        w = max(1, self.canvas.winfo_width())
        self._seek_to(ev.x / w * self._duration())

    # ── drawing ────────────────────────────────────────────────────
    def _draw_meter(self):
        m = self._meter
        m.delete("all")
        w, h = int(m.cget("width")), int(m.cget("height"))
        frac = min(1.0, max(0.0, (self._level + 60) / 60))
        color = COLORS["error"] if self._level > -1 else COLORS["warning"] if self._level > -6 else COLORS["success"]
        m.create_rectangle(0, 0, w, h, fill=COLORS["slider_track"], outline="")
        if frac > 0:
            m.create_rectangle(0, 0, w * frac, h, fill=color, outline="")

    def _draw(self):
        c = self.canvas
        c.delete("all")
        w, h = max(10, c.winfo_width()), max(40, c.winfo_height())
        mid = h / 2
        recording = self._stream is not None
        y = self._samples() if recording else self._audio
        if y is None or y.size == 0:
            c.create_text(w / 2, mid, text="Press ● Record to start" if not recording else "Listening…",
                          fill=COLORS["text_muted"], font=("Segoe UI", 11))
            return
        if recording:                         # scrolling window of the last LIVE_WINDOW seconds
            n = int(LIVE_WINDOW * self._rate)
            y = y[-n:]
            cols = int(w * min(1.0, len(y) / n))
        else:
            cols = int(w)
        cols = max(1, cols)
        usable = y[: (len(y) // cols) * cols] if len(y) >= cols else np.pad(y, (0, cols - len(y)))
        block = usable.reshape(cols, -1)
        hi, lo = block.max(axis=1), block.min(axis=1)
        peak = max(0.05, float(np.max(np.abs(y))))
        amp = h / 2 - 6
        pos_x = None if recording else self._position() / max(1e-9, self._duration()) * w
        for x in range(cols):
            col = COLORS["error"] if recording else (COLORS["accent_primary"] if pos_x is not None and x <= pos_x
                                                     else COLORS["text_muted"])
            c.create_line(x, mid - hi[x] / peak * amp, x, mid - lo[x] / peak * amp + 1, fill=col)
        if pos_x is not None:
            c.create_line(pos_x, 0, pos_x, h, fill=COLORS["success"], width=2)

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
        self._play_row.pack_forget()
        self._done_row.pack_forget()
        self._info.configure(text="")
        self._time_lbl.configure(text="0:00")
        self._tip.configure(text="Take discarded. Press ● Record to try again.", text_color=COLORS["text_muted"])
        self._draw()

    def _add(self):
        if not self._path:
            return
        self._stop_play()
        self._added = True
        if self.on_done:
            self.on_done(self._path, self._duration())
        self.destroy()

    def _close(self):
        if self._stream is not None:
            self._stop_recording()
        if self._path and not self._added:
            if not messagebox.askyesno("Discard recording?", "This take has not been added to the clips. "
                                                             "Discard it?", parent=self):
                return
            if os.path.exists(self._path):
                os.remove(self._path)
        self.destroy()

    def destroy(self):
        self._stop_play()
        self._sound = None
        if self._stream is not None:
            try:
                self._stream.stop_stream()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        if self._pa is not None:
            self._pa.terminate()
            self._pa = None
        super().destroy()
