"""
ui/panels/effects_panel.py  –  Voice effects card

Three sub-sections inside one card:

  ♥ Favourites              a mirror tile for every effect whose heart is on
  🧬 Cloned Voices  [✏]     tiles for entries with exclusive_group "clone",
                            plus a pencil button that opens rename/delete
  🎛 Effects                every other EFFECTS_REGISTRY entry

Tiles are built from EFFECTS_REGISTRY and kept by key. Cloned voices can
appear and disappear while the app runs, so sync() adds, removes, renames
and re-grids tiles. Entries that share an "exclusive_group" act like radio
buttons: switching one on switches the others in that group off.

Every tile has a heart on the right of its switch. Filling it adds a mirror
tile to Favourites; the mirror's switch shares the original's BooleanVar,
so both always show the same state. Favourite keys are saved, in the order
they were added, to <user>/settings/favourites.json.

The search box on the title line looks through cloned voices and effects as
the user types: every word must appear in a tile's name, description or
section. Matches show as rows in a Results panel laid over the top of the
card, names starting with the words first, at most MAX_RESULTS. Each row
shares the effect's BooleanVar. Clicking a row switches it; ↑ / ↓ move the
highlight, Enter switches the highlighted row, Esc clears the search.

Speed (about 170 tiles, most of them cloned voices): hiding and re-showing
the tiles themselves took 0.6 to 1.5 s, and a Results section in the layout
moved every widget under it (0.4 to 1.2 s a keystroke). The overlay takes no
room, and the rows are plain Tk widgets (_ResultRow), so a search only
re-labels a few rows.
"""
import json
import os
import re
import tkinter as tk

import customtkinter as ctk
from core import paths
from core.constants import COLORS, FONTS
from effects.registry import EFFECTS_REGISTRY

CLONE_GROUP = "clone"
HEART_OFF, HEART_ON = "♡", "♥"
HEART_FONT = ("Segoe UI Symbol", 21)
SECTION_FONT = ("Segoe UI", 12, "bold")
SEARCH_DELAY_MS = 120   # search once typing pauses
MAX_RESULTS = 8         # rows in the Results panel
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


class _ResultRow:
    """One search result: switch, name, description and heart in a single row.

    Plain Tk widgets, not CTk: a CTk tile redraws its canvases on every
    change (30 to 80 ms per tile here), a Tk label in about a millisecond.
    Rows are kept and re-pointed at other effects as the search changes.
    Clicking the row switches the effect; clicking the heart favourites it.
    """

    def __init__(self, parent, panel, index):
        self.panel, self.index = panel, index
        self.key = self._var = self._trace = None
        s = parent._get_widget_scaling() if hasattr(parent, "_get_widget_scaling") else 1.0
        self._s = s
        px = lambda n: max(1, round(n * s))         # noqa: E731
        self._pad = px(2)
        bg = COLORS["bg_input"]
        f = tk.Frame(parent, bg=bg, highlightthickness=1, highlightbackground=COLORS["border"],
                     highlightcolor=COLORS["border"], cursor="hand2")
        self.frame = f
        self.pill = tk.Canvas(f, width=px(36), height=px(18), bg=bg, highlightthickness=0)
        self.pill.pack(side="left", padx=(px(10), px(8)), pady=px(8))
        self.name = tk.Label(f, bg=bg, fg=COLORS["text_primary"], anchor="w",
                             font=(FONTS["body"][0], -px(FONTS["body"][1])))
        self.name.pack(side="left")
        self.heart = tk.Label(f, bg=bg, cursor="hand2", font=(HEART_FONT[0], -px(17)))
        self.heart.pack(side="right", padx=(0, px(8)))
        self.desc = tk.Label(f, bg=bg, fg=COLORS["text_muted"], anchor="w",
                             font=("Segoe UI", -px(10)))
        self.desc.pack(side="left", fill="x", expand=True, padx=(px(10), px(6)))
        for w in (f, self.pill, self.name, self.desc):
            w.bind("<Button-1>", lambda e: self.panel.toggle_effect(self.key))
            w.bind("<Enter>", lambda e: self.panel._set_active(self.index))
        self.heart.bind("<Button-1>", lambda e: self.panel.toggle_favourite(self.key))
        self.heart.bind("<Enter>", lambda e: self.panel._set_active(self.index))

    def show(self, key, name, desc, var, favourite):
        if key != self.key or var is not self._var:
            self.unbind_var()
            self.key, self._var = key, var
            self._trace = var.trace_add("write", lambda *_: self.draw())
        if self.name.cget("text") != name:
            self.name.configure(text=name)
        if self.desc.cget("text") != desc:
            self.desc.configure(text=desc)
        self.paint_heart(favourite)
        self.draw()
        if not self.frame.winfo_manager():
            self.frame.pack(fill="x", padx=self._pad, pady=self._pad)

    def hide(self):
        self.unbind_var()
        if self.frame.winfo_manager():
            self.frame.pack_forget()

    def unbind_var(self):
        if self._var is not None and self._trace is not None:
            try:
                self._var.trace_remove("write", self._trace)
            except Exception:
                pass
        self.key = self._var = self._trace = None

    def set_active(self, on):
        color = COLORS["accent_primary"] if on else COLORS["border"]
        self.frame.configure(highlightbackground=color, highlightcolor=color)

    def paint_heart(self, favourite):
        self.heart.configure(text=HEART_ON if favourite else HEART_OFF,
                             fg=COLORS["accent_primary"] if favourite else COLORS["text_muted"])

    def draw(self):
        """The switch, drawn like the CTk ones: a track and a round knob."""
        c, s = self.pill, self._s
        on = bool(self._var.get()) if self._var is not None else False
        w, h = round(36 * s), round(18 * s)
        c.delete("all")
        track = COLORS["switch_on"] if on else COLORS["switch_off"]
        c.create_oval(0, 0, h, h, fill=track, outline=track)
        c.create_oval(w - h, 0, w, h, fill=track, outline=track)
        c.create_rectangle(h / 2, 0, w - h / 2, h, fill=track, outline=track)
        k = round(2 * s)
        x0 = w - h if on else 0
        knob = COLORS["accent_primary"]
        c.create_oval(x0 + k, k, x0 + h - k, h - k, fill=knob, outline=knob)



