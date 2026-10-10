"""
core/voice_cloning/library.py
──────────────────────────────
The saved cloned voices. One folder per voice under runtime.voices_dir():

    voices/<slug>/
        profile.json        VoiceProfile, schema 1
        clips/              copies of the user's recordings
        dataset/            48 kHz mono WAVs for RVC training (removed after success)
        reference.wav       Seed-VC reference, at most 25 s (Quick voices)
        model/<slug>.pth    RVC voice model + model/<slug>.index
        samples/            base.wav (engine voice), preview.wav (cloned)

Paths inside profile.json are relative to the voice folder, so the whole
runtime root can be moved. Standard library only at import time.
"""

import json
import os
import re
import shutil
import threading
import unicodedata
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path

from core.voice_cloning import runtime

SCHEMA = 1
MODES = ("rvc", "seedvc")
STATUSES = ("draft", "training", "ready", "failed")
MAX_REF_SECONDS = 25.0
# Accepted clip formats: core.audio_io.AUDIO_EXTENSIONS (libsndfile + PyAV).

_lock = threading.RLock()


@dataclass
class VoiceProfile:
    name: str
    slug: str
    mode: str                         # "rvc" | "seedvc"
    status: str = "draft"
    created_at: str = ""
    # [{"path": "clips/x.mp3", "seconds": 61.2, "start": 3.0, "end": 23.0, "used": "processed/000_clean.wav"}]
    # start/end (seconds) select part of a clip; "used" is the trimmed/cleaned copy cloning reads.
    clips: list = field(default_factory=list)
    total_seconds: float = 0.0
    reference_wav: str = None         # seedvc
    pth: str = None                   # rvc
    index: str = None                 # rvc
    sample_rate: int = None
    epochs: int = None
    batch_size: int = None
    pitch: int = 0                    # semitones applied at conversion (rvc)
    index_rate: float = 0.3           # rvc
    protect: float = 0.33             # rvc
    diffusion_steps: int = 10         # seedvc quality
    base_engine: str = ""
    base_voice: str = ""
    # Engine-panel dropdown values the voice was made with, e.g.
    # {"engine": "Kokoro-82M (Fast Local AI)", "kind": "kokoro", "lang": "American English",
    #  "voice": "am_onyx"}. Switching the voice on re-selects it (see EnginePanel).
    base_selection: dict = None
    # Optional clip preparation chosen at creation:
    # {"trim": "first10" | "first20" | "first25" | "first60" | ... | "custom" | None,
    #  "separate": bool (remove background music), "denoise": bool (also reduce steady noise),
    #  "skip_silences": bool (drop pauses from the prepared audio, applied last)}
    prep: dict = None
    base_f0_hz: float = None
    target_f0_hz: float = None
    imported: bool = False
    error: str = None
    schema: int = SCHEMA

    # ── paths ──────────────────────────────────────────────────────
    @property
    def dir(self) -> Path:
        return runtime.voices_dir() / self.slug

    def _abs(self, rel):
        return str(self.dir / rel) if rel else None

    @property
    def pth_abs(self):
        return self._abs(self.pth)

    @property
    def index_abs(self):
        return self._abs(self.index)

    @property
    def reference_abs(self):
        return self._abs(self.reference_wav)

    @property
    def engine(self):
        return self.mode

    @property
    def is_ready(self) -> bool:
        if self.status != "ready":
            return False
        if self.mode == "rvc":
            return bool(self.pth_abs) and os.path.isfile(self.pth_abs)
        return bool(self.reference_abs) and os.path.isfile(self.reference_abs)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "VoiceProfile":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


# ── slugs ───────────────────────────────────────────────────────────

def slugify(name: str) -> str:
    text = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40].strip("-")
    return text or "voice"


def _unique_slug(name: str) -> str:
    base = slugify(name)
    root = runtime.voices_dir()
    slug, n = base, 2
    while (root / slug).exists():
        slug = f"{base}-{n}"
        n += 1
    return slug


# ── persistence ─────────────────────────────────────────────────────

def _profile_path(slug) -> Path:
    return runtime.voices_dir() / slug / "profile.json"


