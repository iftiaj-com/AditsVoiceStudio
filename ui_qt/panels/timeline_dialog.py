"""
ui_qt/panels/timeline_dialog.py
───────────────────────────────
Pick the part of a clip to clone from (Qt). A small waveform editor in the
style of a video editor's timeline:

  * drag inside the selection to move it, drag its edges to resize it,
    drag on empty waveform to draw a new selection;
  * − / + / Fit zoom, the scrollbar (or mouse wheel) pans, Ctrl+wheel zooms;
  * Start / End fields for exact times, quick lengths (10 / 20 / 25 s by default);
  * ▶ Play selection (with a moving playhead) and ■ Stop.

on_done(start, end, duration) is called with seconds when the user presses
Use selection; a whole-clip selection is sent as (None, None, duration).

Behavior follows ui/panels/timeline_dialog.py (see docs/ui-parity-checklist.md,
"Timeline dialog"). The waveform, ruler, selection, handles and playhead are
drawn by one QWidget (_Waveform) with QPainter. Two things work differently
under the hood, with the same result on screen:

* The file is decoded on a worker thread (the CTk dialog decoded it on the
  Tk thread 50 ms after opening), so the window stays responsive while
  "Loading…" shows. The result reaches the GUI thread through ui().
* Waveform peaks come from a min/max pyramid built once at load time, so a
  repaint (every 60 ms while playing) does not rescan the whole clip.

Long text in the header is cut to fit: the file name is clipped, as Tk
clips it, and a long load error ends in "…" with the full text in its
tooltip. Without this, a Qt label would widen the window to fit the text.

The dialog is deleted when it closes (WA_DeleteOnClose), like a destroyed
CTkToplevel, so the decoded audio is freed. Esc does nothing, as in the CTk
dialog; Cancel and the close box close it.
"""
import os
import shutil
import tempfile
import threading
import time

import numpy as np
from PySide6.QtCore import QLine, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QFontMetrics, QIcon, QPainter, QPen
from PySide6.QtWidgets import (QDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QScrollBar, QSizePolicy,
                               QVBoxLayout, QWidget)

from core import paths
from core.constants import COLORS
from ui_qt import theme
from ui_qt.bridge import ui
from ui_qt.widgets import ElidedLabel

# The constants below match ui/panels/timeline_dialog.py, which cannot be
# imported here (it loads CustomTkinter).
MIN_LEN = 0.5          # shortest selection, seconds
HANDLE_PX = 7          # grab distance for the edges
SEL_BG = "#24224A"     # selection background (bg_input mixed with accent_primary)
PLAY_CHANNEL = 3       # pygame channel (1 ambiance, 2 previews, 3 timeline)
TICK_MS = 60           # playhead refresh while playing
RULER = 18             # ruler height in px
HELP_TEXT = ("Drag the highlighted part to move it, drag its edges to change the length, "
             "or drag across the waveform to start a new selection.")

_BLOCK = 64            # samples per block at the finest pyramid level
_FACTOR = 4            # each pyramid level merges this many blocks of the one below


def _fmt(t):
    m, s = divmod(max(0.0, t), 60)
    return f"{int(m)}:{s:05.2f}"


def _pyramid(y):
    """Min/max levels for y: [(block, lo, hi), ...], block = samples per entry."""
    levels = []
    n = y.size // _BLOCK
    if n == 0:
        return levels
    blocks = y[: n * _BLOCK].reshape(n, _BLOCK)
    lo, hi = blocks.min(axis=1), blocks.max(axis=1)
    rest = y[n * _BLOCK:]
    if rest.size:                      # keep the tail so the last column is not empty
        lo = np.append(lo, rest.min())
        hi = np.append(hi, rest.max())
    block = _BLOCK
    levels.append((block, lo, hi))
    while lo.size >= _FACTOR * 2:
        m = lo.size // _FACTOR
        nlo = lo[: m * _FACTOR].reshape(m, _FACTOR).min(axis=1)
        nhi = hi[: m * _FACTOR].reshape(m, _FACTOR).max(axis=1)
        if lo.size % _FACTOR:
            nlo = np.append(nlo, lo[m * _FACTOR:].min())
            nhi = np.append(nhi, hi[m * _FACTOR:].max())
        lo, hi, block = nlo, nhi, block * _FACTOR
        levels.append((block, lo, hi))
    return levels


