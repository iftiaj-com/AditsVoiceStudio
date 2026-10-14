# UI Parity Checklist

What the CustomTkinter UI does today, one line per control or behavior. Each
phase of the PySide6 port (see `pyside6-migration-plan.md`) is checked against
the sections it touches. Tick a box only after trying it in the Qt build.

* Built 2026-10-14 by reading every line of `ui/`, plus `effects/registry.py`
  and `effects/cloned_voices.py`. Spot-checked against the code.
* Line numbers are as of commit `aaef920`, with the uncommitted edits to
  `ui/panels/effects_panel.py` and `ui/panels/text_panel.py` on disk at that
  time (search box, footer buttons). They drift as files change; the
  behavior is what counts.
* The app builds every panel with `row=None` (`ui/app.py:189-206`), so the
  `row=None` branches are the live ones.
* Strings marked [dash] contain an em dash in the source, shown here as " - ".
* "Phase" tags say when each section is ported: P2 = Phase 2, P3 = Phase 3,
  P4 = Phase 4.

## Deliberate changes after the port (2026-10-14)

The Qt window no longer matches these CTk items on purpose:

* **Playback & Export** (rows 86 to 105 below): one primary button. Generate
  becomes Cancel with the progress inside it; no progress bar, no separate
  Cancel. Play / Stop is one toggle. Save MP3 and Save SRT are one Save menu
  (MP3, WAV, SRT). Folder is always enabled.
* **Start-up**: the window opens before the engines load; Generate and Preview
  read "Loading engines…" and are disabled until the panels are built.
* **Clone Voice → Clean-up**: no "Skip silences" switch (rows 385 and 458).
  Preview cleanup and ▶ Original / ▶ Cleaned sit in its column.

## Known issues in the current UI

Found while reading. Decide per item whether the port copies the behavior
or fixes it.

- [ ] **Ambiance, Custom after built-in**: pick a custom file, switch to a
  built-in sound, then back to "Custom File...". The label shows the file
  name, but the built-in sound stays loaded, because `_amb_sound` is already
  set. (`ui/panels/customization_panel.py:312-313`, `:385`)
  **Qt port: fixed** (reloads the file; `ui_qt/panels/customization_panel.py`).
- [ ] **↶ / ↷ during a transcription**: the undo and redo buttons are not
  blocked by `_editable()` while Whisper writes into the box.
  (`ui/panels/text_panel.py:213-216`)
- [ ] **"Use current" is never disabled**, even while a Clone Voice task runs.
  (`ui/panels/clone_voice_group.py:523-525`, `:1623`)
- [ ] **Timeline Stop** does not change the Play button text; playback state
  shows only through the playhead. (`ui/panels/timeline_dialog.py:367-376`)
- [ ] **Save MP3 label** stays "Save MP3" when only a WAV exists.
  (`ui/panels/playback_panel.py:150`, `ui/app.py:508-510`)
- [ ] **Speed label format differs**: a slider drag shows 1 decimal ("1.2x"),
  an applied voice selection up to 2 ("1.25x").
  (`ui/panels/engine_panel.py:178-179`, `:351-352`)
- [ ] **Outdated docstring**: `text_panel.py` says dictation is "not built
  yet", but it is. (`ui/panels/text_panel.py:17-18`)
- [ ] **Playback row clips text** at narrow widths ("Cance", "Folde"): six
  buttons share fixed column ratios with no minimum width.
  (`ui/panels/playback_panel.py:130-176`)
  **Qt port: fixed** (two rows of three when one row does not fit).

---

## 1. Main window, Text Input, Playback, TTS Engine (P2, engine P3)

### Main window (ui/app.py)
- [ ] **Window title**: `APP_TITLE` from `core.version`. (`ui/app.py:57`)
- [ ] **Initial size**: 1180x680. (`ui/app.py:58`)
- [ ] **Minimum size**: 1100x600. (`ui/app.py:59`)
- [ ] **Theme**: dark appearance, background `COLORS["bg_dark"]`. (`ui/app.py:60-62`)
- [ ] **Window icon**: `assets/adits_voice_studio.ico` when present. Errors are ignored. (`ui/app.py:65-73`)
- [ ] **No audio device**: `pygame.mixer.init()` fails, the window still opens, and the status shows "No audio output device [dash] playback disabled (<error>)" in the warning color. (`ui/app.py:106-116`)
- [ ] **System voices**: loaded from `Pyttsx3Engine.load_system_voices()` only when pyttsx3 is available. (`ui/app.py:100-102`)
- [ ] **Close box**: if a voice is training, a yes/no box titled "Voice training in progress" (warning icon) asks to close anyway. No keeps the window open. (`ui/app.py:632-639`)
- [ ] **Close sequence**: sets the stop event, quits the pygame mixer, runs `CloneVoiceGroup.cleanup()`, closes the dictation mic and stops transcription (`text_panel.cleanup()`), shuts down the voice-cloning client, deletes temp dirs, then runs customization cleanup. (`ui/app.py:640-662`)
- [ ] **Temp cleanup at exit**: `atexit` deletes every `adits_vs_*` temp dir from this session. (`ui/app.py:92`, `ui/app.py:593-600`)

### Header (ui/app.py)
- [ ] **Logo**: `assets/logo.png` at 48x48. If it is missing or fails to load, a 5x50 accent-color bar is shown instead. (`ui/app.py:132-147`)
- [ ] **Title label**: `APP_TITLE` in the title font. (`ui/app.py:149-150`)
- [ ] **Subtitle label**: "Text-to-Speech, Audio FX & Voice Cloning", muted color. (`ui/app.py:153-156`)

### Layout (ui/app.py, ui/theme.py)
- [ ] **Two columns**: a fixed 50/50 split. Each column has a 500 px minimum. Button text changes must not shift the split. (`ui/app.py:161-165`)
- [ ] **Left column**: Text Input card (fills and expands), then the Playback & Export card. (`ui/app.py:168-201`)
- [ ] **Right column**: scrollable. It holds TTS Engine, then Customization, then Voice Effects. (`ui/app.py:172-209`)
- [ ] **Right column horizontal reset**: the view snaps back to x=0 on focus and on resize. (`ui/app.py:183-186`)
- [ ] **Smooth wheel scroll**: each wheel notch (delta 120) moves the target by 100 px. Each 10 ms frame covers 35% of the remaining distance, with a minimum step of 1 px. (`ui/theme.py:96-98`, `ui/theme.py:112-149`)
- [ ] **Scroll clamps**: stays within 0 and (content minus view). Nothing scrolls when the content fits. (`ui/theme.py:119-123`, `ui/theme.py:134`)
- [ ] **Scrollbar drag cancels the eased scroll**: a view moved by more than 2 px outside the animation stops it. (`ui/theme.py:136-138`)
- [ ] **Shift+wheel or a non-canvas target**: falls back to the default scrolling. (`ui/theme.py:113-115`)
- [ ] **Card style**: background `bg_card`, corner radius 14, 1 px border, title in the subtitle font. (`ui/theme.py:32-47`)
- [ ] **Pop-up icon**: dialogs get the app icon 250 ms after they open. (`ui/theme.py:12-29`)

### Status bar (ui/app.py)
- [ ] **Status dot and label**: the dot color follows the message color, muted by default. (`ui/app.py:219-226`, `ui/app.py:265-267`)
- [ ] **Initial text**: "Ready [dash] Enter text and click Generate". (`ui/app.py:223`)
- [ ] **Site link**: "www.iftiaj.com" on the right with a hand cursor. A left click opens https://www.iftiaj.com in the browser. (`ui/app.py:227-233`)

### Playback & Export panel (ui/panels/playback_panel.py, live grid layout)
- [ ] **Card title**: "🎧  Playback & Export". (`ui/panels/playback_panel.py:27-31`)
- [ ] **Progress bar**: 6 px tall, full width, starts at 0. (`ui/panels/playback_panel.py:41-46`)
- [ ] **Row 1**: "⚡  Generate Speech" stretches. "🎧  Preview" is 110 px wide on its right. Height 38. (`ui/panels/playback_panel.py:110-126`)
- [ ] **Row 2 buttons, in order**: "▶  Play", "⏹  Stop", "💾  Save MP3", "📄  Save SRT", "📂  Folder", "✖  Cancel". Column weights 3:3:4:4:3:3, fixed. (`ui/panels/playback_panel.py:128-176`)
- [ ] **Initial states**: Play, Stop, Save MP3, Save SRT, Folder and Cancel start disabled. Generate and Preview start enabled. (`ui/panels/playback_panel.py:139-175`)

### Generate and Preview (ui/app.py)
- [ ] **Generate click**: starts a full generation. Ignored while one is running. (`ui/app.py:274-275`, `ui/app.py:293-294`)
- [ ] **Preview click**: speaks only the first sentence, cut to 150 characters on a word boundary, and plays it when done. (`ui/app.py:277-290`, `ui/app.py:299-300`, `ui/app.py:431`)
- [ ] **Empty text**: status "⚠  Please enter some text first" (warning). Nothing starts. (`ui/app.py:295-298`)
- [ ] **Settings error**: status "❌  Error: <msg>" plus an error box titled "Generation Error". (`ui/app.py:304-315`)
- [ ] **Settings are read on the UI thread** into a `GenerationRequest` before the worker starts; the worker never reads a widget. (`ui/app.py:304-315`)
- [ ] **While generating**: Generate is disabled and reads "Generating…" ("Previewing…" for Preview). Cancel is enabled. Play, Stop, Save MP3, Save SRT, Folder and Preview are disabled. The progress bar resets to 0. (`ui/app.py:330-340`)
- [ ] **Starting status**: "Generating speech…" or "Generating preview…" (warning color). (`ui/app.py:341-342`)
- [ ] **Worker progress**: pipeline progress moves the bar. Status messages use the accent color, warnings the warning color. (`ui/app.py:395-402`)
- [ ] **Temp dir**: each run makes `adits_vs_*`. On success, older run dirs are deleted. (`ui/app.py:323-324`, `ui/app.py:427-430`)
- [ ] **Cancel click**: sets the stop event. Status "⌛  Cancelling…". Cancel is disabled and reads "Cancelling…". (`ui/app.py:347-351`)
- [ ] **Cancelled result**: status "⏹  Generation cancelled" (warning). The bar resets to 0. Generate and Preview are re-enabled. Play and Save stay disabled. (`ui/app.py:353-363`, `ui/app.py:407-413`)
- [ ] **Success**: Generate reads "⚡  Generate Speech" again. Cancel is disabled. Play, Save MP3 and Preview are enabled. Save SRT only if an SRT exists. Folder only if an MP3 exists. (`ui/app.py:354-373`)
- [ ] **Stop after success**: stays disabled until playback starts. (`ui/app.py:339`, `ui/app.py:452`)
- [ ] **Success status**: "✅  Speech generated successfully!" (success). With a pipeline notice: "⚠  Speech generated. <notice>" (warning). (`ui/app.py:374-378`)
- [ ] **Error result**: buttons reset, the bar goes to 0, status "❌  Error: <msg>", and an error box titled "Generation Error". Play and Save stay disabled. (`ui/app.py:382-391`)

