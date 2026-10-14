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
"""
import json
import os

import customtkinter as ctk
from core import paths
from core.constants import COLORS, FONTS
from ui.theme import card
from effects.registry import EFFECTS_REGISTRY

CLONE_GROUP = "clone"
HEART_OFF, HEART_ON = "♡", "♥"
HEART_FONT = ("Segoe UI Symbol", 21)
SECTION_FONT = ("Segoe UI", 12, "bold")


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


class EffectsPanel:
    def __init__(self, parent, row=None, on_edit_clones=None):
        self.on_edit_clones = on_edit_clones
        if row is not None:
            frame = card(parent, row, "✨  Voice Effects")
            body = ctk.CTkFrame(frame, fg_color="transparent")
            body.pack(fill="x", padx=16, pady=(0, 14))
            num_cols = 4
        else:
            frame = ctk.CTkFrame(
                parent, fg_color=COLORS["bg_card"],
                corner_radius=14, border_width=1, border_color=COLORS["border"]
            )
            frame.pack(fill="x", padx=16, pady=6)

            ctk.CTkLabel(
                frame, text="✨  Voice Effects",
                font=FONTS["subtitle"],
                text_color=COLORS["text_primary"],
            ).pack(anchor="w", padx=16, pady=(14, 10))

            body = ctk.CTkFrame(frame, fg_color="transparent")
            body.pack(fill="x", padx=16, pady=(0, 14))
            num_cols = 2

        self._num_cols = num_cols

        # ── Favourites sub-section ─────────────────────────────────
        ctk.CTkLabel(body, text="♥  Favourites", font=SECTION_FONT,
                     text_color=COLORS["text_secondary"]).pack(anchor="w", padx=4, pady=(0, 4))
        self._fav_grid = ctk.CTkFrame(body, fg_color="transparent")
        self._fav_grid.pack(fill="x")
        self._fav_grid.columnconfigure(tuple(range(num_cols)), weight=1, uniform="fav")
        self._fav_empty = ctk.CTkLabel(
            self._fav_grid, text=f"Click {HEART_OFF} on any effect or cloned voice to keep it here.",
            font=FONTS["body_small"], text_color=COLORS["text_muted"], anchor="w")

        ctk.CTkFrame(body, height=1, fg_color=COLORS["border"]).pack(fill="x", padx=4, pady=(10, 8))

        # ── Cloned Voices sub-section ──────────────────────────────
        clone_head = ctk.CTkFrame(body, fg_color="transparent")
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
        self._clone_grid = ctk.CTkFrame(body, fg_color="transparent")
        self._clone_grid.pack(fill="x")
        self._clone_grid.columnconfigure(tuple(range(num_cols)), weight=1, uniform="clone")
        self._clone_empty = ctk.CTkLabel(
            self._clone_grid,
            text="No cloned voices yet. Create one in Customization → Clone Voice.",
            font=FONTS["body_small"], text_color=COLORS["text_muted"], anchor="w")

        ctk.CTkFrame(body, height=1, fg_color=COLORS["border"]).pack(fill="x", padx=4, pady=(10, 8))

        # ── Effects sub-section ────────────────────────────────────
        ctk.CTkLabel(body, text="🎛  Effects", font=SECTION_FONT,
                     text_color=COLORS["text_secondary"]).pack(anchor="w", padx=4, pady=(0, 4))
        self._fx_grid = ctk.CTkFrame(body, fg_color="transparent")
        self._fx_grid.pack(fill="x")
        self._fx_grid.columnconfigure(tuple(range(num_cols)), weight=1)

        self._vars: dict = {}
        self._tiles: dict = {}
        self._labels: dict = {}
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
