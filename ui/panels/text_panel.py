"""
ui/panels/text_panel.py
────────────────────────
Text input card — textarea + live character counter.

    📝 Text Input                                   [🎤] [+]
    ┌ text box ─────────────────────────────────────────────┐
    └───────────────────────────────────────────────────────┘
    ┌ [▶] interview.mp4                      1:02 / 3:45 [✖] ┐   media strip,
    │ ━━━━━━━━━━━●━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ │   shown after
    │ ▬▬▬▬▬▬ progress ▬▬▬▬▬▬                     [✖ Cancel] │   Transcribe
    │ Transcribing... 42%                                    │
    └───────────────────────────────────────────────────────┘

+ opens a menu: Import captions or text (core.captions), and Transcribe
audio or video (core.subtitles.transcribe_file). 🎤 (live dictation) is not
built yet.

Imported text replaces the box when it is empty or still holds SAMPLE_TEXT.
When it holds the user's own text, a prompt asks Replace / Append / Cancel.
Either way the change is one undo step, so Ctrl+Z brings the old text back.

Transcription runs on a thread and adds each Whisper segment to the box as it
arrives; the box is read-only until it ends. Cancel keeps the text so far.
The strip's player decodes the file on its first ▶ and plays it on pygame
channel MEDIA_PLAY_CHANNEL.
"""
import os
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox

import customtkinter as ctk
from core import captions
from core.audio_io import AUDIO_EXTENSIONS
from core.constants import COLORS, FONTS
from ui.panels.clone_voice_group import _Tip
from ui.theme import styled_slider, use_app_icon

MIC_TIP = ("Dictate: talk and your words are typed at the cursor, a phrase at a time. "
           "Click again or press Esc to stop. English only.")
DICT_STATES = {                     # Dictation state -> (title bar text, color key)
    "loading": ("Loading speech model…", "text_muted"),
    "listening": ("● Listening", "error"),
    "speech": ("● Hearing you", "error"),
    "writing": ("✎ Writing…", "text_secondary"),
    "finishing": ("Finishing…", "text_muted"),
}
LIVE_TAG = "dict_live"              # gray text of the phrase still being spoken
MEDIA_PLAY_CHANNEL = 7             # channels 1 to 6: ambiance, previews, timeline, recorder, analyzer, clips
MEDIA_TYPES = [("Audio and video", " ".join("*" + e for e in AUDIO_EXTENSIONS)), ("All files", "*.*")]

# Shown in the text box at startup, so a first Generate has something to speak.
SAMPLE_TEXT = (
    "I feel that it has become such a big challenge in today's world because there's so much "
    "data available, social media available, and you get instant feedback, right? So I think in "
    "the past, a band would lock themselves in a shed or whatever it may be and then work on stuff "
    "and maybe their friends would listen or they would listen, but it would take months before "
    "the audience heard it. Today, you could literally record something in three seconds, put it "
    "up and get instant feedback. And so the feedback loop has got shorter. The time to create "
    "something has got shorter. It's easier to publish and it's easier to get criticism or "
    "feedback. And so do you say that creativity being stifled and, and is hampering creativity? "
    "Absolutely. And the beauty of it is because so much of what's being made is being made that "
    "way, that if we choose to make something not like that, it really stands out."
)

CAPTION_TYPES = [
    ("Captions and text", " ".join("*" + e for e in captions.IMPORT_EXTENSIONS)),
    ("Captions", " ".join("*" + e for e in captions.CAPTION_EXTENSIONS)),
    ("Text", "*.txt"),
    ("All files", "*.*"),
]


def _icon_btn(parent, text, command, font, state="normal"):
    """A small outlined square button for the card's title bar."""
    return ctk.CTkButton(parent, text=text, width=32, height=28, corner_radius=8, font=font,
                         fg_color="transparent", hover_color=COLORS["bg_input"],
                         text_color=COLORS["text_primary"], border_width=1,
                         border_color=COLORS["border"], command=command, state=state)


