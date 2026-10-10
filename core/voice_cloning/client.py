"""
core/voice_cloning/client.py
─────────────────────────────
The app's side of the worker protocol (see vc_runtime/worker.py).

One worker process per (engine, role):
    ("rvc", "convert")     RVC conversion + model inspection
    ("seedvc", "convert")  Quick (Seed-VC) conversion
    ("rvc", "train")       one training run at a time

GPU policy for a 4 GB card: only one convert worker is alive at a time
(starting one stops the other), training stops every convert worker, and
conversions are refused while training runs. Workers exit on their own
after `idle_seconds` without work, which frees their GPU memory.

All methods block; call them from a worker thread, never the Tk main thread.
"""

import json
import queue
import subprocess
import threading
import time
import uuid

from core.voice_cloning import runtime
from core.voice_cloning.errors import VCBusy, VCCancelled, VCNotInstalled, VCWorkerError

def _abs(path):
    """Workers run with the upstream repo as cwd, so every path sent must be absolute."""
    import os
    return os.path.abspath(str(path)) if path else path


_CANCEL_GRACE = 8.0      # seconds a worker gets to report a cancel before it is killed
_READY_TIMEOUT = 90.0


class _Worker:
    def __init__(self, engine, role, idle_seconds):
        self.engine = engine
        self.role = role
        self.idle_seconds = idle_seconds
        self.proc = None
        self._pending = {}
        self._pending_lock = threading.Lock()
        self._send_lock = threading.Lock()
        self.busy = threading.Lock()          # one request at a time
        self._ready = threading.Event()
        self.last_used = time.monotonic()

    @property
    def label(self):
        return f"{runtime.ENGINES[self.engine]['label']} {self.role} worker"

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self):
        cmd = runtime.worker_cmd(self.engine, self.role, self.idle_seconds)
        runtime.logs_dir().mkdir(parents=True, exist_ok=True)
        self._ready.clear()
        self.proc = subprocess.Popen(
            cmd, cwd=str(runtime.repo_dir(self.engine)), env=runtime.child_env(),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=runtime.CREATE_NO_WINDOW)
        threading.Thread(target=self._read, args=(self.proc,), daemon=True,
                         name=f"vc-{self.engine}-{self.role}").start()
        if not self._ready.wait(_READY_TIMEOUT) or not self.alive():
            self.kill()
            raise VCWorkerError("generic", f"The {self.label} did not start.",
                                hint=f"See {runtime.worker_log(self.engine, self.role)}")

    def _read(self, proc):
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("event") == "ready":
                self._ready.set()
                continue
            with self._pending_lock:
                q = self._pending.get(ev.get("id"))
            if q is not None:
                q.put(ev)
        # process ended: wake every waiter
        with self._pending_lock:
            waiters = list(self._pending.values())
        for q in waiters:
            q.put({"event": "exit"})

    def send(self, obj):
        with self._send_lock:
            try:
                self.proc.stdin.write(json.dumps(obj) + "\n")
                self.proc.stdin.flush()
            except (OSError, ValueError, AttributeError) as e:
                raise VCWorkerError("generic", f"The {self.label} is not responding ({e}).")

    def request(self, payload, cancel_event=None, on_event=None, timeout=None):
        rid = uuid.uuid4().hex[:12]
        q = queue.Queue()
        with self.busy:
            with self._pending_lock:
                self._pending[rid] = q
            try:
                self.last_used = time.monotonic()
                self.send(dict(payload, id=rid))
                deadline = time.monotonic() + timeout if timeout else None
                cancel_sent_at = None
                while True:
                    try:
                        ev = q.get(timeout=0.25)
                    except queue.Empty:
                        if cancel_event is not None and cancel_event.is_set():
                            if cancel_sent_at is None:
                                cancel_sent_at = time.monotonic()
                                try:
                                    self.send({"cmd": "cancel", "target": rid})
                                except VCWorkerError:
                                    pass
                            elif time.monotonic() - cancel_sent_at > _CANCEL_GRACE:
                                # in-process conversions cannot be interrupted; stop the worker
                                self.kill()
                                raise VCCancelled()
                        if deadline and time.monotonic() > deadline:
                            self.kill()
                            raise VCWorkerError("generic", f"The {self.label} timed out.")
                        continue
                    kind = ev.get("event")
                    if kind == "done":
                        return ev.get("result") or {}
                    if kind == "error":
                        if ev.get("kind") == "cancelled":
                            raise VCCancelled()
                        raise VCWorkerError(ev.get("kind", "generic"), ev.get("message", "Unknown error"),
                                            hint=ev.get("hint"))
                    if kind == "exit":
                        if cancel_event is not None and cancel_event.is_set():
                            raise VCCancelled()
                        raise VCWorkerError("generic", f"The {self.label} stopped unexpectedly.",
                                            hint=f"See {runtime.worker_log(self.engine, self.role)}")
                    if on_event is not None:
                        try:
                            on_event(ev)
                        except Exception as cb_err:   # a UI callback must not kill the request
                            print(f"voice cloning event callback failed: {cb_err}")
            finally:
                with self._pending_lock:
                    self._pending.pop(rid, None)
                self.last_used = time.monotonic()

    def shutdown(self, wait=3.0):
        if not self.alive():
            return
        try:
            self.send({"cmd": "shutdown"})
            self.proc.wait(timeout=wait)
        except Exception:
            pass
        self.kill()

    def kill(self):
        if self.proc is not None and self.proc.poll() is None:
            runtime.kill_tree(self.proc.pid)
            try:
                self.proc.wait(timeout=10)
            except Exception:
                pass


