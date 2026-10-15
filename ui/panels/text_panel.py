"""
ui/panels/text_panel.py
──────────────────────────
Text input card (Qt): text box, live character count, footer edit buttons,
+ menu (import captions, transcribe audio or video), 🎤 dictation, and the
media strip with player and transcription progress.

Behavior follows ui/panels/text_panel.py (see docs/ui-parity-checklist.md,
"Text Input panel"), with one deliberate change:

* Live dictation text. The CTk box shows the phrase still being spoken as
  gray text inside the box, written with undo switched off. Qt's
  QTextDocument clears its whole undo history when undo is switched off, so
  here the phrase in progress shows in a gray line under the box instead.
  Each finished phrase is written into the box as its own undo step, as before.

Undo steps: every replace, append, paste, clear and dictated phrase is one
edit block. A transcription is one undo step too: the first change opens it
and each segment joins it (QTextCursor.joinPreviousEditBlock), so Ctrl+Z
removes the whole transcription and brings the old text back.
"""
import os
import threading
import time

from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (QApplication, QFileDialog, QFrame, QGridLayout, QHBoxLayout,
                               QMenu, QMessageBox, QPlainTextEdit, QProgressBar, QSizePolicy)

from core import captions
from core.audio_io import AUDIO_EXTENSIONS
from core.constants import COLORS
from ui import theme
from ui.bridge import ui
from ui.widgets import SeekSlider, ask_choice

# The constants below match ui/panels/text_panel.py, which cannot be imported
# here (it loads CustomTkinter). Phase 6 removes the CTk copy.
MIC_TIP = ("Dictate: talk and your words are typed at the cursor, a phrase at a time. "
           "Click again or press Esc to stop. English only.")
MIC_TIP_ON = "Stop dictation (Esc). Words already heard are still written."
DICT_STATES = {                     # Dictation state -> (title bar text, color key)
    "loading": ("Loading speech model…", "text_muted"),
    "listening": ("● Listening", "error"),
    "speech": ("● Hearing you", "error"),
    "writing": ("✎ Writing…", "text_secondary"),
    "finishing": ("Finishing…", "text_muted"),
}
MEDIA_PLAY_CHANNEL = 7             # channels 1 to 6: ambiance, previews, timeline, recorder, analyzer, clips

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


def _filter(name, exts):
    return f"{name} ({' '.join('*' + e for e in exts)})"


CAPTION_FILTER = ";;".join([
    _filter("Captions and text", captions.IMPORT_EXTENSIONS),
    _filter("Captions", captions.CAPTION_EXTENSIONS),
    "Text (*.txt)",
    "All files (*.*)",
])
MEDIA_FILTER = ";;".join([_filter("Audio and video", AUDIO_EXTENSIONS), "All files (*.*)"])


def _clock(seconds):
    s = int(max(0.0, seconds or 0.0))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


class _EscFilter(QObject):
    """Esc in the text box stops dictation."""

    def __init__(self, on_esc):
        super().__init__()
        self._on_esc = on_esc

    def eventFilter(self, obj, event):
        if event.type() == QEvent.KeyPress and event.key() == Qt.Key_Escape:
            self._on_esc()
            return True
        return False