class _ChoiceDialog(ctk.CTkToplevel):
    """A small modal question with one button per choice. .result is the
    chosen value, or None for Cancel, Esc or the close box."""

    def __init__(self, master, title, message, choices, default=None):
        super().__init__(master)
        self.result = None
        self.withdraw()
        self.title(title)
        self.resizable(False, False)
        self.configure(fg_color=COLORS["bg_card"])
        self.transient(master)
        use_app_icon(self)
        ctk.CTkLabel(self, text=message, font=FONTS["body"], text_color=COLORS["text_primary"],
                     justify="left", anchor="w", wraplength=380).pack(fill="x", padx=20, pady=(18, 14))
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(fill="x", padx=20, pady=(0, 16))
        for label, value in reversed(choices):
            primary = value == default
            ctk.CTkButton(row, text=label, width=96, height=30, corner_radius=8, font=FONTS["button"],
                          fg_color=COLORS["accent_primary"] if primary else "transparent",
                          hover_color=COLORS["accent_secondary"] if primary else COLORS["bg_input"],
                          border_width=0 if primary else 1, border_color=COLORS["border"],
                          text_color=COLORS["text_primary"],
                          command=lambda v=value: self._done(v)).pack(side="right", padx=(8, 0))
        self.bind("<Escape>", lambda e: self._done(None))
        if default is not None:
            self.bind("<Return>", lambda e: self._done(default))
        self.protocol("WM_DELETE_WINDOW", lambda: self._done(None))
        # centre over the main window
        self.update_idletasks()
        top = master.winfo_toplevel()
        x = top.winfo_rootx() + (top.winfo_width() - self.winfo_reqwidth()) // 2
        y = top.winfo_rooty() + (top.winfo_height() - self.winfo_reqheight()) // 3
        self.geometry(f"+{max(0, x)}+{max(0, y)}")
        self.deiconify()
        self.after(50, self._grab)

    def _grab(self):
        try:
            self.lift()
            self.focus_force()
            self.grab_set()
        except tk.TclError:
            pass

    def _done(self, value):
        self.result = value
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.destroy()


