# PySide6 Migration Plan

Move the Adits Voice Studio UI from CustomTkinter to PySide6 (Qt 6), one phase
at a time, with the app usable after every phase.

Status, 2026-10-14: done. Phases 0 to 6 are committed; the classic
CustomTkinter window (`ui/`) was removed on `dev-tts` without waiting for a
release on Qt, at the owner's request. `ui_qt/` was then renamed `ui/`.

---

## 1. Starting point (measured in the repo)

| What | Size | Notes |
| :--- | ---: | :--- |
| `ui/` (13 files) | about 7,400 lines | Rewritten in this migration. |
| `core/`, `engines/`, `effects/` | about 7,500 lines | Not touched, except the pipeline move in Phase 1. |
| `clone_voice_group.py` | 1,990 lines | Largest single file. |
| `app.py` | 795 lines | Holds the window **and** the generation pipeline. |
| CTk widgets | 109 frames, 97 labels, 55 buttons, 8 option menus, 7 switches, 1 textbox | |
| Tk variables | 25 StringVar, 20 DoubleVar, 10 BooleanVar, 2 IntVar | Shared state between widgets. |
| Canvas use | about 60 references | Waveforms, level meter, timeline. |
| Dialogs | 23 `messagebox`, 12 `filedialog`, 6 `CTkToplevel` / `tk.Toplevel` | |
| `after()` calls | 47 | How threads report back to the UI. |
| Worker threads in `ui/` | 9 | Generation, playback, transcription, dictation, clone jobs. |
| pygame channels | 1 to 7 | Ambiance, previews, timeline, recorder, analyzer, clips, media. |
| Automated UI tests | none | Only the headless `--selftest`. |

Two findings shape the plan:

1. **The generation pipeline lives in `ui/app.py`** (`_generate_worker`, about
   270 lines). It must move out of the UI before the port, or it would be
   rewritten twice.
2. **The worker thread reads widget state.** `ui/app.py:418-420` calls
   `get_settings()` and `get_active_effects()` from the background thread.
   Tkinter usually tolerates this. Qt does not: touching widgets off the GUI
   thread can crash the app. Settings must be read on the GUI thread and
   passed to the worker as plain data.

---

## 2. Approach

* **Side by side, not in place.** Build the new UI in a new package `ui_qt/`.
  The CTk UI in `ui/` stays the default until the Qt version reaches parity.
* **Switch at launch.** `tts_app.py --qt` (or `ADITS_UI=qt`) starts the Qt UI.
  The flag goes away at cut-over.
* **One branch.** Work on `qt-migration`, branched from `dev-tts`. Merge at
  the end of each phase so the branch never drifts far.
* **Keep what works.** pygame stays for playback (it does not need a pygame
  window). PyAudio, faster-whisper and the clone workers stay as they are.
* **Same look.** Colors and fonts come from `core/constants.py` (`COLORS`,
  `FONTS`), turned into a Qt style sheet.

---

## 3. Phases

Each phase lists its work, what it produces, and how it is checked. Size is
given as line counts from the table above. Day estimates are rough guesses for
one developer working with an AI assistant, not measurements.

### Phase 0. Prepare (rough guess: under 1 day)

* Create the `qt-migration` branch.
* Add `PySide6` to `requirements.txt`, install it with
  `.\venv_311\Scripts\python.exe -m pip install PySide6`, and confirm it
  imports next to torch in `venv_311`.
* Check that `pygame.mixer.init()` works with a `QApplication` running.
* Write a **parity checklist**: every button, menu, shortcut and dialog in the
  current UI, one line each. It is the test plan for every later phase, since
  there are no UI tests.
* Optional quick win for the current app: fix the playback row overflow seen
  during "Generating…" (`ui/panels/playback_panel.py`, buttons packed with
  fixed widths).

**Done when:** the branch exists, PySide6 imports in `venv_311`, and the
checklist is written.

**Result (2026-10-14):**

* Installed `PySide6-Essentials==6.11.2` (QtCore, QtGui, QtWidgets; no
  Addons). 6.12.0 was the newest, but 6.11.2 has had bug-fix releases.
  `requirements.txt` pins `>=6.11.2,<6.12`. The install added only
  `shiboken6`. The `pip check` warnings it printed (`styletts2`,
  `simpler-kokoro`) were already there and do not involve Qt.
