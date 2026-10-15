"""
core/voice_cloning/previews.py
───────────────────────────────
The saved ▶ Preview of each cloned voice, so it plays at once instead of
being synthesized and converted every time:

    <runtime root>/previews/<slug>.wav    PREVIEW_TEXT spoken by the voice
    <runtime root>/previews/<slug>.json   {"fingerprint": ...} of what made it

The text is the voice's preview_text (about 10 s of the clip's own words,
set when the voice was made from a voice match), else TEXT. For a Quick voice
made right after a voice match, the saved preview is the first 10 s of the
analyzer's ▶ Result (cut_preview), so no extra speech is made.

The fingerprint covers the text, the engine voice and speed that spoke it
(EnginePanel.synthesis_key()), the sound settings (pitch, index rate,
protect or quality steps) and the voice's model file. A saved preview is
only played when the fingerprint of the request matches, so a stale one is
never heard: changing the base voice or a slider simply makes a new one.

From source the runtime root is vc_runtime_data/ in the repo (gitignored).
Standard library only at import time.
"""

import hashlib
import json
import os
import shutil
import threading
from pathlib import Path

from core.voice_cloning import runtime

TEXT = "Hello! This is a short preview of my cloned voice."
MAX_SECONDS = 10.0
SOUND_FIELDS = {"rvc": ("pitch", "index_rate", "protect"), "seedvc": ("diffusion_steps",)}

_lock = threading.Lock()


def previews_dir() -> Path:
    return runtime.runtime_root() / "previews"


def wav_path(slug) -> Path:
    return previews_dir() / f"{slug}.wav"


def _meta_path(slug) -> Path:
    return previews_dir() / f"{slug}.json"


def sound_of(p, overrides=None) -> dict:
    """The sound settings that shape the conversion, e.g. {"diffusion_steps": 10}."""
    out = {f: getattr(p, f) for f in SOUND_FIELDS[p.mode]}
    out.update({k: v for k, v in (overrides or {}).items() if k in out})
    return {k: (int(round(v)) if k in ("pitch", "diffusion_steps") else round(float(v), 2))
            for k, v in out.items()}


def _model_stamp(p):
    path = p.pth_abs if p.mode == "rvc" else p.reference_abs
    try:
        st = os.stat(path)
        return [st.st_size, st.st_mtime_ns]
    except (OSError, TypeError):
        return None


def text_of(p) -> str:
    """The words *p*'s preview speaks."""
    return getattr(p, "preview_text", None) or TEXT


def fingerprint(p, synth_key, sound) -> str:
    """Identifies one preview: text, engine voice and speed, sound settings, model file."""
    data = {"text": text_of(p), "mode": p.mode, "synth": synth_key, "sound": sound,
            "model": _model_stamp(p)}
    raw = json.dumps(data, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha1(raw).hexdigest()


def cached(slug, fp):
    """Path of the saved preview when it was made with fingerprint *fp*, else None."""
    try:
        with open(_meta_path(slug), "r", encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, ValueError):
        return None
    wav = wav_path(slug)
    return str(wav) if fp and meta.get("fingerprint") == fp and wav.is_file() else None


def store(slug, src_wav, fp) -> str:
    """Copy *src_wav* in as the voice's saved preview (replacing any older one)."""
    with _lock:
        d = previews_dir()
        d.mkdir(parents=True, exist_ok=True)
        wav, meta = wav_path(slug), _meta_path(slug)
        tmp = wav.with_name(wav.name + ".tmp")
        shutil.copyfile(src_wav, tmp)
        os.replace(tmp, wav)
        tmp = meta.with_name(meta.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"fingerprint": fp}, f)
        os.replace(tmp, meta)
        return str(wav)


def cut_preview(src_wav, dst_wav, max_seconds=MAX_SECONDS) -> str:
    """Copy the start of *src_wav* to *dst_wav*, at most *max_seconds* long.

    Ends in the last pause before the limit (not before 3 s), so no word is
    cut off; a 30 ms fade-out covers a hard cut when there is no pause.
    """
    from pydub import AudioSegment
    from pydub import silence as pydub_silence
    seg = AudioSegment.from_file(str(src_wav))
    limit = int(max_seconds * 1000)
    if len(seg) > limit:
        thresh = min(-40.0, seg.dBFS - 16) if seg.dBFS != float("-inf") else -40.0
        gaps = [(a, b) for a, b in pydub_silence.detect_silence(seg[:limit + 300], min_silence_len=150,
                                                                silence_thresh=thresh)
                if 3000 <= (a + b) // 2 <= limit]
        end = (gaps[-1][0] + gaps[-1][1]) // 2 if gaps else limit
        seg = seg[:end].fade_out(30)
    seg.export(str(dst_wav), format="wav")
    return str(dst_wav)


def transcribe(wav) -> str:
    """The words heard in *wav* (faster-whisper, the subtitle model), '' when none."""
    from core.subtitles import get_whisper_model
    segments, _info = get_whisper_model().transcribe(str(wav), vad_filter=True)
    return " ".join(s.text.strip() for s in segments).strip()


def delete(slug) -> None:
    with _lock:
        for path in (wav_path(slug), _meta_path(slug)):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
