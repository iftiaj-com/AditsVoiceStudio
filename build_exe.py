import os
import re
import sys
import json
import shutil
import argparse
import subprocess
from pathlib import Path

# Adits Voice Studio PyInstaller Builder
# Compiles the app into dist/AditsVoiceStudio (onedir), trims it, adds license
# notices, self-tests the frozen EXE, and optionally compiles the installer.
#
#   .\venv_311\Scripts\python.exe build_exe.py              # build + test
#   .\venv_311\Scripts\python.exe build_exe.py --installer  # ... + Setup.exe

BASE_DIR = Path(__file__).parent.absolute()
sys.path.insert(0, str(BASE_DIR))
from core.version import (APP_NAME, APP_TITLE, __version__, PUBLISHER,  # noqa: E402
                          SOURCE_URL, LICENSE_ID, EXE_NAME)

APP_DIR  = BASE_DIR / "dist" / EXE_NAME
INTERNAL = APP_DIR / "_internal"

# Every library PyInstaller must be able to see for the build to be complete.
# Keyed by import name; the value is what the engine needs it for.
_REQUIRED = {
    "kokoro":         "Kokoro PyTorch path",
    "kokoro_onnx":    "Kokoro ONNX path",
    "piper":          "Piper TTS engine",
    "melo":           "MeloTTS engine",
    "g2p_en":         "MeloTTS G2P module",
    "unidic_lite":    "MeloTTS Japanese dictionary",
    "pykakasi":       "MeloTTS Japanese transliteration",
    "regex":          "NLTK tokenization",
    "jamo":           "MeloTTS Korean phonemizer",
    "anyascii":       "MeloTTS Korean romanization",
    "cn2an":          "MeloTTS Chinese numbers",
    "jieba":          "MeloTTS Chinese segmentation",
    "pypinyin":       "MeloTTS Chinese pinyin",
    "librosa":        "MeloTTS audio processing",
    "pyttsx3":        "pyttsx3 engine",
    "faster_whisper": "word-level subtitles",
    "customtkinter":  "GUI",
    "soundfile":      "audio I/O and MP3 export",
    "pyaudio":        "Clone Voice microphone recording",
    "av":             "Clone Voice clip import (M4A, AAC, WebM, WMA...)",
}

# Left out of the public build on purpose:
#  - gtts / edge_tts: unofficial Google / Microsoft endpoints. Their adapters
#    report unavailable when the import fails, so they vanish from the UI.
#  - the rest: present in the dev venv, never imported by the app, but pulled
#    in by --collect-all hidden imports (or AGPL / non-commercial licensed).
_EXCLUDE = [
    "gtts", "edge_tts",
    "gradio", "wandb", "langchain", "langchain_core", "langchain_community",
    "langsmith", "langgraph", "IPython", "tensorboard", "sentry_sdk",
    "chattts", "encodec", "PyInstaller",
]

# Removed from dist after PyInstaller: link-time and source files that
# --collect-all drags in but nothing loads at runtime.
_PRUNE_GLOBS = [
    "torch/lib/*.lib",          # static/import libs, ~770 MB (dnnl.lib alone 620 MB)
    "**/*.cpp", "**/*.cc", "**/*.c", "**/*.pyx", "**/*.pxd",
]
_PRUNE_DIRS = [
    "torch/include", "pygame/tests", "pygame/docs", "pygame/examples",
    "jieba/lac_small",          # needs paddle, which is not installed
]

_WHISPER_REPO  = "Systran/faster-whisper-base"
_WHISPER_FILES = ("config.json", "model.bin", "tokenizer.json", "vocabulary.txt")


def _check_interpreter():
    """Refuse to build from an interpreter that cannot see the engine libraries.

    PyInstaller bundles whatever *this* interpreter can import. Building with a
    bare `python` picks up whichever one is first on PATH, which silently drops
    engines from the EXE while the build still reports success.
    """
    import importlib.util
    missing = [(m, why) for m, why in _REQUIRED.items()
               if importlib.util.find_spec(m) is None]
    if not missing:
        return True

    print(f"[!] This interpreter is missing {len(missing)} required package(s):")
    for mod, why in missing:
        print(f"      {mod:16} ({why})")
    print(f"[!] Building here would produce an EXE without them.")
    print(f"[!] Interpreter in use: {sys.executable}")
    print(f"[!] Build with the project venv instead:")
    print(r"        .\venv_311\Scripts\python.exe build_exe.py")
    return False


def _add(command, src, dest):
    command.extend(["--add-data", f"{src}{os.pathsep}{dest}"])


