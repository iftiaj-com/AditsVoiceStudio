"""
core/dictation.py  –  Live dictation: microphone → Silero VAD → Whisper, one phrase at a time

    mic (PyAudio) ─▶ 512-sample blocks at 16 kHz ─▶ PhraseCutter (Silero VAD)
                                                        │ a phrase ends after a 0.6 s pause
                                                        ▼
                     on_text(text) ◀── Whisper (the cached base model, English)
                     on_partial(text) ◀── the phrase so far, while it is spoken

Two daemon threads: capture (mic + VAD) and transcribe (Whisper), joined by a
queue, so speech keeps being captured while an earlier phrase is transcribed.
stop() closes the mic, sends the phrase in progress, and lets the transcribe
thread finish what is queued before on_state("stopped").

Live text: when no finished phrase is waiting, the transcribe thread
re-transcribes the phrase in progress about every PARTIAL_EVERY_S (longer if
a pass is slow) and sends it to on_partial(). Finished phrases always go
first. on_partial("") means the live text is gone (the phrase was dropped or
heard no words); on_text() replaces it.

The VAD is the Silero model that ships inside faster-whisper, run here one
block at a time (StreamVAD) instead of over a whole file. No Tk in here; the
callbacks run on the worker threads.
"""

import collections
import queue
import re
import threading
import time

import numpy as np

RATE = 16000
BLOCK = 512                 # Silero VAD window at 16 kHz: 32 ms
START_PROB = 0.5            # a phrase starts when speech probability reaches this
STOP_PROB = 0.35            # and a block below this counts as silence
PREROLL_S = 0.3             # audio kept from before the start, so first sounds are not clipped
END_SILENCE_S = 0.6         # the pause that ends a phrase
KEEP_SILENCE_S = 0.2        # silence left at the end of a phrase
MIN_SPEECH_S = 0.3          # shorter bursts (a cough, a click) are dropped
SOFT_MAX_S = 8.0            # past this, a shorter pause also ends the phrase
SOFT_SILENCE_S = 0.2
HARD_MAX_S = 12.0           # no pause by then: cut at the quietest block of the last CUT_WINDOW_S
CUT_WINDOW_S = 4.0
PARTIAL_EVERY_S = 0.8       # live text refresh; a pass on a GTX 1650 took 0.35 to 0.75 s
PARTIAL_SLOW = 1.5          # ...or this many times the last pass, when passes are slower
PARTIAL_MIN_S = 0.6         # speech needed before the first live text
LANGUAGE = "en"

_WORDY = re.compile(r"[A-Za-z0-9]")


class DictationError(RuntimeError):
    """The microphone could not be opened."""


class StreamVAD:
    """faster-whisper's Silero VAD model, one 512-sample block per call.

    Carries the model's h / c state and the 64-sample context between calls,
    as SileroVADModel.__call__ does between rows of a whole file.
    """

    CONTEXT = 64

    def __init__(self):
        from faster_whisper.vad import get_vad_model
        self._session = get_vad_model().session
        self.reset()

    def reset(self):
        self._h = np.zeros((1, 1, 128), dtype=np.float32)
        self._c = np.zeros((1, 1, 128), dtype=np.float32)
        self._ctx = np.zeros(self.CONTEXT, dtype=np.float32)

    def __call__(self, block) -> float:
        x = np.concatenate([self._ctx, block]).astype(np.float32)[None, :]
        out, self._h, self._c = self._session.run(None, {"input": x, "h": self._h, "c": self._c})
        self._ctx = np.asarray(block[-self.CONTEXT:], dtype=np.float32)
        return float(np.asarray(out).reshape(-1)[0])


