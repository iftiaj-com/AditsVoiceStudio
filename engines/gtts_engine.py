"""
engines/gtts_engine.py  –  Google TTS adapter
"""
import os
import importlib.util
from engines.base import BaseTTSEngine
from core.constants import GTTS_LANGUAGES

# Imported on first use; checking for the package is enough at start.
_AVAILABLE = importlib.util.find_spec("gtts") is not None


class GTTSEngine(BaseTTSEngine):
    name = "gTTS (Google Translate)"

    @staticmethod
    def is_available() -> bool:
        return _AVAILABLE

    def synthesize(self, text: str, tmp_dir: str, **kwargs) -> dict:
        status_cb = kwargs.get("status_cb", None)
        if status_cb:
            status_cb("Requesting speech from Google Translate TTS API...")

        lang_key = kwargs.get("lang_key", "English (US)")
        lang_cfg = GTTS_LANGUAGES.get(lang_key, {"lang": "en", "tld": "us"})
        from gtts import gTTS as GoogleTTS
        tts = GoogleTTS(text=text, lang=lang_cfg["lang"], tld=lang_cfg["tld"])
        mp3_path = os.path.join(tmp_dir, "output.mp3")
        tts.save(mp3_path)
        return {"playback_file": mp3_path, "save_file": mp3_path, "save_ext": ".mp3"}