def save_voice(p: VoiceProfile) -> None:
    with _lock:
        path = _profile_path(p.slug)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name("profile.json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(p.to_dict(), f, indent=2)
        os.replace(tmp, path)


def load_voice(slug) -> VoiceProfile:
    with open(_profile_path(slug), "r", encoding="utf-8") as f:
        return VoiceProfile.from_dict(json.load(f))


def list_voices(ready_only=False) -> list:
    """All voices with a readable profile, sorted by name. Never raises."""
    root = runtime.voices_dir()
    out = []
    try:
        entries = list(os.scandir(root))
    except OSError:
        return out
    for entry in entries:
        if not entry.is_dir():
            continue
        try:
            p = load_voice(entry.name)
        except (OSError, ValueError, TypeError) as e:
            print(f"Skipping voice folder {entry.name}: {e}")
            continue
        if p.slug != entry.name:
            p.slug = entry.name
        if ready_only and not p.is_ready:
            continue
        out.append(p)
    return sorted(out, key=lambda v: v.name.lower())


def update_params(slug, **changes) -> VoiceProfile:
    allowed = {"name", "pitch", "index_rate", "protect", "diffusion_steps", "status", "error",
               "pth", "index", "sample_rate", "epochs", "batch_size", "reference_wav",
               "base_f0_hz", "target_f0_hz", "base_engine", "base_voice", "base_selection", "prep"}
    bad = set(changes) - allowed
    if bad:
        raise ValueError(f"Cannot update {sorted(bad)}")
    with _lock:
        p = load_voice(slug)
        for k, v in changes.items():
            setattr(p, k, v)
        save_voice(p)
        return p


def delete_voice(slug) -> None:
    """Remove the voice folder and any training leftovers in Applio/logs/<slug>.

    A cancelled or failed training run leaves its sliced audio and up to
    1.3 GB of G_/D_ checkpoints there.
    """
    with _lock:
        shutil.rmtree(runtime.voices_dir() / slug, ignore_errors=True)
        shutil.rmtree(runtime.repo_dir("rvc") / "logs" / slug, ignore_errors=True)


# ── creation ────────────────────────────────────────────────────────

def audio_seconds(path) -> float:
    """Duration of an audio file in seconds (libsndfile reads wav/mp3/flac/ogg)."""
    try:
        import soundfile as sf
        info = sf.info(str(path))
        return float(info.frames) / float(info.samplerate)
    except Exception:
        from core.audio_io import load_segment
        return len(load_segment(str(path))) / 1000.0


def create_voice(name, mode, clip_paths, base_engine="", base_voice="", base_selection=None,
                 prep=None) -> VoiceProfile:
    """clip_paths: file paths, or dicts {"path", "start", "end"} to use part of a clip (seconds)."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    if not clip_paths:
        raise ValueError("At least one audio clip is required")
    with _lock:
        slug = _unique_slug(name)
        d = runtime.voices_dir() / slug
        (d / "clips").mkdir(parents=True)
    clips, used = [], set()
    for item in clip_paths:
        sel = item if isinstance(item, dict) else {"path": item}
        src = sel["path"]
        stem, ext = os.path.splitext(os.path.basename(src))
        fname, n = f"{stem}{ext}", 2
        while fname.lower() in used:
            fname, n = f"{stem}-{n}{ext}", n + 1
        used.add(fname.lower())
        dst = d / "clips" / fname
        shutil.copyfile(src, dst)
        full = audio_seconds(dst)
        clip = {"path": f"clips/{fname}", "seconds": round(full, 2)}
        start, end = sel.get("start"), sel.get("end")
        if start is not None or end is not None:
            start = max(0.0, float(start or 0.0))
            end = min(full, float(end if end is not None else full))
            if end - start < 0.5:
                raise ValueError(f"The selected part of {os.path.basename(src)} is shorter than 0.5 s.")
            clip.update(start=round(start, 3), end=round(end, 3), seconds=round(end - start, 2))
        clips.append(clip)
    p = VoiceProfile(
        name=name.strip() or slug, slug=slug, mode=mode, status="draft",
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        clips=clips, total_seconds=round(sum(c["seconds"] for c in clips), 2),
        base_engine=base_engine, base_voice=base_voice, base_selection=base_selection,
        prep=prep,
    )
    save_voice(p)
    return p


def _trimmed(seg, clip):
    """Apply a clip's start/end selection (seconds) to an AudioSegment."""
    start, end = clip.get("start"), clip.get("end")
    if start is None and end is None:
        return seg
    a = int(round(float(start or 0.0) * 1000))
    b = int(round(float(end) * 1000)) if end is not None else len(seg)
    return seg[a:b]


def clip_audio_paths(p: VoiceProfile) -> list:
    """The audio each clip contributes: its prepared copy when there is one."""
    return [str(p.dir / (c.get("used") or c["path"])) for c in p.clips]


def _clip_segments(p: VoiceProfile):
    from core.audio_io import load_segment
    for c in p.clips:
        if c.get("used"):
            yield load_segment(str(p.dir / c["used"]))      # already trimmed / cleaned
        else:
            yield _trimmed(load_segment(str(p.dir / c["path"])), c)


def _drop_silences(seg):
    """Remove pauses longer than 0.4 s, keeping 150 ms around each phrase.

    The threshold follows the clip's own loudness (16 dB under its average,
    never above -40 dBFS), so quiet and loud recordings are treated alike.
    """
    from pydub import silence as pydub_silence
    if seg.dBFS == float("-inf"):
        return seg
    thresh = min(-40.0, seg.dBFS - 16)
    chunks = pydub_silence.split_on_silence(seg, min_silence_len=400, silence_thresh=thresh,
                                            keep_silence=150)
    if not chunks:
        return seg
    out = chunks[0]
    for c in chunks[1:]:
        out += c
    return out


def prepare_clips(p: VoiceProfile, separate_fn=None, denoise=False, progress_cb=None,
                  check_cancel=None, skip_silences=False) -> VoiceProfile:
    """Write the prepared copy of every clip: trim → remove music/noise → skip silences.

    separate_fn(in_wav, out_wav, denoise) runs the music-removal worker. The
    originals in clips/ are never changed; copies go to processed/ and each
    clip's "used" points at the copy cloning should read.
    """
    say = progress_cb or (lambda frac, msg: None)
    out_dir = p.dir / "processed"
    shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True)
    from core.audio_io import load_segment
    n = len(p.clips)
    for i, c in enumerate(p.clips):
        if check_cancel:
            check_cancel()
        say(i / n, f"Preparing clip {i + 1} of {n}…")
        seg = _trimmed(load_segment(str(p.dir / c["path"])), c)
        trimmed = out_dir / f"{i:03d}.wav"
        seg.set_sample_width(2).export(str(trimmed), format="wav")
        used = trimmed
        if separate_fn is not None:
            say((i + 0.3) / n, f"Removing background music from clip {i + 1} of {n}…")
            cleaned = out_dir / f"{i:03d}_clean.wav"
            separate_fn(str(trimmed), str(cleaned), denoise)
            used = cleaned
        if skip_silences:
            if check_cancel:
                check_cancel()
            say((i + 0.9) / n, f"Skipping silences in clip {i + 1} of {n}…")
            tight = _drop_silences(load_segment(str(used)))
            if len(tight) < 500:
                raise ValueError(f"Clip {i + 1} has almost no speech left after skipping silences.")
            dense = out_dir / f"{i:03d}_nosilence.wav"
            tight.set_sample_width(2).export(str(dense), format="wav")
            used = dense
        c["used"] = used.relative_to(p.dir).as_posix()
    save_voice(p)
    say(1.0, "Clips prepared")
    return p