* Checked in both import orders (torch then Qt, Qt then torch): a
  `QApplication` with a window, a CUDA matmul on the GTX 1650,
  `pygame.mixer.init()` plus `mixer.music` playback, and a worker thread
  emitting a `Signal` that Qt delivered on the GUI thread. All passed.
* At 125% Windows scaling Qt reported a device pixel ratio of 1.25 and
  drew a 500x300 window at 625x375 physical pixels. Emoji (🎤 ♡ ✏️) rendered
  in color. With no style sheet, Qt followed the Windows dark mode.
* Checklist: [ui-parity-checklist.md](ui-parity-checklist.md), with a list
  of possible bugs found in the current UI.
* Playback row quick fix skipped: the clipped labels are a layout issue, but
  the black areas are CTk redraw glitches a layout change would not fix, and
  Phase 2 rebuilds this panel first.

### Phase 1. Separate logic from the UI, still on CTk (rough guess: 2 to 3 days)

This phase changes no visible behavior. It makes the port smaller and fixes
the threading issue in the app people use today.

* **Move the pipeline** from `AditsVoiceStudioApp._generate_worker` to
  `core/pipeline.py`. It takes a plain settings object and callbacks
  (`progress`, `status`, `warn`, `check_cancel`) and returns the result files.
  No Tk imports.
* **Snapshot settings on the UI thread.** Read engine, customization and
  effect settings in `_on_generate` before the thread starts, and pass them in.
  Same for the Clone Voice jobs that read widgets from threads.
* **Move playback** (`_playback_thread` and the pygame calls in `app.py`) into
  `core/playback.py` with `play`, `stop`, `position`, `is_playing`.
* **Define a UI bridge**: one small interface the panels use instead of
  reaching into the app (`set_status`, `engine_panel`, `refresh_cloned_voices`,
  `is_generating`, `engine_lock`). Today panels use 10 different `app`
  members directly, including private ones such as `_engine_panel`.

**Done when:** the CTk app works exactly as before, `core/pipeline.py` runs
from a plain Python script with no UI, and no thread touches a widget.

### Phase 2. Qt shell and the two hardest daily widgets (rough guess: 4 to 6 days)

This is the trial run. Decide at the end whether to continue.

* `ui_qt/app.py`: `QMainWindow`, header, two-column layout with a
  `QSplitter`, right column in a `QScrollArea` (replaces
  `SmoothScrollableFrame`).
* `ui_qt/theme.py`: build the style sheet from `COLORS` / `FONTS`. Rounded
  cards, accent buttons, dark scrollbars.
* `ui_qt/bridge.py`: a `QObject` with signals (`status`, `progress`,
  `finished`, `error`). Worker threads emit signals instead of calling
  `after(0, …)`. Qt delivers them on the GUI thread.
* **Playback panel** (161 lines): Generate, Play, Stop, Save MP3, Save SRT,
  Folder, Cancel, progress bar. Buttons in a `QHBoxLayout` with size policies,
  so text changes cannot push them out of the card.
* **Text panel** (768 lines): `QPlainTextEdit`, character counter, Copy / Paste
  / Paste & Replace / Clear, undo and redo buttons tied to
  `undoAvailable` / `redoAvailable`, the **+** menu (import captions,
  transcribe), the media strip, and 🎤 dictation.
  * **Design risk to settle here:** the CTk version writes the gray live
    dictation text with undo turned off. In Qt,
    `QTextDocument.setUndoRedoEnabled(False)` **clears** the undo stack, so that
    trick does not carry over. Options: show live text in a gray line under the
    editor, or keep it in the document and remove it inside the same edit block
    before each committed phrase. Pick one in this phase.
* Wire Generate end to end through `core/pipeline.py` with fixed default
  engine settings.

**Done when:** text can be typed, imported, dictated and transcribed; speech
can be generated, played, stopped and saved from the Qt window; window resize
and 125% / 150% Windows scaling show no overlap or black areas.

**Go / no-go:** if the text panel or the threading model fights back in ways
the CTk version does not, stop here. Phase 1 still paid for itself.

**Result (2026-10-14):**

