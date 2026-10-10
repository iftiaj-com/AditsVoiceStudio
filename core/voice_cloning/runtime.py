"""
core/voice_cloning/runtime.py
──────────────────────────────
Where the voice-cloning runtime lives, and how it is installed.

The runtime is two Python virtual environments, kept apart from the app's
own venv because the two upstream projects pin incompatible versions of
torch, numpy and transformers:

    rvc     Applio  (RVC training + conversion)   Python 3.12, torch 2.11 cu128
    seedvc  Seed-VC (zero-shot "Quick" cloning)   Python 3.11, torch 2.4  cu121

Layout under runtime_root() (E: drive by default when running from source):

    runtime.json          manifest, one entry per verified engine
    venv_rvc/ venv_seedvc/
    Applio/ seed-vc/      upstream clones at the pinned commits
    logs/                 install.log, worker-<engine>.log
    tmp/ pip-cache/ hf_home/
    voices/               the voice library (kept on uninstall)

Nothing here imports torch or any heavy library. The app imports this
module at startup through effects/registry.py, so it must stay cheap.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from core import paths
from core.voice_cloning.errors import InstallError, VCCancelled

SCHEMA = 1
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
GiB = 1024 ** 3

ENGINES = {
    "rvc": {
        "label": "RVC (Applio)",
        "python": "3.12",
        "venv": "venv_rvc",
        "repo_dir": "Applio",
        "repo_url": "https://github.com/IAHispano/Applio.git",
        "commit": "324f4d89c3e0e8dc0e555a3d53784e3282c16e09",
        "torch_index": "https://download.pytorch.org/whl/cu128",
        "requirements": "requirements-rvc.txt",
        "disk_bytes": 12 * GiB,
    },
    "seedvc": {
        "label": "Seed-VC",
        "python": "3.11",
        "venv": "venv_seedvc",
        "repo_dir": "seed-vc",
        "repo_url": "https://github.com/Plachtaa/seed-vc.git",
        "commit": "51383efd921027683c89e5348211d93ff12ac2a8",   # repo archived 2025-11-21
        "torch_index": "https://download.pytorch.org/whl/cu121",
        "requirements": "requirements-seedvc.txt",
        "disk_bytes": 10 * GiB,
    },
}

# Applio base models fetched by `core.py prerequisites`, with the sizes seen
# in the Phase 0 spike. Applio's downloader never checks size or HTTP status,
# so a truncated file would be kept forever. A mismatch is deleted and fetched
# once more; if it still differs, upstream probably changed the file, and that
# is logged rather than treated as a failure.
APPLIO_MODELS = {
    "rvc/models/embedders/contentvec/pytorch_model.bin": 378342945,
    "rvc/models/embedders/contentvec/config.json": 1388,
    "rvc/models/predictors/rmvpe.pt": 181184272,
    "rvc/models/pretraineds/hifi-gan/f0G32k.pth": 73950049,
    "rvc/models/pretraineds/hifi-gan/f0D32k.pth": 142875703,
    "rvc/models/pretraineds/hifi-gan/f0G40k.pth": 73106273,
    "rvc/models/pretraineds/hifi-gan/f0D40k.pth": 142875703,
    "rvc/models/pretraineds/hifi-gan/f0G48k.pth": 75465569,
    "rvc/models/pretraineds/hifi-gan/f0D48k.pth": 142875703,
}

_manifest_lock = threading.Lock()


# ── settings and paths ──────────────────────────────────────────────

def settings_path() -> Path:
    return paths.user_dir("settings") / "clone_voice.json"


def load_settings() -> dict:
    try:
        with open(settings_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(data: dict) -> None:
    _write_json_atomic(settings_path(), data)


def default_root() -> Path:
    if paths.FROZEN:
        return paths.user_root() / "vc_runtime"
    return paths.bundle_dir() / "vc_runtime_data"


def runtime_root() -> Path:
    """The runtime folder. Not created here: reading status must not write to disk."""
    custom = load_settings().get("runtime_root")
    return Path(custom) if custom else default_root()


def venv_dir(engine) -> Path:
    return runtime_root() / ENGINES[engine]["venv"]


def python_exe(engine) -> Path:
    return venv_dir(engine) / "Scripts" / "python.exe"


def repo_dir(engine) -> Path:
    return runtime_root() / ENGINES[engine]["repo_dir"]


def logs_dir() -> Path:
    return runtime_root() / "logs"


def voices_dir() -> Path:
    return runtime_root() / "voices"


def manifest_path() -> Path:
    return runtime_root() / "runtime.json"


def worker_script() -> Path:
    return paths.bundle_dir() / "vc_runtime" / "worker.py"


def requirements_file(engine) -> Path:
    return paths.bundle_dir() / "vc_runtime" / ENGINES[engine]["requirements"]


def worker_log(engine, role="convert") -> Path:
    suffix = "" if role == "convert" else f"-{role}"
    return logs_dir() / f"worker-{engine}{suffix}.log"


def child_env() -> dict:
    """Environment for every runtime child process.

    Temp files, pip's cache and Hugging Face downloads all go under the
    runtime root, because C: on the dev machine has little free space.
    PYTHONPATH/PYTHONHOME from the app's environment would leak the app's
    packages into the runtime venvs, so they are dropped.
    """
    root = runtime_root()
    env = {k: v for k, v in os.environ.items()
           if k.upper() not in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "PYTHONSTARTUP")}
    tmp = root / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    env.update({
        "TEMP": str(tmp), "TMP": str(tmp),
        "PIP_CACHE_DIR": str(root / "pip-cache"),
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "HF_HOME": str(root / "hf_home"),
        "TORCH_HOME": str(root / "models_cache" / "torch"),   # Demucs weights
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUNBUFFERED": "1",
        "PYTHONNOUSERSITE": "1",
    })
    return env


def worker_cmd(engine, role="convert", idle_seconds=300) -> list:
    return [str(python_exe(engine)), "-u", str(worker_script()),
            "--engine", engine, "--repo", str(repo_dir(engine)),
            "--log", str(worker_log(engine, role)),
            "--idle-seconds", str(int(idle_seconds))]


# ── manifest and status ─────────────────────────────────────────────

def _write_json_atomic(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def load_manifest() -> dict:
    try:
        with open(manifest_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and data.get("schema") == SCHEMA:
            data.setdefault("engines", {})
            return data
    except (OSError, ValueError):
        pass
    return {"schema": SCHEMA, "engines": {}}


def _update_manifest(engine, entry) -> None:
    with _manifest_lock:
        data = load_manifest()
        if entry is None:
            data["engines"].pop(engine, None)
        else:
            data["engines"][engine] = entry
        _write_json_atomic(manifest_path(), data)


def is_installed(engine) -> bool:
    entry = load_manifest()["engines"].get(engine) or {}
    return (bool(entry.get("verified"))
            and entry.get("commit") == ENGINES[engine]["commit"]
            and python_exe(engine).is_file()
            and repo_dir(engine).is_dir())


def needs_update(engine) -> bool:
    """Installed, but vc_runtime/requirements-<engine>.txt changed since (new packages)."""
    entry = load_manifest()["engines"].get(engine) or {}
    if not is_installed(engine):
        return False
    try:
        return entry.get("requirements_sha256") != _sha256(requirements_file(engine))
    except OSError:
        return False


def frozen_unsupported() -> bool:
    """The installed EXE has no Python to build venvs from yet (deferred)."""
    return paths.FROZEN


def status() -> dict:
    manifest = load_manifest()
    return {
        "root": str(runtime_root()),
        "frozen_unsupported": frozen_unsupported(),
        "engines": {
            name: {"label": spec["label"], "installed": is_installed(name),
                   "needs_update": needs_update(name),
                   **(manifest["engines"].get(name) or {})}
            for name, spec in ENGINES.items()
        },
    }


# ── process helpers ─────────────────────────────────────────────────

def kill_tree(pid) -> None:
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=CREATE_NO_WINDOW, timeout=30)
        else:
            os.kill(pid, 9)
    except Exception:
        pass


class _Runner:
    """Runs install commands, streams their output, honors cancel."""

    def __init__(self, log_cb, cancel_event, log_file: Path):
        self.log_cb = log_cb or (lambda line: None)
        self.cancel = cancel_event or threading.Event()
        log_file.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(log_file, "a", encoding="utf-8", buffering=1)
        self._last_bar = 0.0

    def close(self):
        try:
            self._fh.close()
        except Exception:
            pass

    def log(self, line):
        line = line.rstrip()
        if not line:
            return
        self._fh.write(line + "\n")
        bar = bool(re.search(r"\d+%\||it/s\]|s/it\]|━", line))
        now = time.monotonic()
        if bar and now - self._last_bar < 1.0:
            return
        self._last_bar = now if bar else self._last_bar
        self.log_cb(line)

    def check_cancel(self):
        if self.cancel.is_set():
            raise VCCancelled("Install cancelled")

    def run(self, cmd, cwd=None, ok_codes=(0,), capture=False):
        """Run cmd; raise InstallError on a bad exit. Returns the output if capture."""
        self.check_cancel()
        self.log("$ " + " ".join(f'"{c}"' if " " in str(c) else str(c) for c in cmd))
        proc = subprocess.Popen([str(c) for c in cmd], cwd=str(cwd) if cwd else None,
                                env=child_env(), stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                bufsize=0, creationflags=CREATE_NO_WINDOW)
        done = threading.Event()

        def watch():
            while not done.wait(0.3):
                if self.cancel.is_set():
                    kill_tree(proc.pid)
                    return
        threading.Thread(target=watch, daemon=True).start()

        lines, buf = [], b""
        try:
            while True:
                chunk = proc.stdout.read(4096)
                if not chunk:
                    break
                buf += chunk
                parts = re.split(rb"[\r\n]", buf)
                buf = parts.pop()
                for raw in parts:
                    text = raw.decode("utf-8", errors="replace")
                    if text.strip():
                        lines.append(text)
                        self.log(text)
            if buf.strip():
                text = buf.decode("utf-8", errors="replace")
                lines.append(text)
                self.log(text)
            rc = proc.wait()
        finally:
            done.set()
            if proc.poll() is None:
                kill_tree(proc.pid)
        self.check_cancel()
        if rc not in ok_codes:
            tail = "\n".join(lines[-15:])
            raise InstallError(f"Command failed (exit {rc}): {cmd[0]} {cmd[1] if len(cmd) > 1 else ''}",
                               hint=tail)
        return "\n".join(lines) if capture else None


# ── install steps ───────────────────────────────────────────────────

def find_base_python(version: str):
    """Full path of an installed CPython <version> via the py launcher, or None."""
    if frozen_unsupported():
        return None
    try:
        out = subprocess.run(["py", f"-{version}", "-c", "import sys; print(sys.executable)"],
                             capture_output=True, text=True, timeout=30,
                             creationflags=CREATE_NO_WINDOW)
        exe = out.stdout.strip().splitlines()[-1] if out.returncode == 0 and out.stdout.strip() else ""
        return exe if exe and os.path.isfile(exe) else None
    except (OSError, subprocess.SubprocessError, IndexError):
        return None


def _torch_pins(engine):
    pins = []
    for line in requirements_file(engine).read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if re.match(r"^(torch|torchaudio)==", line):
            pins.append(line)
    if len(pins) != 2:
        raise InstallError(f"{requirements_file(engine).name} must pin torch and torchaudio.")
    return pins


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _step_venv(engine, run: _Runner):
    py = python_exe(engine)
    if py.is_file():
        run.log(f"[{engine}] venv present")
        return
    ver = ENGINES[engine]["python"]
    base = find_base_python(ver)
    if not base:
        raise InstallError(f"Python {ver} was not found.",
                           hint=f"Install Python {ver} from python.org (with the py launcher), then retry.")
    run.run([base, "-m", "venv", str(venv_dir(engine))])
    run.run([py, "-m", "pip", "install", "--upgrade", "pip"])


def _step_torch(engine, run: _Runner):
    py = python_exe(engine)
    pins = _torch_pins(engine)
    want = {p.split("==")[0]: p.split("==")[1] for p in pins}
    try:
        out = run.run([py, "-c", "import torch, torchaudio; print('VERS', torch.__version__, torchaudio.__version__)"],
                      capture=True)
        got = re.search(r"VERS (\S+) (\S+)", out or "")
        if got and got.group(1).split("+")[0] == want["torch"] and got.group(2).split("+")[0] == want["torchaudio"]:
            run.log(f"[{engine}] torch {got.group(1)} present")
            return
    except InstallError:
        pass
    run.run([py, "-m", "pip", "install", *pins, "--index-url", ENGINES[engine]["torch_index"]])


def _step_requirements(engine, run: _Runner):
    py = python_exe(engine)
    req = requirements_file(engine)
    stamp = venv_dir(engine) / ".vc-requirements.sha256"
    digest = _sha256(req)
    if stamp.is_file() and stamp.read_text(encoding="utf-8").strip() == digest:
        run.log(f"[{engine}] requirements up to date")
        return
    run.run([py, "-m", "pip", "install", "-r", req, "--extra-index-url", ENGINES[engine]["torch_index"]])
    run.run([py, "-m", "pip", "check"])
    stamp.write_text(digest, encoding="utf-8")


def _step_repo(engine, run: _Runner):
    git = shutil.which("git")
    if not git:
        raise InstallError("Git was not found.", hint="Install Git for Windows, then retry.")
    spec = ENGINES[engine]
    d = repo_dir(engine)
    if (d / ".git").is_dir():
        head = run.run([git, "-C", d, "rev-parse", "HEAD"], capture=True).strip().splitlines()[-1]
        if head == spec["commit"]:
            run.log(f"[{engine}] {spec['repo_dir']} at pinned commit")
            return
    elif d.exists() and any(d.iterdir()):
        raise InstallError(f"{d} exists but is not a git checkout.",
                           hint="Move or delete that folder, then retry.")
    else:
        run.run([git, "init", "-q", d])
        run.run([git, "-C", d, "remote", "add", "origin", spec["repo_url"]])
    run.run([git, "-C", d, "fetch", "--depth", "1", "origin", spec["commit"]])
    run.run([git, "-C", d, "checkout", "-q", "--detach", "FETCH_HEAD"])


def _bad_applio_models():
    base = repo_dir("rvc")
    bad = []
    for rel, size in APPLIO_MODELS.items():
        p = base / rel
        if not p.is_file() or p.stat().st_size != size:
            bad.append(rel)
    return bad


def _step_applio_assets(run: _Runner):
    base = repo_dir("rvc")
    cfg = base / "assets" / "config.json"
    if not cfg.exists():
        # Only Applio's web UI creates this; without it training writes no model.
        shutil.copyfile(base / "assets" / "config_template.json", cfg)
        run.log("[rvc] created assets/config.json from template")
    bad = _bad_applio_models()
    if not bad:
        run.log("[rvc] base models present")
        return
    for attempt in (1, 2):
        for rel in bad:
            p = base / rel
            if p.exists():
                run.log(f"[rvc] re-fetching {rel} (size {p.stat().st_size}, expected {APPLIO_MODELS[rel]})")
                p.unlink()
        run.run([python_exe("rvc"), "core.py", "prerequisites", "--pretraineds-hifigan",
                 "--models", "--no-exe"], cwd=base)
        bad = _bad_applio_models()
        if not bad:
            return
    missing = [rel for rel in bad if not (base / rel).is_file()]
    if missing:
        raise InstallError("Applio base models failed to download: " + ", ".join(missing))
    run.log("[rvc] WARNING: size differs from the recorded value after a fresh download "
            "(upstream may have updated): " + ", ".join(bad))


def _step_selfcheck(engine, run: _Runner) -> dict:
    out = run.run([python_exe(engine), "-u", worker_script(), "--engine", engine,
                   "--repo", repo_dir(engine), "--selfcheck"], cwd=repo_dir(engine), capture=True)
    m = re.findall(r"SELFCHECK_RESULT (\{.*\})", out or "")
    if not m:
        raise InstallError(f"{ENGINES[engine]['label']} self-check gave no result.")
    result = json.loads(m[-1])
    if not result.get("cuda"):
        run.log(f"[{engine}] WARNING: CUDA not available, voice cloning will run on the CPU (slow)")
    return result


def _free_bytes(path: Path) -> int:
    p = path
    while not p.exists() and p.parent != p:
        p = p.parent
    return shutil.disk_usage(p).free


def install(engines=("rvc", "seedvc"), progress_cb=None, log_cb=None, cancel_event=None) -> dict:
    """Install or repair the runtime for *engines*. Each step is skipped when already done.

    progress_cb(fraction, message), log_cb(line). Blocking: call from a thread.
    Raises InstallError or VCCancelled. Returns the manifest.
    """
    if frozen_unsupported():
        raise InstallError("Voice cloning setup is not available in the installed build yet.",
                           hint="Run TTS Studio from source to install it.")
    progress = progress_cb or (lambda f, m: None)
    root = runtime_root()
    root.mkdir(parents=True, exist_ok=True)
    run = _Runner(log_cb, cancel_event, logs_dir() / "install.log")
    run.log(f"=== install {datetime.now().isoformat(timespec='seconds')} root={root} engines={list(engines)}")
    try:
        need = sum(ENGINES[e]["disk_bytes"] for e in engines if not python_exe(e).is_file())
        need = max(need, 2 * GiB)
        free = _free_bytes(root)
        if free < need:
            raise InstallError(f"Not enough free disk space on {root.anchor}: "
                               f"{free / GiB:.1f} GB free, about {need / GiB:.0f} GB needed.",
                               hint="Free some space or choose another location.")

        steps = []
        for e in engines:
            steps += [(e, "Creating Python environment", lambda e=e: _step_venv(e, run), 2),
                      (e, "Installing PyTorch (large download)", lambda e=e: _step_torch(e, run), 30),
                      (e, "Installing libraries", lambda e=e: _step_requirements(e, run), 12),
                      (e, "Downloading engine code", lambda e=e: _step_repo(e, run), 2)]
            if e == "rvc":
                steps.append((e, "Downloading RVC base models", lambda: _step_applio_assets(run), 10))
            steps.append((e, "Loading models (first run downloads them)", None, 8))
        total = sum(w for *_, w in steps)
        done = 0
        for e, label, fn, weight in steps:
            run.check_cancel()
            progress(done / total, f"{ENGINES[e]['label']}: {label}…")
            run.log(f"--- [{e}] {label}")
            if fn is None:
                result = _step_selfcheck(e, run)
                _update_manifest(e, {
                    "verified": True,
                    "installed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "commit": ENGINES[e]["commit"],
                    "requirements_sha256": _sha256(requirements_file(e)),
                    **{k: result.get(k) for k in ("python", "torch", "cuda", "gpu")},
                })
            else:
                fn()
            done += weight
        # Both torch wheels sit in pip's cache (about 5 GB); they are not needed again.
        shutil.rmtree(root / "pip-cache", ignore_errors=True)
        progress(1.0, "Voice cloning is ready")
        run.log("=== install complete")
        return load_manifest()
    except (InstallError, VCCancelled) as e:
        run.log(f"=== install stopped: {e}")
        raise
    except Exception as e:
        run.log(f"=== install failed: {type(e).__name__}: {e}")
        raise InstallError(f"Install failed: {e}") from e
    finally:
        run.close()


def uninstall(engines=("rvc", "seedvc"), keep_voices=True) -> None:
    """Remove venvs, upstream clones and caches. The voice library is kept unless asked."""
    root = runtime_root()
    for e in engines:
        if manifest_path().exists():
            _update_manifest(e, None)
        for d in (venv_dir(e), repo_dir(e)):
            shutil.rmtree(d, ignore_errors=True)
    if set(engines) >= set(ENGINES):
        for name in ("pip-cache", "hf_home", "tmp", "logs"):
            shutil.rmtree(root / name, ignore_errors=True)
        try:
            manifest_path().unlink()
        except OSError:
            pass
        if not keep_voices:
            shutil.rmtree(voices_dir(), ignore_errors=True)
