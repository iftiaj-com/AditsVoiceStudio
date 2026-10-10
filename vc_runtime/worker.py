"""
vc_runtime/worker.py
─────────────────────
Long-lived voice-cloning worker. Started by core/voice_cloning/client.py with
the python.exe of one runtime venv, never imported by the app.

    python worker.py --engine rvc|seedvc --repo DIR --log FILE [--idle-seconds 300]
    python worker.py --engine rvc|seedvc --repo DIR --selfcheck

Protocol: one JSON object per line. Requests arrive on stdin, events leave on
the original stdout. Everything else a library prints (tqdm, warnings,
tracebacks) is redirected at the file-descriptor level into --log, so it can
never corrupt the protocol stream.

Requests:   {"id", "cmd": ping|selfcheck|convert|inspect|train|cancel|shutdown, ...}
Events:     {"event": "ready"} once, then per request:
            {"id", "event": "progress"|"log"|"done"|"error", ...}

The worker exits after --idle-seconds without a request, when stdin closes
(the app went away), or on "shutdown". A closed stdin also cancels the
running request, so a crashed app never leaves training running.
"""

import argparse
import json
import os
import queue
import sys
import threading
import time
import traceback

from vc_common import Cancelled, RequestContext, TaskError


def _open_protocol_stream(log_path):
    """Keep a private handle on stdout for events, then point fds 1 and 2 at the log."""
    proto = os.fdopen(os.dup(1), "w", encoding="utf-8", buffering=1, newline="\n")
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    log = open(log_path, "a", encoding="utf-8", buffering=1)
    os.dup2(log.fileno(), 1)
    os.dup2(log.fileno(), 2)
    sys.stdout = log
    sys.stderr = log
    return proto


def _detach_stdin():
    """Move the request pipe off the process's standard input; return a reader for it.

    On Windows, while one thread waits in a blocking read on a pipe, any
    other call that queries that pipe handle waits too. DLLs built with a
    static C runtime query the standard handles when they load, so with the
    reader thread waiting on stdin, `import numpy` froze until the next
    request arrived (found in Phase 1: a convert request never finished).
    The reader gets a private handle to the pipe, and standard input becomes
    NUL, which never blocks.
    """
    private_fd = os.dup(0)
    nul = os.open(os.devnull, os.O_RDONLY)
    os.dup2(nul, 0)
    os.close(nul)
    if sys.platform == "win32":
        import ctypes
        import msvcrt
        STD_INPUT_HANDLE = -10
        ctypes.windll.kernel32.SetStdHandle(STD_INPUT_HANDLE, msvcrt.get_osfhandle(0))
    sys.stdin = open(os.devnull, "r", encoding="utf-8")
    return os.fdopen(private_fd, "r", encoding="utf-8", errors="replace")


def _load_backend(engine, repo):
    if engine == "rvc":
        from rvc_backend import RvcBackend
        return RvcBackend(repo)
    if engine == "seedvc":
        from seedvc_backend import SeedVcBackend
        return SeedVcBackend(repo)
    raise TaskError("generic", f"Unknown engine {engine!r}")