def build_seedvc_reference(p: VoiceProfile, max_seconds=MAX_REF_SECONDS) -> Path:
    """Join the clips with long pauses removed, cut to max_seconds, 44.1 kHz mono."""
    from pydub import AudioSegment
    from pydub import silence as pydub_silence
    out = AudioSegment.silent(duration=0, frame_rate=44100)
    limit_ms = int(max_seconds * 1000)
    for seg in _clip_segments(p):
        seg = seg.set_channels(1).set_frame_rate(44100).set_sample_width(2)
        thresh = min(-40.0, seg.dBFS - 16) if seg.dBFS != float("-inf") else -40.0
        chunks = pydub_silence.split_on_silence(seg, min_silence_len=400,
                                                silence_thresh=thresh, keep_silence=150)
        for chunk in chunks or [seg]:
            out += chunk
            if len(out) >= limit_ms:
                break
        if len(out) >= limit_ms:
            break
    out = out[:limit_ms]
    if len(out) < 1000:
        raise ValueError("The clips contain less than one second of speech.")
    dest = p.dir / "reference.wav"
    out.export(str(dest), format="wav")
    update_params(p.slug, reference_wav="reference.wav")
    p.reference_wav = "reference.wav"
    return dest


def build_dataset(p: VoiceProfile, sample_rate=48000) -> Path:
    """Write every clip as a mono 16-bit WAV for Applio's preprocess step."""
    d = p.dir / "dataset"
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    for i, seg in enumerate(_clip_segments(p)):
        seg = seg.set_channels(1).set_frame_rate(sample_rate).set_sample_width(2)
        seg.export(str(d / f"{i:03d}.wav"), format="wav")
    return d


def import_rvc_model(name, pth_path, index_path=None, info=None) -> VoiceProfile:
    """Copy an existing RVC model (.pth, optional .index) in as a ready voice."""
    info = info or {}
    with _lock:
        slug = _unique_slug(name)
        model_dir = runtime.voices_dir() / slug / "model"
        model_dir.mkdir(parents=True)
    shutil.copyfile(pth_path, model_dir / f"{slug}.pth")
    index_rel = None
    if index_path:
        shutil.copyfile(index_path, model_dir / f"{slug}.index")
        index_rel = f"model/{slug}.index"
    p = VoiceProfile(
        name=name.strip() or slug, slug=slug, mode="rvc", status="ready",
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        pth=f"model/{slug}.pth", index=index_rel,
        sample_rate=info.get("sample_rate"), epochs=info.get("epochs"), imported=True,
    )
    save_voice(p)
    return p