def _columns(y, levels, i0, i1, cols):
    """Min and max of y[i0:i1] for each of cols pixel columns."""
    chunk_n = i1 - i0
    if chunk_n < cols:                 # fewer samples than columns: sample points (as in CTk)
        chunk = y[i0:i1]
        idx = np.linspace(0, max(0, chunk.size - 1), cols).astype(int)
        lo = hi = chunk[idx] if chunk.size else np.zeros(cols, dtype=np.float32)
        return lo, hi
    spp = chunk_n / cols
    level = None
    for lv in levels:                  # the coarsest level still finer than a column
        if lv[0] * 2 <= spp:
            level = lv
    edges = i0 + (np.arange(cols + 1, dtype=np.float64) * chunk_n / cols).astype(np.int64)
    if level is None:                  # zoomed in: read the samples themselves
        src_lo = src_hi = y[i0:i1]
        starts = edges[:-1] - i0
    else:
        block, lo, hi = level
        b0, b1 = i0 // block, min(lo.size, -(-i1 // block))
        src_lo, src_hi = lo[b0:b1], hi[b0:b1]
        starts = np.minimum(edges[:-1] // block - b0, src_lo.size - 1)
    starts = np.maximum.accumulate(np.clip(starts, 0, src_lo.size - 1))
    return np.minimum.reduceat(src_lo, starts), np.maximum.reduceat(src_hi, starts)


class _Waveform(QWidget):
    """The timeline canvas. Holds no state of its own: it draws and reports
    mouse input to the dialog, like the Tk canvas did."""

    def __init__(self, dialog):
        super().__init__()
        self._d = dialog
        self.setMouseTracking(True)
        self.setCursor(Qt.IBeamCursor)
        self.setMinimumHeight(80)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._ruler_font = QFont("Segoe UI")
        self._ruler_font.setPointSize(8)
        self._cache_key = None
        self._cache = None

    def sizeHint(self):
        size = super().sizeHint()
        size.setHeight(170)
        return size

    def columns(self, v0, v1, cols):
        d = self._d
        key = (v0, v1, cols)
        if key != self._cache_key:
            n = max(1, d._y.size)
            i0 = min(int(v0 * d._sr), n - 1)
            i1 = min(max(i0 + 1, int(v1 * d._sr)), n)
            self._cache = _columns(d._y, d._levels, i0, i1, cols)
            self._cache_key = key
        return self._cache

    def paintEvent(self, _event):
        d = self._d
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(COLORS["bg_input"]))
        if d._y is None:
            return
        w, h = d._view_px(), max(60, self.height())
        mid = RULER + (h - RULER) / 2
        amp = (h - RULER) / 2 - 6
        s, e = d.sel
        xs, xe = d._x(s), d._x(e)
        p.fillRect(QRectF(xs, RULER, xe - xs, h - RULER), QColor(SEL_BG))

        # waveform: one min/max bar per pixel column of the current view
        v0, v1 = d.view
        cols = int(w)
        lo, hi = self.columns(v0, v1, cols)
        k = amp / d._peak
        top = (mid - hi * k).astype(np.int64)
        bottom = (mid - lo * k + 1).astype(np.int64)
        times = v0 + np.arange(cols) / d._view_px() * (v1 - v0)
        inside = (times >= s) & (times <= e)
        sel_lines, other_lines = [], []
        for x in range(cols):
            (sel_lines if inside[x] else other_lines).append(QLine(x, int(top[x]), x, int(bottom[x])))
        p.setPen(QPen(QColor(COLORS["text_muted"]), 1))
        p.drawLines(other_lines)
        p.setPen(QPen(QColor(COLORS["accent_primary"]), 1))
        p.drawLines(sel_lines)

        # ruler
        muted = QColor(COLORS["text_muted"])
        p.setFont(self._ruler_font)
        fm = QFontMetrics(self._ruler_font)
        base = 9 + (fm.ascent() - fm.descent()) / 2
        span = v1 - v0
        step = next((st for st in (0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300) if span / st <= 12), 600)
        t = (int(v0 / step) + 1) * step if v0 % step else v0
        while t <= v1:
            x = d._x(t)
            p.setPen(QPen(muted, 1))
            p.drawLine(int(x), 0, int(x), 6)
            p.drawText(int(x + 3), int(base), _fmt(t)[:-3] if step >= 1 else _fmt(t))
            t += step

        # handles
        handle = QColor(COLORS["accent_secondary"])
        p.setPen(QPen(handle, 2))
        for x in (xs, xe):
            p.drawLine(int(round(x)), RULER, int(round(x)), h)
            p.fillRect(QRectF(x - 4, RULER, 8, 14), handle)
        if d._play_t0 is not None:
            pt = d._play_from + (time.monotonic() - d._play_t0)
            if pt <= e:
                px = int(round(d._x(pt)))
                p.setPen(QPen(QColor(COLORS["success"]), 2))
                p.drawLine(px, RULER, px, h)

    # mouse input goes to the dialog, as the canvas bindings did
    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self._d._on_press(ev.position().x())

    def mouseMoveEvent(self, ev):
        if self._d._drag:
            self._d._on_motion(ev.position().x())
        else:
            self._d._on_hover(ev.position().x())

    def mouseReleaseEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self._d._drag = None

    def wheelEvent(self, ev):
        dy = ev.angleDelta().y()
        if dy:
            self._d._on_wheel(dy, bool(ev.modifiers() & Qt.ControlModifier))
        ev.accept()