def _oom_error(exc):
    return TaskError("oom", f"Out of GPU memory: {exc}",
                     hint="Close other GPU apps or lower the batch size.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", required=True, choices=["rvc", "seedvc"])
    ap.add_argument("--repo", required=True)
    ap.add_argument("--log")
    ap.add_argument("--idle-seconds", type=float, default=300.0)
    ap.add_argument("--selfcheck", action="store_true")
    args = ap.parse_args()

    repo = os.path.abspath(args.repo)
    # Both upstream projects resolve model paths relative to the cwd.
    os.chdir(repo)
    sys.path.append(repo)

    if args.selfcheck:
        # Install-time check: everything goes to stdout so the installer log
        # shows model downloads; the result is the last line.
        backend = _load_backend(args.engine, repo)
        result = backend.selfcheck()
        print("SELFCHECK_RESULT " + json.dumps(result), flush=True)
        return 0

    proto = _open_protocol_stream(args.log or os.path.join(repo, "worker.log"))
    import faulthandler
    faulthandler.enable(file=sys.stderr)          # native crashes land in the log
    if os.environ.get("VC_DEBUG_DUMP_AFTER"):     # diagnose hangs: dump all stacks once
        faulthandler.dump_traceback_later(float(os.environ["VC_DEBUG_DUMP_AFTER"]), file=sys.stderr)
    out_lock = threading.Lock()

    def emit(obj):
        with out_lock:
            try:
                proto.write(json.dumps(obj) + "\n")
                proto.flush()
            except (OSError, ValueError):
                pass   # the app closed the pipe; nothing left to tell

    print(f"--- worker start {time.strftime('%Y-%m-%d %H:%M:%S')} engine={args.engine} pid={os.getpid()}",
          flush=True)

    requests = queue.Queue()
    current = {"ctx": None}
    cur_lock = threading.Lock()
    request_stream = _detach_stdin()

    def reader():
        for line in request_stream:
            line = line.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
            except ValueError:
                print(f"bad request line: {line[:200]}", flush=True)
                continue
            if req.get("cmd") == "cancel":
                with cur_lock:
                    ctx = current["ctx"]
                if ctx is not None and (req.get("target") in (None, ctx.req_id)):
                    print(f"cancel requested for {ctx.req_id}", flush=True)
                    ctx.request_cancel()
                continue
            requests.put(req)
        # stdin closed: the app is gone
        with cur_lock:
            ctx = current["ctx"]
        if ctx is not None:
            ctx.request_cancel()
        requests.put({"cmd": "shutdown", "id": None})

    threading.Thread(target=reader, daemon=True).start()
    emit({"event": "ready", "engine": args.engine, "pid": os.getpid()})

    backend = None
    while True:
        try:
            req = requests.get(timeout=args.idle_seconds)
        except queue.Empty:
            print("idle timeout, exiting", flush=True)
            break
        cmd = req.get("cmd")
        rid = req.get("id")
        if cmd == "shutdown":
            if rid:
                emit({"id": rid, "event": "done", "result": {}})
            break

        ctx = RequestContext(rid, emit)
        with cur_lock:
            current["ctx"] = ctx
        try:
            if cmd == "ping":
                result = {"engine": args.engine, "pid": os.getpid()}
            else:
                if backend is None:
                    ctx.progress("load", 0.0, "Loading voice model…")
                    backend = _load_backend(args.engine, repo)
                if cmd == "selfcheck":
                    result = backend.selfcheck()
                elif cmd == "convert":
                    result = backend.convert(req, ctx)
                elif cmd == "inspect" and args.engine == "rvc":
                    result = backend.inspect(req["pth"])
                elif cmd == "train" and args.engine == "rvc":
                    result = backend.train(req, ctx)
                elif cmd == "separate" and args.engine == "rvc":
                    result = backend.separate(req, ctx)
                else:
                    raise TaskError("generic", f"Unsupported command {cmd!r} for {args.engine}")
            emit({"id": rid, "event": "done", "result": result})
        except TaskError as e:
            # A backend may stop its own child through the cancel flag (OOM);
            # only an otherwise unexplained failure is reported as a cancel.
            if ctx.cancel.is_set() and e.kind == "generic":
                e = Cancelled()
            print(f"request {rid} failed: {e.kind}: {e.message}", flush=True)
            emit({"id": rid, "event": "error", "kind": e.kind,
                  "message": e.message, "hint": e.hint})
        except Exception as e:   # noqa: BLE001 - report everything to the app
            traceback.print_exc()
            if ctx.cancel.is_set():
                err = Cancelled()
            elif type(e).__name__ == "OutOfMemoryError" or "out of memory" in str(e).lower():
                err = _oom_error(e)
            else:
                err = TaskError("generic", f"{type(e).__name__}: {e}")
            emit({"id": rid, "event": "error", "kind": err.kind,
                  "message": err.message, "hint": err.hint})
        finally:
            with cur_lock:
                current["ctx"] = None
            try:
                import torch
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
            except Exception:
                pass

    print(f"--- worker exit {time.strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
