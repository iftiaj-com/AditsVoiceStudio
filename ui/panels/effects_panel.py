"""
ui/panels/effects_panel.py  –  Voice effects card (Qt)

Same sections, rules and public methods as ui/panels/effects_panel.py
(see docs/ui-parity-checklist.md, "Effects panel"):

  ♥ Favourites              a mirror tile for every effect whose heart is on
  🧬 Cloned Voices  [✏]     entries with exclusive_group "clone"
  🎛 Effects                every other EFFECTS_REGISTRY entry

State: one on/off value per effect key (self._on). Every switch that shows a
key (its tile, its Favourites mirror, a search result row) is listed in
self._views[key] and redrawn from that value, which replaces the CTk panel's
shared BooleanVar. A user click goes through _user_toggle(), which applies
the exclusive-group rule only for keys that have a group, as the CTk tiles do.

Tiles are drawn by one widget each (_Tile.paintEvent), not built from a
frame, a switch, a label and a heart button. With 168 effects (134 cloned
voices) plus their Favourites mirrors that was about 1,000 widgets under the
app style sheet: a 60 px scroll step of the right column took 31 ms (9.6 ms
with this card hidden) and a window resize about 100 ms.

Search: as in the CTk panel, matches show in a Results panel laid over the
top of the tiles (it takes no room, so nothing below moves), at most
MAX_RESULTS rows, ranked names-first. The favourites file is the same one
the CTk panel uses.
"""
import json
import os
import re

from PySide6.QtCore import QEvent, QObject, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import (QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QSizePolicy,
                               QVBoxLayout, QWidget)

from core import paths
from core.constants import COLORS
from effects.registry import EFFECTS_REGISTRY
from ui import icons, theme
from ui.widgets import ElidedLabel, ToggleSwitch

CLONE_GROUP = "clone"
HEART_OFF, HEART_ON = "♡", "♥"
SEARCH_DELAY_MS = 120   # search once typing pauses
MAX_RESULTS = 8         # rows in the Results panel
NUM_COLS = 2            # the CTk panel is packed in the right column: 2 columns
_HEART_PX = None        # heart icon size, set by the first tile
SEARCH_TIP = ("Search effects and cloned voices by name or description. Click a result, "
              "or press Enter, to switch it on or off. ↑ ↓ move, Esc clears.")


def _words(text) -> list:
    """Lower-case words of *text*, without emoji and punctuation."""
    return re.findall(r"[0-9a-z]+", (text or "").lower())


def _favourites_path():
    return paths.user_dir("settings") / "favourites.json"


def load_favourites() -> list:
    try:
        with open(_favourites_path(), "r", encoding="utf-8") as f:
            keys = json.load(f).get("keys", [])
        return [k for k in keys if isinstance(k, str)]
    except (OSError, ValueError, AttributeError):
        return []


