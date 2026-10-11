"""
ui/panels/analyzer_dialog.py  –  "Voice match": the best base voice for the clips

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
  5. Test-clones a sentence from each with Seed-VC (the clip is the reference)
     and ranks them by how close each cloned result sounds to the clip, minus
     the same pitch penalty as the shortlist: 1. Best match, 2. and 3. Close match.

Each row: ▶ Sample (the engine voice), ▶ Original (the user's clip as it was
measured: trimmed, and cleaned when music removal is on) and ▶ Result (its
cloned test), all play/stop toggles, and Apply, which selects engine, voice and a speed that
matches the clip's pace (EnginePanel.apply_voice_selection) and closes the
window. The user can still change the controls afterwards.
"""

import os
import shutil
import tempfile
import threading

import customtkinter as ctk

from core.constants import COLORS, FONTS
from core.voice_cloning import voicematch
from core.voice_cloning.errors import VCCancelled, VCError

MAX_ANALYSIS_SECONDS = 60
PLAY_CHANNEL = 5


class AnalyzerDialog(ctk.CTkToplevel):
    def __init__(self, app, selections, clean=False, denoise=False, on_applied=None, gender="auto"):
        super().__init__(app)
        self.app = app
        self.selections = selections
        self.gender_mode = gender if gender in voicematch.GENDERS else "auto"
        self._gender = None           # the gender actually matched: "male", "female" or None (all)
        self._gender_note = ""
        self.clean = clean
        self.denoise = denoise
        self.on_applied = on_applied
        self.title("Voice match")
        self.configure(fg_color=COLORS["bg_dark"])
        self.geometry("840x560")
        self.minsize(640, 480)
        self.transient(app)
        self._cancel = threading.Event()
        self._busy = False
        self._tmp = tempfile.mkdtemp(prefix="adits_vs_match_")
        self._clip = None
        self._clip_wav = None         # the prepared clip (trim, music removal), played by ▶ Original
        self._ranked = []
        self._sound = None
        self._playing = None          # (key, button, idle_text) of the clip being played
        try:
            self._eng_settings = app._engine_panel.get_settings()
        except Exception:
            self._eng_settings = {}

        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(fill="x", padx=16, pady=(14, 2))
        ctk.CTkLabel(head, text="🔍  Voice match", font=FONTS["subtitle"],
                     text_color=COLORS["text_primary"]).pack(side="left")
        ctk.CTkLabel(self, text="Test-clones your clip from the closest English engine voices and ranks them. "
                                "Apply sets the engine, voice and speed; you can still change them.",
                     font=FONTS["body_small"], text_color=COLORS["text_muted"], wraplength=720,
                     justify="left").pack(anchor="w", padx=16)

        # progress (Cancel only while something runs)
        self._task = ctk.CTkFrame(self, fg_color="transparent")
        self._task.pack(fill="x", padx=16, pady=(8, 0))
        self._task.columnconfigure(0, weight=1)
        self._bar = ctk.CTkProgressBar(self._task, height=6, fg_color=COLORS["slider_track"],
                                       progress_color=COLORS["accent_primary"], corner_radius=3)
        self._bar.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self._bar.set(0)
        self._cancel_btn = ctk.CTkButton(self._task, text="✖  Cancel", font=FONTS["button"], width=96,
                                         height=28, corner_radius=8, fg_color="#CC0000",
                                         hover_color=COLORS["error"], command=self._on_cancel)
        self._cancel_btn.grid(row=0, column=1, rowspan=2)
        self._stage = ctk.CTkLabel(self._task, text="", font=FONTS["body_small"],
                                   text_color=COLORS["text_secondary"], anchor="w", justify="left",
                                   wraplength=580)
        self._stage.grid(row=1, column=0, sticky="w", pady=(2, 0))

        # clip summary
        clip_box = ctk.CTkFrame(self, fg_color=COLORS["bg_card"], corner_radius=12, border_width=1,
                                border_color=COLORS["border"])
        clip_box.pack(fill="x", padx=16, pady=(10, 4))
        ctk.CTkLabel(clip_box, text="Your clip", font=("Segoe UI", 12, "bold"),
                     text_color=COLORS["text_secondary"]).pack(anchor="w", padx=12, pady=(8, 0))
        self._clip_lbl = ctk.CTkLabel(clip_box, text="Analyzing…", font=FONTS["body"],
                                      text_color=COLORS["text_primary"], anchor="w", justify="left",
                                      wraplength=700)
        self._clip_lbl.pack(anchor="w", padx=12, pady=(2, 10))

        # ranked matches
        self._list = ctk.CTkFrame(self, fg_color="transparent")
        self._list.pack(fill="both", expand=True, padx=16, pady=4)

        foot = ctk.CTkFrame(self, fg_color="transparent")
        foot.pack(fill="x", padx=16, pady=(4, 14))
        ctk.CTkButton(foot, text="Close", font=FONTS["button"], width=90, height=32, corner_radius=8,
                      fg_color=COLORS["bg_card"], hover_color=COLORS["border"],
                      command=self.destroy).pack(side="right")

        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.after(150, self._grab)
        self.after(300, self._start)

    def _grab(self):
        try:
            self.lift()
            self.focus_force()
        except Exception:
            pass

    # ── task plumbing ──────────────────────────────────────────────
    def _ui(self, fn, *args):
        try:
            self.after(0, lambda: self.winfo_exists() and fn(*args))
        except Exception:
            pass   # window closed while work was still running

    def _progress(self, frac, msg):
        if frac is not None:
            self._bar.set(max(0.0, min(1.0, float(frac))))
        if msg:
            self._stage.configure(text=msg, text_color=COLORS["text_secondary"])

    def _check(self):
        if self._cancel.is_set():
            raise VCCancelled()

    def _on_cancel(self):
        if self._busy:
            self._cancel.set()
            self._cancel_btn.configure(state="disabled", text="Cancelling…")

    def _finish(self, message, color):
        self._busy = False
        self._cancel_btn.grid_remove()
        if message:
            self._stage.configure(text=message, text_color=color)

    # ── the run ────────────────────────────────────────────────────
    def _prepare_clip(self):
        """One WAV of the selected audio (≤60 s), cleaned when music removal is on."""
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
        self._busy = True
        self._cancel.clear()
        self._cancel_btn.grid()

        def work():
            import numpy as np
            from core.audio_io import load_segment
            from core.voice_cloning import get_client, library
            client = get_client()
            cands = voicematch.candidates(list(self.app._engine_map), getattr(self.app, "_system_voices", []))
            bank = voicematch.load_bank()
            if voicematch.needs_work(bank, cands):
                todo = len(voicematch.missing(bank, cands))
                self._ui(self._progress, 0.0, f"Updating the voice bank ({todo} voices)…" if todo
                         else "Updating the voice bank…")
                bank = voicematch.build_bank(self.app, self._eng_settings, cands,
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

            # test-clone each shortlisted voice; rank by how close the result sounds to the clip
            note = None
            try:
                ref = os.path.join(self._tmp, "reference.wav")
                library._drop_silences(load_segment(clip_wav))[:25000].export(ref, format="wav")
                outs = []
                for i, m in enumerate(shortlist):
                    self._check()
                    self._ui(self._progress, 0.62 + 0.3 * i / len(shortlist),
                             f"Test-cloning {i + 1} of {len(shortlist)}: {m['label']}…")
                    out = os.path.join(self._tmp, f"result_{i}.wav")
                    client.convert_reference(os.path.join(voicematch.bank_dir(), "samples", m["wav"]),
                                             ref, out, diffusion_steps=10, cancel_event=self._cancel)
                    outs.append(out)
                self._ui(self._progress, 0.94, "Comparing the results with your clip…")
                c = np.asarray(clip["emb"], dtype=np.float32)
                for m, out, e in zip(shortlist, outs, client.embed(outs, cancel_event=self._cancel)):
                    m["result"] = out
                    # the result takes the clip's timbre whatever the source, so keep the pitch penalty
                    sim = float(np.dot(c, np.asarray(e, dtype=np.float32))) if e else -1.0
                    m["final"] = sim - voicematch.PITCH_PENALTY * (m.get("st") or 0)
                shortlist.sort(key=lambda m: m["final"], reverse=True)
            except VCCancelled:
                raise
            except VCError as e:
                note = f"Could not test-clone ({e.message}). Ranked by voice similarity instead."
            return clip, shortlist, note

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
        self._clip_lbl.configure(text=text)

    def _show(self, ranked, note):
        self._ranked = ranked
        self._finish(note or "Done. Apply the best match, or listen to the others first.",
                     COLORS["warning"] if note else COLORS["success"])
        self._bar.set(1.0)
        self._render()

    # ── results ────────────────────────────────────────────────────
    def _render(self):
        for w in self._list.winfo_children():
            w.destroy()
        if not self._ranked:
            who = f"{self._gender} " if self._gender else ""
            ctk.CTkLabel(self._list, text=f"No matching English {who}engine voice was found.", font=FONTS["body"],
                         text_color=COLORS["text_muted"]).pack(anchor="w", pady=10)
            return
        clip_f0 = (self._clip or {}).get("f0")
        for i, m in enumerate(self._ranked, 1):
            best = i == 1
            card = ctk.CTkFrame(self._list, fg_color=COLORS["bg_input"], corner_radius=10,
                                border_width=2 if best else 1,
                                border_color=COLORS["success"] if best else COLORS["border"])
            card.pack(fill="x", pady=4)
            left = ctk.CTkFrame(card, fg_color="transparent")
            left.pack(side="left", fill="x", expand=True, padx=12, pady=8)
            tag = "Best match" if best else "Close match"
            ctk.CTkLabel(left, text=f"{i}.  {tag}", font=("Segoe UI", 11, "bold"),
                         text_color=COLORS["success"] if best else COLORS["text_secondary"],
                         anchor="w").pack(anchor="w")
            ctk.CTkLabel(left, text=m["label"], font=("Segoe UI", 13, "bold"), text_color=COLORS["text_primary"],
                         anchor="w").pack(anchor="w")
            detail = []
            if m.get("f0") and clip_f0:
                detail.append(f"pitch {m['f0']:.0f} Hz vs your {clip_f0:.0f} Hz")
            if m.get("speed") is not None:
                detail.append(f"speed {m['speed']}{' WPM' if m['selection'].get('kind') == 'pyttsx3' else 'x'}")
            if detail:
                ctk.CTkLabel(left, text=" · ".join(detail), font=FONTS["body_small"],
                             text_color=COLORS["text_secondary"], anchor="w").pack(anchor="w")
            btns = ctk.CTkFrame(card, fg_color="transparent")
            btns.pack(side="right", padx=10)
            sample = os.path.join(voicematch.bank_dir(), "samples", m["wav"])
            self._play_button(btns, f"sample:{m['id']}", "▶ Sample", sample, COLORS["text_muted"])
            if self._clip_wav:
                self._play_button(btns, f"original:{m['id']}", "▶ Original", self._clip_wav,
                                  COLORS["accent_secondary"])
            if m.get("result"):
                self._play_button(btns, f"result:{m['id']}", "▶ Result", m["result"], COLORS["success"])
            ctk.CTkButton(btns, text="Apply", font=FONTS["button"], width=70, height=28, corner_radius=8,
                          fg_color=COLORS["accent_primary"], hover_color=COLORS["accent_secondary"],
                          command=lambda m=m: self._apply(m)).pack(side="left", padx=(6, 0))

    def _play_button(self, parent, key, text, path, color):
        b = ctk.CTkButton(parent, text=text, font=FONTS["body_small"], width=78, height=28, corner_radius=8,
                          fg_color=COLORS["bg_card"], hover_color=color, border_color=color, border_width=1)
        b.configure(command=lambda: self._toggle(key, b, text, path))
        b.pack(side="left", padx=2)
        return b

    def _apply(self, m):
        err = self.app._engine_panel.apply_voice_selection(m["selection"])
        if err:
            self._stage.configure(text=f"⚠  Could not apply: {err}", text_color=COLORS["warning"])
            return
        from ui.panels.engine_panel import EnginePanel
        desc = EnginePanel.describe_selection(m["selection"])
        self.app._set_status(f"🔍  TTS Engine set to {desc} by voice match. Create the voice to keep it "
                             "as its base.", COLORS["success"])
        if self.on_applied:
            self.on_applied(m["selection"])
        self.destroy()

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
            self._sound = pygame.mixer.Sound(path)
            pygame.mixer.Channel(PLAY_CHANNEL).play(self._sound)
        except Exception as e:
            print(f"Voice match playback failed: {e}")
            return
        self._playing = (key, button, idle_text)
        button.configure(text="■ Stop")
        self.after(200, self._watch)

    def _watch(self):
        """Reset the button when the clip ends on its own."""
        if self._playing is None or not self.winfo_exists():
            return
        try:
            import pygame
            busy = pygame.mixer.get_init() and pygame.mixer.Channel(PLAY_CHANNEL).get_busy()
        except Exception:
            busy = False
        if busy:
            self.after(200, self._watch)
        else:
            self._stop()

    def _stop(self):
        try:
            import pygame
            if pygame.mixer.get_init():
                pygame.mixer.Channel(PLAY_CHANNEL).stop()
        except Exception:
            pass
        if self._playing is not None:
            _key, button, idle_text = self._playing
            self._playing = None
            try:
                if button.winfo_exists():
                    button.configure(text=idle_text)
            except Exception:
                pass

    def destroy(self):
        self._cancel.set()
        self._stop()
        self._sound = None
        shutil.rmtree(self._tmp, ignore_errors=True)
        super().destroy()