class VCClient:
    def __init__(self, idle_seconds=300):
        self.idle_seconds = idle_seconds
        self._workers = {}
        self._lock = threading.RLock()
        self._training = threading.Event()

    # ── worker management ──────────────────────────────────────────
    @property
    def training_active(self) -> bool:
        return self._training.is_set()

    def _worker(self, engine, role) -> _Worker:
        if not runtime.is_installed(engine):
            raise VCNotInstalled(f"{runtime.ENGINES[engine]['label']} is not installed.",
                                 hint="Use Install… in the Clone Voice section.")
        with self._lock:
            # Free the GPU for this worker: one convert worker at a time.
            for (e, r), w in list(self._workers.items()):
                if (e, r) == (engine, role):
                    continue
                if role == "train" or (role == "convert" and r == "convert"):
                    w.shutdown()
            w = self._workers.get((engine, role))
            if w is None:
                w = _Worker(engine, role, self.idle_seconds if role == "convert" else 60)
                self._workers[(engine, role)] = w
            if not w.alive():
                w.start()
            return w

    def is_warm(self, engine) -> bool:
        """True when the engine's convert worker is running (no model-load wait)."""
        with self._lock:
            w = self._workers.get((engine, "convert"))
        return w is not None and w.alive()

    def shutdown(self, engine=None):
        with self._lock:
            for (e, _r), w in list(self._workers.items()):
                if engine is None or e == engine:
                    w.shutdown()

    def cancel_training(self):
        """Ask the running training to stop. Returns at once."""
        with self._lock:
            w = self._workers.get(("rvc", "train"))
        if w is not None and w.alive():
            try:
                w.send({"cmd": "cancel"})
            except VCWorkerError:
                w.kill()

    # ── requests ───────────────────────────────────────────────────
    def selfcheck(self, engine) -> dict:
        return self._worker(engine, "convert").request({"cmd": "selfcheck"})

    def inspect_rvc(self, pth) -> dict:
        """Read an RVC model's metadata on the CPU. Allowed while training."""
        role = "train" if self.training_active else "convert"
        if role == "train":
            # never touch the training worker; use a short-lived CPU-only check
            w = _Worker("rvc", "inspect", 30)
            w.start()
            try:
                return w.request({"cmd": "inspect", "pth": _abs(pth)}, timeout=120)
            finally:
                w.shutdown()
        return self._worker("rvc", "convert").request({"cmd": "inspect", "pth": _abs(pth)}, timeout=120)

    def convert(self, profile, in_wav, out_wav, *, pitch=None, index_rate=None, protect=None,
                diffusion_steps=None, cancel_event=None, status_cb=None) -> dict:
        """Convert in_wav into profile's voice, writing out_wav. Returns the worker result."""
        if self.training_active:
            raise VCBusy("A voice is being trained, so cloned voices are unavailable until it finishes.")
        if not profile.is_ready:
            raise VCWorkerError("not_found", f"Voice '{profile.name}' is not ready ({profile.status}).")
        if profile.mode == "rvc":
            payload = {"cmd": "convert", "in": _abs(in_wav), "out": _abs(out_wav),
                       "pth": profile.pth_abs, "index": profile.index_abs,
                       "pitch": int(profile.pitch if pitch is None else pitch),
                       "index_rate": float(profile.index_rate if index_rate is None else index_rate),
                       "protect": float(profile.protect if protect is None else protect)}
        else:
            payload = {"cmd": "convert", "in": _abs(in_wav), "out": _abs(out_wav),
                       "ref": profile.reference_abs,
                       "diffusion_steps": int(profile.diffusion_steps if diffusion_steps is None
                                              else diffusion_steps)}

        def on_event(ev):
            if status_cb and ev.get("event") == "progress" and ev.get("message"):
                status_cb(ev["message"])

        if status_cb:
            status_cb(f"Starting {runtime.ENGINES[profile.mode]['label']}…")
        w = self._worker(profile.mode, "convert")
        return w.request(payload, cancel_event=cancel_event, on_event=on_event)

    def separate(self, in_wav, out_wav, *, denoise=False, cancel_event=None, status_cb=None) -> dict:
        """Remove background music (and optionally steady noise) from in_wav.

        Runs Demucs in the RVC convert worker (GPU). Refused while training.
        """
        if self.training_active:
            raise VCBusy("A voice is being trained, so audio cleanup is unavailable until it finishes.")

        def on_event(ev):
            if status_cb and ev.get("event") == "progress" and ev.get("message"):
                status_cb(ev["message"])

        w = self._worker("rvc", "convert")
        return w.request({"cmd": "separate", "in": _abs(in_wav), "out": _abs(out_wav),
                          "denoise": bool(denoise)},
                         cancel_event=cancel_event, on_event=on_event)

    def embed(self, files, *, cancel_event=None, status_cb=None) -> list:
        """CAMPPlus voiceprints (lists of 192 floats, or None) for audio files. CPU only."""
        def on_event(ev):
            if status_cb and ev.get("event") == "progress" and ev.get("message"):
                status_cb(ev["message"])
        w = self._worker("seedvc", "convert")
        res = w.request({"cmd": "embed", "files": [_abs(f) for f in files]},
                        cancel_event=cancel_event, on_event=on_event)
        return res.get("embeddings") or []

    def convert_reference(self, in_wav, ref_wav, out_wav, *, diffusion_steps=10, cancel_event=None,
                          status_cb=None) -> dict:
        """Zero-shot (Seed-VC) conversion against a reference file, without a saved voice.

        Used by the analyzer's audition, before any voice exists.
        """
        if self.training_active:
            raise VCBusy("A voice is being trained, so auditions are unavailable until it finishes.")

        def on_event(ev):
            if status_cb and ev.get("event") == "progress" and ev.get("message"):
                status_cb(ev["message"])
        w = self._worker("seedvc", "convert")
        return w.request({"cmd": "convert", "in": _abs(in_wav), "ref": _abs(ref_wav), "out": _abs(out_wav),
                          "diffusion_steps": int(diffusion_steps)},
                         cancel_event=cancel_event, on_event=on_event)

    def train_rvc(self, profile, dataset_dir, *, epochs=150, batch_size=2, sample_rate=40000,
                  save_every_epoch=10, keep_checkpoints=False, fresh=True,
                  progress_cb=None, log_cb=None, cancel_event=None) -> dict:
        """Train an RVC model for *profile*. Blocks for the whole run (minutes to hours).

        progress_cb(event_dict) gets stage/fraction/message/epoch/total_epoch/eta_seconds.
        """
        if self._training.is_set():
            raise VCBusy("Another voice is already training.")
        self._training.set()
        try:
            w = self._worker("rvc", "train")

            def on_event(ev):
                if ev.get("event") == "progress" and progress_cb:
                    progress_cb(ev)
                elif ev.get("event") == "log" and log_cb:
                    log_cb(ev.get("line", ""))

            payload = {"cmd": "train", "model_name": profile.slug, "dataset_dir": _abs(dataset_dir),
                       "out_dir": str(profile.dir / "model"), "sample_rate": int(sample_rate),
                       "epochs": int(epochs), "batch_size": int(batch_size),
                       "save_every_epoch": int(save_every_epoch), "fresh": bool(fresh),
                       "keep_checkpoints": bool(keep_checkpoints)}
            return w.request(payload, cancel_event=cancel_event, on_event=on_event)
        finally:
            self._training.clear()