def save_favourites(keys) -> None:
    path = _favourites_path()
    tmp = path.with_suffix(".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"keys": list(keys)}, f, indent=2)
        os.replace(tmp, path)
    except OSError as e:
        print(f"Could not save favourites: {e}")


def _heart(on):
    b = theme.button(HEART_ON if on else HEART_OFF, "heart")
    b.setProperty("on", bool(on))
    return b


def _paint_heart(btn, on):
    btn.setText(HEART_ON if on else HEART_OFF)
    if btn.property("on") != bool(on):
        btn.setProperty("on", bool(on))
        theme.repolish(btn)


def _heart_px():
    f = QFont()
    f.setPixelSize(15)                  # QPushButton[role="heart"] font-size, as IconButton sizes icons
    return max(14, QFontMetrics(f).height() - 2)


class _Tile(QWidget):
    """One effect, drawn in one widget: switch, icon and name on the first
    line with the heart on the right, the description below.

    Same look and geometry as the frame + ToggleSwitch + heart button +
    ElidedLabel tile it replaces (54 px high; switch row at y 8 to 32, heart
    30 x 26 at the right, description at x 57). Clicking the switch row
    toggles the effect, clicking the heart favourites it.
    """
    H = 54
    PAD_L, PAD_R = 11, 6
    SW_Y, SW_H = 8, 24
    HEART_W, HEART_H, HEART_Y = 30, 26, 7
    TRACK_W, TRACK_H, GAP = ToggleSwitch.TRACK_W, ToggleSwitch.TRACK_H, ToggleSwitch.GAP
    DESC_X, DESC_Y, DESC_H = 57, 33, 14

    def __init__(self, panel, key, label, desc, on):
        super().__init__()
        self._panel, self._key = panel, key
        self._on = bool(on)
        self._fav = key in panel._fav_keys
        self._hover = None              # "switch" | "heart" | None
        lead, self._label = icons.split(label)
        self._icon = icons.effect_icon(key) or lead
        self._desc = desc or ""
        self._font = theme.font("body")
        self._desc_font = QFont("Segoe UI")         # QLabel#tileDesc in the style sheet
        self._desc_font.setPixelSize(10)
        self._fm = QFontMetrics(self._font)
        global _HEART_PX
        if _HEART_PX is None:
            _HEART_PX = _heart_px()
        self._dfm = QFontMetrics(self._desc_font)
        self.setFixedHeight(self.H)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)   # equal columns, like CTk "uniform"
        self.setMouseTracking(True)
        self.setAttribute(Qt.WA_OpaquePaintEvent, False)

    # what the panel calls (the old tile's switch / heart did these)
    def isChecked(self):
        return self._on

    def setChecked(self, on):
        if bool(on) != self._on:
            self._on = bool(on)
            self.update(self._switch_rect().toAlignedRect())

    def set_favourite(self, on):
        if bool(on) != self._fav:
            self._fav = bool(on)
            self.update(self._heart_rect().toAlignedRect())

    def set_label(self, label):
        lead, self._label = icons.split(label)
        self._icon = icons.effect_icon(self._key) or lead
        self.update()

    def sizeHint(self):
        return QSize(260, self.H)

    # geometry
    def _heart_rect(self):
        return QRectF(self.width() - self.PAD_R - self.HEART_W, self.HEART_Y, self.HEART_W, self.HEART_H)

    def _switch_rect(self):
        right = self.width() - self.PAD_R - self.HEART_W - 4
        return QRectF(self.PAD_L, self.SW_Y, max(0, right - self.PAD_L), self.SW_H)

    def _part(self, pos):
        if self._heart_rect().contains(pos):
            return "heart"
        if self._switch_rect().contains(pos):
            return "switch"
        return None

    # input
    def mouseMoveEvent(self, e):
        part = self._part(e.position())
        if part != self._hover:
            self._hover = part
            self.setCursor(Qt.PointingHandCursor if part else Qt.ArrowCursor)
            self.update()

    def leaveEvent(self, e):
        if self._hover is not None:
            self._hover = None
            self.update()
        super().leaveEvent(e)

    def mousePressEvent(self, e):
        if e.button() != Qt.LeftButton:
            return
        part = self._part(e.position())
        if part == "switch":
            self.setChecked(not self._on)
            self._panel._user_toggle(self._key, self._on)
        elif part == "heart":
            self._panel.toggle_favourite(self._key)

    # drawing
    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        # card: bg_input, 1 px border, radius 10 (QFrame#tile in the style sheet)
        p.setPen(QPen(QColor(COLORS["border"]), 1))
        p.setBrush(QColor(COLORS["bg_input"]))
        p.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 10, 10)
        # switch track and knob (as ToggleSwitch.paintEvent)
        sw = self._switch_rect()
        h, w = self.TRACK_H, self.TRACK_W
        y = sw.top() + (sw.height() - h) / 2
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(COLORS["switch_on"] if self._on else COLORS["switch_off"]))
        p.drawRoundedRect(QRectF(sw.left(), y, w, h), h / 2, h / 2)
        k = 2
        x0 = sw.left() + (w - h if self._on else 0)
        p.setBrush(QColor(COLORS["accent_secondary"] if self._hover == "switch" else COLORS["accent_primary"]))
        p.drawEllipse(QRectF(x0 + k, y + k, h - 2 * k, h - 2 * k))
        # icon and name
        text_color = QColor(COLORS["text_primary"])
        left = sw.left() + w + self.GAP
        if self._icon:
            size = max(14, self._fm.height() - 2)
            p.drawPixmap(int(left), int(round(sw.top() + (sw.height() - size) / 2)),
                         icons.pixmap(self._icon, text_color, size))
            left += size + 6                # widgets.ICON_GAP
        p.setPen(text_color)
        p.setFont(self._font)
        name = self._fm.elidedText(self._label, Qt.ElideRight, int(max(0, sw.right() - left)))
        p.drawText(QRectF(left, sw.top(), max(0, sw.right() - left), sw.height()),
                   Qt.AlignVCenter | Qt.AlignLeft, name)
        # heart (QPushButton[role="heart"]: hover background bg_card, accent when on)
        hr = self._heart_rect()
        if self._hover == "heart":
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(COLORS["bg_card"]))
            p.drawRoundedRect(hr, 6, 6)
        hs = _HEART_PX
        hcolor = COLORS["accent_primary"] if self._fav else COLORS["text_muted"]
        p.drawPixmap(int(round(hr.center().x() - hs / 2)), int(round(hr.center().y() - hs / 2)),
                     icons.pixmap("heart-filled" if self._fav else "heart", hcolor, hs))
        # description
        if self._desc:
            p.setPen(QColor(COLORS["text_muted"]))
            p.setFont(self._desc_font)
            dw = max(0, self.width() - self.PAD_R - self.DESC_X)
            p.drawText(QRectF(self.DESC_X, self.DESC_Y, dw, self.DESC_H), Qt.AlignVCenter | Qt.AlignLeft,
                       self._dfm.elidedText(self._desc, Qt.ElideRight, int(dw)))