### Play / Stop (ui/app.py)
- [ ] **Play with no file**: status "⚠  No audio to play [dash] generate first". (`ui/app.py:438-441`)
- [ ] **Play with no audio device**: status "⚠  No audio output device [dash] save the file to listen". (`ui/app.py:442-445`)
- [ ] **Play**: pauses ambiance if it is playing. Play reads "▶  Playing…" and is disabled. Stop is enabled. Status "🎵  Playing audio…". (`ui/app.py:446-453`)
- [ ] **Playback output**: `pygame.mixer.music` through `core.playback.Player` (not a numbered channel). (`core/playback.py`)
- [ ] **Playback progress**: a 50 ms timer moves the bar to the playback fraction. (`ui/app.py:454-468`)
- [ ] **Playback ends**: Play reads "▶  Play" and is enabled. Stop is disabled. The bar resets to 0. Status "✅  Playback complete". Ambiance resumes. (`ui/app.py:475-482`)
- [ ] **Stop click**: Play and Stop reset, the bar resets to 0, status "⏹  Playback stopped" (muted) and stays, and ambiance resumes. (`ui/app.py:484-493`)

### Save (ui/app.py)
- [ ] **Save with no file**: status "⚠  No audio to save [dash] generate first". (`ui/app.py:500-503`)
- [ ] **Save dialog**: title "Save Speech Audio". Default `.mp3` with types MP3, WAV, All when an MP3 exists, else `.wav` with types WAV, All. (`ui/app.py:504-526`)
- [ ] **Default folder**: `paths.output_dir()`. (`ui/app.py:519`)
- [ ] **Default file name**: first 3 words of the text joined by `_`, then `_YYYYMMDD_HHMMSS`. "adits_voice_studio" when there are no words. (`ui/app.py:513-517`)
- [ ] **Format chosen**: a `.mp3` path copies the MP3. Any other path copies the save file. (`ui/app.py:531-532`)
- [ ] **SRT alongside**: if an SRT exists, it is copied next to the audio with the same base name. Status "💾  Saved audio & subtitles to <name>", else "💾  Saved to <name>". (`ui/app.py:535-540`)
- [ ] **After a save**: Folder is enabled and the path is remembered. A failure shows "❌  Save error: <e>". (`ui/app.py:542-545`)
- [ ] **Save SRT with none**: status "⚠  No subtitle to save". (`ui/app.py:548-551`)
- [ ] **Save SRT dialog**: title "Save Subtitles (SRT)", `.srt`, default prefix "subtitles" when there are no words. Status "💾  Saved SRT to <name>". Folder is enabled. (`ui/app.py:553-575`)
- [ ] **Folder click**: opens Explorer with the last saved file selected. Before any save, opens `paths.output_dir()`. A failure shows "❌  Could not open folder: <e>". (`ui/app.py:577-587`)

### Text Input panel: title bar (ui/panels/text_panel.py)
- [ ] **Title**: "📝  Text Input". (`ui/panels/text_panel.py:176-180`)
- [ ] **+ button**: 32x28. Tip: "Import captions or a text file, or transcribe an audio or video file." (`ui/panels/text_panel.py:181-183`)
- [ ] **+ menu items**: "Import captions or text…" and "Transcribe audio or video…". Pops up right-aligned under the button. (`ui/panels/text_panel.py:454-471`)
- [ ] **+ menu items disabled**: while a transcription or dictation runs. (`ui/panels/text_panel.py:461-464`)
- [ ] **🎤 button**: toggles dictation. Tip uses `MIC_TIP` ("Dictate: talk and your words are typed at the cursor..."). (`ui/panels/text_panel.py:45-46`, `ui/panels/text_panel.py:184-186`)
- [ ] **Dictation state label**: left of 🎤. "Loading speech model…" (muted), "● Listening" or "● Hearing you" (error color), "✎ Writing…" (secondary) or "Finishing…" (muted). (`ui/panels/text_panel.py:47-53`, `ui/panels/text_panel.py:538-539`)

### Text Input panel: text box
- [ ] **Text box**: monospace font, word wrap, undo on with no limit. Fills the remaining height (base height 140). (`ui/panels/text_panel.py:157`, `ui/panels/text_panel.py:191-199`)
- [ ] **Start text**: `SAMPLE_TEXT` is inserted. The undo history is reset, so Ctrl+Z cannot remove the sample. (`ui/panels/text_panel.py:59-70`, `ui/panels/text_panel.py:200-202`)
- [ ] **Character counter**: right of the footer. "1,234 characters", or "1 character". Updates once per idle on any change. Gray live dictation text is not counted. (`ui/panels/text_panel.py:217-225`, `ui/panels/text_panel.py:291-308`, `ui/panels/text_panel.py:376-382`)
- [ ] **Esc in the box**: stops dictation. (`ui/panels/text_panel.py:227`)
- [ ] **Ctrl+Z / Ctrl+Y**: Tk's default Text bindings. The tooltips name them. Live gray text is removed before any undo. (`ui/panels/text_panel.py:213-216`, `ui/panels/text_panel.py:231`)

### Text Input panel: footer buttons
- [ ] **Copy**: copies all the text, minus live text. The label shows "Copied" for 1.2 s. Does nothing on empty text. Tip: "Copy all the text." (`ui/panels/text_panel.py:208`, `ui/panels/text_panel.py:336-343`)
- [ ] **Paste**: appends the clipboard after the user's text with a blank-line gap. Replaces an empty box or the sample. One undo step. Nothing when the clipboard has no text. (`ui/panels/text_panel.py:209-210`, `ui/panels/text_panel.py:345-351`, `ui/panels/text_panel.py:426-438`)
- [ ] **Paste & Replace**: replaces all text with the clipboard. One undo step. (`ui/panels/text_panel.py:211`, `ui/panels/text_panel.py:353-357`)
- [ ] **Clear**: empties the box as one undo step and focuses it. Nothing if the box is empty. (`ui/panels/text_panel.py:212`, `ui/panels/text_panel.py:359-363`)
- [ ] **Edits during transcription**: Paste, Paste & Replace and Clear are refused. The strip status shows "Wait for the transcription to finish, or cancel it." (`ui/panels/text_panel.py:328-334`)
- [ ] **Edits clear live text**: these footer edits remove gray live dictation text first. (`ui/panels/text_panel.py:333`)
- [ ] **↶ / ↷ buttons**: fire Undo / Redo. Disabled when there is nothing to undo or redo, re-checked on `<<UndoStack>>`. (`ui/panels/text_panel.py:213-216`, `ui/panels/text_panel.py:226`, `ui/panels/text_panel.py:310-317`)
- [ ] **Footer tooltips**: one per button, through `_Tip` (450 ms delay, see Clone Voice "Hover tips"). (`ui/panels/text_panel.py:244`)

### Text Input panel: Replace / Append / Cancel dialog
- [ ] **When shown**: only when the box holds the user's own text (not empty, not the sample). Otherwise "replace" is used without asking. (`ui/panels/text_panel.py:385-388`, `ui/panels/text_panel.py:415-424`)
- [ ] **Dialog**: title "Import text". Message starts "The text box already has your own text." Buttons Replace, Append, Cancel from left to right. Replace is highlighted. (`ui/panels/text_panel.py:420-422`, `ui/panels/text_panel.py:105-112`)
- [ ] **Keys**: Enter means Replace. Esc or the close box means Cancel. Modal after 50 ms. Not resizable. Centered horizontally, one third down the main window. Message wraps at 380 px. (`ui/panels/text_panel.py:97-124`)

### Text Input panel: Import captions or text
- [ ] **File dialog**: title "Import captions or text". Types: Captions and text, Captions, Text (*.txt), All files. Remembers the last folder in memory only. (`ui/panels/text_panel.py:72-77`, `ui/panels/text_panel.py:473-479`)
- [ ] **Parse error**: error box "Import captions or text" with the message. An OS error shows "Could not open the file:\n<e>". (`ui/panels/text_panel.py:480-487`)
- [ ] **Import**: the question reads "“<file>” has N characters. Replace your text with it, add it after your text, or cancel?". One undo step. Replace puts the cursor at the start; append at the end. (`ui/panels/text_panel.py:390-409`, `ui/panels/text_panel.py:488`)

