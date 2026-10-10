"""
vc_runtime/vc_common.py
────────────────────────
Shared helpers for the voice-cloning worker and its backends.

Runs inside a runtime venv (venv_rvc or venv_seedvc), never inside the app.
Standard library only, so it imports before torch is loaded.
"""

import os
import re
import subprocess
import sys
import threading
import time

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

# tqdm redraws its bar with "\r"; those lines are noise in a log view.
_TQDM_RE = re.compile(r"(\d+%\|)|(it/s\]|s/it\])")


class TaskError(Exception):
    """A failure the app can explain to the user.

    kind: oom | ram | cancelled | dataset_too_small | not_found | bad_model | generic
    """

    def __init__(self, kind, message, hint=None):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.hint = hint


class Cancelled(TaskError):
    def __init__(self, message="Cancelled"):
        super().__init__("cancelled", message)


def kill_tree(pid):
    """Kill a process and every child it started. Never raises."""
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=CREATE_NO_WINDOW, timeout=30)
        else:
            os.kill(pid, 9)
    except Exception:
        pass


def is_progress_line(line):
    return bool(_TQDM_RE.search(line))


class RequestContext:
    """What a backend gets for one request: event output, cancel flag, child tracking."""

    def __init__(self, req_id, emit):
        self.req_id = req_id
        self._emit = emit
        self.cancel = threading.Event()
        self._child = None
        self._lock = threading.Lock()
        self._last_bar = 0.0

    def emit(self, event, **fields):
        self._emit(dict(id=self.req_id, event=event, **fields))

    def progress(self, stage, fraction, message="", **extra):
        self.emit("progress", stage=stage, fraction=round(float(fraction), 4),
                  message=message, **extra)

    def log(self, line):
        """Forward a log line; tqdm bars at most every 2 s."""
        line = line.rstrip()
        if not line:
            return
        print(line, flush=True)              # worker log file
        if is_progress_line(line):
            now = time.monotonic()
            if now - self._last_bar < 2.0:
                return
            self._last_bar = now
        self.emit("log", line=line[-500:])

    def check_cancel(self):
        if self.cancel.is_set():
            raise Cancelled()

    def set_child(self, proc):
        with self._lock:
            self._child = proc
            if proc is not None and self.cancel.is_set():
                kill_tree(proc.pid)

    def request_cancel(self):
        self.cancel.set()
        with self._lock:
            if self._child is not None and self._child.poll() is None:
                kill_tree(self._child.pid)


def run_streaming(cmd, cwd, ctx, on_line=None, env=None):
    """Run *cmd*, forward each output line to ctx.log and on_line. Returns the exit code.

    Output is read in raw chunks and split on both "\\r" and "\\n", so tqdm
    bars and normal prints arrive as separate lines as soon as they are written.
    """
    child_env = dict(os.environ if env is None else env)
    child_env.setdefault("PYTHONUNBUFFERED", "1")
    child_env.setdefault("PYTHONIOENCODING", "utf-8")
    ctx.check_cancel()
    proc = subprocess.Popen(cmd, cwd=cwd, env=child_env,
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, bufsize=0,
                            creationflags=CREATE_NO_WINDOW)
    ctx.set_child(proc)
    buf = b""
    try:
        while True:
            chunk = proc.stdout.read(4096)
            if not chunk:
                break
            buf += chunk
            parts = re.split(rb"[\r\n]", buf)
            buf = parts.pop()
            for raw in parts:
                line = raw.decode("utf-8", errors="replace")
                if line.strip():
                    ctx.log(line)
                    if on_line:
                        on_line(line)
        if buf.strip():
            line = buf.decode("utf-8", errors="replace")
            ctx.log(line)
            if on_line:
                on_line(line)
        return proc.wait()
    finally:
        ctx.set_child(None)
        if proc.poll() is None:
            kill_tree(proc.pid)