class _ResultRow(QFrame):
    """One search result: switch, name, description and heart. Clicking the
    row switches the effect; clicking the heart favourites it."""

    def __init__(self, panel, index):
        super().__init__()
        self.panel, self.index, self.key = panel, index, None
        self.setObjectName("resultRow")
        self.setCursor(Qt.PointingHandCursor)
        h = QHBoxLayout(self)
        h.setContentsMargins(10, 4, 8, 4)
        h.setSpacing(8)
        self.switch = ToggleSwitch("")
        self.switch.setFixedWidth(ToggleSwitch.TRACK_W + 2)
        self.switch.setAttribute(Qt.WA_TransparentForMouseEvents)   # the whole row is the button
        h.addWidget(self.switch)
        self.name = theme.label("")
        h.addWidget(self.name)
        self.desc = ElidedLabel("")
        self.desc.setObjectName("tileDesc")
        h.addWidget(self.desc, 1)
        self.heart = _heart(False)
        self.heart.clicked.connect(lambda: self.key and panel.toggle_favourite(self.key))
        h.addWidget(self.heart)

    def show_key(self, key, name, desc, on, favourite):
        self.key = key
        self.name.setText(name)
        self.name.set_icon_name(icons.effect_icon(key))
        self.desc.setText(desc)
        self.switch.setChecked(on)
        _paint_heart(self.heart, favourite)
        self.show()

    def set_active(self, on):
        if self.property("active") != bool(on):
            self.setProperty("active", bool(on))
            theme.repolish(self)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and self.key:
            self.panel.toggle_effect(self.key)

    def enterEvent(self, e):
        self.panel._set_active(self.index)
        super().enterEvent(e)


class _KeyFilter(QObject):
    """Keys in the search box: Enter, ↑, ↓, Esc; and the box's focus border."""

    def __init__(self, panel):
        super().__init__()
        self.p = panel

    def eventFilter(self, obj, e):
        t = e.type()
        if t == QEvent.KeyPress:
            k = e.key()
            if k in (Qt.Key_Return, Qt.Key_Enter):
                self.p._toggle_active()
                return True
            if k == Qt.Key_Down:
                self.p._move_active(1)
                return True
            if k == Qt.Key_Up:
                self.p._move_active(-1)
                return True
            if k == Qt.Key_Escape:
                self.p.clear_search()
                return True
        elif t in (QEvent.FocusIn, QEvent.FocusOut):
            box = self.p._search_box
            box.setProperty("focus", t == QEvent.FocusIn)
            theme.repolish(box)
        elif t == QEvent.Resize and obj is self.p._body:
            self.p._place_results()
        return False