class PhraseCutter:
    """Feeds 512-sample blocks through the VAD and returns each finished phrase."""

    def __init__(self, vad):
        self.vad = vad
        self._pre = collections.deque(maxlen=max(1, int(PREROLL_S * RATE / BLOCK)))
        self._cur = None            # blocks of the phrase in progress, or None between phrases
        self._silence = 0           # trailing silent blocks
        self._speech = 0            # speech blocks in the phrase
        self.phrase_id = 0          # changes whenever a new phrase begins

    @property
    def in_speech(self) -> bool:
        return self._cur is not None

    def live_blocks(self):
        """A copy of the blocks of the phrase in progress (empty between phrases)."""
        return list(self._cur) if self._cur is not None else []

    def feed(self, block):
        """One block in; a finished phrase (float32 array) out, or None."""
        p = self.vad(block)
        if self._cur is None:
            if p >= START_PROB:
                self._cur = list(self._pre) + [block]
                self._pre.clear()
                self._silence, self._speech = 0, 1
                self.phrase_id += 1
            else:
                self._pre.append(block)
            return None
        self._cur.append(block)
        if p < STOP_PROB:
            self._silence += 1
        else:
            self._silence = 0
            self._speech += 1
        dur = len(self._cur) * BLOCK / RATE
        sil = self._silence * BLOCK / RATE
        if dur >= HARD_MAX_S:
            return self._cut_quietest()
        if sil >= END_SILENCE_S or (dur >= SOFT_MAX_S and sil >= SOFT_SILENCE_S):
            return self._finish()
        return None

    def _cut_quietest(self):
        """Nonstop speech: end the phrase at the quietest block of the last
        CUT_WINDOW_S (most likely a gap between words); the blocks after it
        start the next phrase, so no audio is lost."""
        n = len(self._cur)
        first = max(1, n - int(CUT_WINDOW_S * RATE / BLOCK))
        rms = [float(np.sqrt(np.mean(np.square(b)))) for b in self._cur[first:]]
        cut = first + int(np.argmin(rms)) + 1
        phrase, rest = np.concatenate(self._cur[:cut]), self._cur[cut:]
        self._cur, self._silence, self._speech = rest, 0, len(rest)
        self.phrase_id += 1                 # the rest is a new phrase
        return phrase

    def flush(self):
        """The phrase in progress, if it holds enough speech (on stop)."""
        return self._finish() if self._cur is not None else None

    def _finish(self):
        cur, speech = self._cur, self._speech
        drop = max(0, self._silence - int(KEEP_SILENCE_S * RATE / BLOCK))
        if drop:
            cur = cur[:-drop]
        self._cur, self._silence, self._speech = None, 0, 0
        if not cur or speech * BLOCK / RATE < MIN_SPEECH_S:
            return None
        return np.concatenate(cur)


def transcribe_phrase(audio, language=LANGUAGE, beam_size=5) -> str:
    """Whisper text for one phrase; "" when it heard no words.

    Segments Whisper itself marks as probably not speech are dropped; that is
    where "Thank you." on a breath or a click usually comes from. No prompt of
    the text so far: in tests it made Whisper skip words after a cut.
    Live text uses beam_size=1.
    """
    from core.captions import clean_line
    from core.subtitles import get_whisper_model
    segments, _info = get_whisper_model().transcribe(
        audio, language=language, beam_size=beam_size, vad_filter=False,
        condition_on_previous_text=False, without_timestamps=True)
    parts = [s.text for s in segments if not (s.no_speech_prob > 0.6 and s.avg_logprob < -1.0)]
    text = clean_line(" ".join(parts))
    return text if _WORDY.search(text) else ""