def _bundle_models(command):
    """Add model files explicitly; the old `models;models` shipped ~340 MB of
    Kokoro .pth/.pt files no code reads, plus a research-only Piper voice."""
    from core.constants import PIPER_VOICES
    for voice in PIPER_VOICES.values():
        for suffix in (".onnx", ".onnx.json", ".MODEL_CARD"):
            f = BASE_DIR / "models" / "piper" / f"{voice}{suffix}"
            if not f.exists():
                print(f"[!] Missing Piper voice file {f}. Download it from "
                      "https://huggingface.co/rhasspy/piper-voices first.")
                return False
            _add(command, f, "models/piper")

    # Subtitles offline: ship the Whisper model instead of downloading it.
    try:
        from huggingface_hub import snapshot_download
        snap = Path(snapshot_download(_WHISPER_REPO, allow_patterns=list(_WHISPER_FILES)))
        for name in _WHISPER_FILES:
            _add(command, snap / name, "models/whisper-base")
        print(f"[*] Bundling {_WHISPER_REPO} from {snap}")
    except Exception as e:
        print(f"[!] Could not fetch {_WHISPER_REPO}: {e}")
        return False

    # Presence is not enough: a truncated model bundles cleanly and only fails
    # at runtime with "INVALID_PROTOBUF", which is how a 78 MB stub once shipped.
    onnx = BASE_DIR / "kokoro-v1.0.onnx"
    if onnx.exists():
        from engines.kokoro_engine import _onnx_is_complete
        if not _onnx_is_complete(str(onnx)):
            print("[!] kokoro-v1.0.onnx is truncated. Delete it and let the app "
                  "re-download, or fetch it again before building.")
            return False
        _add(command, onnx, ".")
    if (BASE_DIR / "voices.bin").exists():
        _add(command, BASE_DIR / "voices.bin", ".")
    return True


def _bundle_vc_runtime(command):
    """Ship the voice-cloning worker scripts and pinned requirements as data.

    They run inside the separate runtime venvs (see core/voice_cloning/runtime.py),
    never inside the frozen app, so PyInstaller's import analysis cannot find
    them. The frozen app cannot create those venvs yet (no Python to build them
    from), so Clone Voice shows "Not available in this build"; the files are
    bundled so a later release can install the runtime from the EXE.
    Nothing here is added to _REQUIRED or --collect-all: torch for cloning
    lives in the runtime venvs, not in the app.
    """
    src = BASE_DIR / "vc_runtime"
    files = sorted(list(src.glob("*.py")) + list(src.glob("requirements-*.txt")))
    for f in files:
        _add(command, f, "vc_runtime")
    site = sorted((src / "train_site").glob("*.py"))
    for f in site:
        _add(command, f, "vc_runtime/train_site")
    print(f"[*] Bundling voice-cloning worker files: {len(files) + len(site)} files")


def _write_version_file(path):
    nums = [int(x) for x in re.findall(r"\d+", __version__)[:4]]
    nums += [0] * (4 - len(nums))
    t = tuple(nums)
    path.write_text(f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={t}, prodvers={t}, mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', {PUBLISHER!r}),
      StringStruct('FileDescription', {APP_TITLE!r}),
      StringStruct('FileVersion', {__version__!r}),
      StringStruct('InternalName', {EXE_NAME!r}),
      StringStruct('LegalCopyright', 'Licensed under {LICENSE_ID}'),
      StringStruct('OriginalFilename', {EXE_NAME + '.exe'!r}),
      StringStruct('ProductName', {APP_TITLE!r}),
      StringStruct('ProductVersion', {__version__!r})])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
