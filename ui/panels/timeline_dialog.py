"""
ui/panels/timeline_dialog.py  –  pick the part of a clip to clone from

A small waveform editor in the style of a video editor's timeline:

  * drag inside the selection to move it, drag its edges to resize it,
    drag on empty waveform to draw a new selection;
  * − / + / Fit zoom, the scrollbar (or mouse wheel) pans;
  * Start / End fields for exact times, quick lengths 10 / 20 / 25 s;
  * ▶ Play selection (with a moving playhead) and ■ Stop.

on_done(start, end) is called with seconds when the user presses Use selection.
Built on a plain Tk canvas, so no extra libraries are needed.
"""

import os
import shutil
import tempfile
import time

import customtkinter as ctk
import numpy as np

from core.constants import COLORS, FONTS

MIN_LEN = 0.5          # shortest selection, seconds
HANDLE_PX = 7          # grab distance for the edges
SEL_BG = "#24224A"     # selection background (bg_input mixed with accent_primary)


def _fmt(t):
    m, s = divmod(max(0.0, t), 60)
    return f"{int(m)}:{s:05.2f}"


class TimelineDialog(ctk.CTkToplevel):
    def __init__(self, master, path, start=None, end=None, on_done=None, quick_lengths=(10, 20, 25)):
        super().__init__(master)
        self.title(f"Select audio · {os.path.basename(path)}")
        self.configure(fg_color=COLORS["bg_dark"])
        self.geometry("860x420")
        self.minsize(640, 380)
        self.transient(master)
        self.on_done = on_done
        self.path = path
        self._y = None
        self._sr = 1
        self.duration = 0.0
        self.sel = [start or 0.0, end]
        self.view = [0.0, 1.0]
        self._drag = None
        self._tmp = tempfile.mkdtemp(prefix="adits_vs_timeline_")
        self._sound = None
        self._play_t0 = None
        self._play_from = 0.0

        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(fill="x", padx=16, pady=(12, 4))
        ctk.CTkLabel(head, text=f"✂  {os.path.basename(path)}", font=FONTS["subtitle"],
                     text_color=COLORS["text_primary"]).pack(side="left")
        self._dur_lbl = ctk.CTkLabel(head, text="Loading…", font=FONTS["body_small"],
                                     text_color=COLORS["text_muted"])
        self._dur_lbl.pack(side="right")
        ctk.CTkLabel(self, text="Drag the highlighted part to move it, drag its edges to change the length, "
                                "or drag across the waveform to start a new selection.",
                     font=FONTS["body_small"], text_color=COLORS["text_muted"]).pack(anchor="w", padx=16)

        wrap = ctk.CTkFrame(self, fg_color=COLORS["bg_card"], corner_radius=12, border_width=1,
                            border_color=COLORS["border"])
        wrap.pack(fill="both", expand=True, padx=16, pady=8)
        self.canvas = ctk.CTkCanvas(wrap, bg=COLORS["bg_input"], highlightthickness=0, height=170)
        self.canvas.pack(fill="both", expand=True, padx=8, pady=(8, 2))
        self._scroll = ctk.CTkScrollbar(wrap, orientation="horizontal", command=self._on_scroll,
                                        button_color=COLORS["border"],
                                        button_hover_color=COLORS["accent_primary"])
        self._scroll.pack(fill="x", padx=8, pady=(0, 8))

        ctl = ctk.CTkFrame(self, fg_color="transparent")
        ctl.pack(fill="x", padx=16, pady=(0, 4))
        self._start_var = ctk.StringVar()
        self._end_var = ctk.StringVar()
        for label, var in (("Start (s)", self._start_var), ("End (s)", self._end_var)):
            ctk.CTkLabel(ctl, text=label, font=FONTS["body_small"],
                         text_color=COLORS["text_secondary"]).pack(side="left", padx=(0, 4))
            e = ctk.CTkEntry(ctl, textvariable=var, width=70, height=28, font=FONTS["body_small"],
                             fg_color=COLORS["bg_card"], border_color=COLORS["border"])
            e.pack(side="left", padx=(0, 10))
            e.bind("<Return>", lambda ev: self._apply_entries())
            e.bind("<FocusOut>", lambda ev: self._apply_entries())
        self._len_lbl = ctk.CTkLabel(ctl, text="", font=FONTS["body_small"], text_color=COLORS["text_primary"])
        self._len_lbl.pack(side="left", padx=(0, 12))
        for n in quick_lengths:
            self._small_btn(ctl, f"{n} s", lambda n=n: self._set_length(n)).pack(side="left", padx=2)
        self._small_btn(ctl, "All", self._select_all).pack(side="left", padx=2)

        zoom = ctk.CTkFrame(self, fg_color="transparent")
        zoom.pack(fill="x", padx=16, pady=(0, 10))
        self._small_btn(zoom, "−", lambda: self._zoom(2.0), width=34).pack(side="left", padx=(0, 2))
        self._small_btn(zoom, "+", lambda: self._zoom(0.5), width=34).pack(side="left", padx=2)
        self._small_btn(zoom, "Fit", self._fit, width=44).pack(side="left", padx=2)
        self._play_btn = ctk.CTkButton(zoom, text="▶  Play selection", font=FONTS["button"], width=140,
                                       height=30, corner_radius=8, fg_color=COLORS["bg_card"],
                                       hover_color=COLORS["success"], border_color=COLORS["success"],
                                       border_width=1, command=self._play)
        self._play_btn.pack(side="left", padx=(14, 4))
        self._small_btn(zoom, "■  Stop", self._stop, width=70).pack(side="left", padx=2)
        ctk.CTkButton(zoom, text="Use selection", font=FONTS["button"], width=130, height=30,
                      corner_radius=8, fg_color=COLORS["accent_primary"],
                      hover_color=COLORS["accent_secondary"], command=self._ok).pack(side="right")
        ctk.CTkButton(zoom, text="Cancel", font=FONTS["button"], width=80, height=30, corner_radius=8,
                      fg_color=COLORS["bg_card"], hover_color=COLORS["border"],
                      command=self.destroy).pack(side="right", padx=8)

        self.canvas.bind("<Configure>", lambda e: self._draw())
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_motion)
        self.canvas.bind("<ButtonRelease-1>", lambda e: setattr(self, "_drag", None))
        self.canvas.bind("<Motion>", self._on_hover)
        self.canvas.bind("<MouseWheel>", self._on_wheel)
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.after(50, self._load)
        self.after(150, self._grab)

    def _small_btn(self, parent, text, cmd, width=52):
        return ctk.CTkButton(parent, text=text, font=FONTS["body_small"], width=width, height=28,
                             corner_radius=8, fg_color=COLORS["bg_card"], hover_color=COLORS["accent_primary"],
                             border_color=COLORS["border"], border_width=1, command=cmd)

    def _grab(self):
        try:
            self.lift()
            self.focus_force()
            self.grab_set()
        except Exception:
            pass

    # ── data ───────────────────────────────────────────────────────
    def _load(self):
        from core.audio_io import load_segment
        try:
            seg = load_segment(self.path)
        except Exception as e:
            self._dur_lbl.configure(text=f"Could not read the file: {e}", text_color=COLORS["error"])
            return
        self._seg = seg
        mono = seg.set_channels(1)
        self._y = np.array(mono.get_array_of_samples(), dtype=np.float32) / float(2 ** (8 * mono.sample_width - 1))
        self._sr = mono.frame_rate
        self.duration = len(self._y) / self._sr
        self._dur_lbl.configure(text=f"Length {_fmt(self.duration)}")
        s, e = self.sel
        s = min(max(0.0, s or 0.0), max(0.0, self.duration - MIN_LEN))
        e = self.duration if e is None else min(self.duration, e)
        if e - s < MIN_LEN:
            e = min(self.duration, s + MIN_LEN)
        self.sel = [s, e]
        self.view = [0.0, self.duration]
        self._sync_entries()
        self._draw()

    # ── geometry ───────────────────────────────────────────────────
    def _view_px(self):
        return max(10, self.canvas.winfo_width())

    def _x(self, t):
        v0, v1 = self.view
        return (t - v0) / max(1e-9, v1 - v0) * self._view_px()

    def _t(self, x):
        v0, v1 = self.view
        return v0 + x / self._view_px() * (v1 - v0)

    def _draw(self):
        c = self.canvas
        c.delete("all")
        if self._y is None:
            return
        w, h = self._view_px(), max(60, c.winfo_height())
        ruler = 18
        mid = ruler + (h - ruler) / 2
        amp = (h - ruler) / 2 - 6
        s, e = self.sel
        xs, xe = self._x(s), self._x(e)
        c.create_rectangle(xs, ruler, xe, h, fill=SEL_BG, outline="")

        # waveform: one min/max bar per pixel column of the current view
        v0, v1 = self.view
        i0 = int(v0 * self._sr)
        i1 = max(i0 + 1, int(v1 * self._sr))
        chunk = self._y[i0:i1]
        cols = int(w)
        if chunk.size >= cols:
            usable = chunk[: (chunk.size // cols) * cols].reshape(cols, -1)
            lo, hi = usable.min(axis=1), usable.max(axis=1)
        else:
            idx = np.linspace(0, max(0, chunk.size - 1), cols).astype(int)
            lo = hi = chunk[idx] if chunk.size else np.zeros(cols)
        peak = max(1e-3, float(np.max(np.abs(self._y))))
        for x in range(cols):
            t = self._t(x)
            col = COLORS["accent_primary"] if s <= t <= e else COLORS["text_muted"]
            c.create_line(x, mid - hi[x] / peak * amp, x, mid - lo[x] / peak * amp + 1, fill=col)

        # ruler
        span = v1 - v0
        step = next((st for st in (0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300) if span / st <= 12), 600)
        t = (int(v0 / step) + 1) * step if v0 % step else v0
        while t <= v1:
            x = self._x(t)
            c.create_line(x, 0, x, 6, fill=COLORS["text_muted"])
            c.create_text(x + 3, 9, text=_fmt(t)[:-3] if step >= 1 else _fmt(t), anchor="w",
                          fill=COLORS["text_muted"], font=("Segoe UI", 8))
            t += step

        # handles
        for x in (xs, xe):
            c.create_line(x, ruler, x, h, fill=COLORS["accent_secondary"], width=2)
            c.create_rectangle(x - 4, ruler, x + 4, ruler + 14, fill=COLORS["accent_secondary"], outline="")
        if self._play_t0 is not None:
            pt = self._play_from + (time.monotonic() - self._play_t0)
            if pt <= e:
                px = self._x(pt)
                c.create_line(px, ruler, px, h, fill=COLORS["success"], width=2)
        self._len_lbl.configure(text=f"Selected {e - s:.1f} s")
        if self.duration > 0:
            self._scroll.set(v0 / self.duration, v1 / self.duration)

    # ── interaction ────────────────────────────────────────────────
    def _hit(self, x):
        xs, xe = self._x(self.sel[0]), self._x(self.sel[1])
        whole = self.sel[0] <= 0.01 and self.sel[1] >= self.duration - 0.01
        if whole and HANDLE_PX < x < self._view_px() - HANDLE_PX:
            return "new"        # everything selected: a drag draws a new selection
        if abs(x - xs) <= HANDLE_PX:
            return "start"
        if abs(x - xe) <= HANDLE_PX:
            return "end"
        if xs < x < xe:
            return "move"
        return "new"

    def _on_hover(self, ev):
        cur = {"start": "sb_h_double_arrow", "end": "sb_h_double_arrow", "move": "fleur"}.get(self._hit(ev.x), "xterm")
        self.canvas.configure(cursor=cur)

    def _on_press(self, ev):
        if self._y is None:
            return
        kind = self._hit(ev.x)
        t = min(max(0.0, self._t(ev.x)), self.duration)
        if kind == "new":
            self.sel = [t, min(self.duration, t + MIN_LEN)]
            kind = "end"
        self._drag = (kind, t, list(self.sel))
        self._draw()

    def _on_motion(self, ev):
        if not self._drag or self._y is None:
            return
        kind, t0, (s0, e0) = self._drag
        t = min(max(0.0, self._t(ev.x)), self.duration)
        if kind == "start":
            self.sel[0] = min(t, self.sel[1] - MIN_LEN)
        elif kind == "end":
            self.sel[1] = max(t, self.sel[0] + MIN_LEN)
        else:
            length = e0 - s0
            ns = min(max(0.0, s0 + (t - t0)), self.duration - length)
            self.sel = [ns, ns + length]
        self.sel = [max(0.0, self.sel[0]), min(self.duration, self.sel[1])]
        self._sync_entries()
        self._draw()

    def _on_wheel(self, ev):
        if ev.state & 0x4:          # Ctrl + wheel zooms
            self._zoom(0.8 if ev.delta > 0 else 1.25)
        else:
            span = self.view[1] - self.view[0]
            self._pan(-span * 0.1 if ev.delta > 0 else span * 0.1)

    def _on_scroll(self, *args):
        span = self.view[1] - self.view[0]
        if args[0] == "moveto":
            v0 = float(args[1]) * self.duration
        else:
            v0 = self.view[0] + int(args[1]) * span * (0.9 if args[2] == "pages" else 0.1)
        self._pan(v0 - self.view[0])

    def _pan(self, dt):
        span = self.view[1] - self.view[0]
        v0 = min(max(0.0, self.view[0] + dt), max(0.0, self.duration - span))
        self.view = [v0, v0 + span]
        self._draw()

    def _zoom(self, factor):
        if self._y is None:
            return
        span = min(self.duration, max(1.0, (self.view[1] - self.view[0]) * factor))
        centre = (self.sel[0] + self.sel[1]) / 2
        v0 = min(max(0.0, centre - span / 2), max(0.0, self.duration - span))
        self.view = [v0, v0 + span]
        self._draw()

    def _fit(self):
        self.view = [0.0, self.duration]
        self._draw()

    def _set_length(self, n):
        s = self.sel[0]
        e = min(self.duration, s + n)
        if e - s < n:                        # not enough audio after start: slide back
            s = max(0.0, e - n)
        self.sel = [s, e]
        self._sync_entries()
        self._draw()

    def _select_all(self):
        self.sel = [0.0, self.duration]
        self._sync_entries()
        self._draw()

    def _sync_entries(self):
        self._start_var.set(f"{self.sel[0]:.2f}")
        self._end_var.set(f"{self.sel[1]:.2f}")

    def _apply_entries(self):
        try:
            s, e = float(self._start_var.get()), float(self._end_var.get())
        except ValueError:
            self._sync_entries()
            return
        s = min(max(0.0, s), max(0.0, self.duration - MIN_LEN))
        e = min(max(s + MIN_LEN, e), self.duration)
        self.sel = [s, e]
        self._sync_entries()
        self._draw()

    # ── playback ───────────────────────────────────────────────────
    def _play(self):
        try:
            import pygame
            if not pygame.mixer.get_init():
                return
            self._stop()
            s, e = self.sel
            part = self._seg[int(s * 1000):int(e * 1000)]
            path = os.path.join(self._tmp, f"sel_{int(time.time() * 1000)}.wav")
            part.export(path, format="wav")
            self._sound = pygame.mixer.Sound(path)
            pygame.mixer.Channel(3).play(self._sound)
            self._play_from, self._play_t0 = s, time.monotonic()
            self._tick()
        except Exception as e:
            print(f"Timeline playback failed: {e}")

    def _tick(self):
        if self._play_t0 is None or not self.winfo_exists():
            return
        if self._play_from + (time.monotonic() - self._play_t0) > self.sel[1]:
            self._play_t0 = None
        self._draw()
        if self._play_t0 is not None:
            self.after(60, self._tick)

    def _stop(self):
        self._play_t0 = None
        try:
            import pygame
            if pygame.mixer.get_init():
                pygame.mixer.Channel(3).stop()
        except Exception:
            pass
        if self._y is not None:
            self._draw()

    def _ok(self):
        self._apply_entries()
        if self.on_done and self._y is not None:
            s, e = self.sel
            whole = s <= 0.01 and e >= self.duration - 0.01
            self.on_done(None if whole else round(s, 3), None if whole else round(e, 3), self.duration)
        self.destroy()

    def destroy(self):
        self._stop()
        self._sound = None
        shutil.rmtree(self._tmp, ignore_errors=True)
        super().destroy()
