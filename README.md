# Adits Voice Studio

A desktop text-to-speech studio for Windows. Type text, pick an engine and a
voice, add effects, and save the result as WAV or MP3 with optional subtitles.
It can also clone a voice from your own recordings and use it like any other
effect.

Everything runs locally on your PC. Developer and architecture notes are in
[AGENTS.md](AGENTS.md).

## Features

* **Local engines:** Kokoro-82M, Piper, MeloTTS and the Windows system voices.
  gTTS and Edge (online) are available when running from source.
* **Voice effects:** 30+ effects (vocoder, reverb, radio, Darth Vader and more),
  plus Skip Silences, a 3-band EQ and background ambiance.
* **Subtitles:** word- or sentence-level SRT files from the generated speech.
* **Clone Voice:** make your own voice from recordings and switch it on in
  Voice Effects → Cloned Voices. Details below.

## Running from source

Requirements: Windows 10 or 11, Python 3.11. An NVIDIA GPU is optional but
makes local engines and voice cloning much faster.

```powershell
python -m venv venv_311
.\venv_311\Scripts\python.exe -m pip install -r requirements.txt
.\venv_311\Scripts\python.exe -m pip install --no-deps kokoro-onnx
.\run.ps1
```

Always call the venv's own `python.exe` as shown. See *Environment traps* in
[AGENTS.md](AGENTS.md) for why.

## Clone Voice

Find it in **Customization → Clone Voice**.

1. **Install once.** Press **Install voice cloning**. It sets up two separate
   Python environments (RVC and Seed-VC) and downloads about 9 GB, so it needs
   Python 3.11 and 3.12 (with the `py` launcher), Git, and about 21 GB of free
   space. When a newer app version needs extra packages, an **Update** button
   appears.
2. **Add the voice to clone** on the **Create voice** tab:
   * **Add clips** to import recordings (WAV, MP3, M4A, AAC, MP4, FLAC, OGG,
     Opus, WebM, WMA, AIFF, AMR, 3GP), or **Record** to record with your
     microphone and preview it before adding.
   * **Analyze** (optional) finds the English engine voice that clones best
     from your clip. It test-clones the closest voices and ranks them
     **Best match** and **Close match**. **Apply** selects that voice and a
     matching speed as the base voice.
3. **Optional clean-up** before cloning, applied in this order:
   * **Use part of each recording:** first 10, 20 or 25 seconds (Quick), first
     1, 3 or 5 minutes (Train), or pick a range on a waveform timeline.
   * **Remove music & noise:** separates the voice from music, with
     an option to also reduce hiss and hum. **Preview cleanup** lets you compare
     before and after.
   * **Skip silences:** removes long pauses.
4. **Choose a mode and create:**
   * **Quick:** ready in seconds from 10 to 25 seconds of clean speech.
   * **Train (RVC):** trains a voice model. Best with 3+ minutes of speech;
     training can take from minutes to hours. The time estimate updates as you
     change epochs, batch size and sample rate.
   * **Import model:** use an existing RVC `.pth` model, with its `.index` if
     you have it.
5. **Use it.** The voice appears in **Voice Effects → Cloned Voices**. Switching
   it on also selects the engine voice and speed it was made with. Tune pitch
   and quality on the **Active voice** tab; the ✏️ button renames or deletes
   voices.

Cancel stops any running install, analysis, training or preview.

## Building the Windows app

```powershell
.\venv_311\Scripts\python.exe build_exe.py              # app folder in dist\AditsVoiceStudio
.\venv_311\Scripts\python.exe build_exe.py --installer  # also the Setup.exe (Inno Setup 6)
.\venv_311\Scripts\python.exe build_exe.py --skip-test  # skip the frozen self-test
```

### Build & Voice Cloning Options

The core application (TTS engines Kokoro, Piper, MeloTTS, SAPI5, 30+ DSP effects, and Whisper subtitles) is always bundled into the standalone executable. For the heavy AI Voice Cloning feature (RVC + Seed-VC, ~21 GB on disk), there are three build/distribution approaches:

1. **Standard / Current Build (Lightweight App Bundle):**
   * **Size:** ~6 GiB folder (`dist\AditsVoiceStudio\`).
   * Bundles core offline speech models, DSP effects, and the voice cloning worker orchestration scripts (`vc_runtime/worker.py`), keeping installer size manageable.
   * Does not bundle the 21 GB RVC/Seed-VC AI environments.

2. **Option A: Connected / Hybrid Runtime Build (Implemented):**
   * **How it works:** The frozen EXE actively detects and connects to an existing voice-cloning runtime:
     * Next to the executable: `dist\AditsVoiceStudio\vc_runtime_data\`
     * In the repository: `..\..\vc_runtime_data\`
     * In user profile: `%LOCALAPPDATA%\AditsVoiceStudio\vc_runtime`
   * If the runtime is already installed on the machine, Clone Voice is immediately active with all existing voices and models.
   * If not installed on the machine, the in-app **Install** button is enabled as long as Python 3.11/3.12 (with the `py` launcher) and Git are installed on Windows.

3. **Option B: Full Pre-Bundled Standalone Distribution (~27+ GiB):**
   * **How it works:** The entire pre-built `vc_runtime_data` directory (with both `venv_rvc` and `venv_seedvc`, base model weights, and clones) is packaged directly into the distribution folder (`dist\AditsVoiceStudio\vc_runtime_data`).
   * **Pros:** Completely offline with zero internet downloads on client machines.
   * **Considerations:** Huge distribution size (27+ GiB). Standard Windows Python virtual environments contain machine-specific absolute paths in `pyvenv.cfg` and wrapper executables, so portable embedded Python environments or path relinking are required when copying to different systems.

## Where files are kept

| What | Running from source | Installed app |
| :--- | :--- | :--- |
| Saved audio and subtitles | `output\` | `Documents\AditsVoiceStudio` |
| Voice cloning runtime, cloned voices, recordings | `vc_runtime_data\` | `%LOCALAPPDATA%\AditsVoiceStudio\vc_runtime` |
| Logs | repo folder | `%LOCALAPPDATA%\AditsVoiceStudio\logs` |

## License

GPL-3.0-or-later. See [LICENSE.txt](LICENSE.txt). Voice cloning downloads
Applio (MIT), Seed-VC (GPL-3.0) and Demucs (MIT) at install time; they are not
bundled with the app.