### Text Input panel: Transcribe audio or video
- [ ] **Blocked**: does nothing while a job or dictation runs. (`ui/panels/text_panel.py:598-599`)
- [ ] **File dialog**: title "Transcribe audio or video". Types: "Audio and video" (`AUDIO_EXTENSIONS`) and All files. Remembers the folder in memory only. (`ui/panels/text_panel.py:56`, `ui/panels/text_panel.py:600-605`)
- [ ] **Mode question**: "“<file>” will be transcribed into the box. Replace..., add..., or cancel?". Cancel stops here. (`ui/panels/text_panel.py:606-609`)
- [ ] **During the job**: the media strip opens. The box is read-only. 🎤 is disabled. The job row (bar and Cancel) shows. Status "Starting...". (`ui/panels/text_panel.py:610-620`)
- [ ] **Background job**: a daemon thread "transcribe-file" runs `core.subtitles.transcribe_file`. (`ui/panels/text_panel.py:622-635`)
- [ ] **Segments**: each cleaned segment is appended as it arrives. A space separates segments, or a blank line when a new paragraph starts. The box scrolls to the end. (`ui/panels/text_panel.py:642-661`)
- [ ] **Job progress**: the bar shows end/duration. Status "Transcribing... 42% (m:ss of m:ss)". (`ui/panels/text_panel.py:662-665`)
- [ ] **Cancel (job row)**: status "Stopping...". (`ui/panels/text_panel.py:281-283`, `ui/panels/text_panel.py:692-695`)
- [ ] **End**: the job row hides and 🎤 is re-enabled. If nothing was added, the edit is undone. (`ui/panels/text_panel.py:670-673`)
- [ ] **End, cancelled**: "Stopped. The text so far is kept; Ctrl+Z removes it." or "Stopped. Your text is unchanged." (`ui/panels/text_panel.py:674-676`)
- [ ] **End, error**: "⚠  Could not transcribe this file. <why>" (warning). A ValueError gives "The file may be damaged, or have no sound track." (`ui/panels/text_panel.py:677-681`)
- [ ] **End, no speech**: "No speech was found in this file. Your text is unchanged." (warning). (`ui/panels/text_panel.py:682-684`)
- [ ] **End, success**: "Transcribed m:ss · language: xx. Ctrl+Z puts the old text back." (success). (`ui/panels/text_panel.py:685-690`)
- [ ] **Status wrap**: status lines wrap to the strip width. (`ui/panels/text_panel.py:288-289`)

### Text Input panel: media strip player
- [ ] **Strip placement**: above the footer when a file is opened. Holds ▶, the file name, "0:00 / –", ✖, a seek bar, the job row and a status line. (`ui/panels/text_panel.py:246-289`, `ui/panels/text_panel.py:703-712`)
- [ ] **▶ first click**: decodes the file on a thread "media-player-load". The button shows "…" and is disabled until loaded. Nothing happens if the mixer is not initialized. (`ui/panels/text_panel.py:734-764`)
- [ ] **Load error**: "⚠  Could not play this file: <e>" (warning). (`ui/panels/text_panel.py:771-773`)
- [ ] **Play / pause**: ▶ plays from the bar position, or from the start once at the end. While playing, the button reads "❚❚" (smaller font) and a click pauses. (`ui/panels/text_panel.py:738-744`, `ui/panels/text_panel.py:791`, `ui/panels/text_panel.py:794-797`)
- [ ] **pygame channel**: 7 (`MEDIA_PLAY_CHANNEL`). The channel count is raised if needed. (`ui/panels/text_panel.py:55`, `ui/panels/text_panel.py:781-786`)
- [ ] **Time and bar updates**: every 100 ms. At the end, playback stops and the next ▶ starts over. (`ui/panels/text_panel.py:813-825`)
- [ ] **Seek bar drag**: shows the drag time and pauses bar updates. On release, jumps there and keeps playing if it was playing. (`ui/panels/text_panel.py:269-271`, `ui/panels/text_panel.py:827-840`)
- [ ] **Time format**: m:ss, or h:mm:ss at 1 hour or more. Total shows "–" until known. (`ui/panels/text_panel.py:699-701`, `ui/panels/text_panel.py:730-732`)
- [ ] **✖ (close)**: cancels a running transcription, stops playback and hides the strip. Tip: "Close the player. A running transcription stops and keeps its text." (`ui/panels/text_panel.py:263-267`, `ui/panels/text_panel.py:714-718`)

