"""
engines/edge_engine.py  –  Microsoft Edge TTS adapter
"""
import os
import asyncio
import importlib.util
from engines.base import BaseTTSEngine
from core.constants import EDGE_VOICES

# Imported on first use; checking for the package is enough at start.
_AVAILABLE = importlib.util.find_spec("edge_tts") is not None


class EdgeEngine(BaseTTSEngine):
    name = "Edge TTS (Neural Voices)"

    @staticmethod
    def is_available() -> bool:
        return _AVAILABLE

    def synthesize(self, text: str, tmp_dir: str, **kwargs) -> dict:
        status_cb = kwargs.get("status_cb", None)
        if status_cb:
            status_cb("Requesting speech from Microsoft Edge Neural API...")

        voice_key = kwargs.get("voice_key", list(EDGE_VOICES.keys())[0])
        rate_val  = kwargs.get("rate", 0)
        voice_str = EDGE_VOICES[voice_key]
        rate_str  = f"{rate_val:+d}%"
        mp3_path  = os.path.join(tmp_dir, "output.mp3")
        import edge_tts
        communicate = edge_tts.Communicate(text, voice_str, rate=rate_str)
        asyncio.run(communicate.save(mp3_path))
        return {"playback_file": mp3_path, "save_file": mp3_path, "save_ext": ".mp3"}
