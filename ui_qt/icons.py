"""
ui_qt/icons.py
──────────────
Line icons for the Qt window, in place of emoji.

The icons are Lucide (https://lucide.dev, ISC license, licenses/Lucide-ISC.txt),
lucide-static 0.460.0, stored as SVG in assets/icons/lucide/. Each is drawn in
the color it is asked for (the SVGs use stroke="currentColor") at the screen's
device pixel ratio, and cached.

The rest of the UI still writes texts the CTk way, with an emoji in front
("✅  Saved", "🎤", "🤖  Robotic"). split() turns such a text into an icon name
and the plain text, so IconLabel, IconButton and ToggleSwitch can show the
icon instead. The CTk window keeps its emoji: the shared registry and core
messages are unchanged.
"""

import re

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QColor, QGuiApplication, QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

from core import paths
from core.constants import COLORS

# Leading emoji or symbol -> Lucide icon name.
EMOJI = {
    "⚡": "zap", "🎧": "headphones", "▶": "play", "⏹": "square", "■": "square",
    "💾": "save", "📄": "captions", "📂": "folder-open", "✖": "x", "❚❚": "pause",
    "⏸": "pause", "⏮": "skip-back", "📥": "import", "🎤": "mic", "🎙": "mic",
    "🔍": "search", "✂": "scissors", "🧬": "dna", "✏": "pencil", "🗑": "trash-2",
    "🔊": "volume-2", "↺": "rotate-ccw", "⟲": "rotate-ccw", "⬇": "download",
    "⬆": "circle-arrow-up", "🎯": "crosshair", "♥": "heart-filled", "♡": "heart",
    "✅": "circle-check", "⚠": "triangle-alert", "❌": "circle-x", "⌛": "hourglass",
    "🎵": "music", "🔁": "refresh-cw", "ℹ": "info", "⏱": "timer", "●": "circle-dot",
    "✎": "pen-line", "✓": "check", "📝": "type", "⚙": "settings", "✨": "sparkles",
    "🎛": "sliders-horizontal", "♪": "music", "↶": "undo-2", "↷": "redo-2",
    "▾": "chevron-down", "▴": "chevron-up",
}
# Texts that are only a symbol (an icon button with no words).
ONLY = {"+": "plus", "▾": "chevron-down", "▴": "chevron-up"}

# One icon per effect key (effects/registry.py); cloned voices use "dna".
EFFECTS = {
    "normalize": "volume-2", "vocoder": "keyboard-music", "vocoder2": "audio-lines",
    "robotic": "bot", "demonic": "skull", "demonic2": "flame", "darth_vader": "swords",
    "harmonizer": "users", "glitch": "shuffle", "stutter": "scissors", "bitcrush": "gamepad-2",
    "ringmod": "radio-tower", "reverb": "waves", "delay": "repeat", "plate_reverb": "disc-3",
    "shimmer_reverb": "sparkles", "reverse_reverb": "ghost", "chorus": "drama", "climax": "rocket",
    "pitch": "music", "pilot": "plane", "hyperpop": "party-popper", "melancholic": "cloud-rain",
    "natgeo": "video", "seductive_m": "wine", "seductive_f": "flower", "saas_flash": "zap",
    "cinematic": "clapperboard", "arjun": "monitor-play", "dramatic_ads": "tv", "techy": "laptop",
    "telephone": "phone", "radio": "radio", "megaphone": "megaphone",
}
CLONE_ICON = "dna"
DEFAULT_EFFECT = "audio-waveform"

_LEAD = re.compile(r"^\s*(" + "|".join(re.escape(k) for k in sorted(EMOJI, key=len, reverse=True))
                   + r")️?\s*")
# Any other pictograph at the start (a registry label added later): dropped, no icon.
_OTHER = re.compile("^\\s*[\U0001F000-\U0001FAFF☀-➿⬀-⯿]️?\\s*")
_svg_cache = {}
_pix_cache = {}


def split(text):
    """(icon name or None, text without the leading emoji). A text that is
    only a symbol ("+", "▾") gives (icon, "")."""
    if text is None:
        return None, ""
    t = str(text)
    if t.strip() in ONLY:
        return ONLY[t.strip()], ""
    m = _LEAD.match(t)
    if m:
        return EMOJI[m.group(1)], t[m.end():]
    m = _OTHER.match(t)
    if m:
        return None, t[m.end():]
    return None, t


def effect_icon(key):
    if key.startswith("clone:"):
        return CLONE_ICON
    return EFFECTS.get(key, DEFAULT_EFFECT)


def _svg(name):
    if name not in _svg_cache:
        filled = name.endswith("-filled")
        base = name[:-len("-filled")] if filled else name
        path = paths.bundle_dir() / "assets" / "icons" / "lucide" / f"{base}.svg"
        try:
            data = path.read_text(encoding="utf-8")
        except OSError:
            data = ""
        if filled:                       # the same outline, filled (♥ favourite)
            data = data.replace('fill="none"', 'fill="currentColor"')
        _svg_cache[name] = data
    return _svg_cache[name]


def pixmap(name, color=None, size=16, dpr=None):
    """The icon *name* drawn in *color* at *size* logical px (a null pixmap if unknown)."""
    if dpr is None:
        screen = QGuiApplication.primaryScreen()
        dpr = screen.devicePixelRatio() if screen else 1.0
    color = QColor(color or COLORS["text_primary"]).name()
    key = (name, color, size, dpr)
    pm = _pix_cache.get(key)
    if pm is not None:
        return pm
    svg = _svg(name)
    px = max(1, int(round(size * dpr)))
    pm = QPixmap(px, px)
    pm.fill(Qt.transparent)
    if svg:
        r = QSvgRenderer(QByteArray(svg.replace("currentColor", color).encode("utf-8")))
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        r.render(p, QRectF(0, 0, px, px))
        p.end()
    pm.setDevicePixelRatio(dpr)
    _pix_cache[key] = pm
    return pm


def icon(name, color=None, size=16, disabled_color=None):
    """A QIcon with a normal and a disabled look."""
    ic = QIcon()
    ic.addPixmap(pixmap(name, color, size), QIcon.Normal)
    ic.addPixmap(pixmap(name, disabled_color or COLORS["text_muted"], size), QIcon.Disabled)
    return ic
