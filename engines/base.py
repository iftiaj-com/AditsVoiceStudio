"""
engines/base.py
───────────────
Abstract base class every TTS engine adapter must implement.

To add a new engine:
  1. Create engines/my_engine.py and subclass BaseTTSEngine.
  2. Implement `is_available()` and `synthesize()`.
  3. Register it in engines/registry.py.
"""

from abc import ABC, abstractmethod


class BaseTTSEngine(ABC):
    """Interface all TTS engine adapters must satisfy."""

    # Human-readable name shown in the UI dropdown
    name: str = "Unknown Engine"

    @staticmethod
    @abstractmethod
    def is_available() -> bool:
        """Return True if the engine's dependencies are installed."""
        ...

    @abstractmethod
    def synthesize(self, text: str, tmp_dir: str, **kwargs) -> dict:
        """
        Generate speech for *text* and write the result into *tmp_dir*.

        Parameters
        ----------
        text    : The text to synthesise.
        tmp_dir : A temporary directory path where output files can be written.
        **kwargs: Engine-specific settings (voices, rate, device, etc.)

        Returns
        -------
        dict with keys:
          playback_file : str  – path to the file used for playback
          save_file     : str  – path to the file offered for download
          save_ext      : str  – default file extension, e.g. ".mp3" or ".wav"
        """
        ...

    def warm_up(self, **kwargs) -> None:
        """Load what synthesize() with these kwargs will need, so the first
        Generate does not wait for it. Same kwargs as synthesize(). Optional:
        engines with nothing to load (online, system voices) keep this no-op."""
        return None

    def _speak_once(self, **kwargs) -> None:
        """A warm_up() body: speak "Hello." into a temp folder and drop it.

        Loading the model alone leaves work for the first real call (MeloTTS's
        BERT, Kokoro's phonemizer and CUDA kernels): MeloTTS's first Generate
        still took 6.2 s after loading only the model, against 1.1 s warm."""
        import shutil
        import tempfile
        d = tempfile.mkdtemp(prefix="adits_vs_warm_")
        try:
            self.synthesize("Hello.", d, **dict(kwargs, kokoro_subtitle=False))
        finally:
            shutil.rmtree(d, ignore_errors=True)