* Built: `ui_qt/` (`app.py`, `theme.py`, `bridge.py`, `widgets.py`,
  `panels/playback_panel.py`, `panels/text_panel.py`), `core/defaults.py`
  (the panels' starting settings, used for Generate until Phase 3), and
  `core.pipeline.snippet_of()` (Preview's first-sentence rule, now shared).
  `tts_app.py --qt` or `ADITS_UI=qt` starts it; the CTk UI stays the default.
* Live dictation text, decided: the phrase in progress shows as a gray line
  under the box, not inside it. Qt clears the undo history when undo is
  switched off, which the CTk approach relies on. Finished phrases go into the
  box as one undo step each, as before.
* Transcription stays one undo step: the first change opens an edit block
  and each segment joins it (`QTextCursor.joinPreviousEditBlock`). Revert
  undoes only when the transcription itself added an undo step.
* Playback row: each button's minimum width fits its longest text. When the
  card is narrower than all six need, the row becomes two rows of three. At
  1180x680 and 1100x600 (125% scaling) it shows as two rows; no text is
  clipped and no buttons overlap.
* Checked by driving the real window (script, not by hand):
  - Footer edits: Paste over the sample replaces it, Paste over own text
    appends after a blank line, Clear, Paste & Replace; each is one undo
    step, and two undos return to the sample with ↶ greyed out.
  - put_text over own text with Append, then one undo restores it.
  - Dictation fed from a recorded WAV through `Dictation(source=...)`: state
    label went Loading → Hearing you → Writing → Listening; the gray line
    showed partials ("Phase.", "Phase one check.", ...); the final sentence
    was written after "Start." and one undo removed it.
  - Transcription of the same WAV, Append over own text: read-only during
    the job, correct text and status, one undo restored the old text.
    Media strip played (0:01 / 0:03 after 1.2 s), paused, closed.
  - Generate with Kokoro (defaults): 3.0 s at 24 kHz, Whisper read it back
    word for word. Play, progress, Stop and the "Playback stopped" status
    as in the CTk app.
  - `core.defaults` equals the CTk panels' `get_settings()` /
    `get_active_effects()` right after start, key for key.
  - `tts_app.py --qt` opened the window and closed with no process left.
* Not checked yet: the file dialogs, the + menu popup and the Replace /
  Append / Cancel box by hand (the test answered them in code), tooltips,
  Esc and Ctrl+Z / Ctrl+Y from the keyboard, dictation from a real
  microphone, the save dialogs and Folder, and 100% / 150% scaling.
* Not in the Qt UI until Phase 3: ambiance pause during playback (there is
  no ambiance control yet).

### Phase 3. Main panels (rough guess: 5 to 7 days)

* **Engine panel** (492 lines): engine `QComboBox`, GPU toggle, one page per
  engine in a `QStackedWidget` (replaces `_sub_frames` and `key_map`).
  Port `get_settings()`, `settings_for_selection()`, `synthesis_key()` and
  `apply_voice_selection()` with the same return values.
* **Effects panel** (590 lines, about 170 tiles): a tile grid in a flow
  layout. Favourites, Cloned Voices and Effects sections. One state model per
  effect key, so a favourite mirror and its original always agree (this
  replaces the shared `BooleanVar`). Search over the same model; Enter, ↑ / ↓
  and Esc as today. Exclusive groups (`exclusive_group: "clone"`) enforced in
  the model, not the widgets.
* **Customization panel** (438 lines, without the Clone Voice group): speed,
  skip silences, ambiance with custom import, subtitles.
* Replace the Tk variables with plain Python values plus Qt signals, panel by
  panel. Do not build a general "observable" library.

**Done when:** every engine, effect and customization item on the parity
checklist works in the Qt UI, and settings saved by CTk (favourites, voice
profiles) load unchanged.

**Result (2026-10-14):**

* Built: `ui_qt/panels/engine_panel.py`, `customization_panel.py`,
  `effects_panel.py`; `ToggleSwitch`, `ValueSlider` and `ElidedLabel` in
  `ui_qt/widgets.py`; style rules for dropdowns, tiles, the search box and
  the Results overlay. The placeholder column is gone; Generate reads the
  real panels.
* `core/voice_selection.py`: the engine panel's selection rules (engine
  kind, speed ranges and labels, `synthesis_key`, resolving a cloned voice's
  base voice, `settings_for_selection`) moved out of the CTk panel, which
  now calls them under the same method names. Recorded before and after the
  move on 16 cases (valid, missing and out-of-range engines, voices and
  speeds for all six engines): identical output.
* Effects state: one on/off value per key in the panel; every switch that
  shows a key (tile, Favourites mirror, search row) is redrawn from it. This
  replaces the CTk panel's shared `BooleanVar`.
* Fixed in the Qt panel, kept in CTk: choosing "Custom File..." again after
  a built-in ambiance reloads the imported file.
* Checked by driving the real window (favourites pointed at a temp file so
  the user's `favourites.json` was not touched):
  - Engine panel starts equal to `core.defaults`; each engine shows only its
    own options box. `apply_voice_selection()` and `settings_for_selection()`
    give the same settings, errors and speed labels as the CTk panel on the
    same 16 cases.
  - Customization starts equal to `core.defaults`; EQ sliders show only
    when on; Airplane Cabin generates, enables the player, hides Import and
    plays on channel 1; Custom after built-in reloads the file; Remove
    returns to None.
  - Effects: 168 tiles here (34 effects, 134 cloned voices), only Normalize
    on. Heart adds a mirror and writes `{"keys": ["robotic"]}`; mirror and
    tile switch together. Search "robot" gives 3 results ranked as in CTk,
    Enter toggles, ↓↓ moves to row 3, Esc closes; "e" shows 8 of 168 with
    the overflow note; no match shows the note. Two fake cloned voices act
    as one exclusive group and notify subscribers (on, switch, and None
    when removed).
  - Generate with Piper, Robotic, EQ bass +6 dB, Skip Silences and
    sentence subtitles: the status went through Robotic, equalizer and
    subtitles; Whisper read the audio back word for word and the SRT has two
    sentence cues.
* Not checked yet: dropdown popups and the ambiance file dialog by hand,
  and ambiance pausing while speech plays (wired as in CTk, not observed).
* Not in the Qt UI until Phase 4: the Clone Voice group, so switching a
  cloned voice on does not yet apply its base voice to TTS Engine, and its
  sound sliders use the voice's saved values. ✏️ explains this and points
  to the classic window.
* Cosmetic: at 1100 px the engine dropdown cuts a long engine name; the
  CTk dropdown does the same.

### Phase 4. Clone Voice and its dialogs (rough guess: 7 to 10 days)

The largest phase: about 3,900 lines across five files.

| File | Lines | Qt approach |
| :--- | ---: | :--- |
| `clone_voice_group.py` | 1,990 | `QTabWidget` (Active voice / Create voice). Tiles in a `QGridLayout` that switches between one and two columns at `WIDE_PX`. `_Tip` becomes `setToolTip`. `_VoiceList` becomes a `QComboBox` with `setMaxVisibleItems(12)` (Qt combo lists scroll). |
| `clone_voice_dialog.py` | 760 | `QDialog`. Voice editor rows as widgets in a `QScrollArea`. |
| `analyzer_dialog.py` | 419 | `QDialog`. Result rows with ▶ Original / ▶ Result toggles on pygame channel 5. |
| `record_dialog.py` | 459 | `QDialog`. Level meter and live waveform as custom `QWidget`s with `paintEvent`, fed by a `QTimer`. |
| `timeline_dialog.py` | 346 | Custom waveform widget: precomputed peaks, mouse drag / resize / new selection, wheel pan, Ctrl+wheel zoom, playhead from a `QTimer`. |

* Keep every pygame channel number as it is (`CLIP_PLAY_CHANNEL = 6`,
  `MEDIA_PLAY_CHANNEL = 7`, and so on).
* `messagebox` → `QMessageBox`, `filedialog` → `QFileDialog`.
* Job progress, Cancel and `VCClient` status callbacks go through the signal
  bridge from Phase 2.

**Done when:** install, Quick and RVC creation, import model, record, trim,
clean-up preview, Analyze, Apply, ✏️ edit, preview and delete all pass the
parity checklist.

**Result (2026-10-14):**

* `core/voice_cloning/tasks.py`: the Clone Voice jobs and rules moved out of
  the CTk group (create Quick / trained / imported voices, clip
  preparation, first preview, previews, pitch measuring and matching, the
  cleanup preview, clip selections, length rules, estimates). Both groups
  call it; the CTk group kept every method name (checked: no member lost,
  one added, `_ctx()`).
* Built: `ui_qt/panels/clone_voice_group.py` and `clone_voice_dialog.py`
  (lead), and `record_dialog.py`, `timeline_dialog.py`, `analyzer_dialog.py`
  (ported by three agents in parallel, each to a written interface, then
  reviewed). `Segmented` (CTkSegmentedButton) and `ValueSlider.set_range()`
  in `ui_qt/widgets.py`. The main window has `refresh_cloned_voices()` and
  opens the Cloned Voices dialog from ✏️.
* Forced differences: the Active voice picker is a QComboBox with a ♥
  delegate (Qt's list scrolls); tooltips use the system delay; the dialogs
  ignore Esc as the CTk ones do; the recorder reads only the last 12 s of
  frames for its live waveform; the timeline uses a min/max peak pyramid
  (column edges can move up to 64 samples).
* Checked with the installed runtime (test voices made and deleted; the
  user's 134 voices only read):
  - `tasks.py` without a UI: Quick voice made and its preview saved; a
    fresh preview; pitch measured on an RVC voice (offset -6, nothing
    saved); music-removal preview.
  - Qt window: switching on a Quick and an RVC voice applied their base
    voice and showed their saved values; generation with the RVC voice
    went through load and conversion and Whisper read it back word for
    word; Create tab rules for all three modes; Custom trim row; timeline
    and recorder open from the group; one or two tile columns at 471 and
    671 px; Quick test voice created in 90 s, switched on, preview saved;
    a slider move saved to its profile; ▶ Preview / ■ Stop; the Cloned
    Voices dialog listed 135 rows, previewed, renamed, saved and deleted
    the test voice.
  - CTk window after the move: its Create flow made a Quick test voice in
    78 s, switched it on and saved its preview.
  - Agents' own checks: recorder 56 of 56 (fake microphone, for privacy),
    timeline 74 of 74 (70 s WAV), voice match one full real run (ranked
    Kokoro bf_alice first, Apply passed the selection and Result back),
    plus Cancel and closing mid-run.
* Not checked: Install and Update (the runtime is already installed), a
  real RVC training, Import model with a real .pth, recording from a real
  microphone, the voice bank rebuild and the music-removal path inside
  Analyze.
* Found, not changed (also in the CTk app): take names have 1-second
  resolution, so two takes in the same second share a file name; deleting
  the active voice logs "Could not save voice settings" once.

### Phase 5. Packaging and docs (rough guess: 2 to 3 days)

* `build_exe.py`: replace `customtkinter` in `_REQUIRED` and the collect list
  with `PySide6`. PyInstaller ships hooks for PySide6. Exclude Qt modules the
  app does not use (for example WebEngine, Qt3D, QML) to keep size down.
* Extend the frozen self-test: start the Qt UI with
  `QT_QPA_PLATFORM=offscreen`, build the main window, close it. This catches a
  missing `platforms/qwindows.dll` before release.
* Licenses: PySide6 and Qt are LGPL-3 (compatible with the app's
  GPL-3.0-or-later). Add their notices to `licenses/` and
  `THIRD_PARTY_NOTICES.txt`. Check each extra Qt module's license before
  using it, since some Qt add-ons are GPL or commercial only.
* Test the installer build, an upgrade over the CTk version, and the
  non-ASCII install path test from AGENTS.md.
* Update AGENTS.md: file dictionary, architecture section, threading section.

**Done when:** `build_exe.py --installer` produces a Setup.exe whose self-test
passes and whose window opens at 100%, 125% and 150% scaling.

**Result (2026-10-14):**

* `build_exe.py`: PySide6 added to `_REQUIRED`. CustomTkinter stays (it is
  still the default window until phase 6), so this build ships both.
  `PySide6/translations` is pruned (the app is English). PyInstaller's
  PySide6 hook collected only the Qt modules the app imports: PySide6 and
  shiboken6 take 67 MB of the 6.19 GiB app folder.
* Notices: PySide6 and shiboken6 ship only Qt's commercial license file;
  the generator now skips it, and `licenses/NOTICES_HEADER.txt` lists Qt 6
  and Qt for Python under the LGPL-3.0 with their source locations.
* `core/selftest.py`: new `qt_window` check builds the real Qt window on
  the Windows platform plugin without showing it, then closes it.
* Build: 37 min. Frozen self-test passed every check (engines 108 s,
  kokoro, piper_after_kokoro, pyttsx3, mp3_roundtrip, subtitles, nltk_zips,
  melo_import, qt_window 14 s with platform "windows" and 168 effect
  tiles, user_dirs). `qwindows.dll` is bundled. The frozen EXE with `--qt`
  opened its window and closed with no process left.
* Not done, by choice (2026-10-14): the installer build, the upgrade over
  an installed copy, and the non-ASCII install path test. Only 125%
  scaling was available on this machine.

### Phase 6. Cut-over (rough guess: 1 day, then a soak period)

* Make Qt the default. Keep `--ui ctk` for one release as a fallback.
* After that release, delete `ui/` (the CTk package), `customtkinter` from
  `requirements.txt`, and the fallback flag. Rename `ui_qt/` to `ui/`.

**Done when:** one release has shipped on Qt with no fallback use needed.

**Result, first step (2026-10-14):**

* `tts_app.py`: the Qt window is the default. `--ui ctk`, `--ui=ctk` or
  `ADITS_UI=ctk` start the classic window; `--qt` is still accepted and
  changes nothing.
* Checked from source by launching each and reading the window class:
  default and `--qt` gave a Qt window (`Qt6112QWindowIcon`), `--ui ctk` and
  `ADITS_UI=ctk` a Tk one (`TkTopLevel`), each in about 25 s. The first
  default launch after the 37-minute build showed no window within 80 s and
  was stopped while still running; the retry opened in 25 s. Likely a cold
  start, not confirmed.
* The installed app and its shortcuts start the EXE with no arguments, so a
  build made from this commit opens the Qt window. (The phase 5 build in
  `dist/` was made before this change and still opens the classic window.)
* Second step, as planned then: after one release on Qt, delete `ui/`, the
  `customtkinter` requirement and `_REQUIRED` / `--collect-all` entries in
  `build_exe.py`, the fallback flag, and rename `ui_qt/` to `ui/`. Two files
  in `ui/panels/` hold uncommitted edits (search box, footer buttons) that
  are already ported to `ui_qt/`; commit or drop them first.

**Result, second step (2026-10-14, earlier than planned, on request):**

* Deleted `ui/` (13 files), the `--ui ctk` / `ADITS_UI` switch in
  `tts_app.py`, `customtkinter` in `requirements.txt` and in `build_exe.py`
  (`_REQUIRED`, `--collect-all`). `core/voice_cloning/voicematch.py` imported
  the CTk `EnginePanel` for `engine_kind`; it now uses
  `core.voice_selection.engine_kind`.
* Then `ui_qt/` was renamed `ui/` (`git mv`, every import updated). In
  this document, `ui/` before this section means the CustomTkinter package
  and `ui_qt/` the Qt one.
* `docs/ui-parity-checklist.md` stays as the record of what the CTk window
  did; its line numbers point into the deleted files (see git history).

---

## 4. Rough total

Adding the phase guesses gives about 22 to 31 working days. Treat this as an
order of magnitude, not a schedule. Phase 2 is the best point to re-estimate,
because it covers the hardest widget and the new threading model.

---

## 5. Risks

| Risk | Effect | Response |
| :--- | :--- | :--- |
| Live dictation text and undo (see Phase 2) | Undo history lost or wrong | Settle the design in Phase 2 before anything else depends on it. |
| A thread still touches a widget | Random crashes in Qt | Phase 1 removes all of these. Grep for widget access in thread targets before each merge. |
| Emoji in labels render differently | Odd glyph sizes | Qt uses Segoe UI Emoji on Windows. If still uneven, switch to SVG icons with a permissive license. |
| Two UIs drift apart | Fixes land in one only | Freeze new CTk features after Phase 2. Bug fixes only. |
| Bundle size | Larger installer | Exclude unused Qt modules. Qt is small next to the CUDA torch build already shipped. |
| No UI tests | Regressions slip through | Use the parity checklist each phase. Optionally add `pytest-qt` smoke tests for the playback and text panels. |

---

## 6. Not in scope

* Changes to engines, effects, the voice-cloning runtime or its workers.
* Replacing pygame or PyAudio.
* A visual redesign. The goal is the same app, drawn reliably.