class TextPanel:
    def __init__(self, parent, row=None):
        frame = ctk.CTkFrame(
            parent, fg_color=COLORS["bg_card"],
            corner_radius=14, border_width=1, border_color=COLORS["border"]
        )
        if row is not None:
            frame.grid(row=row, column=0, sticky="ew", padx=24, pady=6)
            pack_fill = "x"
            pack_expand = False
        else:
            frame.pack(fill="both", expand=True, padx=16, pady=(14, 6))
            pack_fill = "both"
            pack_expand = True
        tb_height = 140
        self.frame = frame
        self._last_dir = None
        self._media_dir = None
        self._job = None                # running transcription: {"cancel", "prev_text", "prev_end", "added"}
        self._dictation = None          # core.dictation.Dictation from 🎤 until it reports "stopped"
        # media strip player: the file, its decoded audio and the play position
        self._media_path = None
        self._media_dur = 0.0
        self._pl_seg = None             # AudioSegment in the mixer's format, decoded on first ▶
        self._pl_sound = None
        self._pl_from = 0.0             # position (s) playback starts from / was paused at
        self._pl_t0 = None              # monotonic time playback started; None when stopped
        self._pl_seeking = False
        self._pl_loading = False

        # Title bar: title on the left, 🎤 and + on the right.
        head = ctk.CTkFrame(frame, fg_color="transparent")
        head.pack(fill="x", padx=16, pady=(12, 8))
        ctk.CTkLabel(
            head, text="📝  Text Input",
            font=FONTS["subtitle"],
            text_color=COLORS["text_primary"],
        ).pack(side="left")
        self._add_btn = _icon_btn(head, "+", self._open_add_menu, ("Segoe UI", 18, "bold"))
        self._add_btn.pack(side="right")
        _Tip(self._add_btn, "Import captions or a text file, or transcribe an audio or video file.")
        self._mic_btn = _icon_btn(head, "🎤", self._toggle_dictation, ("Segoe UI Emoji", 13))
        self._mic_btn.pack(side="right", padx=(0, 6))
        self._mic_tip = _Tip(self._mic_btn, MIC_TIP)
        self._dict_label = ctk.CTkLabel(head, text="", font=FONTS["body_small"],
                                        text_color=COLORS["text_muted"])
        self._dict_label.pack(side="right", padx=(0, 8))

        self.text_input = ctk.CTkTextbox(
            frame, height=tb_height, font=FONTS["mono"],
            fg_color=COLORS["bg_input"],
            text_color=COLORS["text_primary"],
            border_color=COLORS["border"], border_width=1,
            corner_radius=10, wrap="word",
            undo=True, autoseparators=True, maxundo=-1,
        )
        self.text_input.pack(fill=pack_fill, expand=pack_expand, padx=16, pady=(0, 4))
        self.text_input.insert("1.0", SAMPLE_TEXT)
        self.text_input.edit_reset()        # Ctrl+Z must not remove the sample itself

        self._char_label = ctk.CTkLabel(
            frame, text="0 characters",
            font=FONTS["body_small"],
            text_color=COLORS["text_muted"],
        )
        self._char_label.pack(anchor="e", padx=20, pady=(0, 10))
        self.text_input.bind("<KeyRelease>", self._update)
        self.text_input.bind("<Escape>", lambda e: self._stop_dictation())
        self._tb.tag_configure(LIVE_TAG, foreground=COLORS["text_muted"])
        # Live text is kept out of the undo history; take it out before an undo
        # so the history's positions still match the text.
        self._tb.bind("<<Undo>>", lambda e: self._clear_live(), add="+")
        self._build_media_strip(frame)
        self._update()

    def _build_media_strip(self, frame):
        """Player and transcription progress for an audio or video file; packed when used."""
        strip = ctk.CTkFrame(frame, fg_color=COLORS["bg_input"], corner_radius=10,
                             border_width=1, border_color=COLORS["border"])
        strip.columnconfigure(1, weight=1)
        self._strip = strip
        self._pl_btn = ctk.CTkButton(strip, text="▶", font=FONTS["button"], width=38, height=26,
                                     corner_radius=6, fg_color=COLORS["accent_primary"],
                                     hover_color=COLORS["accent_secondary"], command=self._on_media_play)
        self._pl_btn.grid(row=0, column=0, padx=(8, 8), pady=(8, 0))
        _Tip(self._pl_btn, "Play or pause the file. Drag the bar below to jump.")
        self._media_name = ctk.CTkLabel(strip, text="", font=FONTS["body_small"],
                                        text_color=COLORS["text_primary"], anchor="w")
        self._media_name.grid(row=0, column=1, sticky="ew", pady=(8, 0))
        self._pl_time = ctk.CTkLabel(strip, text="0:00 / 0:00", font=FONTS["body_small"],
                                     text_color=COLORS["text_muted"], anchor="e")
        self._pl_time.grid(row=0, column=2, sticky="e", padx=(6, 4), pady=(8, 0))
        close = ctk.CTkButton(strip, text="✖", width=26, height=26, corner_radius=6, font=FONTS["body_small"],
                              fg_color="transparent", hover_color=COLORS["bg_card"],
                              text_color=COLORS["text_secondary"], command=self._close_media)
        close.grid(row=0, column=3, padx=(0, 8), pady=(8, 0))
        _Tip(close, "Close the player. A running transcription stops and keeps its text.")
        self._pl_seek_var = ctk.DoubleVar(value=0.0)
        self._pl_seek = styled_slider(strip, self._pl_seek_var, 0, 1, width=100, command=self._on_media_slide)
        self._pl_seek.grid(row=1, column=0, columnspan=4, sticky="ew", padx=10, pady=(6, 4))
        self._pl_seek.bind("<ButtonRelease-1>", lambda e: self._media_seek(self._pl_seek_var.get()), add="+")

        job = ctk.CTkFrame(strip, fg_color="transparent")
        job.grid(row=2, column=0, columnspan=4, sticky="ew", padx=10, pady=(2, 0))
        job.columnconfigure(0, weight=1)
        self._job_row = job
        self._job_bar = ctk.CTkProgressBar(job, height=6, fg_color=COLORS["slider_track"],
                                           progress_color=COLORS["accent_primary"], corner_radius=3)
        self._job_bar.grid(row=0, column=0, sticky="ew", padx=(0, 10))
        self._job_bar.set(0)
        ctk.CTkButton(job, text="✖  Cancel", font=FONTS["button"], fg_color="#CC0000",
                      hover_color=COLORS["error"], corner_radius=8, height=26, width=90,
                      command=self._cancel_job).grid(row=0, column=1)
        self._job_status = ctk.CTkLabel(strip, text="", font=FONTS["body_small"],
                                        text_color=COLORS["text_secondary"], anchor="w", justify="left")
        self._job_status.grid(row=3, column=0, columnspan=4, sticky="ew", padx=10, pady=(2, 8))
        # wrap long status lines to the strip's width (wraplength is in unscaled pixels)
        strip.bind("<Configure>", lambda e: self._job_status.configure(
            wraplength=max(100, e.width / strip._get_widget_scaling() - 30)), add="+")

    def _update(self, _event=None):
        count = len(self.get_text())
        self._char_label.configure(
            text=f"{count:,} character{'s' if count != 1 else ''}"
        )

    def get_text(self) -> str:
        """The box's text, without gray live dictation (not final yet)."""
        tb = self._tb
        live = tb.tag_ranges(LIVE_TAG)
        if not live:
            return tb.get("1.0", "end-1c")
        return tb.get("1.0", live[0]) + tb.get(live[1], "end-1c")

    # ── putting text in ────────────────────────────────────────────
    def has_own_text(self) -> bool:
        """True when the box holds something other than the sample or blanks."""
        text = self.get_text().strip()
        return bool(text) and text != SAMPLE_TEXT.strip()

    def put_text(self, text, source=None) -> bool:
        """Put imported text in the box; False if the user cancelled.

        An empty box or the sample is replaced without asking. Over the
        user's own text, asks Replace / Append / Cancel. One undo step.
        """
        name = f"“{source}”" if source else "The import"
        mode = self._ask_mode(f"{name} has {len(text):,} characters. Replace your text with it, "
                              "add it after your text, or cancel?")
        if mode is None:
            return False
        tb = self._tb
        start = self._begin_edit(mode)
        try:
            tb.insert("end-1c", text)
            tb.mark_set("insert", "1.0" if mode == "replace" else "end-1c")
            tb.see(start)
        finally:
            self._end_edit()
        return True

    @property
    def _tb(self):
        return self.text_input._textbox         # the tk.Text inside CTkTextbox

    def _ask_mode(self, question):
        """"replace" for an empty box or the sample; otherwise ask. None = Cancel."""
        if not self.has_own_text():
            return "replace"
        top = self.frame.winfo_toplevel()
        dlg = _ChoiceDialog(top, "Import text", "The text box already has your own text.\n\n" + question,
                            [("Replace", "replace"), ("Append", "append"), ("Cancel", None)],
                            default="replace")
        top.wait_window(dlg)
        return dlg.result

    def _begin_edit(self, mode):
        """Start one undo step: clear the box (replace) or add a blank line (append).
        Returns the index new text starts at."""
        tb = self._tb
        tb.configure(state="normal", autoseparators=False)
        tb.edit_separator()
        if mode == "replace":
            tb.delete("1.0", "end")
        else:
            current = tb.get("1.0", "end-1c")
            trailing = len(current) - len(current.rstrip("\n"))
            tb.insert("end-1c", "\n" * max(0, 2 - trailing))
        return tb.index("end-1c")

    def _end_edit(self, revert=False):
        """Close the undo step; revert=True undoes it (nothing was added)."""
        tb = self._tb
        tb.configure(state="normal")
        tb.edit_separator()
        if revert:
            try:
                tb.edit_undo()
            except tk.TclError:
                pass                        # nothing had changed
        tb.configure(autoseparators=True)
        self._update()

    # ── + menu ─────────────────────────────────────────────────────
    def _open_add_menu(self):
        btn = self._add_btn
        size = -max(1, round(FONTS["body"][1] * btn._get_widget_scaling()))
        menu = tk.Menu(btn, tearoff=0, bg=COLORS["bg_card"], fg=COLORS["text_primary"],
                       activebackground=COLORS["accent_primary"], activeforeground="#FFFFFF",
                       disabledforeground=COLORS["text_muted"], bd=0, relief="flat",
                       font=(FONTS["body"][0], size))
        busy = self._job is not None or self._dictation is not None
        state = "disabled" if busy else "normal"
        menu.add_command(label="  Import captions or text…", command=self._import_captions, state=state)
        menu.add_command(label="  Transcribe audio or video…", command=self._transcribe_media, state=state)
        menu.update_idletasks()
        x = btn.winfo_rootx() + btn.winfo_width() - menu.winfo_reqwidth()
        y = btn.winfo_rooty() + btn.winfo_height() + 2
        try:
            menu.tk_popup(x, y)
        finally:
            menu.grab_release()

    def _import_captions(self):
        top = self.frame.winfo_toplevel()
        path = filedialog.askopenfilename(parent=top, title="Import captions or text",
                                          filetypes=CAPTION_TYPES, initialdir=self._last_dir)
        if not path:
            return
        self._last_dir = os.path.dirname(path)
        try:
            text = captions.load_text(path)
        except captions.CaptionError as e:
            messagebox.showerror("Import captions or text", str(e), parent=top)
            return
        except OSError as e:
            messagebox.showerror("Import captions or text", f"Could not open the file:\n{e}", parent=top)
            return
        self.put_text(text, os.path.basename(path))

    # ── 🎤 dictation ───────────────────────────────────────────────
    def _toggle_dictation(self):
        if self._dictation is None:
            self._start_dictation()
        else:
            self._stop_dictation()

    def _start_dictation(self):
        if self._job is not None:
            return
        from core.dictation import Dictation
        if not self.has_own_text():
            # the sample (or blanks) goes at once, as one undo step
            self._begin_edit("replace")
            self._end_edit()
        self.text_input.focus_set()
        d = Dictation(on_text=lambda t: self._ui(self._place_dictated, t, False),
                      on_partial=lambda t: self._ui(self._place_dictated, t, True),
                      on_state=lambda s: self._ui(self._on_dictation_state, d, s),
                      on_error=lambda m: self._ui(self._on_dictation_error, m))
        self._dictation = d
        self._mic_btn.configure(fg_color="#CC0000", hover_color=COLORS["error"], border_color="#CC0000")
        self._mic_tip.text = "Stop dictation (Esc). Words already heard are still written."
        d.start()

    def _stop_dictation(self):
        if self._dictation is not None:
            self._dictation.stop()          # "finishing", then "stopped" once queued phrases are written

    def cleanup(self):
        """App closing: close the mic and stop a transcription or playback."""
        if self._dictation is not None:
            self._dictation.stop(wait=1.0)
        if self._job is not None:
            self._job["cancel"].set()
        self._media_stop()

    def _on_dictation_state(self, d, state):
        if d is not self._dictation:
            return
        if state == "stopped":
            self._clear_live()
            self._dictation = None
            self._mic_btn.configure(fg_color="transparent", hover_color=COLORS["bg_input"],
                                    border_color=COLORS["border"])
            self._mic_tip.text = MIC_TIP
            self._dict_label.configure(text="")
            return
        text, color = DICT_STATES.get(state, ("", "text_muted"))
        self._dict_label.configure(text=text, text_color=COLORS[color])

    def _on_dictation_error(self, msg):
        messagebox.showerror("Dictation", msg, parent=self.frame.winfo_toplevel())

    def _clear_live(self):
        """Remove the gray live text, outside the undo history. Returns where it was."""
        tb = self._tb
        live = tb.tag_ranges(LIVE_TAG)
        if not live:
            return None
        pos = tb.index(live[0])
        state = tb.cget("state")
        tb.configure(state="normal", undo=False)
        tb.delete(live[0], live[1])
        tb.configure(undo=True, state=state)
        self._update()
        return pos

    def _place_dictated(self, text, live):
        """Show a phrase where the dictation is writing: the place of the live
        text if there is some, else the cursor. live=True writes gray text
        outside the undo history; live=False writes the final words, one undo
        step, and puts the cursor after them. A space is added on either side
        where needed. live with text "" just clears the gray text."""
        tb = self._tb
        if tb.cget("state") == "disabled":
            return
        pos = self._clear_live() or tb.index("insert")
        if not text:
            return
        before = tb.get(f"{pos} -1c", pos) if pos != "1.0" else ""
        after = tb.get(pos, f"{pos} +1c")
        lead = "" if (not before or before.isspace()) else " "
        tail = " " if (after and not after.isspace() and after not in ".,;:!?") else ""
        cursor_here = tb.compare("insert", "==", pos)
        if live:
            tb.configure(undo=False)
            tb.insert(pos, lead + text + tail, (LIVE_TAG,))
            tb.configure(undo=True)
            if cursor_here:
                tb.mark_set("insert", pos)      # typing goes before the gray text
        else:
            tb.edit_separator()                 # each phrase is its own undo step
            tb.insert(pos, lead + text + tail)
            tb.edit_separator()
            tb.mark_set("insert", f"{pos} +{len(lead + text)}c")
        tb.see(f"{pos} +{len(lead + text)}c")
        self._update()

    # ── transcribe audio or video ──────────────────────────────────
    def _ui(self, fn, *args):
        """Run fn on the Tk thread (safe from worker threads)."""
        try:
            self.frame.after(0, lambda: fn(*args))
        except (RuntimeError, tk.TclError):
            pass   # window already closed

    def _transcribe_media(self):
        if self._job is not None or self._dictation is not None:
            return
        top = self.frame.winfo_toplevel()
        path = filedialog.askopenfilename(parent=top, title="Transcribe audio or video",
                                          filetypes=MEDIA_TYPES, initialdir=self._media_dir)
        if not path:
            return
        self._media_dir = os.path.dirname(path)
        mode = self._ask_mode(f"“{os.path.basename(path)}” will be transcribed into the box. "
                              "Replace your text with it, add it after your text, or cancel?")
        if mode is None:
            return
        self._open_media(path)
        self._begin_edit(mode)
        self._tb.see("end-1c")
        self._tb.configure(state="disabled")    # read-only while segments arrive
        cancel = threading.Event()
        job = {"cancel": cancel, "prev_text": "", "prev_end": None, "added": False}
        self._job = job
        self._mic_btn.configure(state="disabled")
        self._job_bar.set(0)
        self._job_row.grid()
        self._set_job_status("Starting...")

        def work():
            from core.subtitles import SubtitleCancelled, transcribe_file
            try:
                info = transcribe_file(
                    path, check_cancel=cancel.is_set,
                    on_segment=lambda s, e, t, d: self._ui(self._add_segment, job, s, e, t, d),
                    status_cb=lambda m: self._ui(self._set_job_status, m, job))
                self._ui(self._job_done, job, info, None)
            except SubtitleCancelled:
                self._ui(self._job_done, job, None, "cancelled")
            except Exception as e:   # noqa: BLE001
                print(f"Transcription failed: {e}")
                self._ui(self._job_done, job, None, e)
        threading.Thread(target=work, daemon=True, name="transcribe-file").start()

    def _set_job_status(self, text, job=None, color=None):
        if job is not None and job is not self._job:
            return
        self._job_status.configure(text=text, text_color=color or COLORS["text_secondary"])

    def _add_segment(self, job, start, end, text, duration):
        if job is not self._job:
            return
        if duration and not self._media_dur:
            self._set_media_duration(duration)
        text = captions.clean_line(text)            # drops Whisper's [Music], ♪ and the like
        if text:
            if not job["prev_text"]:
                sep = ""
            elif captions.starts_paragraph(job["prev_text"], job["prev_end"], start):
                sep = "\n\n"
            else:
                sep = " "
            tb = self._tb
            tb.configure(state="normal")
            tb.insert("end-1c", sep + text)
            tb.configure(state="disabled")
            tb.see("end-1c")
            job["prev_text"], job["prev_end"], job["added"] = text, end, True
            self._update()
        if duration:
            frac = min(1.0, end / duration)
            self._job_bar.set(frac)
            self._set_job_status(f"Transcribing... {frac:.0%} ({self._clock(end)} of {self._clock(duration)})")

    def _job_done(self, job, info, error):
        if job is not self._job:
            return
        self._job = None
        self._mic_btn.configure(state="normal")
        self._job_row.grid_remove()
        self._end_edit(revert=not job["added"])     # nothing found: the old text comes back
        if error == "cancelled":
            self._set_job_status("Stopped. The text so far is kept; Ctrl+Z removes it."
                                 if job["added"] else "Stopped. Your text is unchanged.")
        elif error is not None:
            # the full error is printed to the log; the strip gets the gist
            why = ("The file may be damaged, or have no sound track."
                   if isinstance(error, ValueError) else str(error).splitlines()[0][:160])
            self._set_job_status(f"⚠  Could not transcribe this file. {why}", color=COLORS["warning"])
        elif not job["added"]:
            self._set_job_status("No speech was found in this file. Your text is unchanged.",
                                 color=COLORS["warning"])
        else:
            if info is not None and info.duration and not self._media_dur:
                self._set_media_duration(info.duration)
            lang = f" · language: {info.language}" if info is not None and info.language else ""
            self._set_job_status(f"Transcribed {self._clock(self._media_dur)}{lang}. "
                                 "Ctrl+Z puts the old text back.", color=COLORS["success"])

    def _cancel_job(self):
        if self._job is not None:
            self._job["cancel"].set()
            self._set_job_status("Stopping...")

    # ── media strip player ─────────────────────────────────────────
    @staticmethod
    def _clock(seconds):
        s = int(max(0.0, seconds or 0.0))
        return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"

    def _open_media(self, path):
        self._media_stop()
        self._media_path = path
        self._media_dur = 0.0
        self._pl_seg, self._pl_from = None, 0.0
        self._media_name.configure(text=os.path.basename(path))
        self._pl_seek.configure(to=1.0)
        self._pl_seek_var.set(0.0)
        self._pl_time.configure(text="0:00 / –")
        self._strip.pack(fill="x", padx=16, pady=(4, 2), before=self._char_label)

    def _close_media(self):
        self._cancel_job()
        self._media_stop()
        self._media_path, self._pl_seg = None, None
        self._strip.pack_forget()

    def _set_media_duration(self, seconds):
        self._media_dur = float(seconds)
        self._pl_seek.configure(to=max(0.1, self._media_dur))
        self._media_show_pos(self._media_position())

    def _media_position(self):
        if self._pl_t0 is None:
            return self._pl_from
        return min(self._media_dur, self._pl_from + time.monotonic() - self._pl_t0)

    def _media_show_pos(self, pos):
        total = self._clock(self._media_dur) if self._media_dur else "–"
        self._pl_time.configure(text=f"{self._clock(pos)} / {total}")

    def _on_media_play(self):
        """▶ plays from the bar's position (from the start once it reached the end); ❚❚ pauses."""
        if self._pl_loading or not self._media_path:
            return
        if self._pl_t0 is not None:
            self._media_pause()
            return
        if self._pl_seg is not None:
            start = self._pl_from if self._pl_from < self._media_dur - 0.05 else 0.0
            self._media_play_at(start)
            return
        try:
            import pygame
            fmt = pygame.mixer.get_init()
        except Exception:
            fmt = None
        if not fmt:
            return
        # decode off the Tk thread: a long MP3 or MP4 takes a moment
        path, (freq, _size, ch) = self._media_path, fmt
        self._pl_loading = True
        self._pl_btn.configure(text="…", state="disabled")

        def load():
            try:
                from core.audio_io import load_segment
                seg = load_segment(path).set_frame_rate(freq).set_channels(ch).set_sample_width(2)
                self._ui(self._media_loaded, path, seg, None)
            except Exception as e:   # noqa: BLE001
                self._ui(self._media_loaded, path, None, e)
        threading.Thread(target=load, daemon=True, name="media-player-load").start()

    def _media_loaded(self, path, seg, error):
        self._pl_loading = False
        self._pl_btn.configure(text="▶", state="normal")
        if path != self._media_path:
            return                          # another file was opened meanwhile
        if error is not None:
            self._set_job_status(f"⚠  Could not play this file: {error}", color=COLORS["warning"])
            return
        self._pl_seg = seg
        self._set_media_duration(len(seg) / 1000.0)
        self._media_play_at(min(self._pl_from, max(0.0, self._media_dur - 0.05)))

    def _media_play_at(self, pos):
        try:
            import pygame
            if pygame.mixer.get_num_channels() <= MEDIA_PLAY_CHANNEL:
                pygame.mixer.set_num_channels(MEDIA_PLAY_CHANNEL + 1)
            part = self._pl_seg[int(pos * 1000):]
            self._media_stop()
            self._pl_sound = pygame.mixer.Sound(buffer=part.raw_data)
            pygame.mixer.Channel(MEDIA_PLAY_CHANNEL).play(self._pl_sound)
        except Exception as e:   # noqa: BLE001
            print(f"Media playback failed: {e}")
            return
        self._pl_from, self._pl_t0 = pos, time.monotonic()
        self._pl_btn.configure(text="❚❚", font=FONTS["body_small"])   # smaller, so the button keeps its width
        self._media_tick()

    def _media_pause(self):
        if self._pl_t0 is not None:
            self._pl_from = self._media_position()
            self._media_stop()

    def _media_stop(self):
        self._pl_t0 = None
        try:
            import pygame
            if pygame.mixer.get_init() and pygame.mixer.get_num_channels() > MEDIA_PLAY_CHANNEL:
                pygame.mixer.Channel(MEDIA_PLAY_CHANNEL).stop()
        except Exception:
            pass
        self._pl_sound = None
        try:
            self._pl_btn.configure(text="▶", font=FONTS["button"])
        except Exception:
            pass

    def _media_tick(self):
        if self._pl_t0 is None:
            return
        pos = self._media_position()
        if pos >= self._media_dur - 0.01:
            self._media_stop()
            self._pl_from = 0.0             # next ▶ starts over
            pos = self._media_dur
        if not self._pl_seeking:            # while the bar is dragged, it shows the drag
            self._pl_seek_var.set(pos)
            self._media_show_pos(pos)
        if self._pl_t0 is not None:
            self.frame.after(100, self._media_tick)

    def _on_media_slide(self, value):
        self._pl_seeking = True
        if self._pl_t0 is None:
            self._pl_from = float(value)
        self._media_show_pos(float(value))

    def _media_seek(self, pos):
        """Bar released: jump there, and keep playing if it was playing."""
        self._pl_seeking = False
        pos = min(max(0.0, float(pos)), self._media_dur or 0.0)
        self._pl_from = pos
        self._media_show_pos(pos)
        if self._pl_t0 is not None:
            self._media_play_at(pos)
