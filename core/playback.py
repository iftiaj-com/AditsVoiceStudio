"""
core/playback.py
────────────────
Player: plays the generated speech on pygame.mixer.music.

No thread and no UI: the UI polls is_playing() and fraction() from a timer
(Tk after(), or a QTimer later), so nothing calls into widgets off their thread.
"""

import os
import time
import wave


class Player:

    def __init__(self):
        self._playing  = False
        self._start    = 0.0
        self._duration = 1.0   # seconds

    @staticmethod
    def duration_of(path) -> float:
        """Length of *path* in seconds; a rough guess for non-WAV files."""
        try:
            if path.endswith(".wav"):
                with wave.open(path, "r") as wf:
                    return wf.getnframes() / wf.getframerate()
            return os.path.getsize(path) / 4000
        except Exception:
            return 10.0

    def play(self, path):
        import pygame
        if self._playing:
            pygame.mixer.music.stop()
        pygame.mixer.music.load(path)
        pygame.mixer.music.play()
        self._duration = max(self.duration_of(path), 0.001)
        self._start    = time.time()
        self._playing  = True

    def stop(self):
        """Stop playback; returns True if something was playing."""
        if not self._playing:
            return False
        self._playing = False
        try:
            import pygame
            pygame.mixer.music.stop()
        except Exception:
            pass
        return True

    def is_playing(self) -> bool:
        """True until the audio ends or stop() is called."""
        if not self._playing:
            return False
        try:
            import pygame
            busy = pygame.mixer.music.get_busy()
        except Exception:
            busy = False
        if not busy:
            self._playing = False
        return self._playing

    def fraction(self) -> float:
        """How far playback has got, 0.0 to 1.0."""
        return min((time.time() - self._start) / self._duration, 1.0)