class EffectsPanel:
    def __init__(self, parent, row=None, on_edit_clones=None):
        self.on_edit_clones = on_edit_clones
        frame = ctk.CTkFrame(
            parent, fg_color=COLORS["bg_card"],
            corner_radius=14, border_width=1, border_color=COLORS["border"]
        )
        if row is not None:
            frame.grid(row=row, column=0, sticky="ew", padx=24, pady=6)
            num_cols = 4
        else:
            frame.pack(fill="x", padx=16, pady=6)
            num_cols = 2

        # Title line: title on the left, search on the right.
        head = ctk.CTkFrame(frame, fg_color="transparent")
        head.pack(fill="x", padx=16, pady=(14, 10))
        ctk.CTkLabel(
            head, text="✨  Voice Effects",
            font=FONTS["subtitle"],
            text_color=COLORS["text_primary"],
        ).pack(side="left")
        self._build_search(head)

        body = ctk.CTkFrame(frame, fg_color="transparent")
        body.pack(fill="x", padx=16, pady=(0, 14))
        self._num_cols = num_cols

        def section(sep=True):
            sec = ctk.CTkFrame(body, fg_color="transparent")
            sec.pack(fill="x")
            if sep:
                ctk.CTkFrame(sec, height=1, fg_color=COLORS["border"]).pack(fill="x", padx=4, pady=(10, 8))
            return sec

        # ── Results panel: placed over the top of the body while searching ──
        # An overlay takes no room in the layout, so nothing below it moves.
        # Moving the ~900 widgets under it cost 0.4 to 1.2 s per keystroke.
        self._res_sec = ctk.CTkFrame(body, fg_color=COLORS["bg_card"], corner_radius=10,
                                     border_width=1, border_color=COLORS["accent_primary"])
        res_head = ctk.CTkFrame(self._res_sec, fg_color="transparent")
        res_head.pack(fill="x", padx=12, pady=(10, 4))
        ctk.CTkLabel(res_head, text="🔍  Results", font=SECTION_FONT,
                     text_color=COLORS["text_secondary"]).pack(side="left")
        ctk.CTkLabel(res_head, text="Click or Enter switches  ·  ↑ ↓ move  ·  Esc closes",
                     font=FONTS["body_small"], text_color=COLORS["text_muted"]).pack(side="right")
        self._res_list = ctk.CTkFrame(self._res_sec, fg_color="transparent")
        self._res_list.pack(fill="x", padx=10, pady=(0, 10))
        self._res_note = ctk.CTkLabel(self._res_sec, text="", font=FONTS["body_small"],
                                      text_color=COLORS["text_muted"], anchor="w")
        self._res_rows: list = []       # _ResultRow, reused between searches
        self._res_by_key: dict = {}     # key -> row showing it now
        self._active = 0                # highlighted row, switched by Enter

        # ── Favourites sub-section ─────────────────────────────────
        self._fav_sec = section(sep=False)
        ctk.CTkLabel(self._fav_sec, text="♥  Favourites", font=SECTION_FONT,
                     text_color=COLORS["text_secondary"]).pack(anchor="w", padx=4, pady=(0, 4))
        self._fav_grid = ctk.CTkFrame(self._fav_sec, fg_color="transparent")
        self._fav_grid.pack(fill="x")
        self._fav_grid.columnconfigure(tuple(range(num_cols)), weight=1, uniform="fav")
        self._fav_empty = ctk.CTkLabel(
            self._fav_grid, text=f"Click {HEART_OFF} on any effect or cloned voice to keep it here.",
            font=FONTS["body_small"], text_color=COLORS["text_muted"], anchor="w")

        # ── Cloned Voices sub-section ──────────────────────────────
        self._clone_sec = section()
        clone_head = ctk.CTkFrame(self._clone_sec, fg_color="transparent")
        clone_head.pack(fill="x", padx=4, pady=(0, 4))
        ctk.CTkLabel(clone_head, text="🧬  Cloned Voices", font=SECTION_FONT,
                     text_color=COLORS["text_secondary"]).pack(side="left")
        self._edit_btn = ctk.CTkButton(
            clone_head, text="✏️", width=32, height=28, corner_radius=8,
            font=("Segoe UI Emoji", 13), fg_color="transparent",
            hover_color=COLORS["bg_input"], text_color=COLORS["text_secondary"],
            border_width=1, border_color=COLORS["border"],
            command=self._on_edit_clicked)
        self._edit_btn.pack(side="right")
        self._clone_grid = ctk.CTkFrame(self._clone_sec, fg_color="transparent")
        self._clone_grid.pack(fill="x")
        self._clone_grid.columnconfigure(tuple(range(num_cols)), weight=1, uniform="clone")
        self._clone_empty = ctk.CTkLabel(
            self._clone_grid,
            text="No cloned voices yet. Create one in Customization → Clone Voice.",
            font=FONTS["body_small"], text_color=COLORS["text_muted"], anchor="w")

        # ── Effects sub-section ────────────────────────────────────
        self._fx_sec = section()
        ctk.CTkLabel(self._fx_sec, text="🎛  Effects", font=SECTION_FONT,
                     text_color=COLORS["text_secondary"]).pack(anchor="w", padx=4, pady=(0, 4))
        self._fx_grid = ctk.CTkFrame(self._fx_sec, fg_color="transparent")
        self._fx_grid.pack(fill="x")
        self._fx_grid.columnconfigure(tuple(range(num_cols)), weight=1)

        self._vars: dict = {}
        self._tiles: dict = {}
        self._labels: dict = {}
        self._descs: dict = {}          # key -> description, for search
        self._groups: dict = {}         # key -> exclusive group name
        self._subscribers: dict = {}    # group -> [callback(active_key | None)]
        self._fav_keys = load_favourites()  # saved order; may name voices not loaded now
        self._fav_tiles: dict = {}      # key -> mirror tile in Favourites
        self._fav_labels: dict = {}     # key -> mirror switch
        self._hearts: dict = {}         # key -> heart on the original tile
        self._fav_hearts: dict = {}     # key -> heart on the mirror tile
        self._fav_subscribers: list = []  # callback() after Favourites change
        for effect in EFFECTS_REGISTRY:
            self._add_tile(effect)
        self._regrid()

    # ── search ─────────────────────────────────────────────────────
    def _build_search(self, head):
        """Search box on the right of the title line: 🔍, entry, ✖ (shown with text)."""
        from ui.panels.clone_voice_group import _Tip   # lazy: that module imports this one
        self._query = []                # words of the active search
        self._search_job = None
        box = ctk.CTkFrame(head, fg_color=COLORS["bg_input"], corner_radius=8,
                           border_width=1, border_color=COLORS["border"])
        box.pack(side="right")
        self._search_box = box
        ctk.CTkLabel(box, text="🔍", font=("Segoe UI Emoji", 11), width=20,
                     text_color=COLORS["text_muted"]).pack(side="left", padx=(8, 0))
        # no textvariable: CTkEntry shows no placeholder when it has one
        entry = ctk.CTkEntry(box, width=190, height=28,
                             border_width=0, fg_color=COLORS["bg_input"], font=FONTS["body_small"],
                             text_color=COLORS["text_primary"],
                             placeholder_text="Search effects and voices",
                             placeholder_text_color=COLORS["text_muted"])
        entry.pack(side="left", padx=(2, 0), pady=1)
        self._search_entry = entry
        self._search_clear = ctk.CTkButton(box, text="✖", width=24, height=24, corner_radius=6,
                                           font=FONTS["body_small"], fg_color="transparent",
                                           hover_color=COLORS["bg_card"],
                                           text_color=COLORS["text_secondary"],
                                           command=self.clear_search)
        _Tip(entry, SEARCH_TIP)
        _Tip(self._search_clear, "Clear the search (Esc).")
        self._search_count = ctk.CTkLabel(head, text="", font=FONTS["body_small"],
                                          text_color=COLORS["text_muted"])
        self._search_count.pack(side="right", padx=(0, 10))
        self._typed = ""
        entry.bind("<KeyRelease>", lambda e: self._on_typed(), add="+")
        entry.bind("<Return>", lambda e: self._toggle_active())
        entry.bind("<Down>", lambda e: self._move_active(1))
        entry.bind("<Up>", lambda e: self._move_active(-1))
        entry.bind("<Escape>", lambda e: self.clear_search())
        entry.bind("<FocusIn>", lambda e: box.configure(border_color=COLORS["accent_primary"]), add="+")
        entry.bind("<FocusOut>", lambda e: box.configure(border_color=COLORS["border"]), add="+")

    def _on_typed(self):
        """Any key in the box; search again once the text changed and typing pauses."""
        text = self._search_entry.get()     # "" while the placeholder shows
        if text == self._typed:
            return                          # arrows, Enter, Shift...
        self._typed = text
        has_text = bool(text)
        if has_text != self._search_clear.winfo_ismapped():
            if has_text:
                self._search_clear.pack(side="left", padx=(0, 4))
            else:
                self._search_clear.pack_forget()
        if self._search_job is not None:
            self._search_box.after_cancel(self._search_job)
        self._search_job = self._search_box.after(SEARCH_DELAY_MS, self._apply_search)

    def _apply_search(self):
        if self._search_job is not None:
            self._search_box.after_cancel(self._search_job)
            self._search_job = None
        query = _words(self._typed)
        if query != self._query:
            self._query = query
            self._refresh_results()

    def clear_search(self):
        self._search_entry.delete(0, "end")
        self._on_typed()                    # hides ✖
        self._apply_search()

    def _rank(self, key):
        """None when *key* does not match the search; else a sort key, lower is better:
        0 every word starts a word of the name, 1 all in the name, 2 elsewhere."""
        name = _words(self._labels[key].cget("text"))
        section = "cloned voice clone" if self._groups.get(key) == CLONE_GROUP else "effect"
        hay = " ".join(name + _words(f"{self._descs.get(key, '')} {key} {section}"))
        if not all(w in hay for w in self._query):
            return None
        if all(any(n.startswith(w) for n in name) for w in self._query):
            return 0
        return 1 if all(w in " ".join(name) for w in self._query) else 2

    def _refresh_results(self):
        """Show the matches as rows in the Results panel. Cloned Voices and
        Effects are left as they are, since re-gridding ~170 tiles takes about a second."""
        self._res_by_key.clear()
        if not self._query:
            for row in self._res_rows:
                row.unbind_var()
            self._res_sec.place_forget()
            self._active = 0
            self._search_count.configure(text="")
            return
        order = {e["key"]: i for i, e in enumerate(EFFECTS_REGISTRY)}
        ranked = []
        for key in self._tiles:
            r = self._rank(key)
            if r is not None:
                ranked.append((r, order.get(key, 0), key))
        matches = [k for _r, _o, k in sorted(ranked)]
        shown = matches[:MAX_RESULTS]
        while len(self._res_rows) < len(shown):
            self._res_rows.append(_ResultRow(self._res_list, self, len(self._res_rows)))
        for i, row in enumerate(self._res_rows):
            if i < len(shown):
                key = shown[i]
                row.show(key, self._labels[key].cget("text"), self._descs.get(key, ""),
                         self._vars[key], key in self._fav_keys)
                self._res_by_key[key] = row
            else:
                row.hide()
        self._set_active(0)

        n = len(matches)
        self._search_count.configure(text=f"{n} result{'s' if n != 1 else ''}" if n else "")
        # an empty CTkFrame keeps its default 200 px height, so the list goes when it has no rows
        if shown and not self._res_list.winfo_manager():
            above_note = {"before": self._res_note} if self._res_note.winfo_manager() else {}
            self._res_list.pack(fill="x", padx=10, pady=(0, 10), **above_note)
        elif not shown and self._res_list.winfo_manager():
            self._res_list.pack_forget()
        typed = self._typed.strip()
        if not n:
            note = f"No effects or voices match “{typed}”."
        elif n > len(shown):
            note = f"Showing {len(shown)} of {n}. Type more to narrow it down."
        else:
            note = ""
        if note != self._res_note.cget("text"):
            self._res_note.configure(text=note)
        if note and not self._res_note.winfo_manager():
            self._res_note.pack(fill="x", padx=14, pady=(0, 10))
        elif not note and self._res_note.winfo_manager():
            self._res_note.pack_forget()
        if not self._res_sec.winfo_manager():
            self._res_sec.place(x=0, y=0, relwidth=1.0)
            self._res_sec.lift()

    def _shown_rows(self):
        return [r for r in self._res_rows if r.key is not None]

    def _set_active(self, i):
        """Highlight row i: Enter switches it."""
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
        return "break"

    def _toggle_active(self):
        """Enter: switch the highlighted result on or off."""
        self._apply_search()                        # in case Enter came before the delay
        rows = self._shown_rows()
        if rows:
            self.toggle_effect(rows[min(self._active, len(rows) - 1)].key)
        return "break"

    def toggle_effect(self, key):
        """Switch *key* on or off as its switch would, with the group rule."""
        if key in self._vars:
            self.set_active(key, not self._vars[key].get())

    def _on_edit_clicked(self):
        if self.on_edit_clones:
            self.on_edit_clones()

    # ── tiles ──────────────────────────────────────────────────────
    def _build_tile(self, parent, key, label, desc, var, group):
        """One effect tile: switch and heart on the first line, description below."""
        tile = ctk.CTkFrame(parent, fg_color=COLORS["bg_input"],
                            corner_radius=10, border_width=1,
                            border_color=COLORS["border"])
        inner = ctk.CTkFrame(tile, fg_color="transparent")
        inner.pack(fill="x", padx=(10, 6), pady=6)
        inner.columnconfigure(0, weight=1)
        switch = ctk.CTkSwitch(inner, text=label, font=FONTS["body"],
                               text_color=COLORS["text_primary"], variable=var,
                               onvalue=True, offvalue=False,
                               button_color=COLORS["accent_primary"],
                               button_hover_color=COLORS["accent_secondary"],
                               progress_color=COLORS["switch_on"],
                               fg_color=COLORS["switch_off"],
                               switch_width=36, switch_height=18,
                               command=(lambda k=key: self._on_toggle(k)) if group else None)
        switch.grid(row=0, column=0, sticky="w")
        heart = ctk.CTkButton(inner, text=HEART_OFF, font=HEART_FONT, width=32, height=28,
                              corner_radius=6, fg_color="transparent", hover_color=COLORS["bg_card"],
                              text_color=COLORS["text_muted"],
                              command=lambda k=key: self.toggle_favourite(k))
        heart.grid(row=0, column=1, sticky="e")
        # anchor="w": on narrow windows long descriptions clip at the end, not both sides
        ctk.CTkLabel(inner, text=desc, font=("Segoe UI", 10), anchor="w",
                     text_color=COLORS["text_muted"]).grid(row=1, column=0, columnspan=2,
                                                           sticky="ew", padx=(42, 0))
        return tile, switch, heart

    def _add_tile(self, effect):
        key, label, desc = effect["key"], effect["label"], effect["desc"]
        var = ctk.BooleanVar(value=effect.get("default", False))
        group = effect.get("exclusive_group")
        self._vars[key] = var
        self._descs[key] = desc
        if group:
            self._groups[key] = group
        parent = self._clone_grid if group == CLONE_GROUP else self._fx_grid
        tile, switch, heart = self._build_tile(parent, key, label, desc, var, group)
        self._tiles[key] = tile
        self._labels[key] = switch
        self._hearts[key] = heart
        if key in self._fav_keys:
            self._add_mirror(effect)
        self._paint_hearts(key)

    # ── favourites ─────────────────────────────────────────────────
    def _add_mirror(self, effect):
        """A copy of the tile in Favourites, sharing the original's switch variable."""
        key = effect["key"]
        if key in self._fav_tiles or key not in self._vars:
            return
        tile, switch, heart = self._build_tile(self._fav_grid, key, self._labels[key].cget("text"),
                                               effect["desc"], self._vars[key], self._groups.get(key))
        self._fav_tiles[key] = tile
        self._fav_labels[key] = switch
        self._fav_hearts[key] = heart

    def _remove_mirror(self, key):
        self._fav_labels.pop(key, None)
        self._fav_hearts.pop(key, None)
        tile = self._fav_tiles.pop(key, None)
        if tile is not None:
            tile.destroy()

    def _paint_hearts(self, key):
        on = key in self._fav_keys
        if key in self._res_by_key:
            self._res_by_key[key].paint_heart(on)
        for h in (self._hearts.get(key), self._fav_hearts.get(key)):
            if h is not None:
                h.configure(text=HEART_ON if on else HEART_OFF,
                            text_color=COLORS["accent_primary"] if on else COLORS["text_muted"])

    def is_favourite(self, key) -> bool:
        return key in self._fav_keys

    def toggle_favourite(self, key):
        """Add *key* to Favourites, or remove it; the change is saved."""
        if key in self._fav_keys:
            self._fav_keys.remove(key)
            self._remove_mirror(key)
        elif key in self._tiles:
            self._fav_keys.append(key)
            self._add_mirror(next(e for e in EFFECTS_REGISTRY if e["key"] == key))
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
            tile.destroy()
        self._hearts.pop(key, None)
        self._labels.pop(key, None)
        self._descs.pop(key, None)
        var = self._vars.pop(key, None)
        group = self._groups.pop(key, None)
        if group and var is not None and var.get():
            self._notify(group)

    def _regrid(self):
        clone_keys, fx_keys = [], []
        for e in EFFECTS_REGISTRY:
            if e["key"] in self._tiles:
                (clone_keys if e.get("exclusive_group") == CLONE_GROUP else fx_keys).append(e["key"])
        for keys in (clone_keys, fx_keys):
            for i, key in enumerate(keys):
                r, c = divmod(i, self._num_cols)
                self._tiles[key].grid(row=r, column=c, padx=4, pady=4, sticky="nsew")
        fav_keys = [k for k in self._fav_keys if k in self._fav_tiles]
        for i, key in enumerate(fav_keys):
            r, c = divmod(i, self._num_cols)
            self._fav_tiles[key].grid(row=r, column=c, padx=4, pady=4, sticky="nsew")
        if fav_keys:
            self._fav_empty.grid_forget()
        else:
            self._fav_empty.grid(row=0, column=0, columnspan=self._num_cols,
                                 padx=6, pady=(2, 4), sticky="w")
        if clone_keys:
            self._clone_empty.grid_forget()
        else:
            self._clone_empty.grid(row=0, column=0, columnspan=self._num_cols,
                                   padx=6, pady=(2, 4), sticky="w")

    def sync(self):
        """Match tiles to EFFECTS_REGISTRY: add new entries, drop removed ones, re-grid."""
        registry_keys = {e["key"] for e in EFFECTS_REGISTRY}
        for key in [k for k in self._tiles if k not in registry_keys]:
            self._remove_tile(key)
        for effect in EFFECTS_REGISTRY:
            key = effect["key"]
            if key not in self._tiles:
                self._add_tile(effect)
            elif self._labels[key].cget("text") != effect["label"]:
                self._labels[key].configure(text=effect["label"])
                if key in self._fav_labels:
                    self._fav_labels[key].configure(text=effect["label"])
        self._regrid()
        if self._query:                     # new, renamed or deleted voices may match now
            self._refresh_results()

    # ── exclusive groups ───────────────────────────────────────────
    def _on_toggle(self, key):
        group = self._groups.get(key)
        if group and self._vars[key].get():
            for other, g in self._groups.items():
                if g == group and other != key:
                    self._vars[other].set(False)
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
            if g == group and self._vars[key].get():
                return key
        return None

    def set_active(self, key, value=True):
        """Switch an effect on or off from code, applying its group rule."""
        if key not in self._vars:
            return
        self._vars[key].set(bool(value))
        if key in self._groups:
            self._on_toggle(key)

    def clear_group(self, group):
        for key, g in self._groups.items():
            if g == group:
                self._vars[key].set(False)
        self._notify(group)

    def get_active_effects(self) -> dict:
        return {key: var.get() for key, var in self._vars.items()}