class EffectsPanel:
    def __init__(self, on_edit_clones=None):
        self.on_edit_clones = on_edit_clones
        self.frame, lay = theme.card()

        # Title line: title on the left, result count and search on the right.
        head = QHBoxLayout()
        title = theme.label("✨  Voice Effects")
        title.setObjectName("cardTitle")
        head.addWidget(title)
        head.addStretch(1)
        self._filter = _KeyFilter(self)
        self._build_search(head)
        lay.addLayout(head)

        self._body = QWidget()
        body_l = QVBoxLayout(self._body)
        body_l.setContentsMargins(0, 0, 0, 0)
        body_l.setSpacing(4)
        lay.addWidget(self._body)
        self._body.installEventFilter(self._filter)

        # Favourites
        body_l.addWidget(theme.section_label("♥  Favourites"))
        self._fav_grid = self._grid(body_l)
        self._fav_empty = theme.label(f"Click {HEART_OFF} on any effect or cloned voice to keep it here.",
                                      muted=True, small=True)
        # Cloned Voices
        body_l.addSpacing(6)
        body_l.addWidget(theme.separator())
        body_l.addSpacing(4)
        clone_head = QHBoxLayout()
        clone_head.addWidget(theme.section_label("🧬  Cloned Voices"))
        clone_head.addStretch(1)
        self._edit_btn = theme.button("✏️", theme.ROLE_ICON)
        self._edit_btn.clicked.connect(self._on_edit_clicked)
        clone_head.addWidget(self._edit_btn)
        body_l.addLayout(clone_head)
        self._clone_grid = self._grid(body_l)
        self._clone_empty = theme.label("No cloned voices yet. Create one in Customization → Clone Voice.",
                                        muted=True, small=True)
        # Effects
        body_l.addSpacing(6)
        body_l.addWidget(theme.separator())
        body_l.addSpacing(4)
        body_l.addWidget(theme.section_label("🎛  Effects"))
        self._fx_grid = self._grid(body_l)

        # Results overlay: a child of the body, not in its layout.
        self._results = QFrame(self._body)
        self._results.setObjectName("results")
        rl = QVBoxLayout(self._results)
        rl.setContentsMargins(10, 10, 10, 10)
        rl.setSpacing(4)
        rh = QHBoxLayout()
        rh.addWidget(theme.section_label("🔍  Results"))
        rh.addStretch(1)
        rh.addWidget(theme.label("Click or Enter switches  ·  ↑ ↓ move  ·  Esc closes", muted=True, small=True))
        rl.addLayout(rh)
        self._res_list = QVBoxLayout()
        self._res_list.setSpacing(2)
        rl.addLayout(self._res_list)
        self._res_note = theme.label("", muted=True, small=True)
        rl.addWidget(self._res_note)
        self._results.hide()
        self._res_rows = []
        self._res_by_key = {}
        self._active = 0

        self._on = {}                   # key -> bool
        self._views = {}                # key -> [ToggleSwitch] showing it
        self._tiles = {}
        self._labels = {}               # key -> label text
        self._descs = {}
        self._groups = {}               # key -> exclusive group name
        self._subscribers = {}          # group -> [callback(active_key | None)]
        self._fav_keys = load_favourites()
        self._fav_tiles = {}
        self._fav_subscribers = []
        for effect in EFFECTS_REGISTRY:
            self._add_tile(effect)
        self._regrid()

    @staticmethod
    def _grid(parent_layout):
        holder = QWidget()
        g = QGridLayout(holder)
        g.setContentsMargins(0, 0, 0, 0)
        g.setSpacing(8)
        for c in range(NUM_COLS):
            g.setColumnStretch(c, 1)
        parent_layout.addWidget(holder)
        return g

    # ── search ─────────────────────────────────────────────────────
    def _build_search(self, head):
        self._query = []
        self._typed = ""
        self._search_timer = QTimer()
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(SEARCH_DELAY_MS)
        self._search_timer.timeout.connect(self._apply_search)
        self._search_count = theme.label("", muted=True, small=True)
        head.addWidget(self._search_count)
        head.addSpacing(6)
        box = QFrame()
        box.setObjectName("searchBox")
        bl = QHBoxLayout(box)
        bl.setContentsMargins(8, 1, 4, 1)
        bl.setSpacing(2)
        icon = QLabel()
        icon.setPixmap(icons.pixmap("search", COLORS["text_muted"], 14))
        bl.addWidget(icon)
        self._search_entry = QLineEdit()
        self._search_entry.setPlaceholderText("Search effects and voices")
        self._search_entry.setFixedWidth(190)
        self._search_entry.setToolTip(SEARCH_TIP)
        self._search_entry.textChanged.connect(self._on_typed)
        self._search_entry.installEventFilter(self._filter)
        bl.addWidget(self._search_entry)
        self._search_clear = theme.button("✖", theme.ROLE_ICON, "Clear the search (Esc).")
        self._search_clear.setStyleSheet("border: none; min-height: 22px; max-height: 22px;")
        self._search_clear.clicked.connect(self.clear_search)
        self._search_clear.hide()
        bl.addWidget(self._search_clear)
        self._search_box = box
        head.addWidget(box)

    def _on_typed(self, text):
        if text == self._typed:
            return
        self._typed = text
        self._search_clear.setVisible(bool(text))
        self._search_timer.start()

    def _apply_search(self):
        self._search_timer.stop()
        query = _words(self._typed)
        if query != self._query:
            self._query = query
            self._refresh_results()

    def clear_search(self):
        self._search_entry.clear()          # _on_typed hides ✖
        self._apply_search()

    def _rank(self, key):
        """None when *key* does not match; else 0 every word starts a word of the
        name, 1 all in the name, 2 elsewhere (lower is better)."""
        name = _words(self._labels[key])
        section = "cloned voice clone" if self._groups.get(key) == CLONE_GROUP else "effect"
        hay = " ".join(name + _words(f"{self._descs.get(key, '')} {key} {section}"))
        if not all(w in hay for w in self._query):
            return None
        if all(any(n.startswith(w) for n in name) for w in self._query):
            return 0
        return 1 if all(w in " ".join(name) for w in self._query) else 2

    def _refresh_results(self):
        self._res_by_key.clear()
        if not self._query:
            for row in self._res_rows:
                row.key = None
            self._results.hide()
            self._body.setMinimumHeight(0)
            self._active = 0
            self._search_count.setText("")
            return
        order = {e["key"]: i for i, e in enumerate(EFFECTS_REGISTRY)}
        ranked = sorted((r, order.get(k, 0), k) for k in self._tiles
                        for r in [self._rank(k)] if r is not None)
        matches = [k for _r, _o, k in ranked]
        shown = matches[:MAX_RESULTS]
        while len(self._res_rows) < len(shown):
            row = _ResultRow(self, len(self._res_rows))
            self._res_rows.append(row)
            self._res_list.addWidget(row)
        for i, row in enumerate(self._res_rows):
            if i < len(shown):
                key = shown[i]
                row.show_key(key, self._labels[key], self._descs.get(key, ""), self._on[key],
                             key in self._fav_keys)
                self._res_by_key[key] = row
            else:
                row.key = None
                row.hide()
        self._set_active(0)
        n = len(matches)
        self._search_count.setText(f"{n} result{'s' if n != 1 else ''}" if n else "")
        typed = self._typed.strip()
        if not n:
            note = f"No effects or voices match “{typed}”."
        elif n > len(shown):
            note = f"Showing {len(shown)} of {n}. Type more to narrow it down."
        else:
            note = ""
        self._res_note.setText(note)
        self._res_note.setVisible(bool(note))
        self._results.show()
        self._place_results()
        self._results.raise_()

    def _place_results(self):
        """Lay the Results panel over the top of the tiles, full width."""
        if not self._results.isVisible():
            return
        self._results.layout().activate()
        h = self._results.sizeHint().height()
        self._results.setGeometry(0, 0, self._body.width(), h)
        # With few tiles the body could be shorter than the panel: make room.
        # Compare with the body's natural height, not its current one, so a
        # resize cannot flip the minimum on and off.
        natural = self._body.layout().sizeHint().height()
        self._body.setMinimumHeight(h if h > natural else 0)

    def _shown_rows(self):
        return [r for r in self._res_rows if r.key is not None]

    def _set_active(self, i):
        rows = self._shown_rows()
        if not rows:
            self._active = 0
            return
        self._active = max(0, min(i, len(rows) - 1))
        for j, row in enumerate(rows):
            row.set_active(j == self._active)

    def _move_active(self, step):
        if self._query:
            self._apply_search()
            self._set_active(self._active + step)

    def _toggle_active(self):
        """Enter: switch the highlighted result on or off."""
        self._apply_search()
        rows = self._shown_rows()
        if rows:
            self.toggle_effect(rows[min(self._active, len(rows) - 1)].key)

    def toggle_effect(self, key):
        """Switch *key* on or off as its switch would, with the group rule."""
        if key in self._on:
            self.set_active(key, not self._on[key])

    def _on_edit_clicked(self):
        if self.on_edit_clones:
            self.on_edit_clones()

    # ── state ──────────────────────────────────────────────────────
    def _show_state(self, key):
        """Redraw every switch that shows *key*."""
        on = self._on.get(key, False)
        for sw in self._views.get(key, []):
            if sw.isChecked() != on:
                sw.blockSignals(True)
                sw.setChecked(on)
                sw.blockSignals(False)
        row = self._res_by_key.get(key)
        if row is not None:
            row.switch.setChecked(on)

    def _set(self, key, value):
        self._on[key] = bool(value)
        self._show_state(key)

    def _user_toggle(self, key, checked):
        """A tile or mirror switch was clicked."""
        self._set(key, checked)
        if key in self._groups:
            self._on_toggle(key)

    # ── tiles ──────────────────────────────────────────────────────
    def _add_tile(self, effect):
        key, label, desc = effect["key"], effect["label"], effect["desc"]
        self._on[key] = bool(effect.get("default", False))
        self._labels[key] = label
        self._descs[key] = desc
        group = effect.get("exclusive_group")
        if group:
            self._groups[key] = group
        tile = _Tile(self, key, label, desc, self._on[key])
        self._tiles[key] = tile
        self._views[key] = [tile]
        if key in self._fav_keys:
            self._add_mirror(key)

    def _add_mirror(self, key):
        if key in self._fav_tiles or key not in self._tiles:
            return
        tile = _Tile(self, key, self._labels[key], self._descs.get(key, ""), self._on[key])
        self._fav_tiles[key] = tile
        self._views[key].append(tile)

    def _remove_mirror(self, key):
        tile = self._fav_tiles.pop(key, None)
        if tile is not None:
            if key in self._views and tile in self._views[key]:
                self._views[key].remove(tile)
            tile.setParent(None)
            tile.deleteLater()

    def _paint_hearts(self, key):
        on = key in self._fav_keys
        for t in (self._tiles.get(key), self._fav_tiles.get(key)):
            if t is not None:
                t.set_favourite(on)
        row = self._res_by_key.get(key)
        if row is not None:
            _paint_heart(row.heart, on)

    # ── favourites ─────────────────────────────────────────────────
    def is_favourite(self, key) -> bool:
        return key in self._fav_keys

    def toggle_favourite(self, key):
        """Add *key* to Favourites, or remove it; the change is saved."""
        if key in self._fav_keys:
            self._fav_keys.remove(key)
            self._remove_mirror(key)
        elif key in self._tiles:
            self._fav_keys.append(key)
            self._add_mirror(key)
        else:
            return
        self._paint_hearts(key)
        save_favourites(self._fav_keys)
        self._regrid()
        self._notify_favourites()

    def subscribe_favourites(self, callback):
        """callback() after a key is added to or removed from Favourites."""
        self._fav_subscribers.append(callback)

    def _notify_favourites(self):
        for cb in self._fav_subscribers:
            try:
                cb()
            except Exception as e:
                print(f"Favourites subscriber failed: {e}")

    def _remove_tile(self, key):
        self._remove_mirror(key)
        if key in self._fav_keys:               # the effect is gone (a deleted voice)
            self._fav_keys.remove(key)
            save_favourites(self._fav_keys)
            self._notify_favourites()
        tile = self._tiles.pop(key, None)
        if tile is not None:
            tile.setParent(None)
            tile.deleteLater()
        self._views.pop(key, None)
        self._labels.pop(key, None)
        self._descs.pop(key, None)
        was_on = self._on.pop(key, False)
        group = self._groups.pop(key, None)
        if group and was_on:
            self._notify(group)

    @staticmethod
    def _place(grid, widgets, empty_label):
        while grid.count():
            grid.takeAt(0)
        for i, w in enumerate(widgets):
            r, c = divmod(i, NUM_COLS)
            grid.addWidget(w, r, c)
            w.show()
        if empty_label is None:
            return
        if widgets:
            empty_label.hide()
        else:
            grid.addWidget(empty_label, 0, 0, 1, NUM_COLS)
            empty_label.show()

    def _regrid(self):
        clone_keys, fx_keys = [], []
        for e in EFFECTS_REGISTRY:
            if e["key"] in self._tiles:
                (clone_keys if e.get("exclusive_group") == CLONE_GROUP else fx_keys).append(e["key"])
        self._place(self._clone_grid, [self._tiles[k] for k in clone_keys], self._clone_empty)
        self._place(self._fx_grid, [self._tiles[k] for k in fx_keys], None)
        fav = [self._fav_tiles[k] for k in self._fav_keys if k in self._fav_tiles]
        self._place(self._fav_grid, fav, self._fav_empty)
        self._place_results()

    def sync(self):
        """Match tiles to EFFECTS_REGISTRY: add new entries, drop removed ones, re-grid."""
        registry_keys = {e["key"] for e in EFFECTS_REGISTRY}
        for key in [k for k in self._tiles if k not in registry_keys]:
            self._remove_tile(key)
        for effect in EFFECTS_REGISTRY:
            key = effect["key"]
            if key not in self._tiles:
                self._add_tile(effect)
            elif self._labels[key] != effect["label"]:
                self._labels[key] = effect["label"]
                for t in (self._tiles[key], self._fav_tiles.get(key)):
                    if t is not None:
                        t.set_label(effect["label"])
        self._regrid()
        if self._query:                     # new, renamed or deleted voices may match now
            self._refresh_results()

    # ── exclusive groups ───────────────────────────────────────────
    def _on_toggle(self, key):
        group = self._groups.get(key)
        if group and self._on.get(key):
            for other, g in self._groups.items():
                if g == group and other != key and self._on.get(other):
                    self._set(other, False)
        if group:
            self._notify(group)

    def _notify(self, group):
        active = self.active_in_group(group)
        for cb in self._subscribers.get(group, []):
            try:
                cb(active)
            except Exception as e:
                print(f"Effects subscriber failed: {e}")

    def subscribe(self, group, callback):
        """callback(active_key_or_None) after any change in *group*."""
        self._subscribers.setdefault(group, []).append(callback)

    def active_in_group(self, group):
        for key, g in self._groups.items():
            if g == group and self._on.get(key):
                return key
        return None

    def set_active(self, key, value=True):
        """Switch an effect on or off from code, applying its group rule."""
        if key not in self._on:
            return
        self._set(key, value)
        if key in self._groups:
            self._on_toggle(key)

    def clear_group(self, group):
        for key, g in self._groups.items():
            if g == group:
                self._set(key, False)
        self._notify(group)

    def get_active_effects(self) -> dict:
        return dict(self._on)
