"""
engines/melo_engine.py  –  MeloTTS adapter
"""
import os
import sys
import logging
import importlib.util
from engines.base import BaseTTSEngine
from core import paths
from core.model_cache import MODEL_CACHE

logger = logging.getLogger("AditsVoiceStudio")

# In frozen PyInstaller app, ensure NLTK can locate bundled nltk_data inside _internal
if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
    try:
        import nltk
        bundle_nltk = os.path.join(sys._MEIPASS, "nltk_data")
        if os.path.exists(bundle_nltk) and bundle_nltk not in nltk.data.path:
            nltk.data.path.insert(0, bundle_nltk)
    except Exception as e:
        logger.debug("Could not configure bundled NLTK data path: %s", e)

# `import melo.api` is deferred to first use. Importing it loads six BERT
# tokenizers from Hugging Face at module level, so doing it at startup made
# the app reach the network before its window opened, and dropped MeloTTS
# from the list on any PC that was offline at launch.
_AVAILABLE = importlib.util.find_spec("melo") is not None
_MELO_TTS = None


def _melo_tts_class():
    global _MELO_TTS
    if _MELO_TTS is None:
        try:
            from melo.api import TTS
        except Exception as e:
            _write_error_log(e)
            logger.warning("MeloTTS failed to load: %s", e, exc_info=True)
            raise RuntimeError(
                "MeloTTS could not start. On first use it downloads its language "
                f"models from Hugging Face, so an internet connection is needed.\n\n{e}"
            ) from e
        _MELO_TTS = TTS
    return _MELO_TTS


def _write_error_log(err):
    try:
        import traceback
        err_file = os.path.join(str(paths.logs_dir()), "melo_startup_error.log")
        with open(err_file, "w", encoding="utf-8") as f:
            f.write(f"MeloTTS failed to load:\n{err}\n\nTraceback:\n")
            traceback.print_exc(file=f)
    except Exception:
        pass


_NLTK_READY = False


def _ensure_nltk_resources(status_cb=None):
    """Check/download NLTK data once per process instead of on every synthesis."""
    global _NLTK_READY
    if _NLTK_READY:
        return
    import nltk
    try:
        nltk.data.find('taggers/averaged_perceptron_tagger_eng')
        nltk.data.find('corpora/cmudict')
    except LookupError:
        if status_cb:
            status_cb("Downloading NLTK resources...")
        # Never into the (read-only) bundle: use the per-user data folder.
        target = str(paths.user_dir("nltk_data")) if paths.FROZEN else None
        if target and target not in nltk.data.path:
            nltk.data.path.append(target)
        nltk.download('averaged_perceptron_tagger_eng', quiet=True, download_dir=target)
        nltk.download('averaged_perceptron_tagger', quiet=True, download_dir=target)
        nltk.download('cmudict', quiet=True, download_dir=target)
    _NLTK_READY = True


class MeloEngine(BaseTTSEngine):
    name = "MeloTTS (Multi-Lingual)"

    @staticmethod
    def is_available() -> bool:
        return _AVAILABLE

    def _model(self, **kwargs):
        device_str = kwargs.get("device", "cpu")
        status_cb  = kwargs.get("status_cb", None)
        _ensure_nltk_resources(status_cb)
        language = kwargs.get("melo_voice_cfg", {}).get("language", "EN")

        def _load():
            if status_cb:
                status_cb(f"Initializing MeloTTS ({language}). First use of a language "
                          "downloads several hundred MB, please wait...")
            return _melo_tts_class()(language=language, device=device_str)

        # Cached per (language, device); idle-evicted by MODEL_CACHE.
        return MODEL_CACHE.get(("melo", language, device_str), _load)

    def warm_up(self, **kwargs):
        self._speak_once(**kwargs)

    def release_gpu(self):
        """MeloTTS keeps each language's BERT in a module global (melo.text.*_bert:
        `model` or `models`), outside MODEL_CACHE: about 0.5 GB on the GPU for English."""
        for name, mod in list(sys.modules.items()):
            if name.startswith("melo.text.") and name.endswith("_bert"):
                if getattr(mod, "model", None) is not None:
                    mod.model = None
                if isinstance(getattr(mod, "models", None), dict):
                    mod.models.clear()

    def synthesize(self, text: str, tmp_dir: str, **kwargs) -> dict:
        device_str = kwargs.get("device", "cpu")
        status_cb  = kwargs.get("status_cb", None)

        if status_cb:
            status_cb(f"Generating MeloTTS speech on {device_str}…")

        voice_cfg  = kwargs.get("melo_voice_cfg", {"language": "EN", "speaker": "EN-US"})
        speaker    = voice_cfg.get("speaker", "EN-US")
        model       = self._model(**kwargs)
        speaker_ids = dict(model.hps.data.spk2id)

        # Fallback to first available speaker if the requested one isn't in the model
        spk_id = speaker_ids.get(speaker, list(speaker_ids.values())[0])

        speed = kwargs.get("melo_speed", 1.0)
        wav_path = os.path.join(tmp_dir, "output.wav")
        model.tts_to_file(text, spk_id, wav_path, speed=speed)
        # The first call imports nltk, and a traceback saved during that import
        # keeps this frame alive, and with it `model` (about 0.25 GB of GPU
        # memory that MODEL_CACHE.clear() could then not free).
        del model

        return {"playback_file": wav_path, "save_file": wav_path, "save_ext": ".wav"}