### Text Input panel: dictation
- [ ] **Start**: refused while a transcription runs. Clears the sample or blanks as one undo step, focuses the box and starts `core.dictation.Dictation`. (`ui/panels/text_panel.py:497-513`)
- [ ] **Recording look**: 🎤 turns red (#CC0000). Tip: "Stop dictation (Esc). Words already heard are still written." (`ui/panels/text_panel.py:511-512`)
- [ ] **Stop**: a click on 🎤 or Esc stops it. Phrases already heard are still written. (`ui/panels/text_panel.py:491-495`, `ui/panels/text_panel.py:515-517`)
- [ ] **Stopped state**: live text is cleared, 🎤 style and tip are restored, the label is emptied. (`ui/panels/text_panel.py:530-537`)
- [ ] **Live text**: the phrase in progress shows in gray, outside the undo history, at the cursor. Typing goes before it. Empty text clears it. (`ui/panels/text_panel.py:228`, `ui/panels/text_panel.py:558-580`) **Port note:** Qt clears the undo stack when undo is switched off; see the Phase 2 design risk in the plan.
- [ ] **Final phrase**: written as its own undo step. Spaces added where needed, none before punctuation. The cursor moves after it. (`ui/panels/text_panel.py:570-587`)
- [ ] **Read-only box**: nothing is written while the box is disabled. (`ui/panels/text_panel.py:565-566`)
- [ ] **Error**: error box titled "Dictation" with the message. (`ui/panels/text_panel.py:541-542`)

### TTS Engine panel (ui/panels/engine_panel.py) (P3)
- [ ] **Card title**: "🔊  TTS Engine". (`ui/panels/engine_panel.py:30-34`)
- [ ] **Engine dropdown**: lists available engine names, or "No TTS engines available". Defaults to the first name containing "Kokoro", else the first. Stretches between the label and the GPU switch. (`ui/panels/engine_panel.py:40-67`)
- [ ] **GPU switch**: "GPU Acceleration (PyTorch)". Defaults to `torch.cuda.is_available()`. (`ui/panels/engine_panel.py:69-78`)
- [ ] **Engine change**: hides all option frames and shows the one whose keyword (gTTS, Edge, Kokoro, Piper, Melo, pyttsx3) is in the name. (`ui/panels/engine_panel.py:294-311`)
- [ ] **First display**: the default engine's frame is shown 50 ms after build. (`ui/panels/engine_panel.py:90`)
- [ ] **gTTS, Language / Accent**: `GTTS_LANGUAGES` keys. Default "English (US)". (`ui/panels/engine_panel.py:104-112`)
- [ ] **Edge, Neural Voice**: `EDGE_VOICES` keys. Default the first. (`ui/panels/engine_panel.py:116-122`)
- [ ] **Edge, Speed (+/- %)**: slider -50 to 50, default 0. Label "+0%" (`{:+d}%`). (`ui/panels/engine_panel.py:124-136`)
- [ ] **Kokoro, Language**: `KOKORO_LANGS` keys. Default "American English". (`ui/panels/engine_panel.py:147-152`)
- [ ] **Kokoro, Voice**: `KOKORO_VOICES[lang]`. A language change refills the list and selects the first voice. (`ui/panels/engine_panel.py:159-164`, `ui/panels/engine_panel.py:197-201`)
- [ ] **Kokoro, Speed (0.5x - 2x)**: slider 0.5 to 2.0, default 1.0. Label like "1.0x". (`ui/panels/engine_panel.py:167-180`)
- [ ] **Kokoro, "↺ 1x" button**: resets speed to 1.0 and the label to "1.0x". (`ui/panels/engine_panel.py:182-193`)
- [ ] **Piper, Voice**: `PIPER_VOICES` keys. Default the first. (`ui/panels/engine_panel.py:203-211`)
- [ ] **Melo, Language / Accent**: `MELO_VOICES` keys. Default the first. (`ui/panels/engine_panel.py:213-221`)
- [ ] **Melo, Speed (0.5x - 2x)**: slider 0.5 to 2.0, default 1.0, with "↺ 1x". (`ui/panels/engine_panel.py:223-249`)
- [ ] **pyttsx3, Voice**: system voice names, or "Default" when there are none. Default the first. (`ui/panels/engine_panel.py:254-262`)
- [ ] **pyttsx3, Speed (WPM)**: slider 50 to 350, default 175. Label "175 WPM". (`ui/panels/engine_panel.py:264-276`)
- [ ] **pyttsx3, Volume**: slider 0.0 to 1.0, default 0.9. Label "90%". (`ui/panels/engine_panel.py:278-290`)
- [ ] **Applying a cloned voice's base**: `apply_voice_selection()` sets engine, Kokoro language, voice and speed, clamped to the engine's range. Speed label via `format_speed` ("1.25x", "+10%", "175 WPM"). Nothing changes if any part cannot be applied; the reason is returned. (`ui/panels/engine_panel.py:318-322`, `ui/panels/engine_panel.py:344-352`, `ui/panels/engine_panel.py:411-421`, `ui/panels/engine_panel.py:437-493`)
- [ ] **Trying a voice leaves the card alone**: `settings_for_selection()` returns settings without changing the dropdowns. (`ui/panels/engine_panel.py:495-528`)
- [ ] **`get_settings()` keys** (used by `core.pipeline` and `core.synth`): `engine_name`, `use_gpu`, `gtts_lang_key`, `edge_voice_key`, `edge_rate`, `kokoro_lang_code`, `kokoro_voice_key`, `kokoro_speed`, `piper_voice_key`, `melo_voice_cfg`, `melo_speed`, `pyttsx3_voice_id`, `pyttsx3_rate`, `pyttsx3_volume`. (`ui/panels/engine_panel.py:532-554`)

### Persistence (main window, text, playback, engine)
- [ ] **No settings file**: engine, GPU, slider and text values are not saved between runs. (`ui/panels/engine_panel.py:532-554`)
- [ ] **Folders remembered in memory only**: the last import and transcribe folders, for the session. (`ui/panels/text_panel.py:159-160`, `ui/panels/text_panel.py:479`, `ui/panels/text_panel.py:605`)
- [ ] **Files written**: only the user's save dialogs (audio, SRT) and `adits_vs_*` temp dirs. (`ui/app.py:323`, `ui/app.py:532-537`, `ui/app.py:570`)

---

## 2. Voice Effects and Customization (P3)

### Effects panel: card and layout
- [ ] **Card title**: "✨  Voice Effects" on the left of the title line, the search box on the right. (`ui/panels/effects_panel.py:188-193`)
- [ ] **Column count**: 4 columns when the panel gets a grid `row`, 2 when packed (the live case). (`ui/panels/effects_panel.py:178-183`)
- [ ] **Section order**: Favourites (no separator), then Cloned Voices, then Effects, each with a 1 px separator above. (`ui/panels/effects_panel.py:199-263`)
- [ ] **Tile build**: every registry entry gets one tile at start, then all tiles are gridded. (`ui/panels/effects_panel.py:277-279`)
- [ ] **Tile placement**: `exclusive_group == "clone"` goes in Cloned Voices, all others in Effects. (`ui/panels/effects_panel.py:492`)
- [ ] **Tile order**: EFFECTS_REGISTRY order, row-major, across `num_cols` columns. (`ui/panels/effects_panel.py:575-583`)

### Effects panel: tile
- [ ] **Switch**: label is the registry `label`, bound to a BooleanVar starting at the entry's `default` (False if missing). (`ui/panels/effects_panel.py:463-471`, `:486`)
- [ ] **Switch command**: runs the group rule only when the entry has an `exclusive_group`. (`ui/panels/effects_panel.py:471`)
- [ ] **Heart button**: right of the switch. ♡ muted when off, ♥ accent when on. A click toggles the favourite. (`ui/panels/effects_panel.py:473-477`, `:520-527`)
- [ ] **Description line**: under the switch, left-aligned, indented 42 px. Clips at the end on narrow windows. (`ui/panels/effects_panel.py:478-481`)

### Effects panel: registry rules
- [ ] **Static effects**: 34 entries. Only `normalize` starts on. (`effects/registry.py:22-232`, `:31`)
- [ ] **No static groups**: only clone entries have an `exclusive_group`. (`effects/registry.py:22-232`, `effects/cloned_voices.py:99`)
- [ ] **Normalize position**: shown first, applied last by the pipeline. (`effects/registry.py:23-25`)
- [ ] **Clone loading**: cloned voices are added at import. A failure prints "Cloned voices not loaded: ..." and the app still starts. (`effects/registry.py:237-241`)

### Effects panel: cloned voice tiles
- [ ] **Which voices**: one tile per ready voice. (`effects/cloned_voices.py:112-114`)
- [ ] **Key / label**: `clone:<slug>`, label `🧬  <name>`. (`effects/cloned_voices.py:94-95`)
- [ ] **Description**: "Cloned voice (RVC)" or "Cloned voice (Quick)". (`effects/cloned_voices.py:92`, `:96`)
- [ ] **Position**: right after `normalize`, or index 0 if normalize is missing. (`effects/cloned_voices.py:104-121`)
- [ ] **Order after refresh**: new voices sorted with existing clones by label, case-insensitive. Removed or no-longer-ready voices dropped. A renamed voice gets its label updated. (`effects/cloned_voices.py:124-152`)
- [ ] **Empty state**: "No cloned voices yet. Create one in Customization → Clone Voice." (`ui/panels/effects_panel.py:252-255`, `:593-597`)
- [ ] **✏️ button**: right of the "🧬  Cloned Voices" header. Opens the Cloned Voices dialog. No tooltip. (`ui/panels/effects_panel.py:242-248`, `:450-452`)

### Effects panel: exclusive group ("clone")
- [ ] **One at a time**: switching a grouped tile on switches the others in that group off. (`ui/panels/effects_panel.py:617-622`)
- [ ] **Subscribers notified**: after any change in a group, including off, subscribers get the active key or None. (`ui/panels/effects_panel.py:623-642`)
- [ ] **Same rule from code and search**: `set_active()` and `toggle_effect()` apply it too. (`ui/panels/effects_panel.py:445-448`, `:644-650`)
- [ ] **Clear group**: `clear_group()` switches every tile in the group off and notifies. (`ui/panels/effects_panel.py:652-656`)
- [ ] **Removing an active voice** notifies the group subscribers. (`ui/panels/effects_panel.py:571-573`)

### Effects panel: Favourites
- [ ] **Header**: "♥  Favourites". Empty state: "Click ♡ on any effect or cloned voice to keep it here." (`ui/panels/effects_panel.py:227-234`, `:588-592`)
- [ ] **Add with heart**: appends the key and adds a mirror tile sharing the original's BooleanVar, so both switches always match. (`ui/panels/effects_panel.py:502-511`, `:537-539`)
- [ ] **Mirror group rule**: a mirror switch applies the same group rule. (`ui/panels/effects_panel.py:507-508`)
- [ ] **Remove with heart**: a filled heart on the original or the mirror removes the mirror. (`ui/panels/effects_panel.py:534-536`, `:513-518`)
- [ ] **Hearts stay in step**: original, mirror and search-row hearts update together. (`ui/panels/effects_panel.py:520-527`)
- [ ] **Order**: the order they were added, not registry order. (`ui/panels/effects_panel.py:584-587`)
- [ ] **Saved at once**, and favourite subscribers are called. (`ui/panels/effects_panel.py:542-556`)
- [ ] **Unknown keys kept**: keys of voices not loaded now stay in the file but are not shown. (`ui/panels/effects_panel.py:271`, `:584`)
- [ ] **Deleted voice dropped** from Favourites, and the file is saved. (`ui/panels/effects_panel.py:558-563`)
- [ ] **Rename sync**: `sync()` updates the label on tile and mirror. (`ui/panels/effects_panel.py:608-611`)
- [ ] **File**: `paths.user_dir("settings") / "favourites.json"`, format `{"keys": [...]}` with indent 2, written to `.tmp` then `os.replace`d. A write error prints "Could not save favourites: ...". A missing or bad file loads as `[]`; non-string keys are dropped. (`ui/panels/effects_panel.py:59-80`)

### Effects panel: search box
- [ ] **Box**: 🔍 icon, entry 190 px wide, placeholder "Search effects and voices". Accent border on focus. (`ui/panels/effects_panel.py:291-299`, `:317-318`)
- [ ] **Entry tooltip**: "Search effects and cloned voices by name or description. Click a result, or press Enter, to switch it on or off. ↑ ↓ move, Esc clears." (`ui/panels/effects_panel.py:50-51`, `:306`)
- [ ] **✖ clear button**: shown only while the box has text. Tip "Clear the search (Esc)." (`ui/panels/effects_panel.py:301-307`, `:326-331`)
- [ ] **Result count**: "N result" / "N results" left of the box. Blank with no matches or no query. (`ui/panels/effects_panel.py:308-310`, `:393-394`)
- [ ] **Delay**: searches 120 ms after typing pauses. Keys that do not change the text do not restart it. (`ui/panels/effects_panel.py:48`, `:320-334`)
- [ ] **Word split**: lower-cased, split into `[0-9a-z]+` words; emoji, punctuation and non-ASCII letters dropped. (`ui/panels/effects_panel.py:54-56`)
- [ ] **Match rule**: every word must be a substring of the name, description, key or section words ("cloned voice clone" or "effect"). (`ui/panels/effects_panel.py:350-357`)
- [ ] **Rank**: 0 when every word starts a word of the name, 1 when all are in the name, 2 otherwise. Ties by registry order. (`ui/panels/effects_panel.py:358-360`, `:373-379`)
- [ ] **Max results**: 8 rows. (`ui/panels/effects_panel.py:49`, `:380`)
- [ ] **Results overlay**: placed over the top of the body, full width, raised above the tiles. The tiles do not move. (`ui/panels/effects_panel.py:209`, `:414-416`)
- [ ] **Results header**: "🔍  Results", with "Click or Enter switches  ·  ↑ ↓ move  ·  Esc closes" on the right. (`ui/panels/effects_panel.py:213-216`)
- [ ] **No-match note**: "No effects or voices match “<typed>”." Overflow note: "Showing 8 of N. Type more to narrow it down." (`ui/panels/effects_panel.py:402-405`)
- [ ] **Empty query**: hides the overlay, resets the highlight, blanks the count. (`ui/panels/effects_panel.py:366-372`)
- [ ] **Result row**: drawn switch pill, name, description, heart. Shares the effect's BooleanVar. (`ui/panels/effects_panel.py:83-131`, `:154-167`)
- [ ] **Row click** (except the heart) toggles the effect with the group rule. **Heart click** toggles the favourite. Hover adds an accent border. (`ui/panels/effects_panel.py:113-117`, `:146-148`)
- [ ] **First row highlighted** after each search. (`ui/panels/effects_panel.py:391`)
- [ ] **↓ / ↑**: move the highlight, clamped, only while a query is active. The key is consumed. (`ui/panels/effects_panel.py:314-315`, `:431-435`)
- [ ] **Enter**: runs any pending search, then toggles the highlighted row. (`ui/panels/effects_panel.py:313`, `:437-443`)
- [ ] **Esc**: clears the text, hides ✖, closes the results. (`ui/panels/effects_panel.py:316`, `:345-348`)
- [ ] **Search after sync**: an active query refreshes after `sync()`. (`ui/panels/effects_panel.py:613-614`)

### Effects panel: public API used by the app
- [ ] **`get_active_effects()`**: `{key: bool}` for every tile. (`ui/panels/effects_panel.py:658-659`)
- [ ] **`sync()`**: adds new registry entries, removes missing ones, renames labels, re-grids. (`ui/panels/effects_panel.py:599-612`)
- [ ] **Others**: `subscribe(group, cb)`, `subscribe_favourites(cb)`, `is_favourite(key)`, `toggle_favourite(key)`, `set_active(key, value)`, `clear_group(group)`, `active_in_group(group)`. (`ui/panels/effects_panel.py:529-549`, `:634-656`)

### Customization panel: card
- [ ] **Card title**: "⚙️  Customization Options". (`ui/panels/customization_panel.py:16-34`)
- [ ] **Box style**: when packed (the live case), each group sits in a bordered `bg_input` box. (`ui/panels/customization_panel.py:42-44`)

### Customization panel: speech
- [ ] **Skip Silences switch**: default off. (`ui/panels/customization_panel.py:49-57`)
- [ ] **No speed control here**: `get_settings()` always returns `speed` 1.0; speed is set per engine. (`ui/panels/customization_panel.py:59`, `:465`)

### Customization panel: subtitles
- [ ] **Auto-Subtitles switch**: default off. (`ui/panels/customization_panel.py:69-77`)
- [ ] **Mode dropdown**: "Word-Level" / "Sentence-Level", default "Word-Level". Stays enabled whether or not the switch is on. "Sentence-Level" gives `"sentence"`, otherwise `"word"`. (`ui/panels/customization_panel.py:79-91`, `:475`)

### Customization panel: 3-band EQ
- [ ] **3-Band EQ switch**: default off. Sliders hidden while off. (`ui/panels/customization_panel.py:101-115`, `:260-267`)
- [ ] **Bass, Mids, Treble sliders**: -12 to 12, default 0.0, continuous. (`ui/panels/customization_panel.py:126-185`, `ui/theme.py:68-81`)
- [ ] **Slider labels**: start "0dB"; after a move `int(v)` with sign and "dB" ("+3dB", "-5dB", "+0dB"). (`ui/panels/customization_panel.py:127-130`)
- [ ] **EQ layout (packed)**: one stacked row per band, sliders 150 px wide. (`ui/panels/customization_panel.py:119-186`)

### Customization panel: ambiance
- [ ] **Ambiance dropdown**: "None", "Airplane Cabin", "Nature/Forest", "Custom File...", default "None". (`ui/panels/customization_panel.py:196-208`)
- [ ] **Vol slider**: 0 to 100, default 50. Label "50%", `int(v)%` after a move. Moving it sets the playing channel's volume at once. (`ui/panels/customization_panel.py:210-217`, `:271-276`)
- [ ] **"None"**: hides the player row, stops the channel, clears the custom path, shows "No file selected", disables Play, Pause, Restart and Remove, deletes preview temp files. (`ui/panels/customization_panel.py:287-297`)
- [ ] **"Custom File..."**: shows Import, Play, Pause, Restart, Remove and the file label. With no file: "No file selected", the four buttons disabled. With a file: shows its name, enables the buttons. (`ui/panels/customization_panel.py:298-322`)
- [ ] **Built-in choice**: no Import. Label "Generating..." while a 30 s, 24 kHz preview WAV is written to the temp folder, then the ambiance name, buttons enabled. On error "Generation failed". (`ui/panels/customization_panel.py:323-393`)
- [ ] **No audio device**: label "No audio device" (warning). (`ui/panels/customization_panel.py:334-336`, `:366-368`)
- [ ] **📥 Import**: dialog "Select Ambiance Music", types "Audio Files" (`*.mp3 *.wav *.ogg *.flac`) and "All Files". Cancel changes nothing. (`ui/panels/customization_panel.py:396-400`)
- [ ] **After import**: label shows the file name, cut to 17 characters plus "..." when over 20. Non-WAV files are converted to a temp WAV. A load error shows "Load failed", buttons stay enabled. (`ui/panels/customization_panel.py:401-409`, `:342-353`)
- [ ] **pygame channel 1**; the mixer is set to 8 channels. (`ui/panels/customization_panel.py:337-339`, `:369-371`)
- [ ] **▶ Play**: sets the volume; plays looped when idle, else unpauses. **⏸ Pause**: pauses. **⏮ Restart**: stops and plays looped from the start. **✖ Remove**: sets "None". (`ui/panels/customization_panel.py:411-431`)
- [ ] **Ambiance pauses** while generated speech plays and resumes after. (`ui/app.py:446-453`, `:475-493`)
- [ ] **Temp cleanup**: `cleanup()` stops the channel and deletes preview temp files; also runs on each new load and on "None". (`ui/panels/customization_panel.py:434-460`)
- [ ] **Start state**: as if "None" was picked, EQ off. (`ui/panels/customization_panel.py:256-257`)

### Customization panel: messages and settings
- [ ] **No message boxes or tooltips** in this panel. Errors show in the ambiance label or the console. (`ui/panels/customization_panel.py:276`, `:352-353`, `:392-393`)
- [ ] **Clone Voice group** is built after the ambiance box, only when `app` is passed (see section 3). (`ui/panels/customization_panel.py:248-253`)
- [ ] **`get_settings()` keys**: `skip_silences`, `speed` (1.0), `env_choice`, `env_volume` (0 to 1), `custom_amb_path`, `ambiance_channel`, `auto_subtitle`, `eq_enabled`, `eq_bass`, `eq_mids`, `eq_treble`, `sub_segmentation`, `clone_voice`. (`ui/panels/customization_panel.py:462-477`)

---

## 3. Clone Voice and its dialogs (P4)

### Clone Voice box: header and install (clone_voice_group.py)
- [ ] **Header**: "🧬  Clone Voice" on the left, status text on the right. (`ui/panels/clone_voice_group.py:365-368`)
- [ ] **Frozen build**: status "Not available in this build"; setup text names Python 3.11 and 3.12 with the `py` launcher. Install hidden. (`ui/panels/clone_voice_group.py:981-985`)
- [ ] **Not installed**: status "Not installed"; setup text names the missing engines, "about 9 GB" download, "about 21 GB" on the runtime drive. Setup panel shown, tabs hidden. (`ui/panels/clone_voice_group.py:986-992`, `:1006-1010`)
- [ ] **Update available**: status in warning color, "⬆  Update" in the header. (`ui/panels/clone_voice_group.py:994-997`)
- [ ] **Ready**: "Ready · N voice(s)" in success color, plural handled. Setup hidden, tabs shown. (`ui/panels/clone_voice_group.py:998-1005`)
- [ ] **"⬇  Install voice cloning"**: yes/no "Install voice cloning" (about 9 GB, Python 3.11, 3.12 and Git). Yes runs "Install" ("Starting install…"). Success "✅  Voice cloning is installed". (`ui/panels/clone_voice_group.py:383-385`, `:1702-1715`)
- [ ] **"⬆  Update"**: yes/no "Update voice cloning". Reinstalls only outdated engines ("Updating…"). No dialog when nothing is outdated. Success "✅  Voice cloning is up to date". (`ui/panels/clone_voice_group.py:1717-1732`)
- [ ] **Setup text wrap**: box width minus 240. (`ui/panels/clone_voice_group.py:382`)

### Tabs and layout
- [ ] **Tabs**: "Active voice" / "Create voice", default Active voice. (`ui/panels/clone_voice_group.py:388-396`)
- [ ] **Switching tabs** pauses the clip player. Create refreshes the create state; Active refreshes the estimate. (`ui/panels/clone_voice_group.py:1026-1035`)
- [ ] **WIDE_PX = 600**: two tile columns at 600 unscaled px or more, else one. Updates only when the width changes by 8 px or more; width is divided by the widget scaling. (`ui/panels/clone_voice_group.py:63`, `:834-849`)
- [ ] **A tile alone in its row spans both columns**; a pair sits side by side with a 4 px gap. (`ui/panels/clone_voice_group.py:785-799`)
- [ ] **Active page rows**: [Voice, Sound]; Sound only with an active voice. (`ui/panels/clone_voice_group.py:812`)
- [ ] **Create page rows**: Quick [New voice, Recordings], [Clean-up], [footer]. Train adds [Training] before the footer. Import [New voice, Model files], [footer]. (`ui/panels/clone_voice_group.py:813-818`)
- [ ] **Recordings buttons**: one row of 4 when narrow but at least 470 px, otherwise 2x2 (wide mode too). (`ui/panels/clone_voice_group.py:775-782`, `:806`)
- [ ] **Clean-up layout**: wide puts 3 columns side by side with no indent; narrow stacks them with a 40 px indent for sub-options. (`ui/panels/clone_voice_group.py:745-773`)
- [ ] **"Match pitch to base voice"** sits beside Batch / Rate when wide or one-row, else below. (`ui/panels/clone_voice_group.py:823-826`)
- [ ] **▶ Preview row**: in the Voice tile (under Base) for RVC, in the Sound tile (under Quality) for Quick. (`ui/panels/clone_voice_group.py:827-832`)
- [ ] **Label wrap widths follow the box width**: stage text, estimates, clips note. (`ui/panels/clone_voice_group.py:417`, `:562`, `:640`, `:686`, `:844-848`)
- [ ] **No empty frame gridded**: an empty frame keeps a 200 px default height (AGENTS.md lesson).

### Hover tips (_Tip, also used by the text panel)
- [ ] **Behavior**: shows 450 ms after mouse enter, below the widget, wrap 320. Hides on leave or any button press. (`ui/panels/clone_voice_group.py:100-151`)
- [ ] **Tip texts, short form**: ♡ "Add this voice to Favourites…" (`:507`). Base "TTS engine voice this clone was made from…" (`:521`). Use current "Save the voice now selected in TTS Engine…" (`:526`). Pitch "semitones" (`:537`). 🎯 "Match pitch: measure…" (`:541`). Index "accent closer to target" (`:545`). Protect "0.33 suits most voices" (`:548`). Quality "10 is a good balance" (`:551`).
- [ ] **Mode tips (MODE_HINTS)**: Quick "up to 25 s… ready in seconds". Train "3+ minutes… cloned voices can't be used while training". Import ".pth with its .index". (`ui/panels/clone_voice_group.py:49-54`, `:582`)
- [ ] **Create page tips**: Base "voice selected in TTS Engine…" (`:586`). Add clips "WAV, MP3, M4A…" (`:597`). Record "Record with your microphone…" (`:599`). Analyze "Find the English engine voice…" (`:602`).
- [ ] **Auto / Male / Female tips**: Auto decides from the clip; Male and Female compare only that gender. (`ui/panels/clone_voice_group.py:611-614`)
- [ ] **Clip player ▶ tip**: "Play or pause the clip. Drag the bar below to jump." (`ui/panels/clone_voice_group.py:623`)
- [ ] **Training tips**: Epochs "150 is typical" (`:661`). Batch "2 fits a 4 GB GPU…" (`:667`). Rate "40 kHz suits most voices" (`:672`). Match switch (`:678`).
- [ ] **Clean-up tips**: Remove music "Demucs… about 80 MB" (`:715`). Preview cleanup "up to 30 s of the first clip" (`:728`). Skip silences "pauses longer than 0.4 s" (`:743`).
- [ ] **Trim switch tip is dynamic**: "Use the first N seconds… or Custom ranges…"; Quick adds " Quick mode reads at most 25 s." (`ui/panels/clone_voice_group.py:903-905`)

### Active voice tab: Voice tile
- [ ] **Mode tag**: "RVC model", "RVC model · imported", "Quick · zero-shot", or empty. (`ui/panels/clone_voice_group.py:1089`, `:1096`, `:1106`)
- [ ] **Voice picker**: "None" plus ready voice names; a duplicate name shows as "Name (slug)". (`ui/panels/clone_voice_group.py:1013-1019`)
- [ ] **Picking a voice** switches its effect tile on. "None" clears the clone group. The picker follows the active effect. (`ui/panels/clone_voice_group.py:1038-1050`)
- [ ] **Empty hint**: "Switch on a cloned voice here or in Voice Effects." only with no active voice. (`ui/panels/clone_voice_group.py:508-510`, `:1090`, `:1093`)
- [ ] **♡ favourite button**: ♥ accent when on, ♡ muted when off, disabled with no voice, repainted by the favourites subscription. (`ui/panels/clone_voice_group.py:501-505`, `:1056-1066`, `:966`)
- [ ] **Base row**: "Base" plus the description, or "not set" (muted). Hidden with no voice. Old voices fall back to `base_engine` / `base_voice`. (`ui/panels/clone_voice_group.py:515-520`, `:1215-1239`)
- [ ] **"Use current"**: saves the TTS Engine selection as the base (`base_selection`, `base_engine`, `base_voice`). Status "✅  Base voice of 'X' set to …"; RVC voices with clips add " Press 🎯 Match to re-measure the pitch." (`ui/panels/clone_voice_group.py:523-525`, `:1257-1269`)
- [ ] **Switching a voice on applies its base** to the TTS Engine card. Status "ℹ  'X' has no base voice yet…", "⚠  Base voice for 'X' not selected: err", or "🔁  TTS Engine set to …, the base voice of 'X'". (`ui/panels/clone_voice_group.py:1070`, `:1085-1086`, `:1241-1255`)

### Active voice tab: voice list (_VoiceList)
- [ ] **Opens a custom list**, not a menu. Clicking the picker again closes it. (`ui/panels/clone_voice_group.py:302-307`)
- [ ] **Rows**: current value on `bg_input`; ♥ on the right of favourites; hover highlights in accent. Release on a row picks it and closes. (`ui/panels/clone_voice_group.py:191-206`, `:274-278`)
- [ ] **Size**: picker width (at least 160 px), up to 12 rows tall; scrollbar only on overflow. (`ui/panels/clone_voice_group.py:162`, `:214-232`)
- [ ] **Placement**: below the picker, or above when there is more room. The current row is centered when the list scrolls. (`ui/panels/clone_voice_group.py:219-235`)
- [ ] **Mouse wheel**: 3 rows per notch, does not scroll the right column. (`ui/panels/clone_voice_group.py:248-250`)
- [ ] **Closes on** a click outside, Escape, or lost focus. Grabs input while open. (`ui/panels/clone_voice_group.py:237-272`)

### Active voice tab: Sound tile
- [ ] **Pitch**: -24 to +24, 48 steps, default 0, "+N st". RVC only. (`ui/panels/clone_voice_group.py:531`, `:535-537`)
- [ ] **Index**: 0 to 1, 100 steps, default 0.3, "0.00". RVC only. (`ui/panels/clone_voice_group.py:532`, `:543-545`)
- [ ] **Protect**: 0 to 0.5, 50 steps, default 0.33, "0.00". RVC only. (`ui/panels/clone_voice_group.py:533`, `:546-548`)
- [ ] **Quality**: 4 to 50, 46 steps, default 10, "N steps". Quick only. (`ui/panels/clone_voice_group.py:534`, `:549-552`)
- [ ] **Sliders load the profile values** when a voice is switched on. (`ui/panels/clone_voice_group.py:1097-1111`)
- [ ] **Slider moves save to profile.json** after 600 ms (debounced): RVC `pitch`, `index_rate`, `protect`; Quick `diffusion_steps`. Pending saves are flushed before switching voices and before Preview. (`ui/panels/clone_voice_group.py:1115-1141`, `:1073`, `:1978`)
- [ ] **🎯 Match pitch**: enabled only for RVC voices with clips and no task running. "Match pitch" task ("Synthesizing a sample with the current engine voice…", "Measuring pitch…"). Writes `pitch`, `base_f0_hz`, `target_f0_hz` and moves the slider. Message "✅  Pitch set to +N st (base X Hz → target Y Hz)". (`ui/panels/clone_voice_group.py:538-540`, `:1104`, `:1918-1938`)
- [ ] **Time estimate**: "⏱  Adds X to this text", or "Adds X per 10 s of speech" under 0.5 s of text; ", +Y the first time" when the worker is cold. Refreshes every 2 s while the tab shows a voice. (`ui/panels/clone_voice_group.py:1171-1206`)

### Active voice tab: ▶ Preview / ■ Stop
- [ ] **Disabled** with no voice or while a task runs. (`ui/panels/clone_voice_group.py:1091`, `:1094`, `:1623`)
- [ ] **Saved preview with a matching fingerprint** plays at once: "▶  Playing the saved preview of 'X'". (`ui/panels/clone_voice_group.py:1982-1987`)
- [ ] **Otherwise** runs "Preview" ("Starting…", "Synthesizing the preview sentence…", "Converting to the cloned voice…"). Saved only when the settings equal the voice's saved base and sound. Message "▶  Playing preview of 'X'" plus " (saved for next time)". (`ui/panels/clone_voice_group.py:1988-1997`, `:1950-1952`)
- [ ] **While playing** the button reads "■  Stop"; a click stops. Resets to "▶  Preview" when the audio ends, another sound takes channel 2, or another voice is switched on. (`ui/panels/clone_voice_group.py:1972-1974`, `:2116-2134`, `:1071-1072`)
- [ ] **pygame channel 2**. (`ui/panels/clone_voice_group.py:2158`)

### Create voice tab: New voice tile
- [ ] **Name entry**: placeholder "e.g. My narrator"; every keystroke refreshes the Create button. (`ui/panels/clone_voice_group.py:573-577`)
- [ ] **Mode**: Quick / Train (RVC) / Import model, default Quick. (`ui/panels/clone_voice_group.py:48`, `:579-581`)
- [ ] **Base label**: the current TTS Engine selection, or "not set". (`ui/panels/clone_voice_group.py:584`, `:1291`)
- [ ] **Name auto-fill** only when empty: first clip's file name (40 chars) after Add clips, "My voice" after a recording, or the .pth file name. (`ui/panels/clone_voice_group.py:1378-1379`, `:1544-1545`, `:1600-1601`)

### Create voice tab: Recordings tile
- [ ] **Clips label**: "N clip(s) · length" or "No clips", colored by the length rule. (`ui/panels/clone_voice_group.py:1311-1314`)
- [ ] **Length rule, Quick**: under 3 s error ("need at least 3 s", Create disabled); under 8 s warning ("10–25 s works best"). (`ui/panels/clone_voice_group.py:1298-1302`)
- [ ] **Length rule, Train**: under 30 s error; under 60 s "under 1 min: Quick mode is recommended"; under 180 s "under 3 min: expect rough results". (`ui/panels/clone_voice_group.py:1303-1309`)
- [ ] **Clips note**: "Using X" when trimming changes the length, plus the rule note. Hidden when empty. (`ui/panels/clone_voice_group.py:1315-1321`)
- [ ] **"📥  Add clips"**: multi-file dialog "Choose recordings of the target voice". Duplicates skipped. Unreadable file: warning "Clip skipped". (`ui/panels/clone_voice_group.py:596`, `:1363-1380`)
- [ ] **"🎙  Record"**: opens the recorder saving to `<runtime root>/recordings`, or raises it. A finished take becomes a clip. (`ui/panels/clone_voice_group.py:598`, `:1534-1548`)
- [ ] **"🔍  Analyze"**: disabled with no clips or while busy. Opens Voice match with the selections, clean / denoise switches and gender, or raises it. (`ui/panels/clone_voice_group.py:600-601`, `:939`, `:1550-1562`)
- [ ] **"✖  Clear"**: removes all clips and the cleanup preview. No confirmation. (`ui/panels/clone_voice_group.py:604`, `:1589-1592`)
- [ ] **Auto / Male / Female**: default Auto, sent lowercased to the analyzer. (`ui/panels/clone_voice_group.py:607-610`, `:1562`)
- [ ] **Applied voice match Result**: copied to a temp folder and kept for the next Create. (`ui/panels/clone_voice_group.py:1564-1575`)

### Create voice tab: clip player
- [ ] **Shown only when clips exist**. Clip menu entries "N. filename", cut to 34 chars with "…". (`ui/panels/clone_voice_group.py:1383-1400`)
- [ ] **▶ first click** decodes on a thread (button "…", disabled), then plays. While playing "❚❚" pauses. ▶ resumes from the bar, or from 0 at the end. (`ui/panels/clone_voice_group.py:1428-1470`, `:1483`)
- [ ] **Time label** "m:ss / m:ss", every 100 ms. **Seek bar** drag shows the time; release jumps there and keeps playing. (`ui/panels/clone_voice_group.py:632`, `:1425-1426`, `:1505-1532`, `:635-637`)
- [ ] **Picking another clip** stops playback and resets to 0:00. Decode failure: "⚠  Could not play name: err". (`ui/panels/clone_voice_group.py:1402-1413`, `:1465-1467`)
- [ ] **pygame channel 6**; plays the whole file, not only the trimmed part. Stops on a tab switch (pause), Clear and cleanup. (`ui/panels/clone_voice_group.py:64`, `:1475-1478`, `:1027`, `:1390-1391`, `:2166`)

### Create voice tab: Model files tile (Import model)
- [ ] **"📂  Model .pth"**: dialog for `*.pth`, shows the name, required for Create. (`ui/panels/clone_voice_group.py:645-648`, `:1594-1602`, `:1323`)
- [ ] **"📂  Index"**: optional `*.index`; label "Optional" until picked. (`ui/panels/clone_voice_group.py:649-652`, `:1604-1609`)

### Create voice tab: Clean-up tile
- [ ] **"Use part of each recording"**: off by default. Presets Quick 10 s / 20 s / 25 s / Custom, Train 1 min / 3 min / 5 min / Custom, default "20 s". An invalid value resets to the 2nd preset. "First N" counts across clips in order. (`ui/panels/clone_voice_group.py:59-62`, `:700-707`, `:852-873`, `:898-902`)
- [ ] **Custom**: each clip with name (32 chars), "all · length" or "m:ss–m:ss · N.N s" (success color), and "✂  Edit" opening the timeline. Quick lengths (10, 20, 25) or Train (30, 60, 120). Done stores `start`, `end`, `seconds` and discards the cleanup preview. (`ui/panels/clone_voice_group.py:916-957`)
- [ ] **"Remove music & noise"**: off by default; when on shows "Also reduce hiss & hum" and "🎧  Preview cleanup". (`ui/panels/clone_voice_group.py:711-727`, `:907-909`)
- [ ] **Preview cleanup**: disabled with no clips or while busy. "Cleanup preview" task on up to 30 s of the first selection, then shows ▶ Original / ▶ Cleaned and plays Cleaned. Message "▶  Playing the cleaned audio (N s, music removed in N s). Compare with ▶ Original." (`ui/panels/clone_voice_group.py:938`, `:2033-2062`)
- [ ] **▶ Original / ▶ Cleaned**: play/stop toggles on channel 2; the playing one reads "■ Stop" and resets at the end. Hidden until a preview exists. (`ui/panels/clone_voice_group.py:729-736`, `:2064-2108`)
- [ ] **"Skip silences"**: off by default. (`ui/panels/clone_voice_group.py:739-743`)
- [ ] **Prep options sent to create**: trim ("firstN", "custom" or None), separate, denoise, skip_silences. (`ui/panels/clone_voice_group.py:885-892`)

### Create voice tab: Training tile (Train only)
- [ ] **Epochs**: 10 to 500, 49 steps, default 150. (`ui/panels/clone_voice_group.py:659-662`)
- [ ] **Batch**: 1 / 2 / 4, default 2; 4 adds " · batch 4 may not fit a 4 GB GPU" to the estimate. (`ui/panels/clone_voice_group.py:668-669`, `:1335-1336`)
- [ ] **Rate**: 32 / 40 / 48 kHz, default 40 kHz. (`ui/panels/clone_voice_group.py:55`, `:673-674`)
- [ ] **"Match pitch to base voice"**: on by default. (`ui/panels/clone_voice_group.py:676-677`)

### Create voice tab: footer and Create
- [ ] **Estimate**: Quick "⏱  Ready in X"; Train "⏱  X: prepare Y, then Z per epoch"; Import "⏱  Ready in about 15 s"; " · music removal +X" with cleanup. Empty: "Add or record clips of the voice to clone." / "Pick a model file to import." (`ui/panels/clone_voice_group.py:1325-1352`)
- [ ] **"🧬  Create Voice" enabled** only when the length rule passes (or a .pth is set), the name is not blank, and nothing is busy. (`ui/panels/clone_voice_group.py:1355-1356`)
- [ ] **Duplicate name**: warning "Name in use". Name cut to 40 chars. (`ui/panels/clone_voice_group.py:1735-1740`)
- [ ] **Train while generating**: info "Busy", "Wait for the current generation to finish…". (`ui/panels/clone_voice_group.py:1754-1757`)
- [ ] **Quick create**: "Create voice" ("Preparing the reference voice…", "Trimming silence and building the reference…"). "Voice 'X' is ready. It is switched on in Voice Effects." (`ui/panels/clone_voice_group.py:1750-1752`, `:1839-1851`)
- [ ] **Import**: "Import" ("Checking the model…"). "Voice 'X' imported (N kHz model)." (`ui/panels/clone_voice_group.py:1741-1744`, `:1853-1858`)
- [ ] **Train**: "Training"; stages include "Measuring pitch of the base and target voices…", "Converting clips for training…", "Training · epoch e/E · m:ss left". "Voice 'X' trained in T and switched on." (`ui/panels/clone_voice_group.py:1860-1916`)
- [ ] **Train profile fields**: status training → ready, `batch_size`, `pth`, `index`, `sample_rate`, `epochs`, `pitch`, `base_f0_hz`, `target_f0_hz`. Dataset folder deleted. OOM with batch over 1 adds "Try again with batch size N". (`ui/panels/clone_voice_group.py:1877-1910`)
- [ ] **Failed or cancelled create deletes the new voice**. (`ui/panels/clone_voice_group.py:1848-1850`, `:1907-1914`)
- [ ] **First preview saved after create**: "Taking the preview from the voice match result…" or "Saving a preview of the voice…"; writes `preview_text`. (`ui/panels/clone_voice_group.py:1801-1837`, `:1577-1587`)
- [ ] **After success**: clips, .pth and index cleared, name emptied, labels reset, view switches to Active voice, status set, list refreshed with the new voice on. (`ui/panels/clone_voice_group.py:1764-1776`)

### Progress row and Cancel (shared)
- [ ] **One task at a time**: a new task is ignored while busy. (`ui/panels/clone_voice_group.py:1648-1651`)
- [ ] **On start**: row shown, progress 0, Cancel "✖  Cancel"; Create, Install, Preview and 🎯 disabled. (`ui/panels/clone_voice_group.py:1614-1624`)
- [ ] **Cancel**: disabled "Cancelling…", stage "Cancelling…" (warning). (`ui/panels/clone_voice_group.py:1642-1646`)
- [ ] **On end**: bar 1.0 on success else 0; Cancel disabled, Install re-enabled, all refreshed. Row hides after 8 s (30 s on failure). (`ui/panels/clone_voice_group.py:1626-1634`)
- [ ] **Cancelled**: "Name cancelled" (warning). **Failure**: stage and app status "❌  Name failed: msg", error box "Name failed" with message and hint. (`ui/panels/clone_voice_group.py:1659-1670`)
- [ ] **"Preview update"**: background remake of a stale preview, skipped while busy. "✅  Saved a new preview of 'X'" or "⚠  Preview of 'X' not updated: …". (`ui/panels/clone_voice_group.py:1999-2031`)
- [ ] **cleanup()** (called from `AditsVoiceStudioApp.destroy()`): cancels the task, stops the clip player and channel 2, deletes temp folders. (`ui/panels/clone_voice_group.py:2164-2176`, `ui/app.py`)

### Recorder dialog (record_dialog.py)
- [ ] **Window**: "Record a clip", 720x430, minimum 600x400, transient, modal after 150 ms. (`ui/panels/record_dialog.py:44-48`, `:142`, `:150-156`)
- [ ] **Microphone picker**: inputs of the default host API, no "Mapper" devices, default device first. (`ui/panels/record_dialog.py:71-79`, `:159-187`)
- [ ] **No PyAudio**: red tip with the install command, Record disabled. **No microphone**: "No microphone was found…", Record disabled. (`ui/panels/record_dialog.py:164-174`, `:189-191`)
- [ ] **"●  Record"**: tries mono then stereo. Button becomes "■  Stop", device menu disabled, preview rows hidden, tip "Recording… press Stop when you are done." Open failure: error "Recording failed". (`ui/panels/record_dialog.py:194-240`)
- [ ] **Level meter**: -60 to 0 dB; green below -6, warning above -6, red above -1. Every 50 ms. Timer "m:ss". (`ui/panels/record_dialog.py:413-421`, `:251-265`, `:94`)
- [ ] **Live waveform**: last 12 s in red; "Listening…" with no data, "Press ● Record to start" when idle. (`ui/panels/record_dialog.py:32`, `:423-450`)
- [ ] **Auto-stop at 600 s**. Take under 0.5 s: "That take was shorter than half a second. Try again." (`ui/panels/record_dialog.py:30`, `:262-264`, `:283-287`)
- [ ] **Take saved** as 16-bit mono WAV "Recording YYYY-MM-DD HHMMSS.wav" in the recordings folder; an earlier unused take is deleted. (`ui/panels/record_dialog.py:290-301`)
- [ ] **Take tips**: peak above -0.5 dBFS "Some parts clipped…"; below -30 dBFS "very quiet…"; else "Listen back…". Info "N.N s · peak N dBFS · N kHz". (`ui/panels/record_dialog.py:305-313`)
- [ ] **Preview**: ▶ / ⏸, seek slider, "m:ss / m:ss"; pygame channel 4, every 50 ms. Waveform shows played part in accent, rest muted, green playhead. Click or drag on the waveform seeks (after a take only). (`ui/panels/record_dialog.py:114-128`, `:332-410`, `:446-452`)
- [ ] **"⟲  Record again"**, **"🗑  Discard"** ("Take discarded. Press ● Record to try again."), **"✓  Add to clips"** (calls back and closes). (`ui/panels/record_dialog.py:131-137`, `:455-480`)
- [ ] **Window close**: stops recording; an unadded take asks "Discard recording?" (No keeps the window, Yes deletes). (`ui/panels/record_dialog.py:139`, `:482-491`)

### Timeline dialog (timeline_dialog.py)
- [ ] **Window**: "Select audio · <file>", 860x420, minimum 640x380, modal. Header "✂  <file>" with "Loading…", then "Length m:ss.ss"; load error "Could not read the file: …". (`ui/panels/timeline_dialog.py:41-45`, `:61-65`, `:144-152`)
- [ ] **Help line**: "Drag the highlighted part to move it…". (`ui/panels/timeline_dialog.py:66-68`)
- [ ] **Drag inside** the selection moves it (fleur cursor). **Drag an edge** (within 7 px) resizes it (double arrow), minimum 0.5 s. **Drag on empty waveform** starts a new selection; with the whole clip selected, a drag anywhere except near the ends starts a new one. (`ui/panels/timeline_dialog.py:27-28`, `:231-271`)
- [ ] **Look**: ruler with auto step, handles in accent_secondary, selected waveform in accent, selection background #24224A. (`ui/panels/timeline_dialog.py:29`, `:186-220`)
- [ ] **Zoom**: "−" out 2x, "+" in 2x (span at least 1 s), centered on the selection; "Fit" shows all. Wheel pans 10%; Ctrl+wheel zooms (0.8x / 1.25x). Horizontal scrollbar pans. (`ui/panels/timeline_dialog.py:100-102`, `:121`, `:276-308`, `:75-78`)
- [ ] **Start (s) / End (s)**: applied on Enter or focus loss, clamped, invalid text reverts, 2 decimals. "Selected N.N s". (`ui/panels/timeline_dialog.py:84-91`, `:324-338`, `:226`)
- [ ] **Quick length "N s"** sets the length from the start, sliding back at the end. "All" selects the whole clip. (`ui/panels/timeline_dialog.py:94-96`, `:310-322`)
- [ ] **"▶  Play selection"**: pygame channel 3, green playhead every 60 ms, stops at the selection end. "■  Stop" stops it. (`ui/panels/timeline_dialog.py:103-108`, `:341-376`)
- [ ] **"Use selection"**: calls back with start, end, duration (None, None for the whole clip) and closes. **"Cancel" / close**: closes without saving. (`ui/panels/timeline_dialog.py:109-122`, `:378-384`)

### Voice match dialog (analyzer_dialog.py)
- [ ] **Window**: "Voice match", 840x560, minimum 640x480, transient, lifted and focused, **not modal**. (`ui/panels/analyzer_dialog.py:63-67`, `:136-141`)
- [ ] **Header**: "🔍  Voice match" with an explainer line. (`ui/panels/analyzer_dialog.py:86-92`)
- [ ] **Run starts on its own** 300 ms after opening; Cancel shown while it runs. (`ui/panels/analyzer_dialog.py:134`, `:195-198`)
- [ ] **Stages**: "Updating the voice bank (N voices)…" (0 to 45%); "Preparing your clip…" (46%), "Removing background music before measuring…" with Clean on, clip capped at 25 s; clip analysis (48 to 60%); "▶ Result i of n: your clip's words in X…", "cloning X…", "Comparing the results with your clip…". (`ui/panels/analyzer_dialog.py:47`, `:172-220`, `:292-317`)
- [ ] **"Your clip" box**: pitch (gender), speaking speed, language with % sure, seconds measured, and the gender line. Warnings for a manual gender that does not fit the pitch, and for non-English speech. (`ui/panels/analyzer_dialog.py:112-120`, `:252-271`)
- [ ] **Cancel**: disabled "Cancelling…", ends as "Cancelled." (warning). **Errors**: "❌  Voice match failed: …" in the stage line, no box. (`ui/panels/analyzer_dialog.py:160-163`, `:243-249`)
- [ ] **Done text**: "Done. Apply the best match, or listen to the others first.", or a warning note (no words heard, Results could not be made, "No ▶ Result for X…"). No matches: "No matching English <gender> engine voice was found." (`ui/panels/analyzer_dialog.py:234-236`, `:315-342`)
- [ ] **Ranked rows**: "1.  Best match" (green 2 px border), then "N.  Close match", each with the voice label and "pitch X Hz vs your Y Hz · speed N x/WPM". (`ui/panels/analyzer_dialog.py:344-365`)
- [ ] **▶ Original / ▶ Result**: play/stop on pygame channel 5, "■ Stop" while playing, reset at the end; Result only when made. (`ui/panels/analyzer_dialog.py:48`, `:368-382`, `:403-449`)
- [ ] **Apply**: sets the TTS Engine. Error: "⚠  Could not apply: …", window stays. Success: status "🔍  TTS Engine set to X by voice match…", Result handed back, window closes. (`ui/panels/analyzer_dialog.py:373-375`, `:384-400`)
- [ ] **Close**: cancels the run, stops audio, deletes the temp folder. (`ui/panels/analyzer_dialog.py:128-132`, `:451-456`)

### Cloned Voices dialog (clone_voice_dialog.py)
- [ ] **Window**: "Cloned Voices", 620x600, minimum 520x300, resizable, modal after 100 ms. Header "✏  Cloned Voices" with a help line. (`ui/panels/clone_voice_dialog.py:320-335`, `:364-372`)
- [ ] **Lists every voice**, including draft, training and failed. None: "No cloned voices yet. Create one in Customization → Clone Voice." (`ui/panels/clone_voice_dialog.py:393-406`)
- [ ] **"📂  Open folder"**: opens the voices folder, creating it if needed. **"Close"**: closes; closing cancels a running job and stops playback. (`ui/panels/clone_voice_dialog.py:346-352`, `:374-378`, `:834-840`)
- [ ] **Name entry (rename)**: Save enabled when the name or the editor changed; Enter saves when enabled. (`ui/panels/clone_voice_dialog.py:415-419`, `:457-460`, `:491-493`)
- [ ] **Row meta**: "🧬 Quick|RVC[ · imported][ · training| · unfinished| · failed] · N MB", warning color when not ready. **Base line**: "Base  <description>" or "not set", with a tip. (`ui/panels/clone_voice_dialog.py:421-435`)
- [ ] **"🔊  Voice settings ▾/▴"**: opens or closes the editor in the row, built on first open; open state survives a rebuild. (`ui/panels/clone_voice_dialog.py:446-453`, `:462-495`)
- [ ] **Editor menus**: Engine / Language (Kokoro only) / Voice (hidden when the engine has no choices). Going back to the saved engine restores its voice and speed. Unusable saved base: "The saved base can't be used here: … Showing the TTS Engine voice." (warning). (`ui/panels/clone_voice_dialog.py:91-109`, `:145-147`, `:200-247`)
- [ ] **Editor speed**: 0.5 to 2.0 at first, then the engine's range; 0.01x steps for Kokoro and Melo, 1 unit for Edge and pyttsx3; "↺" resets; engines without speed: "This engine has no speed setting." (`ui/panels/clone_voice_dialog.py:111-118`, `:222-262`)
- [ ] **Editor sound sliders**: RVC Pitch with 🎯, Index with ↺, Protect with ↺ (reset to profile default), same ranges as the Clone Voice box. Quick: Quality 4 to 50. (`ui/panels/clone_voice_dialog.py:40-53`, `:124-139`)
- [ ] **Editor ▶ Preview**: saved preview when the fingerprint matches ("Playing the saved preview."); otherwise makes one with the editor values ("Speaking the preview sentence…"), saved only when equal to the saved settings, channel 2, "Playing. Save keeps these settings." Not-ready voices: "Preview works once the voice is ready." (disabled). (`ui/panels/clone_voice_dialog.py:465-474`, `:616`, `:683-716`, `:790-807`)
- [ ] **Button states during a job**: running button reads "✖  Cancel" (preview) or "✖" (match); playing preview "■  Stop"; others disabled. A second click cancels ("Cancelling…"). (`ui/panels/clone_voice_dialog.py:599-644`)
- [ ] **Job blocked**: "Clone Voice is busy (task). Try again when it finishes.", "Wait for the current generation to finish.", "Can't preview: err.", "Can't load the voice: …". (`ui/panels/clone_voice_dialog.py:648-664`)
- [ ] **Editor 🎯**: RVC voices with clips. "Speaking a sample with the base voice…". Sets the editor's Pitch only (not the file) and records f0. "Pitch set to +N st (base … → target …). Preview it, then Save." Cannot measure: "Couldn't measure the pitch of the base voice/the clips… Pitch was not changed." (`ui/panels/clone_voice_dialog.py:395`, `:718-766`)
- [ ] **Preview / match messages**: "Preview cancelled.", "Preview failed: …", "Pitch match cancelled.", "Pitch match failed: …"; cancel in warning color, failure in error color. (`ui/panels/clone_voice_dialog.py:710-715`, `:740-753`, `:794`)
- [ ] **Save**: name cut to 40 chars; duplicate: warning "Name in use". Writes `name`, `base_selection` / `base_engine` / `base_voice`, sound fields, and `base_f0_hz` / `target_f0_hz` after 🎯. (`ui/panels/clone_voice_dialog.py:516-538`)
- [ ] **Save status**: "✅  Renamed 'A' to 'B'; base voice of … set to …; sound settings of … saved", plus ", selected in TTS Engine" or "(TTS Engine not changed: …)" when active, plus "Press 🎯 next to Pitch…" when the base changed without a re-match. Updates the Clone Voice box when active; the saved preview is kept or remade in the background. (`ui/panels/clone_voice_dialog.py:542-580`)
- [ ] **"🗑  Delete"**: yes/no "Delete voice", "This cannot be undone.", warning icon. Deletes the folder and preview, refreshes, status "🗑  Deleted voice 'X'". (`ui/panels/clone_voice_dialog.py:441-445`, `:582-596`)
- [ ] **Save / Delete blocked** by info boxes "Voice is training", "Generating", or "Busy". (`ui/panels/clone_voice_dialog.py:499-514`)

### Files written by Clone Voice
- [ ] **profile.json fields**: `pitch`, `index_rate`, `protect`, `diffusion_steps`, `base_selection`, `base_engine`, `base_voice`, `name`, `base_f0_hz`, `target_f0_hz`, `preview_text`, `status`, `batch_size`, `pth`, `index`, `sample_rate`, `epochs`. (`ui/panels/clone_voice_group.py:1134-1138`, `:1263`, `:1825`, `:1877-1902`; `ui/panels/clone_voice_dialog.py:526-535`)
- [ ] **Recordings**: `<runtime root>/recordings/Recording <timestamp>.wav`. (`ui/panels/clone_voice_group.py:1548`, `ui/panels/record_dialog.py:298`)
- [ ] **Saved previews** through `previews.store` / `previews.delete`. (`ui/panels/clone_voice_group.py:1833`, `:1994`, `:2029`; `ui/panels/clone_voice_dialog.py:577-579`)
- [ ] **Train base sample**: `<voice>/samples/base.wav`. (`ui/panels/clone_voice_group.py:1871-1874`)
- [ ] **Temp folders**: `adits_vs_clone_`, `adits_vs_match_`, `adits_vs_timeline_`, deleted on close or cleanup. (`ui/panels/clone_voice_group.py:1695`, `ui/panels/analyzer_dialog.py:70`, `ui/panels/timeline_dialog.py:54`)

---

## pygame channels (whole app)

Keep these numbers in the port.

| Channel | Used by |
| :--- | :--- |
| `mixer.music` | Generated speech (Play / Stop) |
| 1 | Ambiance |
| 2 | Clone Voice ▶ Preview, cleanup Original / Cleaned, Cloned Voices dialog preview |
| 3 | Timeline ▶ Play selection |
| 4 | Recorder preview |
| 5 | Voice match Original / Result |
| 6 | Clip player (Recordings tile) |
| 7 | Text Input media strip |
