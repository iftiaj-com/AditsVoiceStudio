"""
ui/panels/clone_voice_dialog.py  –  rename / delete cloned voices

Opened by the ✏ button in the "Cloned Voices" header of the Voice Effects
card. One row per voice in the library (including drafts and failed
trainings, so they can be cleaned up): editable name, kind, size, Save,
Delete. Changes go through core.voice_cloning.library, then the app
refreshes the registry, the effect tiles and the Clone Voice picker.
"""

import os
import subprocess
from tkinter import messagebox

import customtkinter as ctk

from core.constants import COLORS, FONTS
from core.voice_cloning import library, runtime


def _folder_mb(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total / (1024 * 1024)


class ManageVoicesDialog(ctk.CTkToplevel):
    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.title("Cloned Voices")
        self.configure(fg_color=COLORS["bg_dark"])
        self.geometry("560x420")
        self.minsize(480, 260)
        self.transient(app)
        self.resizable(True, True)

        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(fill="x", padx=16, pady=(14, 6))
        ctk.CTkLabel(head, text="✏  Cloned Voices", font=FONTS["subtitle"],
                     text_color=COLORS["text_primary"]).pack(side="left")
        ctk.CTkLabel(self, text="Rename a voice and press Save, or delete it. Deleting removes its "
                                "recordings and model from disk.",
                     font=FONTS["body_small"], text_color=COLORS["text_muted"],
                     wraplength=520, justify="left").pack(anchor="w", padx=16, pady=(0, 8))

        self._list = ctk.CTkScrollableFrame(self, fg_color=COLORS["bg_card"], corner_radius=12,
                                            border_width=1, border_color=COLORS["border"],
                                            scrollbar_button_color=COLORS["border"],
                                            scrollbar_button_hover_color=COLORS["accent_primary"])
        self._list.pack(fill="both", expand=True, padx=16, pady=(0, 10))
        self._list.columnconfigure(0, weight=1)

        foot = ctk.CTkFrame(self, fg_color="transparent")
        foot.pack(fill="x", padx=16, pady=(0, 14))
        ctk.CTkButton(foot, text="📂  Open folder", font=FONTS["button"], fg_color=COLORS["bg_input"],
                      hover_color=COLORS["accent_primary"], border_color=COLORS["accent_primary"],
                      border_width=1, corner_radius=8, height=32, width=130,
                      command=self._open_folder).pack(side="left")
        ctk.CTkButton(foot, text="Close", font=FONTS["button"], fg_color=COLORS["accent_primary"],
                      hover_color=COLORS["accent_secondary"], corner_radius=8, height=32, width=90,
                      command=self.destroy).pack(side="right")

        self._rows = []
        self._build_rows()
        self.after(100, self._focus)

    def _focus(self):
        try:
            self.lift()
            self.focus_force()
            self.grab_set()
        except Exception:
            pass

    def _build_rows(self):
        for r in self._rows:
            r.destroy()
        self._rows = []
        voices = library.list_voices()
        if not voices:
            lbl = ctk.CTkLabel(self._list, text="No cloned voices yet. Create one in "
                                                "Customization → Clone Voice.",
                               font=FONTS["body"], text_color=COLORS["text_muted"])
            lbl.grid(row=0, column=0, padx=12, pady=20, sticky="w")
            self._rows.append(lbl)
            return
        for i, v in enumerate(voices):
            self._rows.append(self._row(i, v))

    def _row(self, i, v):
        row = ctk.CTkFrame(self._list, fg_color=COLORS["bg_input"], corner_radius=10,
                           border_width=1, border_color=COLORS["border"])
        row.grid(row=i, column=0, sticky="ew", padx=6, pady=4)
        row.columnconfigure(0, weight=1)

        name_var = ctk.StringVar(value=v.name)
        entry = ctk.CTkEntry(row, textvariable=name_var, font=FONTS["body"], height=30,
                             fg_color=COLORS["bg_card"], border_color=COLORS["border"],
                             text_color=COLORS["text_primary"])
        entry.grid(row=0, column=0, sticky="ew", padx=(10, 8), pady=(8, 2))

        kind = "RVC" + (" · imported" if v.imported else "") if v.mode == "rvc" else "Quick"
        state = {"ready": "", "training": " · training", "draft": " · unfinished",
                 "failed": " · failed"}.get(v.status, f" · {v.status}")
        size = _folder_mb(v.dir)
        meta = ctk.CTkLabel(row, text=f"🧬 {kind}{state} · {size:.0f} MB", font=FONTS["body_small"],
                            text_color=COLORS["warning"] if state else COLORS["text_muted"])
        meta.grid(row=1, column=0, sticky="w", padx=12, pady=(0, 8))

        save = ctk.CTkButton(row, text="Save", font=FONTS["button"], width=70, height=30,
                             corner_radius=8, fg_color=COLORS["accent_primary"],
                             hover_color=COLORS["accent_secondary"], state="disabled",
                             command=lambda: self._rename(v, name_var.get()))
        save.grid(row=0, column=1, padx=(0, 6), pady=(8, 2))
        delete = ctk.CTkButton(row, text="🗑  Delete", font=FONTS["button"], width=90, height=30,
                               corner_radius=8, fg_color=COLORS["bg_card"], hover_color=COLORS["error"],
                               border_color=COLORS["error"], border_width=1,
                               command=lambda: self._delete(v))
        delete.grid(row=0, column=2, padx=(0, 10), pady=(8, 2))

        def on_edit(*_):
            changed = name_var.get().strip() and name_var.get().strip() != v.name
            save.configure(state="normal" if changed else "disabled")
        name_var.trace_add("write", on_edit)
        entry.bind("<Return>", lambda e: save.cget("state") == "normal" and self._rename(v, name_var.get()))
        return row

    def _busy_with(self, v, action):
        from core.voice_cloning import is_training
        if v.status == "training" and is_training():
            messagebox.showinfo("Voice is training", f"'{v.name}' is training. Cancel the training "
                                f"first, then {action} it.", parent=self)
            return True
        if self.app._is_generating:
            messagebox.showinfo("Generating", f"Wait for the current generation to finish, then {action} "
                                "the voice.", parent=self)
            return True
        return False

    def _rename(self, v, new_name):
        new_name = new_name.strip()[:40]
        if not new_name or new_name == v.name:
            return
        if any(o.name.lower() == new_name.lower() and o.slug != v.slug for o in library.list_voices()):
            messagebox.showwarning("Name in use", f"A voice named '{new_name}' already exists.", parent=self)
            return
        if self._busy_with(v, "rename"):
            return
        library.update_params(v.slug, name=new_name)
        self.app.refresh_cloned_voices()
        self.app._set_status(f"✏  Renamed '{v.name}' to '{new_name}'", COLORS["success"])
        self._build_rows()

    def _delete(self, v):
        if self._busy_with(v, "delete"):
            return
        if not messagebox.askyesno("Delete voice",
                                   f"Delete '{v.name}'?\n\nIts recordings and model are removed "
                                   "from disk. This cannot be undone.",
                                   icon="warning", parent=self):
            return
        library.delete_voice(v.slug)
        self.app.refresh_cloned_voices()
        self.app._set_status(f"🗑  Deleted voice '{v.name}'", COLORS["warning"])
        self._build_rows()

    def _open_folder(self):
        d = runtime.voices_dir()
        d.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(str(d))
        except AttributeError:
            subprocess.Popen(["xdg-open", str(d)])
