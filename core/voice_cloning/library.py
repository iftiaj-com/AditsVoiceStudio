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
    clips: list = field(default_factory=list)       # [{"path": "clips/x.mp3", "seconds": 61.2}]
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
               "base_f0_hz", "target_f0_hz", "base_engine", "base_voice", "base_selection"}
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


def create_voice(name, mode, clip_paths, base_engine="", base_voice="", base_selection=None) -> VoiceProfile:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    if not clip_paths:
        raise ValueError("At least one audio clip is required")
    with _lock:
        slug = _unique_slug(name)
        d = runtime.voices_dir() / slug
        (d / "clips").mkdir(parents=True)
    clips, used = [], set()
    for src in clip_paths:
        stem, ext = os.path.splitext(os.path.basename(src))
        fname, n = f"{stem}{ext}", 2
        while fname.lower() in used:
            fname, n = f"{stem}-{n}{ext}", n + 1
        used.add(fname.lower())
        dst = d / "clips" / fname
        shutil.copyfile(src, dst)
        clips.append({"path": f"clips/{fname}", "seconds": round(audio_seconds(dst), 2)})
    p = VoiceProfile(
        name=name.strip() or slug, slug=slug, mode=mode, status="draft",
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        clips=clips, total_seconds=round(sum(c["seconds"] for c in clips), 2),
        base_engine=base_engine, base_voice=base_voice, base_selection=base_selection,
    )
    save_voice(p)
    return p


def _clip_segments(p: VoiceProfile):
    from core.audio_io import load_segment
    for c in p.clips:
        yield load_segment(str(p.dir / c["path"]))


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
