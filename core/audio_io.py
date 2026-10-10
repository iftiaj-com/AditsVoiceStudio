"""
core/audio_io.py
─────────────────
MP3 and other non-WAV audio I/O through soundfile's bundled libsndfile.

pydub shells out to ffmpeg for anything but WAV. ffmpeg is not shipped with
the installer, and a PC without it on PATH lost MP3 export and custom
ambiance import silently. libsndfile (already bundled for soundfile) reads
and writes MP3, FLAC and OGG itself, so pydub is only used for WAV here.
"""

import os

import numpy as np
import soundfile as sf
from pydub import AudioSegment

# Sample rates an MPEG layer III stream can carry.
_MP3_RATES = (8000, 11025, 12000, 16000, 22050, 24000, 32000, 44100, 48000)


# Formats users can pick when importing audio (voice clips). libsndfile reads
# WAV/MP3/FLAC/OGG/AIFF/CAF itself; the rest (AAC in M4A/MP4, Opus/WebM, WMA,
# AMR, 3GP) are decoded by PyAV, which bundles its own FFmpeg libraries and is
# already shipped because faster-whisper (subtitles) depends on it.
AUDIO_EXTENSIONS = (".wav", ".mp3", ".m4a", ".aac", ".mp4", ".flac", ".ogg", ".oga",
                    ".opus", ".webm", ".wma", ".aiff", ".aif", ".caf", ".amr", ".3gp")


def _decode_with_av(path) -> AudioSegment:
    """Decode with PyAV into 16-bit PCM at the stream's own rate (mono or stereo)."""
    import av
    with av.open(str(path)) as container:
        stream = next((s for s in container.streams if s.type == "audio"), None)
        if stream is None:
            raise ValueError("the file has no audio track")
        rate = stream.rate or stream.codec_context.sample_rate
        channels = 2 if (stream.channels or 1) >= 2 else 1
        resampler = av.AudioResampler(format="s16", layout="stereo" if channels == 2 else "mono",
                                      rate=rate)
        chunks = []
        for frame in container.decode(stream):
            for out in resampler.resample(frame):
                chunks.append(out.to_ndarray())
        for out in resampler.resample(None):      # flush
            chunks.append(out.to_ndarray())
    if not chunks:
        raise ValueError("no audio could be decoded")
    pcm = np.concatenate([c.reshape(-1) for c in chunks]).astype(np.int16)
    return AudioSegment(data=pcm.tobytes(), sample_width=2, frame_rate=int(rate), channels=channels)


def load_segment(path: str) -> AudioSegment:
    """Decode an audio file into a 16-bit AudioSegment.

    libsndfile first (WAV, MP3, FLAC, OGG, AIFF...); PyAV for what it cannot
    read (M4A/AAC, Opus/WebM, WMA, AMR...). Files libsndfile reads take the
    same path as before.
    """
    if str(path).lower().endswith(".wav"):
        try:
            return AudioSegment.from_wav(path)
        except Exception:
            pass   # e.g. float WAV pydub can't parse; libsndfile can
    try:
        data, sr = sf.read(path, dtype="int16", always_2d=True)
    except Exception as sf_err:
        try:
            return _decode_with_av(path)
        except ImportError:
            raise sf_err
        except Exception as av_err:
            raise ValueError(f"Unsupported or damaged audio file ({os.path.basename(str(path))}): "
                             f"{av_err}") from av_err
    return AudioSegment(data=np.ascontiguousarray(data).tobytes(), sample_width=2,
                        frame_rate=sr, channels=data.shape[1])


def export_mp3(seg: AudioSegment, path: str) -> None:
    """Encode *seg* as constant-bitrate MP3 (about 192 kbps, or 160 below 32 kHz)."""
    if seg.frame_rate not in _MP3_RATES:
        seg = seg.set_frame_rate(44100)
    seg = seg.set_sample_width(2)
    samples = np.array(seg.get_array_of_samples(), dtype=np.int16)
    if seg.channels > 1:
        samples = samples.reshape(-1, seg.channels)
    # libsndfile maps the level linearly onto the stream's bitrate range:
    # 32-320 kbps for MPEG-1 rates, 8-160 kbps below 32 kHz.
    level = 0.45 if seg.frame_rate >= 32000 else 0.0
    sf.write(path, samples, seg.frame_rate, format="MP3", subtype="MPEG_LAYER_III",
             compression_level=level, bitrate_mode="CONSTANT")