""", encoding="utf-8")


def _prune():
    freed = 0
    for pattern in _PRUNE_GLOBS:
        for f in INTERNAL.glob(pattern):
            if f.is_file():
                freed += f.stat().st_size
                f.unlink()
    for rel in _PRUNE_DIRS:
        d = INTERNAL / rel
        if d.is_dir():
            freed += sum(p.stat().st_size for p in d.rglob("*") if p.is_file())
            shutil.rmtree(d)
    # nltk_data's .zip files look redundant next to the unpacked folders, but
    # g2p_en (imported by melo) looks them up by name and re-downloads them,
    # into the install folder, when they are missing. Keep them.
    print(f"[+] Pruned {freed / 2**20:,.0f} MiB of build-only files")


def _bundled_module_names():
    """Top-level module names that made it into this build."""
    names = {p.name.split(".")[0] for p in INTERNAL.iterdir()}
    toc = BASE_DIR / "build" / EXE_NAME / "PYZ-00.toc"
    if toc.exists():
        names |= set(re.findall(r"\('([A-Za-z_][\w]*)[.']", toc.read_text(encoding="utf-8",
                                                                              errors="ignore")))
    return names


def _write_notices():
    """LICENSE.txt, THIRD_PARTY_NOTICES.txt and licenses/ into the app folder.

    The package list is generated from what this build actually contains, so
    it stays right when the venv changes.
    """
    from importlib import metadata
    bundled = _bundled_module_names()
    rows, texts = [], []
    for dist in sorted(metadata.distributions(), key=lambda d: (d.metadata["Name"] or "").lower()):
        name = dist.metadata["Name"]
        if not name:
            continue
        top = set()
        try:
            top = set((dist.read_text("top_level.txt") or "").split())
        except Exception:
            pass
        if not top:
            top = {str(f).split("/")[0].split(".")[0] for f in (dist.files or [])
                   if not str(f).startswith("..") and ".dist-info" not in str(f)}
        top.add(name.replace("-", "_"))
        if not top & bundled:
            continue
        lic = (dist.metadata.get("License-Expression") or dist.metadata.get("License") or "").strip()
        if not lic or len(lic) > 80:
            classifiers = [c.split("::")[-1].strip() for c in dist.metadata.get_all("Classifier") or []
                           if c.startswith("License ::")]
            lic = "; ".join(classifiers) or (lic.splitlines()[0][:80] if lic else "see project")
        url = dist.metadata.get("Home-page") or ""
        for entry in dist.metadata.get_all("Project-URL") or []:
            url = url or entry.split(",", 1)[-1].strip()
        rows.append(f"  {name} {dist.version}\n      License: {lic}\n      {url}".rstrip())
        for f in dist.files or []:
            fn = Path(str(f)).name.upper()
            if ".dist-info" in str(f) and fn.startswith(("LICENSE", "LICENCE", "COPYING", "NOTICE", "AUTHORS")):
                try:
                    body = Path(f.locate()).read_text(encoding="utf-8", errors="replace")
                except Exception:
                    continue
                texts.append(f"\n\n{'=' * 78}\n{name} {dist.version}: {Path(str(f)).name}\n"
                             f"{'=' * 78}\n{body}")

    header = (BASE_DIR / "licenses" / "NOTICES_HEADER.txt").read_text(encoding="utf-8")
    header = header.format(app_title=APP_TITLE, version=__version__, source_url=SOURCE_URL)
    out = header + "\n".join(rows) + "\n\n\nPACKAGE LICENSE TEXTS\n---------------------" + "".join(texts)
    (APP_DIR / "THIRD_PARTY_NOTICES.txt").write_text(out, encoding="utf-8")
    shutil.copy2(BASE_DIR / "LICENSE.txt", APP_DIR / "LICENSE.txt")
    lic_dir = APP_DIR / "licenses"
    lic_dir.mkdir(exist_ok=True)
    for f in (BASE_DIR / "licenses").glob("*.txt"):
        if f.name != "NOTICES_HEADER.txt":
            shutil.copy2(f, lic_dir / f.name)
    print(f"[+] Wrote THIRD_PARTY_NOTICES.txt ({len(rows)} packages, {len(out) // 1024} KiB)")


def _selftest():
    """Run the frozen EXE headless and check real engine output."""
    report = BASE_DIR / "build" / "selftest.json"
    report.unlink(missing_ok=True)
    print("\n[*] Self-testing the frozen app (takes a few minutes)...")
    try:
        proc = subprocess.run([str(APP_DIR / f"{EXE_NAME}.exe"), "--selftest", str(report)],
                              timeout=1800)
    except subprocess.TimeoutExpired:
        print("[!] Self-test did not finish within 30 minutes.")
        return False
    if not report.exists():
        print(f"[!] Self-test produced no report (exit {proc.returncode}). "
              f"See {Path(os.environ['LOCALAPPDATA']) / APP_NAME / 'logs'}")
        return False
    r = json.loads(report.read_text(encoding="utf-8"))
    for name, c in r["checks"].items():
        mark = "+" if c["ok"] else ("!" if c.get("fatal", True) else "~")
        detail = c.get("error") or c.get("warning") or ""
        print(f"[{mark}] {name:20} {c.get('secs', '')}s {detail}")
    print(f"[*] Engines: {r['checks'].get('engines', {}).get('available')}")
    return r["ok"]


def _find_iscc():
    for base in (os.environ.get("LOCALAPPDATA", ""), os.environ.get("ProgramFiles(x86)", ""),
                 os.environ.get("ProgramFiles", "")):
        sub = "Programs/Inno Setup 6" if base == os.environ.get("LOCALAPPDATA") else "Inno Setup 6"
        p = Path(base) / sub / "ISCC.exe"
        if base and p.exists():
            return p
    return shutil.which("ISCC")


def _build_installer():
    iscc = _find_iscc()
    if not iscc:
        print("[!] ISCC.exe not found. Install Inno Setup 6: winget install JRSoftware.InnoSetup")
        return False
    cmd = [str(iscc), f"/DMyAppVersion={__version__}", f"/DMyAppPublisher={PUBLISHER}",
           f"/DMyAppURL={SOURCE_URL}", f"/DMyAppTitle={APP_TITLE}",
           f"/DMyAppName={APP_NAME}", f"/DMyAppExeBase={EXE_NAME}",
           str(BASE_DIR / "installer.iss")]
    print(f"\n[*] Compiling installer (this can take an hour): {' '.join(cmd)}")
    try:
        subprocess.check_call(cmd, cwd=BASE_DIR)
    except subprocess.CalledProcessError as e:
        print(f"[!] ISCC failed with exit code {e.returncode}")
        return False
    out =BASE_DIR / "installer_output" / f"{APP_NAME}-Setup-{__version__}.exe"
    size = out.stat().st_size
    print(f"[+] Installer: {out} ({size / 1e9:.2f} GB)")
    if size > 4.0e9:
        print("[!] Over 4 GB: enable DiskSpanning in installer.iss (see the note there).")
    return True


def build(args):
    print(f"=== {APP_TITLE} {__version__} Build System ===")
    if not _check_interpreter():
        return False

    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("[!] PyInstaller not found. Installing...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "pyinstaller"])

    build_dir = BASE_DIR / "build"
    build_dir.mkdir(exist_ok=True)
    version_file = build_dir / "version_info.txt"
    _write_version_file(version_file)

    # --noconsole for a clean GUI app; --collect-all so heavy libraries are
    # fully included with their data files.
    command = [
        sys.executable, "-m", "PyInstaller",
        "--noconsole", f"--name={EXE_NAME}", "--clean", "--noconfirm",
        "--icon", str(BASE_DIR / "assets" / "adits_voice_studio.ico"),
        "--splash", str(BASE_DIR / "assets" / "splash.png"),
        "--version-file", str(version_file),
    ]
    for pkg in ("torch", "transformers", "customtkinter", "kokoro", "language_tags",
                "espeakng_loader", "faster_whisper", "kokoro_onnx", "melo", "g2p_en",
                "unidic_lite", "unidic", "gruut", "gruut_lang_en", "gruut_lang_es",
                "gruut_lang_fr", "cached_path", "nltk", "pykakasi", "regex", "jamo",
                "anyascii", "cn2an", "jieba", "pypinyin", "librosa", "piper",
                "soundfile", "pygame"):
        command += ["--collect-all", pkg]
    for mod in _EXCLUDE:
        command += ["--exclude-module", mod]
    command += ["--hidden-import", "pydub",
                "--workpath", str(build_dir), "--distpath", str(BASE_DIR / "dist")]

    if not _bundle_models(command):
        return False

    # Locate and bundle NLTK data
    try:
        import nltk
        for p in nltk.data.path:
            if os.path.exists(p) and os.listdir(p):
                print(f"[*] Bundling NLTK data from: {p}")
                _add(command, p, "nltk_data")
                break
    except Exception as e:
        print(f"[*] Warning: Could not locate NLTK data directory: {e}")

    _bundle_vc_runtime(command)
    _add(command, str(BASE_DIR / "assets"), "assets")

    command.append(str(BASE_DIR / "tts_app.py"))

    print(f"[*] Running PyInstaller...")
    try:
        subprocess.check_call(command, cwd=BASE_DIR)
    except subprocess.CalledProcessError as e:
        print(f"[!] Build failed with exit code: {e.returncode}")
        return False
    print(f"\n[+] PyInstaller compilation successful: {APP_DIR}")

    _prune()
    _write_notices()

    if not args.skip_test and not _selftest():
        print("[!] Self-test failed. Not building the installer.")
        return False

    size = sum(f.stat().st_size for f in APP_DIR.rglob("*") if f.is_file())
    print(f"[+] App folder: {size / 2**30:.2f} GiB")

    if "github.com" in SOURCE_URL:
        print(f"[*] Reminder: the GPL source offer points at {SOURCE_URL}. "
              "It must be publicly reachable before you distribute the installer.")

    if args.installer:
        return _build_installer()
    return True


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--installer", action="store_true", help="also compile the Inno Setup installer")
    ap.add_argument("--skip-test", action="store_true", help="skip the frozen self-test")
    sys.exit(0 if build(ap.parse_args()) else 1)
