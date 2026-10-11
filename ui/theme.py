"""
ui/theme.py
───────────
Shared UI helpers and the _card() factory used across all panels.
Import COLORS and FONTS from here in all UI code.
"""

import customtkinter as ctk
from core.constants import COLORS, FONTS


def use_app_icon(win):
    """Give a pop-up window the app icon instead of CustomTkinter's blue one.

    CTkToplevel sets its own icon 200 ms after it is created, whatever was set
    before (customtkinter/windows/ctk_toplevel.py), so ours goes on after that.
    """
    from core import paths
    ico = paths.bundle_dir() / "assets" / "adits_voice_studio.ico"
    if not ico.exists():
        return

    def apply():
        try:
            if win.winfo_exists():
                win.iconbitmap(str(ico))
        except Exception:
            pass
    win.after(250, apply)


def card(parent, row: int, title: str) -> ctk.CTkFrame:
    """Create a styled card frame with a section title and return it."""
    frame = ctk.CTkFrame(
        parent,
        fg_color=COLORS["bg_card"],
        corner_radius=14,
        border_width=1,
        border_color=COLORS["border"],
    )
    frame.grid(row=row, column=0, sticky="ew", padx=24, pady=6)
    ctk.CTkLabel(
        frame, text=title,
        font=FONTS["subtitle"],
        text_color=COLORS["text_primary"],
    ).pack(anchor="w", padx=16, pady=(14, 10))
    return frame


def styled_option_menu(parent, variable, values, width=280, **kw):
    """Return a pre-styled CTkOptionMenu."""
    return ctk.CTkOptionMenu(
        parent,
        variable=variable,
        values=values,
        font=FONTS["body"],
        fg_color=COLORS["bg_card"],
        button_color=COLORS["accent_primary"],
        button_hover_color=COLORS["accent_secondary"],
        dropdown_fg_color=COLORS["bg_card"],
        dropdown_hover_color=COLORS["accent_primary"],
        corner_radius=8,
        width=width,
        **kw,
    )


def styled_slider(parent, variable, from_, to, width=200, command=None):
    """Return a pre-styled CTkSlider."""
    return ctk.CTkSlider(
        parent,
        from_=from_,
        to=to,
        variable=variable,
        width=width,
        button_color=COLORS["accent_primary"],
        button_hover_color=COLORS["accent_secondary"],
        progress_color=COLORS["accent_primary"],
        fg_color=COLORS["slider_track"],
        command=command,
    )


class SmoothScrollableFrame(ctk.CTkScrollableFrame):
    """CTkScrollableFrame with eased mouse-wheel scrolling.

    CTk scrolls 20 px per wheel notch on Windows and redraws on every wheel
    event. The right column holds about 900 widgets, so one redraw takes
    15-20 ms; a fast wheel spin queues more redraws than the screen can show
    and the column stutters, then lags behind the wheel. Here a wheel event
    only moves a target position, and one timer moves the view part of the way
    there each frame, so a spin costs one redraw per frame however many wheel
    events arrive. Horizontal (Shift) scrolling keeps CTk's behaviour.
    """

    STEP_PX = 100       # distance per wheel notch (delta 120), like a browser
    FRAME_MS = 10       # timer between frames; the redraw itself adds 15-20 ms
    EASE = 0.35         # share of the remaining distance covered per frame

    def __init__(self, *args, **kwargs):
        self._target = None         # wanted top of the view, in pixels
        self._last_top = None       # top we last set, to notice scrollbar drags
        self._job = None
        super().__init__(*args, **kwargs)

    def _content_height(self):
        try:
            return float(str(self._parent_canvas.cget("scrollregion")).split()[3])
        except (IndexError, ValueError):
            return 0.0

    def _mouse_wheel_all(self, event):
        if (self._shift_pressed or self._orientation != "vertical"
                or not self.check_if_master_is_canvas(event.widget)):
            return super()._mouse_wheel_all(event)
        canvas = self._parent_canvas
        total = self._content_height()
        view = canvas.winfo_height()
        if total <= view or canvas.yview() == (0.0, 1.0):
            return
        top = canvas.yview()[0] * total
        base = self._target if self._target is not None else top
        self._target = min(max(base - event.delta / 120 * self.STEP_PX, 0.0), total - view)
        if self._job is None:
            self._job = self.after(0, self._animate)

    def _animate(self):
        self._job = None
        canvas = self._parent_canvas
        total = self._content_height()
        if self._target is None or total <= 0:
            return
        # the content can grow or shrink mid-animation (a panel opens)
        self._target = min(max(self._target, 0.0), max(0.0, total - canvas.winfo_height()))
        top = canvas.yview()[0] * total
        if self._last_top is not None and abs(top - self._last_top) > 2:
            self._target = self._last_top = None    # moved by the scrollbar: stop
            return
        diff = self._target - top
        if abs(diff) < 1:
            canvas.yview_moveto(self._target / total)
            self._target = self._last_top = None
            return
        step = diff * self.EASE
        if abs(step) < 1:
            step = 1 if diff > 0 else -1
        canvas.yview_moveto((top + step) / total)
        self._last_top = canvas.yview()[0] * total
        self._job = self.after(self.FRAME_MS, self._animate)

    def destroy(self):
        if self._job is not None:
            try:
                self.after_cancel(self._job)
            except Exception:
                pass
            self._job = None
        super().destroy()


def styled_switch(parent, text, variable, **kw):
    """Return a pre-styled CTkSwitch."""
    return ctk.CTkSwitch(
        parent,
        text=text,
        variable=variable,
        onvalue=True,
        offvalue=False,
        button_color=COLORS["accent_primary"],
        button_hover_color=COLORS["accent_secondary"],
        progress_color=COLORS["switch_on"],
        fg_color=COLORS["switch_off"],
        **kw,
    )
