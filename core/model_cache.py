"""
core/model_cache.py
────────────────────
Thread-safe, idle-expiring cache for heavy TTS / ASR models.

Engines register a model under a key with a factory; repeated synthesis
requests reuse the loaded instance instead of paying multi-second load
times on every click. A background sweeper evicts entries that have been
idle longer than `ttl_seconds` (the app uses 20 minutes) so RAM/VRAM is
released when the user walks away.

Usage:
    from core.model_cache import MODEL_CACHE
    model = MODEL_CACHE.get(("melo", "EN", "cuda"),
                            lambda: MeloTTS(language="EN", device="cuda"))
    MODEL_CACHE.clear()            # manual "free memory now"
    MODEL_CACHE.clear(("melo", "EN", "cuda"))
"""

import gc
import time
import threading


def _release_accelerator_memory():
    """Return CUDA memory to the OS after evicting models (no-op without torch)."""
    gc.collect()
    try:
        import sys
        torch = sys.modules.get("torch")    # nothing to free if no model loaded torch
        if torch is not None and torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass


class _Entry:
    __slots__ = ("value", "last_used")

    def __init__(self, value):
        self.value = value
        self.last_used = time.monotonic()


class ModelCache:
    def __init__(self, ttl_seconds=300.0, sweep_interval=30.0):
        self._ttl = ttl_seconds
        self._sweep_interval = sweep_interval
        self._entries = {}
        self._lock = threading.RLock()
        self._sweeper_started = False

    def get(self, key, factory):
        """Return the cached model for *key*, loading it via *factory* if needed.

        The lock is held during factory() so concurrent callers never
        double-load the same multi-GB model.
        """
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                entry = _Entry(factory())
                self._entries[key] = entry
                self._ensure_sweeper()
            entry.last_used = time.monotonic()
            return entry.value

    def touch(self, key):
        """Refresh the idle timer for *key* (e.g. during a long synthesis)."""
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                entry.last_used = time.monotonic()

    def clear(self, key=None):
        """Drop one entry (or all) and release RAM/VRAM immediately."""
        with self._lock:
            if key is None:
                evicted = bool(self._entries)
                self._entries.clear()
            else:
                evicted = self._entries.pop(key, None) is not None
        if evicted:
            _release_accelerator_memory()

    def _ensure_sweeper(self):
        if self._sweeper_started:
            return
        self._sweeper_started = True
        threading.Thread(target=self._sweep_loop, daemon=True,
                         name="model-cache-sweeper").start()

    def _sweep_loop(self):
        while True:
            time.sleep(self._sweep_interval)
            now = time.monotonic()
            with self._lock:
                stale = [k for k, e in self._entries.items()
                         if now - e.last_used > self._ttl]
                for k in stale:
                    del self._entries[k]
            if stale:
                _release_accelerator_memory()


# Single shared cache for the whole app.
# 20 minutes: with 5 a short break meant MeloTTS reloading (about 49 s cold)
# and Whisper (about 7 s). RVC training frees the cache first (Synthesizer.release_gpu).
MODEL_CACHE = ModelCache(ttl_seconds=1200.0)