class TextPanel:
    def __init__(self):
        self.frame, lay = theme.card()
        self._last_dir = None
        self._media_dir = None
        self._job = None                # running transcription: {"cancel", "prev_text", "prev_end", "added"}
        self._dictation = None          # core.dictation.Dictation from 🎤 until it reports "stopped"
        self._edit_base = None          # undo steps before the open transcription edit, or None
        self._count_pending = False
        # media strip player
        self._media_path = None
        self._media_dur = 0.0
        self._pl_seg = None
        self._pl_sound = None
        self._pl_from = 0.0
        self._pl_t0 = None
        self._pl_seeking = False
        self._pl_loading = False
        self._pl_timer = QTimer()           # moves the bar and time while playing
        self._pl_timer.setInterval(100)
        self._pl_timer.timeout.connect(self._media_tick)

        # Title bar: title on the left, state label, 🎤 and + on the right.
        head = QHBoxLayout()
        title = theme.label("📝  Text Input")
        title.setObjectName("cardTitle")
        head.addWidget(title)
        head.addStretch(1)
        self._dict_label = theme.label("", small=True)
        head.addWidget(self._dict_label)
        self._mic_btn = theme.button("🎤", theme.ROLE_ICON, MIC_TIP)
        self._mic_btn.clicked.connect(self._toggle_dictation)
        head.addWidget(self._mic_btn)
        self._add_btn = theme.button("+", theme.ROLE_ICON,
                                     "Import captions or a text file, or transcribe an audio or video file.")
        self._menu = QMenu(self._add_btn)
        self._act_import = self._menu.addAction("Import captions or text…", self._import_captions)
        self._act_transcribe = self._menu.addAction("Transcribe audio or video…", self._transcribe_media)
        self._add_btn.clicked.connect(self._open_add_menu)
        head.addWidget(self._add_btn)
        lay.addLayout(head)

        self.text_input = QPlainTextEdit()
        self.text_input.setFont(theme.font("mono"))
        self.text_input.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        self.text_input.setMinimumHeight(140)
        self.text_input.setPlainText(SAMPLE_TEXT)       # also clears undo: Ctrl+Z must not remove the sample
        lay.addWidget(self.text_input, 1)
        self._esc = _EscFilter(self._stop_dictation)
        self.text_input.installEventFilter(self._esc)

        # The phrase still being spoken, gray, under the box (see module docstring).
        self._live = theme.label("", muted=True)
        self._live.setFont(theme.font("mono"))
        self._live.setWordWrap(True)
        self._live.hide()
        lay.addWidget(self._live)

        self._build_media_strip(lay)

        # Footer: edit buttons on the left, character count on the right.
        foot = QHBoxLayout()
        foot.setSpacing(6)
        self._copy_btn = self._foot_btn(foot, "Copy", self._copy_all, "Copy all the text.")
        self._foot_btn(foot, "Paste", self._paste_append,
                       "Paste the copied text after yours (or in place of the sample).")
        self._foot_btn(foot, "Paste & Replace", self._paste_replace, "Replace all the text with the copied text.")
        self._foot_btn(foot, "Clear", self._clear_all, "Clear the box. Undo brings the text back.")
        foot.addSpacing(2)
        self._undo_btn = self._foot_btn(foot, "↶", self.text_input.undo, "Undo (Ctrl+Z)", icon=True)
        self._redo_btn = self._foot_btn(foot, "↷", self.text_input.redo, "Redo (Ctrl+Y)", icon=True)
        foot.addStretch(1)
        self._char_label = theme.label("0 characters", muted=True, small=True)
        foot.addWidget(self._char_label)
        lay.addLayout(foot)

        self.text_input.undoAvailable.connect(self._undo_btn.setEnabled)
        self.text_input.redoAvailable.connect(self._redo_btn.setEnabled)
        self._undo_btn.setEnabled(False)
        self._redo_btn.setEnabled(False)
        # Count on any change (typing, paste, drag-and-drop, the buttons), once per event-loop turn.
        self.text_input.textChanged.connect(self._schedule_count)
        self._update()

    def _foot_btn(self, foot, text, command, tip, icon=False):
        btn = theme.button(text, theme.ROLE_GHOST, tip, small=True)
        if icon:
            btn.setFixedWidth(30)
        btn.clicked.connect(command)
        foot.addWidget(btn)
        return btn

    def _build_media_strip(self, lay):
        """Player and transcription progress for an audio or video file; shown when used."""
        strip = QFrame()
        strip.setObjectName("strip")
        g = QGridLayout(strip)
        g.setContentsMargins(8, 8, 8, 8)
        g.setHorizontalSpacing(6)
        g.setVerticalSpacing(4)
        g.setColumnStretch(1, 1)
        self._strip = strip
        self._pl_btn = theme.button("▶", theme.ROLE_PRIMARY,
                                    "Play or pause the file. Drag the bar below to jump.")
        self._pl_btn.setFixedWidth(38)
        self._pl_btn.clicked.connect(self._on_media_play)
        g.addWidget(self._pl_btn, 0, 0)
        self._media_name = theme.label("", small=True)
        self._media_name.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        g.addWidget(self._media_name, 0, 1)
        self._pl_time = theme.label("0:00 / 0:00", muted=True, small=True)
        g.addWidget(self._pl_time, 0, 2)
        close = theme.button("✖", theme.ROLE_ICON,
                             "Close the player. A running transcription stops and keeps its text.")
        close.clicked.connect(self._close_media)
        g.addWidget(close, 0, 3)

        self._pl_seek = SeekSlider()
        self._pl_seek.sliderPressed.connect(self._on_seek_press)
        self._pl_seek.sliderMoved.connect(lambda v: self._on_media_slide(v / 1000.0))
        self._pl_seek.sliderReleased.connect(lambda: self._media_seek(self._pl_seek.seconds()))
        g.addWidget(self._pl_seek, 1, 0, 1, 4)

        self._job_row = QFrame()
        jr = QHBoxLayout(self._job_row)
        jr.setContentsMargins(0, 0, 0, 0)
        self._job_bar = QProgressBar()
        self._job_bar.setRange(0, 1000)
        self._job_bar.setTextVisible(False)
        jr.addWidget(self._job_bar, 1)
        cancel = theme.button("✖  Cancel", theme.ROLE_DANGER)
        cancel.setFixedWidth(100)
        cancel.clicked.connect(self._cancel_job)
        jr.addWidget(cancel)
        g.addWidget(self._job_row, 2, 0, 1, 4)
        self._job_row.hide()

        self._job_status = theme.label("", secondary=True, small=True)
        self._job_status.setWordWrap(True)
        g.addWidget(self._job_status, 3, 0, 1, 4)
        strip.hide()
        lay.addWidget(strip)

    # ── counting and undo state ────────────────────────────────────
    def _schedule_count(self):
        if not self._count_pending:
            self._count_pending = True
            QTimer.singleShot(0, self._update)

    def _update(self):
        self._count_pending = False
        count = len(self.get_text())
        self._char_label.setText(f"{count:,} character{'s' if count != 1 else ''}")

    # ── footer buttons ─────────────────────────────────────────────
    @staticmethod
    def _clipboard_text():
        """The clipboard's text, or None when it holds no text."""
        text = QApplication.clipboard().text()
        return text.replace("\r\n", "\n") or None

    def _editable(self):
        """False while a transcription is writing into the box."""
        if self._job is not None:
            self._set_job_status("Wait for the transcription to finish, or cancel it.", self._job)
            return False
        return True

    def _copy_all(self):
        text = self.get_text()
        if not text:
            return
        QApplication.clipboard().setText(text)
        self._copy_btn.setText("Copied")
        QTimer.singleShot(1200, lambda: self._copy_btn.setText("Copy"))

    def _paste_append(self):
        text = self._clipboard_text()
        if text is None or not self._editable():
            return
        # an empty box or the sample is replaced; the user's own text is kept
        self._put(text, "append" if self.has_own_text() else "replace")

    def _paste_replace(self):
        text = self._clipboard_text()
        if text is None or not self._editable():
            return
        self._put(text, "replace")

    def _clear_all(self):
        if not self._editable() or not self.text_input.toPlainText():
            return
        self._put("", "replace")
        self.text_input.setFocus()

    def _put(self, text, mode, cursor_at_start=False):
        """Replace or append text as one undo step; cursor at its end (or the start)."""
        cur = QTextCursor(self.text_input.document())
        cur.beginEditBlock()
        start = self._prepare(cur, mode)
        cur.insertText(text)
        cur.endEditBlock()
        if cursor_at_start:
            cur.setPosition(start)
        self.text_input.setTextCursor(cur)
        self.text_input.ensureCursorVisible()
        self._update()

    @staticmethod
    def _prepare(cur, mode):
        """Inside an edit block: clear the box (replace) or end it with a blank
        line (append). Leaves cur at the end; returns where new text starts."""
        if mode == "replace":
            cur.select(QTextCursor.Document)
            cur.removeSelectedText()
        else:
            cur.movePosition(QTextCursor.End)
            current = cur.document().toPlainText()
            trailing = len(current) - len(current.rstrip("\n"))
            cur.insertText("\n" * max(0, 2 - trailing))
        cur.movePosition(QTextCursor.End)
        return cur.position()

    def get_text(self) -> str:
        """The box's text. (Live dictation text is outside the box in this UI.)"""
        return self.text_input.toPlainText()

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
        self._put(text, mode, cursor_at_start=(mode == "replace"))
        return True

    def _ask_mode(self, question):
        """"replace" for an empty box or the sample; otherwise ask. None = Cancel."""
        if not self.has_own_text():
            return "replace"
        return ask_choice(self.frame.window(), "Import text",
                          "The text box already has your own text.\n\n" + question,
                          [("Replace", "replace"), ("Append", "append"), ("Cancel", None)],
                          default="replace")

    # ── one undo step that stays open while a transcription runs ───
    def _begin_edit(self, mode):
        doc = self.text_input.document()
        self._edit_base = doc.availableUndoSteps()
        cur = QTextCursor(doc)
        cur.beginEditBlock()
        self._prepare(cur, mode)
        cur.endEditBlock()

    def _append_to_edit(self, text):
        doc = self.text_input.document()
        cur = QTextCursor(doc)
        cur.movePosition(QTextCursor.End)
        if doc.availableUndoSteps() > self._edit_base:
            cur.joinPreviousEditBlock()     # same undo step as the start of this transcription
        else:
            cur.beginEditBlock()            # nothing changed yet: open the step now
        cur.insertText(text)
        cur.endEditBlock()

    def _end_edit(self, revert=False):
        """Close the step; revert=True takes it back (nothing was added)."""
        doc = self.text_input.document()
        if revert and doc.availableUndoSteps() > self._edit_base:
            doc.undo()
        self._edit_base = None
        self._update()

    # ── + menu ─────────────────────────────────────────────────────
    def _open_add_menu(self):
        busy = self._job is not None or self._dictation is not None
        self._act_import.setEnabled(not busy)
        self._act_transcribe.setEnabled(not busy)
        btn = self._add_btn
        pos = btn.mapToGlobal(btn.rect().bottomRight())
        pos.setX(pos.x() - self._menu.sizeHint().width())
        pos.setY(pos.y() + 2)
        self._menu.exec(pos)

    def _import_captions(self):
        top = self.frame.window()
        path, _ = QFileDialog.getOpenFileName(top, "Import captions or text", self._last_dir or "",
                                              CAPTION_FILTER)
        if not path:
            return
        self._last_dir = os.path.dirname(path)
        try:
            text = captions.load_text(path)
        except captions.CaptionError as e:
            QMessageBox.critical(top, "Import captions or text", str(e))
            return
        except OSError as e:
            QMessageBox.critical(top, "Import captions or text", f"Could not open the file:\n{e}")
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
            self._put("", "replace")        # the sample (or blanks) goes at once, as one undo step
        self.text_input.setFocus()
        d = Dictation(on_text=lambda t: ui(self._place_dictated, t, False),
                      on_partial=lambda t: ui(self._place_dictated, t, True),
                      on_state=lambda s: ui(self._on_dictation_state, d, s),
                      on_error=lambda m: ui(self._on_dictation_error, m))
        self._dictation = d
        self._mic_btn.setProperty("recording", True)
        theme.repolish(self._mic_btn)
        self._mic_btn.setToolTip(MIC_TIP_ON)
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
            self._set_live("")
            self._dictation = None
            self._mic_btn.setProperty("recording", False)
            theme.repolish(self._mic_btn)
            self._mic_btn.setToolTip(MIC_TIP)
            self._dict_label.setText("")
            return
        text, color = DICT_STATES.get(state, ("", "text_muted"))
        self._dict_label.setText(text)
        theme.set_color(self._dict_label, COLORS[color])

    def _on_dictation_error(self, msg):
        QMessageBox.critical(self.frame.window(), "Dictation", msg)

    def _set_live(self, text):
        self._live.setText(text)
        self._live.setVisible(bool(text))

    def _place_dictated(self, text, live):
        """live=True: show the phrase in progress under the box ("" hides it).
        live=False: write the finished phrase at the cursor as one undo step,
        with a space on either side where needed, and put the cursor after it."""
        if live:
            self._set_live(text)
            return
        self._set_live("")
        if not text or self.text_input.isReadOnly():
            return
        doc = self.text_input.document()
        cur = self.text_input.textCursor()
        cur.clearSelection()
        pos = cur.position()
        before = doc.characterAt(pos - 1) if pos > 0 else ""
        after = doc.characterAt(pos)
        before = "" if before in ("", "\x00") else before
        after = "" if after in ("", "\x00") else after
        lead = "" if (not before or before.isspace()) else " "
        tail = " " if (after and not after.isspace() and after not in ".,;:!?") else ""
        cur.beginEditBlock()                # each phrase is its own undo step
        cur.insertText(lead + text + tail)
        cur.endEditBlock()
        cur.setPosition(pos + len(lead + text))
        self.text_input.setTextCursor(cur)
        self.text_input.ensureCursorVisible()
        self._update()

    # ── transcribe audio or video ──────────────────────────────────
    def _transcribe_media(self):
        if self._job is not None or self._dictation is not None:
            return
        top = self.frame.window()
        path, _ = QFileDialog.getOpenFileName(top, "Transcribe audio or video", self._media_dir or "",
                                              MEDIA_FILTER)
        if not path:
            return
        path = os.path.normpath(path)
        self._media_dir = os.path.dirname(path)
        mode = self._ask_mode(f"“{os.path.basename(path)}” will be transcribed into the box. "
                              "Replace your text with it, add it after your text, or cancel?")
        if mode is None:
            return
        self._open_media(path)
        self._begin_edit(mode)
        self.text_input.moveCursor(QTextCursor.End)
        self.text_input.setReadOnly(True)    # read-only while segments arrive
        theme.repolish(self.text_input)
        cancel = threading.Event()
        job = {"cancel": cancel, "prev_text": "", "prev_end": None, "added": False}
        self._job = job
        self._mic_btn.setEnabled(False)
        self._job_bar.setValue(0)
        self._job_row.show()
        self._set_job_status("Starting...")

        def work():
            from core.subtitles import SubtitleCancelled, transcribe_file
            try:
                info = transcribe_file(
                    path, check_cancel=cancel.is_set,
                    on_segment=lambda s, e, t, d: ui(self._add_segment, job, s, e, t, d),
                    status_cb=lambda m: ui(self._set_job_status, m, job))
                ui(self._job_done, job, info, None)
            except SubtitleCancelled:
                ui(self._job_done, job, None, "cancelled")
            except Exception as e:   # noqa: BLE001
                print(f"Transcription failed: {e}")
                ui(self._job_done, job, None, e)
        threading.Thread(target=work, daemon=True, name="transcribe-file").start()

    def _set_job_status(self, text, job=None, color=None):
        if job is not None and job is not self._job:
            return
        self._job_status.setText(text)
        theme.set_color(self._job_status, color or COLORS["text_secondary"])

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
            self._append_to_edit(sep + text)
            self.text_input.moveCursor(QTextCursor.End)
            self.text_input.ensureCursorVisible()
            job["prev_text"], job["prev_end"], job["added"] = text, end, True
            self._update()
        if duration:
            frac = min(1.0, end / duration)
            self._job_bar.setValue(round(frac * 1000))
            self._set_job_status(f"Transcribing... {frac:.0%} ({_clock(end)} of {_clock(duration)})")

    def _job_done(self, job, info, error):
        if job is not self._job:
            return
        self._job = None
        self._mic_btn.setEnabled(True)
        self._job_row.hide()
        self.text_input.setReadOnly(False)
        theme.repolish(self.text_input)
        self._end_edit(revert=not job["added"])     # nothing found: the old text comes back
        if error == "cancelled":
            self._set_job_status("Stopped. The text so far is kept; Ctrl+Z removes it."
                                 if job["added"] else "Stopped. Your text is unchanged.")
        elif error is not None:
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
            self._set_job_status(f"Transcribed {_clock(self._media_dur)}{lang}. "
                                 "Ctrl+Z puts the old text back.", color=COLORS["success"])

    def _cancel_job(self):
        if self._job is not None:
            self._job["cancel"].set()
            self._set_job_status("Stopping...")

    # ── media strip player ─────────────────────────────────────────
    def _open_media(self, path):
        self._media_stop()
        self._media_path = path
        self._media_dur = 0.0
        self._pl_seg, self._pl_from = None, 0.0
        self._media_name.setText(os.path.basename(path))
        self._media_name.setToolTip(path)
        self._pl_seek.set_length(1.0)
        self._pl_seek.set_seconds(0.0)
        self._pl_time.setText("0:00 / –")
        self._job_status.setText("")
        self._strip.show()

    def _close_media(self):
        self._cancel_job()
        self._media_stop()
        self._media_path, self._pl_seg = None, None
        self._strip.hide()

    def _set_media_duration(self, seconds):
        self._media_dur = float(seconds)
        self._pl_seek.set_length(max(0.1, self._media_dur))
        self._media_show_pos(self._media_position())

    def _media_position(self):
        if self._pl_t0 is None:
            return self._pl_from
        return min(self._media_dur, self._pl_from + time.monotonic() - self._pl_t0)

    def _media_show_pos(self, pos):
        total = _clock(self._media_dur) if self._media_dur else "–"
        self._pl_time.setText(f"{_clock(pos)} / {total}")

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
        # decode off the GUI thread: a long MP3 or MP4 takes a moment
        path, (freq, _size, ch) = self._media_path, fmt
        self._pl_loading = True
        self._pl_btn.setText("…")
        self._pl_btn.setEnabled(False)

        def load():
            try:
                from core.audio_io import load_segment
                seg = load_segment(path).set_frame_rate(freq).set_channels(ch).set_sample_width(2)
                ui(self._media_loaded, path, seg, None)
            except Exception as e:   # noqa: BLE001
                ui(self._media_loaded, path, None, e)
        threading.Thread(target=load, daemon=True, name="media-player-load").start()

    def _media_loaded(self, path, seg, error):
        self._pl_loading = False
        self._pl_btn.setText("▶")
        self._pl_btn.setEnabled(True)
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
        self._pl_btn.setText("❚❚")
        self._pl_timer.start()
        self._media_tick()

    def _media_pause(self):
        if self._pl_t0 is not None:
            self._pl_from = self._media_position()
            self._media_stop()

    def _media_stop(self):
        self._pl_t0 = None
        self._pl_timer.stop()
        try:
            import pygame
            if pygame.mixer.get_init() and pygame.mixer.get_num_channels() > MEDIA_PLAY_CHANNEL:
                pygame.mixer.Channel(MEDIA_PLAY_CHANNEL).stop()
        except Exception:
            pass
        self._pl_sound = None
        self._pl_btn.setText("▶")

    def _media_tick(self):
        if self._pl_t0 is None:
            return
        pos = self._media_position()
        if pos >= self._media_dur - 0.01:
            self._media_stop()
            self._pl_from = 0.0             # next ▶ starts over
            pos = self._media_dur
        if not self._pl_seeking:            # while the bar is dragged, it shows the drag
            self._pl_seek.set_seconds(pos)
            self._media_show_pos(pos)

    def _on_seek_press(self):
        self._on_media_slide(self._pl_seek.seconds())

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