class TimelineDialog(QDialog):
    def __init__(self, parent, path, start=None, end=None, on_done=None, quick_lengths=(10, 20, 25)):
        super().__init__(parent)
        name = os.path.basename(path)
        self.setObjectName("timelineDialog")
        self.setWindowTitle(f"Select audio · {name}")
        ico = paths.bundle_dir() / "assets" / "adits_voice_studio.ico"
        if ico.exists():
            self.setWindowIcon(QIcon(str(ico)))
        self.resize(860, 420)
        self.setMinimumSize(640, 380)
        self.setModal(True)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setStyleSheet(self._sheet())
        self.on_done = on_done
        self.path = path
        self._y = None
        self._seg = None
        self._levels = []
        self._peak = 1.0
        self._sr = 1
        self.duration = 0.0
        self.sel = [start or 0.0, end]
        self.view = [0.0, 1.0]
        self._drag = None
        self._tmp = tempfile.mkdtemp(prefix="adits_vs_timeline_")
        self._sound = None
        self._play_t0 = None
        self._play_from = 0.0
        self._closed = False
        self._syncing_scroll = False
        self._timer = QTimer(self)
        self._timer.setInterval(TICK_MS)
        self._timer.timeout.connect(self._tick)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        head = QHBoxLayout()
        head.setContentsMargins(16, 12, 16, 4)
        title = theme.label(f"✂  {name}")
        title.setObjectName("cardTitle")
        title.setMinimumWidth(1)         # a long name is cut instead of widening the window
        head.addWidget(title)
        self._dur_lbl = ElidedLabel("Loading…")   # a long error is cut with "…" (full text in its tip)
        self._dur_lbl.setProperty("muted", True)
        self._dur_lbl.setProperty("small", True)
        self._dur_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        head.addSpacing(12)
        head.addWidget(self._dur_lbl, 1)
        root.addLayout(head)
        hint = QLabel(HELP_TEXT)
        hint.setWordWrap(True)          # wraps at the 640 px minimum width instead of cutting off
        hint.setProperty("muted", True)
        hint.setProperty("small", True)
        hint.setContentsMargins(16, 0, 16, 0)
        root.addWidget(hint)

        wrap = QFrame()
        wrap.setObjectName("tlWrap")
        wl = QVBoxLayout(wrap)
        wl.setContentsMargins(8, 8, 8, 8)
        wl.setSpacing(2)
        self.wave = _Waveform(self)
        wl.addWidget(self.wave, 1)
        self._scroll = QScrollBar(Qt.Horizontal)
        self._scroll.setRange(0, 0)
        self._scroll.valueChanged.connect(self._on_scroll)
        wl.addWidget(self._scroll)
        wrap_row = QVBoxLayout()
        wrap_row.setContentsMargins(16, 8, 16, 8)
        wrap_row.addWidget(wrap)
        root.addLayout(wrap_row, 1)

        ctl = QHBoxLayout()
        ctl.setContentsMargins(16, 0, 16, 4)
        ctl.setSpacing(0)
        self._start_edit = QLineEdit()
        self._end_edit = QLineEdit()
        for text, edit in (("Start (s)", self._start_edit), ("End (s)", self._end_edit)):
            ctl.addWidget(theme.label(text, secondary=True, small=True))
            ctl.addSpacing(4)
            edit.setFixedSize(70, 28)
            edit.editingFinished.connect(self._apply_entries)   # Enter or focus loss
            ctl.addWidget(edit)
            ctl.addSpacing(10)
        self._len_lbl = theme.label("", small=True)
        ctl.addWidget(self._len_lbl)
        ctl.addSpacing(10)
        self._quick_btns = []
        for n in quick_lengths:
            b = self._small_btn(f"{n} s", lambda n=n: self._set_length(n))
            self._quick_btns.append(b)
            ctl.addSpacing(2)
            ctl.addWidget(b)
            ctl.addSpacing(2)
        self._all_btn = self._small_btn("All", self._select_all)
        ctl.addSpacing(2)
        ctl.addWidget(self._all_btn)
        ctl.addStretch(1)
        root.addLayout(ctl)

        zoom = QHBoxLayout()
        zoom.setContentsMargins(16, 0, 16, 10)
        zoom.setSpacing(0)
        self._zoom_out_btn = self._small_btn("−", lambda: self._zoom(2.0), width=34)
        self._zoom_in_btn = self._small_btn("+", lambda: self._zoom(0.5), width=34)
        self._fit_btn = self._small_btn("Fit", self._fit, width=44)
        zoom.addWidget(self._zoom_out_btn)
        zoom.addSpacing(4)
        zoom.addWidget(self._zoom_in_btn)
        zoom.addSpacing(4)
        zoom.addWidget(self._fit_btn)
        zoom.addSpacing(16)
        self._play_btn = self._btn("▶  Play selection", "tlPlay", self._play, 140)
        zoom.addWidget(self._play_btn)
        zoom.addSpacing(6)
        self._stop_btn = self._small_btn("■  Stop", self._stop, width=70)
        zoom.addWidget(self._stop_btn)
        zoom.addStretch(1)
        self._cancel_btn = self._btn("Cancel", "tlCancel", self.close, 80)
        zoom.addWidget(self._cancel_btn)
        zoom.addSpacing(8)
        self._ok_btn = self._btn("Use selection", "tlOk", self._ok, 130, role=theme.ROLE_PRIMARY)
        zoom.addWidget(self._ok_btn)
        root.addLayout(zoom)

        QTimer.singleShot(0, self._start_load)

    @staticmethod
    def _sheet():
        c = COLORS
        return f"""
QDialog#timelineDialog {{ background: {c['bg_dark']}; }}
QFrame#tlWrap {{
    background: {c['bg_card']}; border: 1px solid {c['border']}; border-radius: 12px;
}}
QDialog#timelineDialog QLineEdit {{
    background: {c['bg_card']}; border: 1px solid {c['border']}; border-radius: 6px;
    font-family: 'Segoe UI'; font-size: 11px; padding: 0 4px;
}}
QDialog#timelineDialog QLineEdit:focus {{ border-color: {c['accent_primary']}; }}
QDialog#timelineDialog QPushButton {{ min-height: 0px; padding: 0; }}
QPushButton[tlSmall="true"] {{
    background: {c['bg_card']}; border: 1px solid {c['border']}; border-radius: 8px;
    font-family: 'Segoe UI'; font-size: 11px;
}}
QPushButton[tlSmall="true"]:hover {{ background: {c['accent_primary']}; }}
QPushButton#tlPlay {{ background: {c['bg_card']}; border: 1px solid {c['success']}; border-radius: 8px; }}
QPushButton#tlPlay:hover {{ background: {c['success']}; }}
QPushButton#tlCancel {{ background: {c['bg_card']}; border: none; border-radius: 8px; }}
QPushButton#tlCancel:hover {{ background: {c['border']}; }}
QPushButton#tlOk {{ border: none; border-radius: 8px; }}
QScrollBar:horizontal {{ background: transparent; height: 12px; margin: 0; }}
QScrollBar::handle:horizontal {{ background: {c['border']}; border-radius: 6px; min-width: 30px; }}
QScrollBar::handle:horizontal:hover {{ background: {c['accent_primary']}; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; height: 0; }}
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{ background: transparent; }}
"""

    def _small_btn(self, text, cmd, width=52):
        b = theme.button(text, theme.ROLE_GHOST, tlSmall=True)
        b.setFixedSize(width, 28)
        b.setAutoDefault(False)
        b.clicked.connect(lambda _checked=False: cmd())   # clicked passes a bool; cmd takes none
        return b

    def _btn(self, text, name, cmd, width, role=theme.ROLE_GHOST):
        b = theme.button(text, role)
        b.setObjectName(name)
        b.setFixedSize(width, 30)
        b.setAutoDefault(False)       # Enter in Start / End must not press a button
        b.clicked.connect(lambda _checked=False: cmd())
        return b

    def showEvent(self, e):
        super().showEvent(e)
        self.raise_()
        self.activateWindow()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:   # the CTk dialog has no Esc binding
            e.accept()
            return
        super().keyPressEvent(e)

    # ── data ───────────────────────────────────────────────────────
    def _start_load(self):
        path = self.path

        def load():
            try:
                from core.audio_io import load_segment
                seg = load_segment(path)
                mono = seg.set_channels(1)
                y = np.array(mono.get_array_of_samples(), dtype=np.float32) / float(
                    2 ** (8 * mono.sample_width - 1))
                levels = _pyramid(y)
                peak = max(1e-3, float(np.max(np.abs(y)))) if y.size else 1e-3
                ui(self._loaded, seg, y, mono.frame_rate, levels, peak, None)
            except Exception as e:   # noqa: BLE001
                ui(self._loaded, None, None, 1, [], 1.0, e)
        threading.Thread(target=load, daemon=True, name="timeline-load").start()

    def _loaded(self, seg, y, sr, levels, peak, error):
        if self._closed:
            return
        if error is not None:
            theme.set_color(self._dur_lbl, COLORS["error"])
            self._dur_lbl.setText(f"Could not read the file: {error}")
            return
        self._seg = seg
        self._y, self._sr, self._levels, self._peak = y, sr, levels, peak
        self.duration = len(y) / sr
        self._dur_lbl.setText(f"Length {_fmt(self.duration)}")
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
        return max(10, self.wave.width())

    def _x(self, t):
        v0, v1 = self.view
        return (t - v0) / max(1e-9, v1 - v0) * self._view_px()

    def _t(self, x):
        v0, v1 = self.view
        return v0 + x / self._view_px() * (v1 - v0)

    def _draw(self):
        """Repaint the waveform and update the length label and scrollbar."""
        self.wave.update()
        if self._y is None:
            return
        s, e = self.sel
        self._len_lbl.setText(f"Selected {e - s:.1f} s")
        if self.duration > 0:
            v0, v1 = self.view
            self._syncing_scroll = True
            self._scroll.setRange(0, max(0, int(round((self.duration - (v1 - v0)) * 1000))))
            self._scroll.setPageStep(max(1, int(round((v1 - v0) * 1000))))
            self._scroll.setSingleStep(max(1, int(round((v1 - v0) * 100))))
            self._scroll.setValue(int(round(v0 * 1000)))
            self._syncing_scroll = False

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

    def _on_hover(self, x):
        if self._y is None:
            return
        kind = self._hit(x)
        cur = {"start": Qt.SizeHorCursor, "end": Qt.SizeHorCursor, "move": Qt.SizeAllCursor}
        self.wave.setCursor(cur.get(kind, Qt.IBeamCursor))

    def _on_press(self, x):
        if self._y is None:
            return
        kind = self._hit(x)
        t = min(max(0.0, self._t(x)), self.duration)
        if kind == "new":
            self.sel = [t, min(self.duration, t + MIN_LEN)]
            kind = "end"
        self._drag = (kind, t, list(self.sel))
        self._draw()

    def _on_motion(self, x):
        if not self._drag or self._y is None:
            return
        kind, t0, (s0, e0) = self._drag
        t = min(max(0.0, self._t(x)), self.duration)
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

    def _on_wheel(self, delta, ctrl):
        if self._y is None:
            return
        if ctrl:                    # Ctrl + wheel zooms
            self._zoom(0.8 if delta > 0 else 1.25)
        else:
            span = self.view[1] - self.view[0]
            self._pan(-span * 0.1 if delta > 0 else span * 0.1)

    def _on_scroll(self, value):
        if self._syncing_scroll or self._y is None:
            return
        self._pan(value / 1000.0 - self.view[0])

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
        if self._y is None:
            return
        self.view = [0.0, self.duration]
        self._draw()

    def _set_length(self, n):
        if self._y is None:
            return
        s = self.sel[0]
        e = min(self.duration, s + n)
        if e - s < n:                        # not enough audio after start: slide back
            s = max(0.0, e - n)
        self.sel = [s, e]
        self._sync_entries()
        self._draw()

    def _select_all(self):
        if self._y is None:
            return
        self.sel = [0.0, self.duration]
        self._sync_entries()
        self._draw()

    def _sync_entries(self):
        self._start_edit.setText(f"{self.sel[0]:.2f}")
        self._end_edit.setText(f"{self.sel[1]:.2f}")

    def _apply_entries(self):
        if self._y is None:
            return
        try:
            s, e = float(self._start_edit.text()), float(self._end_edit.text())
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
            if not pygame.mixer.get_init() or self._seg is None:
                return
            self._stop()
            if pygame.mixer.get_num_channels() <= PLAY_CHANNEL:
                pygame.mixer.set_num_channels(PLAY_CHANNEL + 1)
            s, e = self.sel
            part = self._seg[int(s * 1000):int(e * 1000)]
            path = os.path.join(self._tmp, f"sel_{int(time.time() * 1000)}.wav")
            part.export(path, format="wav")
            self._sound = pygame.mixer.Sound(path)
            pygame.mixer.Channel(PLAY_CHANNEL).play(self._sound)
            self._play_from, self._play_t0 = s, time.monotonic()
            self._timer.start()
            self._tick()
        except Exception as e:   # noqa: BLE001
            print(f"Timeline playback failed: {e}")

    def _tick(self):
        if self._play_t0 is None or self._closed:
            self._timer.stop()
            return
        if self._play_from + (time.monotonic() - self._play_t0) > self.sel[1]:
            self._play_t0 = None
            self._timer.stop()
        self._draw()

    def _stop(self):
        self._play_t0 = None
        self._timer.stop()
        try:
            import pygame
            if pygame.mixer.get_init() and pygame.mixer.get_num_channels() > PLAY_CHANNEL:
                pygame.mixer.Channel(PLAY_CHANNEL).stop()
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
        self.close()

    def closeEvent(self, e):
        if not self._closed:
            self._stop()
            self._closed = True
            self._sound = None
            self._seg = None
            self._y = None
            self._levels = []
            shutil.rmtree(self._tmp, ignore_errors=True)
        super().closeEvent(e)
