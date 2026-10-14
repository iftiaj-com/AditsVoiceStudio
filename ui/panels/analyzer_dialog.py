"""
ui/panels/analyzer_dialog.py
───────────────────────────────
"Voice match" (Qt): the best base voice for the clips.

Opened by 🔍 Analyze in Customization → Clone Voice → Create voice. One run:

  1. Brings the voice bank up to date (only what changed: new voices, engines
     whose model version changed, voices that failed in an older app version).
  2. Measures the selected clip audio (after trim, and after music removal
     when that switch is on): pitch, rough gender guess, speaking speed,
     language, voiceprint.
  3. Picks the gender to match: the user's Male / Female choice, or in Auto
     the clip's own (voicematch.decide_gender). Only voices of that gender
     are compared.
  4. Shortlists the 3 English engine voices closest to the clip.
  5. Makes each one's ▶ Result: the clip's Whisper transcript spoken by that
     engine voice at the suggested speed, cloned with Seed-VC (the clip is the
     reference). The Results are ranked by how close they sound to the clip,
     minus the same pitch penalty as the shortlist: 1. Best match, 2. and 3.
     Close match.

Each row has two play/stop toggles (▶ Original, the clip as measured, and
▶ Result) and Apply, which selects engine, voice and speed
(EnginePanel.apply_voice_selection), hands the Result to on_applied and
closes the window.

Behavior follows ui/panels/analyzer_dialog.py (see docs/ui-parity-checklist.md,
"Voice match dialog"). The window is not modal, as in the CTk dialog: it is
owned by the main window (so it stays on top of it), raised and focused.

Differences, forced by Qt:

* The ranked rows sit in a QScrollArea that shows no frame or scrollbar while
  they fit. A Qt layout grows the window's minimum size to fit its contents
  instead of clipping them as Tk's pack does, so without it the 640x480
  minimum could not hold.
* Esc does nothing (QDialog would close on Esc; the CTk dialog has no Esc
  binding). Close and the close box close it.
* The dialog is deleted when it closes (WA_DeleteOnClose), like a destroyed
  CTkToplevel. Calls that the worker thread queues after that are dropped
  (the _closed flag), as the CTk _ui() did with winfo_exists().
"""
import os
import shutil
import tempfile
import threading

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (QDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QProgressBar,
                               QScrollArea, QSizePolicy, QVBoxLayout, QWidget)

from core import paths, voice_selection
from core.constants import COLORS
from core.voice_cloning import voicematch
from core.voice_cloning.errors import VCCancelled, VCError
from ui import theme
from ui.widgets import IconLabel
from ui.bridge import ui

# The constants below match ui/panels/analyzer_dialog.py, which cannot be
# imported here (it loads CustomTkinter).
# The selection (Clean-up → Use part of each recording) is measured and spoken
# up to this length: Quick voices read at most 25 s, and each Result is spoken
# and cloned once per shortlisted voice.
MAX_ANALYSIS_SECONDS = 25
PLAY_CHANNEL = 5
WATCH_MS = 200         # how often a playing clip is checked for its end

EXPLAINER = ("Speaks your clip's words with the closest English engine voices, clones them "
             "and ranks the results. Apply sets the engine, voice and speed; you can still "
             "change them.")


