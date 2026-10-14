"""
engines/piper_engine.py  –  Piper TTS adapter
"""
import os
import wave
import importlib.util
from engines.base import BaseTTSEngine
from core import paths
from core.model_cache import MODEL_CACHE

# piper (onnxruntime) is imported on first use.
_AVAILABLE = importlib.util.find_spec("piper") is not None


class PiperEngine(BaseTTSEngine):
    name = "Piper TTS (Local Fast)"

    @staticmethod
    def is_available() -> bool:
        return _AVAILABLE

    def synthesize(self, text: str, tmp_dir: str, **kwargs) -> dict:
        from piper import PiperVoice
        status_cb  = kwargs.get("status_cb", None)

        if status_cb:
            status_cb("Loading Piper voice…")

        voice_id = kwargs.get("voice_key", "en_US-ljspeech-medium")
        model_name = f"{voice_id}.onnx"

        default_model = os.path.join(str(paths.bundle_dir()), "models", "piper", model_name)
        model_path = kwargs.get("model_path", default_model)

        if not os.path.exists(model_path):
            raise RuntimeError(
                f"Piper model missing at {model_path}! Download an ONNX model file first."
            )

        def _load():
            from piper.phonemize_espeak import ESPEAK_DATA_DIR
            return PiperVoice.load(
                model_path,
                # espeak-ng fails on non-ASCII data paths (see paths.ascii_path).
                espeak_data_dir=paths.ascii_path(ESPEAK_DATA_DIR),
                # Only used by Chinese voices; default is the CWD.
                download_dir=str(paths.user_dir("models") / "piper") if paths.FROZEN else None,
            )

        # Cached per model file; idle-evicted by MODEL_CACHE after 5 min.
        voice    = MODEL_CACHE.get(("piper", model_path), _load)
        wav_path = os.path.join(tmp_dir, "output.wav")
        with wave.open(wav_path, "wb") as w:
            voice.synthesize_wav(text, w)

        return {"playback_file": wav_path, "save_file": wav_path, "save_ext": ".wav"}