class Dictation:
    """Microphone dictation. Callbacks run on worker threads:

    on_text(text)    one finished phrase
    on_partial(text) the phrase in progress so far ("" clears it); optional
    on_state(state)  "loading", "listening", "speech", "writing", "finishing", "stopped"
    on_error(msg)    the mic or Whisper failed; "stopped" follows

    *source* (tests) replaces the microphone: an iterable of float32 16 kHz arrays.
    """

    def __init__(self, on_text, on_state=None, on_error=None, language=LANGUAGE, source=None,
                 on_partial=None):
        self._on_text = on_text
        self._on_partial = on_partial
        self._on_state = on_state or (lambda s: None)
        self._on_error = on_error or (lambda m: None)
        self.language = language
        self._source = source
        self._stop = threading.Event()
        self._q = queue.Queue()
        self._lock = threading.Lock()
        self._ready = False             # Whisper loaded
        self._in_speech = False
        self._pending = 0               # phrases queued or being transcribed
        self._capturing = False
        self._last_state = None
        self._cutter = None             # the capture thread's PhraseCutter (read under _lock)
        self._next_partial = 0.0        # monotonic time the next live pass may start
        self._last_live = None          # (phrase_id, blocks) of the last live pass

    def start(self):
        self._capturing = True
        self._emit()
        threading.Thread(target=self._transcribe_loop, daemon=True, name="dictation-whisper").start()
        self._capture = threading.Thread(target=self._capture_loop, daemon=True, name="dictation-mic")
        self._capture.start()

    def stop(self, wait=0.0):
        """Close the mic; queued phrases are still written, then "stopped".
        wait > 0 blocks up to that many seconds until the mic is closed (app exit)."""
        self._stop.set()
        capture = getattr(self, "_capture", None)
        if wait and capture is not None and capture is not threading.current_thread():
            capture.join(wait)

    # ── state ──────────────────────────────────────────────────────
    def _emit(self, final=False):
        with self._lock:
            if final:
                state = "stopped"
            elif not self._ready:
                state = "loading"
            elif not self._capturing:
                state = "finishing"
            elif self._in_speech:
                state = "speech"
            elif self._pending:
                state = "writing"
            else:
                state = "listening"
            if state == self._last_state:
                return
            self._last_state = state
        try:
            self._on_state(state)
        except Exception as e:   # noqa: BLE001
            print(f"Dictation state callback failed: {e}")

    def _send_partial(self, text):
        if self._on_partial is None:
            return
        try:
            self._on_partial(text)
        except Exception as e:   # noqa: BLE001
            print(f"Dictation live text callback failed: {e}")

    def _fail(self, msg):
        self._stop.set()
        try:
            self._on_error(msg)
        except Exception as e:   # noqa: BLE001
            print(f"Dictation error callback failed: {e}")

    # ── capture thread ─────────────────────────────────────────────
    def _capture_loop(self):
        cutter = None
        try:
            cutter = PhraseCutter(StreamVAD())
            with self._lock:
                self._cutter = cutter
            blocks = self._source if self._source is not None else self._mic_blocks()
            for block in blocks:
                if self._stop.is_set():
                    break
                with self._lock:            # the transcribe thread copies the live phrase
                    phrase = cutter.feed(block)
                if phrase is not None:
                    self._queue(phrase)
                elif self._in_speech and not cutter.in_speech:
                    self._send_partial("")  # the phrase was too short: its live text goes
                if cutter.in_speech != self._in_speech:
                    self._in_speech = cutter.in_speech
                    self._emit()
        except DictationError as e:
            self._fail(str(e))
        except Exception as e:   # noqa: BLE001
            print(f"Dictation capture failed: {e}")
            self._fail(f"The microphone stopped: {e}")
        finally:
            if cutter is not None:
                with self._lock:
                    phrase = cutter.flush()
                if phrase is not None:
                    self._queue(phrase)
                elif self._in_speech:
                    self._send_partial("")
            self._in_speech = False
            self._capturing = False
            self._q.put(None)               # the transcribe thread ends after the queue
            self._emit()

    def _queue(self, phrase):
        with self._lock:
            self._pending += 1
        self._q.put(phrase)

    def _mic_blocks(self):
        """512-sample float32 blocks at 16 kHz from the default input device."""
        try:
            import pyaudio
        except ImportError as e:
            raise DictationError("Dictation needs the PyAudio package.") from e
        pa = pyaudio.PyAudio()
        stream = None
        try:
            try:
                info = pa.get_default_input_device_info()
            except (IOError, OSError) as e:
                raise DictationError("No microphone was found. Connect one and try again.") from e
            index, native = info["index"], int(info["defaultSampleRate"]) or 44100
            raw = queue.Queue()

            def callback(data, frames, time_info, status):
                raw.put(data)
                return (None, pyaudio.paContinue)

            err, rate, channels = None, None, None
            # 16 kHz first (MME resamples for us); else the device's own rate
            for r in dict.fromkeys((RATE, native)):
                for ch in (1, 2):
                    try:
                        stream = pa.open(format=pyaudio.paInt16, channels=ch, rate=r, input=True,
                                         input_device_index=index,
                                         frames_per_buffer=max(256, int(BLOCK * r / RATE)),
                                         stream_callback=callback)
                        rate, channels = r, ch
                        break
                    except (IOError, OSError, ValueError) as e:
                        err, stream = e, None
                if stream is not None:
                    break
            if stream is None:
                raise DictationError(f"Could not open the microphone ({info['name']}): {err}")
            stream.start_stream()
            buf = np.zeros(0, dtype=np.float32)
            while not self._stop.is_set():
                try:
                    data = raw.get(timeout=0.1)
                except queue.Empty:
                    continue
                x = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
                if channels == 2:
                    x = x.reshape(-1, 2).mean(axis=1)
                if rate != RATE:
                    from scipy.signal import resample_poly
                    x = resample_poly(x, RATE, rate).astype(np.float32)
                buf = np.concatenate([buf, x])
                while len(buf) >= BLOCK:
                    yield buf[:BLOCK]
                    buf = buf[BLOCK:]
        finally:
            if stream is not None:
                try:
                    stream.stop_stream()
                    stream.close()
                except Exception:
                    pass
            pa.terminate()

    # ── transcribe thread ──────────────────────────────────────────
    def _transcribe_loop(self):
        broken = False
        try:
            from core.subtitles import get_whisper_model
            get_whisper_model()             # load now, not on the first phrase
        except Exception as e:   # noqa: BLE001
            print(f"Dictation could not load Whisper: {e}")
            self._fail(f"The speech model could not be loaded: {e}")
            broken = True
        self._ready = True
        self._emit()
        # Phrases queued before stop() are still written; after a failure the
        # queue is only drained.
        while True:
            try:
                phrase = self._q.get(timeout=0.05)
            except queue.Empty:
                if not broken:
                    self._live_pass()       # only when no finished phrase is waiting
                continue
            if phrase is None:
                break
            self._emit()
            try:
                if not broken:
                    text = transcribe_phrase(phrase, self.language)
                    if text:
                        self._on_text(text)     # replaces the live text
                    else:
                        self._send_partial("")
            except Exception as e:   # noqa: BLE001
                print(f"Dictation transcription failed: {e}")
                self._fail(f"Transcription failed: {e}")
                broken = True
            finally:
                with self._lock:
                    self._pending -= 1
                self._emit()
        self._emit(final=True)

    def _live_pass(self):
        """Transcribe the phrase in progress (fast settings) and send it as live text."""
        if self._on_partial is None or time.monotonic() < self._next_partial:
            return
        with self._lock:
            cutter = self._cutter
            if cutter is None:
                return
            blocks, pid = cutter.live_blocks(), cutter.phrase_id
        key = (pid, len(blocks))
        if len(blocks) * BLOCK / RATE < PARTIAL_MIN_S or key == self._last_live:
            return
        self._last_live = key
        t0 = time.monotonic()
        try:
            text = transcribe_phrase(np.concatenate(blocks), self.language, beam_size=1)
        except Exception as e:   # noqa: BLE001
            print(f"Dictation live pass failed: {e}")
            self._next_partial = t0 + 5.0
            return
        took = time.monotonic() - t0
        self._next_partial = t0 + max(PARTIAL_EVERY_S, PARTIAL_SLOW * took)
        with self._lock:                    # still the same phrase, and nothing finished meanwhile?
            current = cutter.in_speech and cutter.phrase_id == pid
        if text and current and self._q.empty():
            self._send_partial(text)