class AnalyzerDialog(QDialog):
    def __init__(self, app, selections, clean=False, denoise=False, on_applied=None, gender="auto"):
        super().__init__(app)
        self.setObjectName("analyzerDialog")
        ico = paths.bundle_dir() / "assets" / "adits_voice_studio.ico"
        if ico.exists():
            self.setWindowIcon(QIcon(str(ico)))
        self.app = app
        self.selections = selections
        self.gender_mode = gender if gender in voicematch.GENDERS else "auto"
        self._gender = None           # the gender actually matched: "male", "female" or None (all)
        self._gender_note = ""
        self.clean = clean
        self.denoise = denoise
        self.on_applied = on_applied
        self.setWindowTitle("Voice match")
        self.resize(840, 560)
        self.setMinimumSize(640, 480)
        self.setModal(False)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setStyleSheet(self._sheet())
        self._cancel = threading.Event()
        self._busy = False
        self._closed = False
        self._tmp = tempfile.mkdtemp(prefix="adits_vs_match_")
        self._clip = None
        self._clip_wav = None         # the prepared clip (trim, music removal), played by ▶ Original
        self._ranked = []
        self._cands = {}              # candidate id -> voicematch.candidates() entry
        self._ref = None              # Seed-VC reference made from the clip
        self._clip_text = ""          # the clip's Whisper transcript, spoken by ▶ Result
        self._sound = None
        self._playing = None          # (key, button, idle_text) of the clip being played
        self._watch_timer = QTimer(self)
        self._watch_timer.setInterval(WATCH_MS)
        self._watch_timer.timeout.connect(self._watch)
        try:
            self._eng_settings = app.engine_panel.get_settings()
        except Exception:
            self._eng_settings = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(0)

        title = IconLabel("🔍  Voice match")
        title.setObjectName("cardTitle")
        root.addWidget(title)
        root.addSpacing(2)
        explainer = theme.label(EXPLAINER, muted=True, small=True)
        explainer.setWordWrap(True)
        root.addWidget(explainer)

        # progress (Cancel only while something runs)
        task = QGridLayout()
        task.setContentsMargins(0, 8, 0, 0)
        task.setHorizontalSpacing(8)
        task.setVerticalSpacing(2)
        task.setColumnStretch(0, 1)
        self._bar = QProgressBar()
        self._bar.setRange(0, 1000)
        self._bar.setValue(0)
        self._bar.setTextVisible(False)
        task.addWidget(self._bar, 0, 0, Qt.AlignVCenter)
        self._cancel_btn = theme.button("✖  Cancel", theme.ROLE_DANGER)
        self._cancel_btn.setObjectName("amCancel")
        self._cancel_btn.setFixedSize(96, 28)
        self._cancel_btn.setAutoDefault(False)
        self._cancel_btn.clicked.connect(lambda _c=False: self._on_cancel())
        task.addWidget(self._cancel_btn, 0, 1, 2, 1, Qt.AlignVCenter)
        self._stage = theme.label("", small=True)
        self._stage.setWordWrap(True)
        self._stage.setMinimumWidth(1)
        theme.set_color(self._stage, COLORS["text_secondary"])
        task.addWidget(self._stage, 1, 0)
        root.addLayout(task)

        # clip summary
        root.addSpacing(10)
        clip_box = QFrame()
        clip_box.setObjectName("amClip")
        cl = QVBoxLayout(clip_box)
        cl.setContentsMargins(12, 8, 12, 10)
        cl.setSpacing(2)
        cl.addWidget(theme.section_label("Your clip"))
        self._clip_lbl = QLabel("Analyzing…")
        self._clip_lbl.setObjectName("amClipText")
        self._clip_lbl.setWordWrap(True)
        self._clip_lbl.setMinimumWidth(1)
        cl.addWidget(self._clip_lbl)
        root.addWidget(clip_box)
        root.addSpacing(4)

        # ranked matches (a scroll area only so a small window clips them instead of growing)
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        holder = QWidget()
        holder.setObjectName("amList")
        self._list = QVBoxLayout(holder)
        self._list.setContentsMargins(0, 4, 0, 4)
        self._list.setSpacing(8)
        self._list.addStretch(1)
        self._scroll.setWidget(holder)
        root.addWidget(self._scroll, 1)

        foot = QHBoxLayout()
        foot.setContentsMargins(0, 4, 0, 0)
        foot.addStretch(1)
        close = theme.button("Close", theme.ROLE_GHOST)
        close.setObjectName("amClose")
        close.setFixedSize(90, 32)
        close.setAutoDefault(False)
        close.clicked.connect(lambda _c=False: self.close())
        foot.addWidget(close)
        root.addLayout(foot)

        QTimer.singleShot(150, self._grab)
        QTimer.singleShot(300, self._start)

    @staticmethod
    def _sheet():
        c = COLORS
        return f"""
QDialog#analyzerDialog {{ background: {c['bg_dark']}; }}
QDialog#analyzerDialog QPushButton {{ min-height: 0px; padding: 0; border-radius: 8px; }}
QFrame#amClip {{
    background: {c['bg_card']}; border: 1px solid {c['border']}; border-radius: 12px;
}}
QFrame#amClip QLabel {{ background: transparent; }}
QLabel#amClipText {{ font-family: 'Segoe UI'; font-size: 13px; color: {c['text_primary']}; }}
QWidget#amList {{ background: transparent; }}
QFrame#amRow {{
    background: {c['bg_input']}; border: 1px solid {c['border']}; border-radius: 10px;
}}
QFrame#amRow[best="true"] {{ border: 2px solid {c['success']}; }}
QFrame#amRow QLabel {{ background: transparent; border: none; }}
QLabel#amTag {{ font-family: 'Segoe UI'; font-size: 11px; font-weight: bold; color: {c['text_secondary']}; }}
QLabel#amTag[best="true"] {{ color: {c['success']}; }}
QLabel#amVoice {{ font-family: 'Segoe UI'; font-size: 13px; font-weight: bold; color: {c['text_primary']}; }}
QLabel#amDetail {{ font-family: 'Segoe UI'; font-size: 11px; color: {c['text_secondary']}; }}
QLabel#amNone {{ font-family: 'Segoe UI'; font-size: 13px; color: {c['text_muted']}; }}
QPushButton#amPlay {{
    background: {c['bg_card']}; border: 1px solid {c['border']};
    font-family: 'Segoe UI'; font-size: 11px;
}}
QPushButton#amApply {{ border: none; }}
QPushButton#amCancel {{ border: none; }}
QPushButton#amClose {{ background: {c['bg_card']}; border: none; }}
QPushButton#amClose:hover {{ background: {c['border']}; }}
"""

    def _grab(self):
        if self._closed:
            return
        self.raise_()
        self.activateWindow()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:   # the CTk dialog has no Esc binding
            e.accept()
            return
        super().keyPressEvent(e)

    # ── task plumbing ──────────────────────────────────────────────
    def _ui(self, fn, *args):
        """Run fn(*args) on the GUI thread, unless the window has closed meanwhile."""
        ui(self._guarded, fn, args)

    def _guarded(self, fn, args):
        if not self._closed:          # window closed while work was still running
            fn(*args)

    def _progress(self, frac, msg):
        if frac is not None:
            self._bar.setValue(int(round(max(0.0, min(1.0, float(frac))) * 1000)))
        if msg:
            self._stage.setText(msg)
            theme.set_color(self._stage, COLORS["text_secondary"])

    def _check(self):
        if self._cancel.is_set():
            raise VCCancelled()

    def _on_cancel(self):
        if self._busy:
            self._cancel.set()
            self._cancel_btn.setEnabled(False)
            self._cancel_btn.setText("Cancelling…")

    def _finish(self, message, color):
        self._busy = False
        self._cancel_btn.hide()
        if message:
            self._stage.setText(message)
            theme.set_color(self._stage, color)

    # ── the run ────────────────────────────────────────────────────
    def _prepare_clip(self):
        """One WAV of the selected audio (≤MAX_ANALYSIS_SECONDS), cleaned when music removal is on."""
        from core.audio_io import load_segment
        from pydub import AudioSegment
        out = AudioSegment.silent(duration=0, frame_rate=44100)
        for sel in self.selections:
            seg = load_segment(sel["path"]).set_channels(1).set_frame_rate(44100).set_sample_width(2)
            a = int(round((sel.get("start") or 0.0) * 1000))
            b = int(round(sel["end"] * 1000)) if sel.get("end") is not None else len(seg)
            out += seg[a:b]
            if len(out) >= MAX_ANALYSIS_SECONDS * 1000:
                break
        out = out[:MAX_ANALYSIS_SECONDS * 1000]
        path = os.path.join(self._tmp, "clip.wav")
        out.export(path, format="wav")
        if self.clean:
            from core.voice_cloning import get_client
            self._ui(self._progress, None, "Removing background music before measuring…")
            cleaned = os.path.join(self._tmp, "clip_clean.wav")
            get_client().separate(path, cleaned, denoise=self.denoise, cancel_event=self._cancel)
            path = cleaned
        return path

    def _start(self):
        if self._closed:
            return
        self._busy = True
        self._cancel.clear()
        self._cancel_btn.setEnabled(True)
        self._cancel_btn.setText("✖  Cancel")
        self._cancel_btn.show()
        engine_names = list(self.app.synth.engine_map)
        system_voices = self.app.system_voices

        def work():
            from core.voice_cloning import get_client
            client = get_client()
            cands = voicematch.candidates(engine_names, system_voices)
            self._cands = {c["id"]: c for c in cands}
            bank = voicematch.load_bank()
            if voicematch.needs_work(bank, cands):
                todo = len(voicematch.missing(bank, cands))
                self._ui(self._progress, 0.0, f"Updating the voice bank ({todo} voices)…" if todo
                         else "Updating the voice bank…")
                bank = voicematch.build_bank(self.app.synth, self._eng_settings, cands,
                                             progress=lambda f, m: self._ui(self._progress, 0.45 * f, m),
                                             check_cancel=self._check)
            self._check()
            self._ui(self._progress, 0.46, "Preparing your clip…")
            clip_wav = self._prepare_clip()
            self._clip_wav = clip_wav
            self._check()
            clip = voicematch.analyze_clip(
                clip_wav, embed_fn=lambda p: (client.embed([p], cancel_event=self._cancel) or [None])[0],
                progress=lambda f, m: self._ui(self._progress, 0.48 + 0.12 * f, m))
            self._clip_text = (clip.get("text") or "").strip()
            if self.gender_mode == "auto":
                gender, why = voicematch.decide_gender(bank, cands, clip)
                self._gender_note = f"Auto: {why}"
            else:
                gender = self.gender_mode
                self._gender_note = "your choice"
            self._gender = gender
            self._ui(self._show_clip, clip)
            shortlist = voicematch.rank(bank, cands, clip, gender=gender)
            if not shortlist:
                return clip, [], None

            if not self._clip_text:
                return clip, shortlist, ("No words were heard in your clip, so there is no Result. "
                                         "Ranked by voice similarity.")
            return clip, *self._make_results(shortlist, clip_wav, clip)

        def runner():
            try:
                clip, ranked, note = work()
                self._ui(self._show, ranked, note)
            except VCCancelled:
                self._ui(self._finish, "Cancelled.", COLORS["warning"])
            except VCError as e:
                self._ui(self._finish, f"❌  Voice match failed: {e.message}" + (f" {e.hint}" if e.hint else ""),
                         COLORS["error"])
            except Exception as e:   # noqa: BLE001
                self._ui(self._finish, f"❌  Voice match failed: {type(e).__name__}: {e}", COLORS["error"])
        threading.Thread(target=runner, daemon=True, name="voice-match").start()

    def _show_clip(self, clip):
        self._clip = clip
        lang = voicematch.LANG_NAMES.get(clip["lang"], clip["lang"] or "unknown")
        speed = (f"{clip['rate']} {voicematch.rate_unit(clip['lang'])}" if clip.get("rate") else "not measured")
        f0 = f"{clip['f0']:.0f} Hz" if clip.get("f0") else "not measured"
        text = (f"Pitch {f0} ({clip['gender']}) · speaking speed {speed} · {lang} "
                f"({clip['lang_prob'] * 100:.0f}% sure) · {clip['seconds']} s of speech measured")
        if self._gender:
            text += f"\nMatching {self._gender} voices only ({self._gender_note})."
        else:
            text += f"\nMatching all voices ({self._gender_note})."
        f0_hz = clip.get("f0")
        if self.gender_mode != "auto" and f0_hz and not clip.get("f0_unsure") and (
                (self._gender == "male" and f0_hz > voicematch.CLIP_FEMALE_ABOVE)
                or (self._gender == "female" and f0_hz < voicematch.CLIP_MALE_BELOW)):
            text += f"\n⚠  The clip's pitch ({f0_hz:.0f} Hz) sounds {'female' if self._gender == 'male' else 'male'}."
        if clip.get("lang") and clip["lang"] != "en" and clip.get("lang_prob", 0) >= voicematch.LANG_CONFIDENCE:
            text += (f"\n⚠  The clip sounds like {lang}. Voice match compares English voices only, "
                     "so treat the matches as rough.")
        self._clip_lbl.setText(text)

    def _make_results(self, shortlist, clip_wav, clip):
        """Speak the clip's words with each shortlisted voice, clone them, rank by the results.

        Runs on the worker thread. Returns (ranked, note). A voice whose Result
        fails keeps its shortlist place after the voices that have one.
        """
        import numpy as np
        from core.audio_io import load_segment
        from core.voice_cloning import get_client, library
        client = get_client()
        ref = os.path.join(self._tmp, "reference.wav")
        library._drop_silences(load_segment(clip_wav))[:25000].export(ref, format="wav")
        self._ref = ref
        text, n, failed = self._clip_text, len(shortlist), []
        for i, m in enumerate(shortlist):
            self._check()
            cand = self._cands[m["id"]]
            try:
                self._ui(self._progress, 0.62 + 0.3 * i / n,
                         f"▶ Result {i + 1} of {n}: your clip's words in {m['label']}…")
                src = os.path.join(self._tmp, f"transcript_{i}.wav")
                voicematch.speak(self.app.synth, self._eng_settings, cand, text, src, speed=m.get("speed"),
                                 check_cancel=self._check)
                self._check()
                self._ui(self._progress, 0.62 + 0.3 * (i + 0.4) / n,
                         f"▶ Result {i + 1} of {n}: cloning {m['label']}…")
                out = os.path.join(self._tmp, f"result_{i}.wav")
                client.convert_reference(src, ref, out, diffusion_steps=10, cancel_event=self._cancel)
                m["result"] = out
                # what the Result was spoken with, so a voice created with the same
                # base can reuse it as its preview
                m["synth_key"] = voice_selection.synthesis_key(
                    voicematch.speak_settings(self._eng_settings, cand, m.get("speed")))
            except VCCancelled:
                raise
            except Exception as e:   # noqa: BLE001
                if self._cancel.is_set():
                    raise VCCancelled()
                failed.append(m["label"])
                print(f"Voice match Result failed for {m['label']}: {e}")
        made = [m for m in shortlist if m.get("result")]
        if not made:
            return shortlist, "The Results could not be made. Ranked by voice similarity instead."
        self._ui(self._progress, 0.94, "Comparing the results with your clip…")
        c = np.asarray(clip["emb"], dtype=np.float32)
        for m, e in zip(made, client.embed([m["result"] for m in made], cancel_event=self._cancel)):
            # the result takes the clip's timbre whatever the source, so keep the pitch penalty
            sim = float(np.dot(c, np.asarray(e, dtype=np.float32))) if e else -1.0
            m["final"] = sim - voicematch.PITCH_PENALTY * (m.get("st") or 0)
        made.sort(key=lambda m: m["final"], reverse=True)
        note = (f"No ▶ Result for {', '.join(failed)}; it is listed after the others." if failed else None)
        return made + [m for m in shortlist if not m.get("result")], note

    def _show(self, ranked, note):
        self._ranked = ranked
        self._finish(note or "Done. Apply the best match, or listen to the others first.",
                     COLORS["warning"] if note else COLORS["success"])
        self._bar.setValue(1000)
        self._render()

    # ── results ────────────────────────────────────────────────────
    def _render(self):
        self._stop()
        while self._list.count() > 1:            # keep the trailing stretch
            w = self._list.takeAt(0).widget()
            if w is not None:
                w.deleteLater()
        if not self._ranked:
            who = f"{self._gender} " if self._gender else ""
            lab = QLabel(f"No matching English {who}engine voice was found.")
            lab.setObjectName("amNone")
            lab.setContentsMargins(0, 6, 0, 6)
            self._list.insertWidget(0, lab)
            return
        clip_f0 = (self._clip or {}).get("f0")
        for i, m in enumerate(self._ranked, 1):
            best = i == 1
            card = QFrame()
            card.setObjectName("amRow")
            card.setProperty("best", best)
            row = QHBoxLayout(card)
            row.setContentsMargins(12, 8, 10, 8)
            row.setSpacing(0)
            left = QVBoxLayout()
            left.setSpacing(0)
            tag = QLabel(f"{i}.  {'Best match' if best else 'Close match'}")
            tag.setObjectName("amTag")
            tag.setProperty("best", best)
            left.addWidget(tag)
            voice = QLabel(m["label"])
            voice.setObjectName("amVoice")
            voice.setMinimumWidth(1)
            left.addWidget(voice)
            detail = []
            if m.get("f0") and clip_f0:
                detail.append(f"pitch {m['f0']:.0f} Hz vs your {clip_f0:.0f} Hz")
            if m.get("speed") is not None:
                detail.append(f"speed {m['speed']}{' WPM' if m['selection'].get('kind') == 'pyttsx3' else 'x'}")
            if detail:
                d = QLabel(" · ".join(detail))
                d.setObjectName("amDetail")
                d.setMinimumWidth(1)
                left.addWidget(d)
            row.addLayout(left, 1)
            row.addSpacing(10)
            if self._clip_wav:
                row.addWidget(self._play_button(f"original:{m['id']}", "▶ Original", self._clip_wav,
                                                COLORS["accent_secondary"]))
                row.addSpacing(4)
            if m.get("result"):
                row.addWidget(self._play_button(f"result:{m['id']}", "▶ Result", m["result"], COLORS["success"]))
                row.addSpacing(4)
            apply_btn = theme.button("Apply", theme.ROLE_PRIMARY)
            apply_btn.setObjectName("amApply")
            apply_btn.setFixedSize(70, 28)
            apply_btn.setAutoDefault(False)
            apply_btn.clicked.connect(lambda _c=False, m=m: self._apply(m))
            row.addSpacing(2)
            row.addWidget(apply_btn, 0, Qt.AlignVCenter)
            card.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
            self._list.insertWidget(self._list.count() - 1, card)

    def _play_button(self, key, text, path, color):
        b = theme.button(text, theme.ROLE_GHOST)
        b.setObjectName("amPlay")
        # each toggle is outlined in its own color and fills with it on hover
        b.setStyleSheet(f"QPushButton#amPlay {{ border-color: {color}; }}"
                        f"QPushButton#amPlay:hover {{ background: {color}; }}")
        b.setFixedSize(78, 28)
        b.setAutoDefault(False)
        b.clicked.connect(lambda _c=False: self._toggle(key, b, text, path))
        return b

    def _apply(self, m):
        err = self.app.engine_panel.apply_voice_selection(m["selection"])
        if err:
            self._stage.setText(f"⚠  Could not apply: {err}")
            theme.set_color(self._stage, COLORS["warning"])
            return
        desc = voice_selection.describe_selection(m["selection"])
        self.app.set_status(f"🔍  TTS Engine set to {desc} by voice match. Create the voice to keep it "
                            "as its base.", COLORS["success"])
        if self.on_applied:
            result = None
            if m.get("result") and os.path.isfile(m["result"]):
                # on_applied copies the file before close() removes this window's temp folder
                result = {"wav": m["result"], "synth_key": m["synth_key"], "selections": self.selections,
                          "clean": self.clean, "denoise": self.denoise}
            self.on_applied(m["selection"], result)
        self.close()

    # ── play / stop toggles ────────────────────────────────────────
    def _toggle(self, key, button, idle_text, path):
        same = self._playing is not None and self._playing[0] == key
        self._stop()
        if same:
            return                      # second click on the playing button: stop only
        try:
            import pygame
            if not pygame.mixer.get_init() or not os.path.exists(path):
                return
            if pygame.mixer.get_num_channels() <= PLAY_CHANNEL:
                pygame.mixer.set_num_channels(PLAY_CHANNEL + 1)
            self._sound = pygame.mixer.Sound(path)
            pygame.mixer.Channel(PLAY_CHANNEL).play(self._sound)
        except Exception as e:
            print(f"Voice match playback failed: {e}")
            return
        self._playing = (key, button, idle_text)
        button.setText("■ Stop")
        self._watch_timer.start()

    def _watch(self):
        """Reset the button when the clip ends on its own."""
        if self._playing is None or self._closed:
            self._watch_timer.stop()
            return
        try:
            import pygame
            busy = pygame.mixer.get_init() and pygame.mixer.Channel(PLAY_CHANNEL).get_busy()
        except Exception:
            busy = False
        if not busy:
            self._stop()

    def _stop(self):
        self._watch_timer.stop()
        try:
            import pygame
            if pygame.mixer.get_init() and pygame.mixer.get_num_channels() > PLAY_CHANNEL:
                pygame.mixer.Channel(PLAY_CHANNEL).stop()
        except Exception:
            pass
        if self._playing is not None:
            _key, button, idle_text = self._playing
            self._playing = None
            try:
                button.setText(idle_text)
            except RuntimeError:        # the row was rebuilt meanwhile
                pass

    # ── closing ────────────────────────────────────────────────────
    def _release(self):
        """Cancel the run, stop audio and delete the temp folder (the CTk destroy())."""
        if self._closed:
            return
        self._closed = True
        self._cancel.set()
        self._stop()
        self._sound = None
        shutil.rmtree(self._tmp, ignore_errors=True)

    def closeEvent(self, e):
        self._release()
        super().closeEvent(e)

    def reject(self):
        self._release()
        super().reject()
