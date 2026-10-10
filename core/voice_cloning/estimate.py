"""
core/voice_cloning/estimate.py
───────────────────────────────
Rough time estimates shown in the Clone Voice UI before a job starts.

Calibrated on the dev PC (GTX 1650 4 GB, 14 GB RAM) during Phase 0-2:

  RVC training, 94 s of audio, batch 2, 40 kHz:
    slicing 45-54 s, feature extraction 67-73 s, trainer start ~60 s,
    ~31 s per epoch, ~60 s for the final save; 3 epochs took 358-365 s.
    Batch 4 epochs ran 52-269 s (memory spill on 4 GB), so about 2x.
  Seed-VC (Quick) conversion, warm: real-time factor 2.5 at 10 steps,
    6.7 at 30 steps -> about 0.6 + 0.2 x steps. First use adds ~40 s.
  RVC conversion, warm: 2.2 s for 8.1 s of speech (RTF ~0.3).
    First use adds ~35 s.

Batch 1, 32 kHz and 48 kHz factors were not measured; they are guesses
and the UI says "about". The live ETA during training comes from the
measured epoch times instead.
"""

WORDS_PER_SECOND = 2.6          # Kokoro at speed 1.0, approximate

_BATCH_FACTOR = {1: 1.3, 2: 1.0, 4: 2.0, 8: 3.0}
_SR_FACTOR = {32000: 0.9, 40000: 1.0, 48000: 1.15}


def training_seconds(audio_seconds, epochs, batch_size=2, sample_rate=40000, save_every=10):
    """{"total": s, "slice": s, "features": s, "train": s, "per_epoch": s} for an RVC run."""
    a = max(1.0, float(audio_seconds))
    slicing = 20 + 0.27 * a
    features = 40 + 0.29 * a
    per_epoch = 0.33 * a * _BATCH_FACTOR.get(int(batch_size), 1.5) * _SR_FACTOR.get(int(sample_rate), 1.0)
    saves = max(0, int(epochs) // max(1, int(save_every)) - 1) * 30
    train = 60 + int(epochs) * per_epoch + saves + 60
    total = slicing + features + train + 15
    return {"total": total, "slice": slicing, "features": features,
            "train": train, "per_epoch": per_epoch}


def quick_create_seconds(audio_seconds):
    """Building a Seed-VC reference: decoding and trimming only."""
    return 3 + 0.05 * float(audio_seconds)


def speech_seconds(text):
    words = len((text or "").split())
    return words / WORDS_PER_SECOND


def conversion_seconds(mode, speech_sec, steps=10, first_use=False):
    """Seconds to convert speech_sec of audio into a cloned voice."""
    s = max(0.0, float(speech_sec))
    if mode == "seedvc":
        t = s * (0.6 + 0.2 * int(steps))
        return t + (40 if first_use else 0)
    t = s * 0.3 + 1
    return t + (35 if first_use else 0)


def fmt(seconds) -> str:
    """'about 45 s', 'about 6 min', 'about 7 h 30 min'."""
    s = int(round(max(0.0, float(seconds))))
    if s < 60:
        return f"about {max(5, int(round(s / 5.0)) * 5)} s"
    m = int(round(s / 60.0))
    if m < 60:
        return f"about {m} min"
    h, m = divmod(m, 60)
    m = int(round(m / 10.0)) * 10
    if m == 60:
        h, m = h + 1, 0
    return f"about {h} h" + (f" {m} min" if m else "")


def fmt_clock(seconds) -> str:
    """'4:05' or '1:02:30' for live remaining time."""
    s = int(max(0, seconds))
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
