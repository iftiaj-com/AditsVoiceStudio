"""
core/synth.py
─────────────
Synthesizer: one place that turns EnginePanel.get_settings() values into an
engine.synthesize() call.

Used by generation (core.pipeline), the Clone Voice tools and the voice match.
No UI imports, so it works the same under any toolkit and from any thread.
"""

import threading


class Synthesizer:

    def __init__(self, engines):
        """engines: the available engine instances (engines.registry.get_available_engines())."""
        self.engine_map = {e.name: e for e in engines}
        # Engines are not known to be thread-safe, and generation, previews and
        # the voice match can overlap, so one synthesize() call at a time.
        self.lock = threading.Lock()

    @staticmethod
    def device_for(eng_settings) -> str:
        from core.gpu import cuda_available     # no torch import for engines that never use it
        return "cuda" if eng_settings.get("use_gpu") and cuda_available() else "cpu"

    @staticmethod
    def engine_kwargs(eng_settings, device_str, status_cb, check_cancel) -> dict:
        """Keyword arguments for engine.synthesize() from EnginePanel.get_settings()."""
        engine_name = eng_settings["engine_name"]
        kwargs = dict(
            device=device_str,
            status_cb=status_cb,
            check_cancel=check_cancel,
            lang_key=eng_settings["gtts_lang_key"],
            voice_key=(
                eng_settings["edge_voice_key"]   if "Edge"    in engine_name else
                eng_settings["kokoro_voice_key"] if "Kokoro"  in engine_name else
                eng_settings["piper_voice_key"]  if "Piper"   in engine_name else
                eng_settings["kokoro_voice_key"]  # fallback
            ),
            melo_voice_cfg=eng_settings["melo_voice_cfg"],
            melo_speed=eng_settings["melo_speed"],
            rate=eng_settings["edge_rate"],
            kokoro_speed=eng_settings["kokoro_speed"],
            kokoro_lang_code=eng_settings.get("kokoro_lang_code", "a"),
            kokoro_subtitle=eng_settings.get("kokoro_subtitle", False),
            voice_id=eng_settings["pyttsx3_voice_id"],
            volume=eng_settings["pyttsx3_volume"],
        )
        if "pyttsx3" in engine_name:
            kwargs["rate"] = eng_settings["pyttsx3_rate"]
        return kwargs

    def needs_warm_up(self, eng_settings) -> bool:
        """True if the engine in *eng_settings* has something to load ahead (warm_up())."""
        from engines.base import BaseTTSEngine
        engine = self.engine_map.get(eng_settings.get("engine_name"))
        return engine is not None and type(engine).warm_up is not BaseTTSEngine.warm_up

    def warm_up(self, eng_settings, status_cb=None) -> bool:
        """Load the engine and voice in *eng_settings* ahead of the first synthesize().

        Holds the lock, so a Generate started meanwhile waits for the load
        instead of loading the same model a second time. False if the engine
        has nothing to load or is not available.
        """
        if not self.needs_warm_up(eng_settings):
            return False
        engine = self.engine_map[eng_settings["engine_name"]]
        kwargs = self.engine_kwargs(eng_settings, self.device_for(eng_settings),
                                    status_cb or (lambda m: None), lambda: False)
        with self.lock:
            engine.warm_up(**kwargs)
        return True

    def release_gpu(self):
        """Free the GPU memory the app's own models hold (before RVC training).

        Kokoro on the GPU stays loaded for the whole session, and MODEL_CACHE
        keeps MeloTTS, Piper and Whisper for 20 idle minutes: about 1.5 GB of a
        4 GB card with all three, which training needs more than the app does.
        """
        from core.model_cache import MODEL_CACHE
        with self.lock:
            for engine in self.engine_map.values():
                engine.release_gpu()
            MODEL_CACHE.clear()
        import gc
        import sys
        gc.collect()
        torch = sys.modules.get("torch")
        if torch is not None and torch.cuda.is_available():
            torch.cuda.empty_cache()

    def synthesize(self, text, tmp_dir, eng_settings, status_cb=None, check_cancel=None) -> dict:
        """Speak *text* with the engine and voice in *eng_settings*; returns the engine's result dict.

        Raises RuntimeError when the engine named in eng_settings is not available.
        """
        engine = self.engine_map.get(eng_settings["engine_name"])
        if engine is None:
            raise RuntimeError(f"Engine '{eng_settings['engine_name']}' not found.")
        kwargs = self.engine_kwargs(eng_settings, self.device_for(eng_settings),
                                    status_cb or (lambda m: None),
                                    check_cancel or (lambda: False))
        with self.lock:
            return engine.synthesize(text, tmp_dir, **kwargs)
