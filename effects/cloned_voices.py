"""
effects/cloned_voices.py
─────────────────────────
Cloned voices as entries in EFFECTS_REGISTRY.

Each ready voice in the voice library (core/voice_cloning/library.py)
becomes one effect tile, keyed "clone:<slug>". The entries:

  * sit right after "normalize", so voice conversion runs before every
    other DSP effect (converting after reverb or a vocoder would convert
    the effect too);
  * share exclusive_group "clone", so at most one is on at a time;
  * call the voice-cloning worker, which runs in its own process and venv.

The registry list is changed in place: ui/app.py imported the same list
object, so rebinding it would leave the app on a stale copy.
"""

import os
import shutil
import tempfile

import numpy as np

CLONE_GROUP = "clone"
KEY_PREFIX = "clone:"
# The "tts_studio_" prefix lets core/temp_cleanup.py sweep these after a crash.
TEMP_PREFIX = "tts_studio_vc_"


def is_clone_entry(entry) -> bool:
    return entry.get("exclusive_group") == CLONE_GROUP


def slug_of(key: str):
    return key[len(KEY_PREFIX):] if key.startswith(KEY_PREFIX) else None


def _resample(y, src_sr, dst_sr):
    if src_sr == dst_sr:
        return y
    try:
        import soxr
        return soxr.resample(y, src_sr, dst_sr, quality="HQ")
    except ImportError:
        import librosa
        return librosa.resample(y, orig_sr=src_sr, target_sr=dst_sr)


def convert_samples(slug, samples, sr, pitch=None, index_rate=None, protect=None,
                    diffusion_steps=None, cancel_event=None, status_cb=None):
    """Convert float32 mono *samples* at *sr* into voice *slug*; same rate out.

    Raises core.voice_cloning.errors.VCError subclasses (VCCancelled on cancel).
    """
    import soundfile as sf
    from core.voice_cloning import get_client, library
    from core.voice_cloning.errors import VCError

    try:
        profile = library.load_voice(slug)
    except (OSError, ValueError) as e:
        raise VCError(f"Cloned voice '{slug}' could not be loaded: {e}")

    tmp = tempfile.mkdtemp(prefix=TEMP_PREFIX)
    try:
        src = os.path.join(tmp, "in.wav")
        out = os.path.join(tmp, "out.wav")
        pcm = (np.clip(np.asarray(samples, dtype=np.float32), -1.0, 1.0) * 32767.0).astype(np.int16)
        sf.write(src, pcm, int(sr), subtype="PCM_16")
        get_client().convert(profile, src, out, pitch=pitch, index_rate=index_rate,
                             protect=protect, diffusion_steps=diffusion_steps,
                             cancel_event=cancel_event, status_cb=status_cb)
        y, out_sr = sf.read(out, dtype="float32", always_2d=True)
        y = y.mean(axis=1) if y.shape[1] > 1 else y[:, 0]
        return _resample(y, int(out_sr), int(sr)).astype(np.float32)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _make_fn(slug):
    def fn(samples, sr, pitch=None, index_rate=None, protect=None, diffusion_steps=None,
           cancel_event=None, status_cb=None, **_ignored):
        return convert_samples(slug, samples, sr, pitch=pitch, index_rate=index_rate,
                               protect=protect, diffusion_steps=diffusion_steps,
                               cancel_event=cancel_event, status_cb=status_cb)
    fn.__name__ = f"clone_{slug.replace('-', '_')}"
    return fn


def make_entry(profile) -> dict:
    kind = "RVC" if profile.mode == "rvc" else "Quick"
    return {
        "key": KEY_PREFIX + profile.slug,
        "label": f"🧬  {profile.name}",
        "desc": f"Cloned voice ({kind})",
        "fn": _make_fn(profile.slug),
        "kwargs": {},
        "exclusive_group": CLONE_GROUP,
        "dynamic": True,
    }


def _insert_at(registry) -> int:
    """Index just after "normalize" (or 0), before any non-clone effect."""
    for i, e in enumerate(registry):
        if e.get("key") == "normalize":
            return i + 1
    return 0


def _ready_profiles():
    from core.voice_cloning import library
    return library.list_voices(ready_only=True)


def extend_with_cloned_voices(registry: list) -> None:
    """Insert an entry per ready voice. Called once when effects.registry loads."""
    at = _insert_at(registry)
    for offset, p in enumerate(_ready_profiles()):
        registry.insert(at + offset, make_entry(p))


def refresh_cloned_voices(registry: list):
    """Bring the clone entries in line with the library. Returns (added_entries, removed_keys).

    New voices are inserted in name order among the existing clone entries;
    entries whose voice was deleted or is no longer ready are removed.
    """
    profiles = _ready_profiles()
    wanted = {KEY_PREFIX + p.slug: p for p in profiles}
    removed = []
    for i in range(len(registry) - 1, -1, -1):
        e = registry[i]
        if is_clone_entry(e) and e["key"] not in wanted:
            removed.append(e["key"])
            del registry[i]
    present = {e["key"]: e for e in registry if is_clone_entry(e)}
    added = []
    for key, p in wanted.items():
        if key in present:
            present[key]["label"] = f"🧬  {p.name}"      # renamed voice
            continue
        added.append(make_entry(p))
    if added:
        clones = [e for e in registry if is_clone_entry(e)] + added
        clones.sort(key=lambda e: e["label"].lower())
        for e in [e for e in registry if is_clone_entry(e)]:
            registry.remove(e)
        at = _insert_at(registry)
        registry[at:at] = clones
    return added, removed
