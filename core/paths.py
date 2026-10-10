"""
core/paths.py
──────────────
Where the app reads and writes files.

An installed build must treat its own folder as read-only: a Program Files
install is not writable by normal users, and anything written next to the
EXE is mixed with 30k bundled files and left behind by the uninstaller. So:

  bundle_dir()  read-only app files (sys._MEIPASS when frozen, repo in dev)
  user_dir(x)   %LOCALAPPDATA%\\AditsVoiceStudio\\x  for logs, caches, downloads
  output_dir()  Documents\\AditsVoiceStudio  default folder for saved audio

Running from source keeps the old layout (repo/output, repo-root weights),
so dev behaviour is unchanged.
"""

import os
import sys
import logging
from pathlib import Path

from core.version import APP_NAME, LEGACY_APP_NAME

FROZEN = getattr(sys, "frozen", False)


def bundle_dir() -> Path:
    if FROZEN:
        return Path(getattr(sys, "_MEIPASS", os.path.dirname(sys.executable)))
    return Path(__file__).resolve().parent.parent


def user_root() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    root = Path(base) / APP_NAME
    if not root.exists():
        _adopt_legacy_root(Path(base) / LEGACY_APP_NAME, root)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _adopt_legacy_root(old: Path, new: Path) -> None:
    """Move the data folder of the pre-rebrand app (VoiceCraft) to the new name.

    Settings, favourites, downloaded models and caches come along, so the
    rebrand loses nothing. Runs once: afterwards the new folder exists. If the
    move fails (the old app is still running), the app starts with a fresh
    folder and the old one stays where it is.
    """
    try:
        if old.is_dir():
            os.rename(old, new)
    except OSError as e:
        print(f"Could not move {old} to {new}: {e}")


def user_dir(name: str) -> Path:
    d = user_root() / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def logs_dir() -> Path:
    return user_dir("logs") if FROZEN else bundle_dir()


def _documents_dir() -> Path:
    """The real Documents folder, which OneDrive or a policy may have moved."""
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            class GUID(ctypes.Structure):
                _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                            ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

            # FOLDERID_Documents {FDD39AD0-238F-46AF-ADB4-6C85480369C7}
            fid = GUID(0xFDD39AD0, 0x238F, 0x46AF,
                       (ctypes.c_ubyte * 8)(0xAD, 0xB4, 0x6C, 0x85, 0x48, 0x03, 0x69, 0xC7))
            out = ctypes.c_wchar_p()
            shell32 = ctypes.windll.shell32
            if shell32.SHGetKnownFolderPath(ctypes.byref(fid), 0, None, ctypes.byref(out)) == 0:
                path = out.value
                ctypes.windll.ole32.CoTaskMemFree(out)
                if path:
                    return Path(path)
        except Exception:
            pass
    return Path.home() / "Documents"


def output_dir() -> Path:
    """Default folder for the Save dialogs. Never raises."""
    candidates = ([_documents_dir() / APP_NAME] if FROZEN else [bundle_dir() / "output"])
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    candidates.append(Path(base) / APP_NAME / "output")
    for d in candidates:
        try:
            d.mkdir(parents=True, exist_ok=True)
            return d
        except OSError:
            continue
    return Path.home()


def ascii_path(path) -> str:
    """An ASCII-only path to *path* (file or folder), or the path unchanged.

    espeak-ng's native code cannot open its data or DLL through a non-ASCII
    path ("language en-us is not supported"), and the default install lives
    under C:\\Users\\<name>, so any user name with an accent broke Kokoro and
    Piper. The 8.3 alias does not help (it fails the same way), so make a
    one-off copy (~20 MB per folder) in an ASCII location every user can
    write: %ProgramData%, then C:\\Users\\Public. The uninstaller removes it.
    """
    path = str(path)
    if sys.platform != "win32" or path.isascii():
        return path
    import shutil
    import hashlib
    from core.version import __version__
    key = hashlib.sha1(path.encode("utf-8")).hexdigest()[:10]
    # One top-level folder per source path: another user's folder under the
    # same parent would not be writable for us.
    folder = f"{APP_NAME}-ascii-{__version__}-{key}"
    bases = [os.environ.get("ProgramData") or r"C:\ProgramData",
             os.environ.get("PUBLIC") or r"C:\Users\Public"]
    for base in bases:
        dest = Path(base) / folder / Path(path).name
        if not str(dest).isascii():
            continue
        try:
            if not dest.exists():
                tmp = dest.with_name(dest.name + ".part")
                if os.path.isdir(path):
                    shutil.rmtree(tmp, ignore_errors=True)
                    shutil.copytree(path, tmp)
                else:
                    tmp.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, tmp)
                os.replace(tmp, dest)
            return str(dest)
        except Exception as e:
            print(f"Could not make an ASCII copy of {path} in {base}: {e}")
    return path


def configure_runtime():
    """Call first thing at startup, before torch / transformers are imported.

    Frozen only: route stdout/stderr and logging to a log file (a --noconsole
    EXE has no console, so these are None and output was being discarded),
    and keep Hugging Face downloads under the app's own user folder so they
    can be found, and removed on uninstall.
    """
    if not FROZEN:
        return
    try:
        os.environ.setdefault("HF_HOME", str(user_dir("cache") / "huggingface"))
    except OSError:
        pass   # unwritable profile: leave the library default

    try:
        log_path = logs_dir() / "adits_voice_studio.log"
        # Keep one previous session for diagnosis, drop anything older.
        if log_path.exists() and log_path.stat().st_size > 0:
            os.replace(log_path, log_path.with_suffix(".prev.log"))
        stream = open(log_path, "a", encoding="utf-8", buffering=1)
    except OSError:
        import io
        stream = io.StringIO()
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream
    logging.basicConfig(stream=stream, level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
