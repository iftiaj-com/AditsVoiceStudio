"""
core/voice_cloning/pitch.py
────────────────────────────
Pitch matching between the base engine voice and the target clips.

RVC keeps the input's pitch contour, so a high Kokoro voice converted into
a low target voice comes out at the wrong height. The fix is a semitone
offset: 12 * log2(target median F0 / base median F0).

Runs in the app's own venv (librosa is already there). librosa is imported
inside the functions so app startup does not pay for it.
"""

import math

BASE_SENTENCE = ("The quick brown fox jumps over the lazy dog, "
                 "while the rain in Spain stays mainly in the plain.")
FMIN, FMAX = 65.0, 500.0
_SR = 16000


def _load_mono_16k(path, max_seconds):
    import numpy as np
    from core.audio_io import load_segment
    seg = load_segment(str(path)).set_channels(1)
    seg = seg[: int(max_seconds * 1000)]
    peak = float(2 ** (8 * seg.sample_width - 1))
    y = np.array(seg.get_array_of_samples(), dtype=np.float32) / peak
    if seg.frame_rate != _SR:
        try:
            import soxr
            y = soxr.resample(y, seg.frame_rate, _SR)
        except ImportError:
            import librosa
            y = librosa.resample(y, orig_sr=seg.frame_rate, target_sr=_SR)
    return y


def voiced_f0(path, max_seconds=30.0):
    """F0 values (Hz) of the voiced frames in *path*."""
    import librosa
    import numpy as np
    y = _load_mono_16k(path, max_seconds)
    if y.size < _SR // 2:
        return np.array([], dtype=np.float32)
    f0, voiced, _ = librosa.pyin(y, fmin=FMIN, fmax=FMAX, sr=_SR, frame_length=1024)
    f0 = f0[voiced & ~np.isnan(f0)]
    return f0.astype(np.float32)


def median_f0(path, max_seconds=30.0):
    """Median F0 in Hz, or None when there are fewer than 20 voiced frames."""
    import numpy as np
    f0 = voiced_f0(path, max_seconds)
    return float(np.median(f0)) if f0.size >= 20 else None


def semitone_offset(base_hz, target_hz, limit=24) -> int:
    # ±24 is Applio's own range. A female engine voice into a deep male
    # target measured -14 st in testing (198 Hz → 84 Hz), so ±12 is too tight.
    if not base_hz or not target_hz:
        return 0
    st = 12.0 * math.log2(target_hz / base_hz)
    return int(max(-limit, min(limit, round(st))))


def estimate_offset(base_wav, clip_paths, status_cb=None, max_clips=3, seconds_per_clip=30.0) -> dict:
    """{"base_f0", "target_f0", "offset"} for converting base_wav's voice toward the clips."""
    import numpy as np
    say = status_cb or (lambda m: None)
    say("Measuring the base voice pitch…")
    base = median_f0(base_wav, 30.0)
    pooled = []
    for i, clip in enumerate(list(clip_paths)[:max_clips], 1):
        say(f"Measuring target pitch ({i}/{min(len(clip_paths), max_clips)})…")
        pooled.append(voiced_f0(clip, seconds_per_clip))
    pooled = np.concatenate(pooled) if pooled else np.array([])
    target = float(np.median(pooled)) if pooled.size >= 20 else None
    return {"base_f0": base, "target_f0": target, "offset": semitone_offset(base, target)}
