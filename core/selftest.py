"""
core/selftest.py
─────────────────
`AditsVoiceStudio.exe --selftest report.json` exercises the frozen build without
opening the window and writes a JSON report. build_exe.py runs it after every
build; it is also handy on a test PC after installing.

Checks real output, not just imports: each offline engine must produce a WAV
with audible samples, Kokoro runs before Piper (that order is the one that
broke espeak-ng on non-ASCII paths), and MP3 export must round-trip without
ffmpeg. MeloTTS is only import-checked, because its first use downloads
models; a network error there is reported as a warning, a missing module as
a failure.

The Qt window is built (not shown) and closed, so a Qt module or the Windows
platform plugin missing from the bundle fails the build.
"""

import json
import os
import sys
import tempfile
import time
import traceback


def _wav_stats(path):
    import soundfile as sf
    import numpy as np
    data, sr = sf.read(path, dtype="float32")
    return {"seconds": round(len(data) / sr, 2), "rate": sr,
            "peak": round(float(np.max(np.abs(data))) if len(data) else 0.0, 3)}


def run(report_path):
    from core import paths
    from core.version import __version__
    report = {"version": __version__, "frozen": paths.FROZEN,
              "bundle_dir": str(paths.bundle_dir()), "checks": {}, "ok": True}
    checks = report["checks"]

    def check(name, fn, fatal=True):
        t0 = time.time()
        try:
            checks[name] = {"ok": True, **(fn() or {})}
        except Exception as e:
            checks[name] = {"ok": False, "fatal": fatal, "error": f"{type(e).__name__}: {e}",
                            "trace": traceback.format_exc()[-2000:]}
            if fatal:
                report["ok"] = False
        checks[name]["secs"] = round(time.time() - t0, 1)

    tmp = tempfile.mkdtemp(prefix="adits_vs_selftest_")
    engines = {}

    def _engines():
        from engines.registry import get_available_engines
        for e in get_available_engines():
            engines[e.name] = e
        return {"available": list(engines)}
    check("engines", _engines)

    def _synth(prefix, **kw):
        def go():
            eng = next(e for n, e in engines.items() if n.startswith(prefix))
            out = os.path.join(tmp, prefix.split()[0].lower())
            os.makedirs(out, exist_ok=True)
            res = eng.synthesize("Hello, this is a quick self test.", out,
                                 device="cpu", status_cb=None,
                                 check_cancel=lambda: False, **kw)
            stats = _wav_stats(res["playback_file"])
            if stats["peak"] < 0.01 or stats["seconds"] < 0.5:
                raise RuntimeError(f"output is silent or too short: {stats}")
            return {"file": res["playback_file"], **stats}
        return go

    check("kokoro", _synth("Kokoro", voice_key="af_heart", kokoro_lang_code="a",
                           kokoro_speed=1.0))
    check("piper_after_kokoro", _synth("Piper"))
    check("pyttsx3", _synth("pyttsx3", voice_id="", rate=175, volume=0.9), fatal=False)

    def _mp3():
        from core.audio_io import load_segment, export_mp3
        src = checks.get("kokoro", {}).get("file")
        if not src:
            raise RuntimeError("no Kokoro output to encode")
        mp3 = os.path.join(tmp, "roundtrip.mp3")
        export_mp3(load_segment(src), mp3)
        back = load_segment(mp3)
        return {"bytes": os.path.getsize(mp3), "seconds": round(len(back) / 1000, 2)}
    check("mp3_roundtrip", _mp3)

    def _whisper():
        from core.subtitles import generate_subtitles
        src = checks.get("kokoro", {}).get("file")
        srt = os.path.join(tmp, "test.srt")
        generate_subtitles(src, srt, segmentation="sentence")
        with open(srt, encoding="utf-8") as f:
            text = f.read()
        if "self" not in text.lower():
            raise RuntimeError(f"transcript does not match: {text[:200]!r}")
        bundled = (paths.bundle_dir() / "models" / "whisper-base" / "model.bin").exists()
        if paths.FROZEN and not bundled:
            raise RuntimeError("Whisper model is not bundled; subtitles would need a download")
        return {"bundled_model": bundled, "srt_head": text[:120]}
    check("subtitles", _whisper)

    def _nltk_zips():
        # g2p_en looks these up by .zip name; without them it downloads them.
        nd = paths.bundle_dir() / "nltk_data"
        missing = [z for z in ("corpora/cmudict.zip", "taggers/averaged_perceptron_tagger.zip")
                   if paths.FROZEN and not (nd / z).exists()]
        if missing:
            raise RuntimeError(f"NLTK zips missing from bundle: {missing}")
        return {}

    def _melo():
        if "MeloTTS (Multi-Lingual)" not in engines:
            raise RuntimeError("MeloTTS not in engine list")
        try:
            import melo.api  # noqa: F401
        except (ImportError, ModuleNotFoundError):
            raise
        except Exception as e:   # network / HF errors: not a bundling bug
            return {"import": "network-dependent", "warning": f"{type(e).__name__}: {e}"}
        return {"import": "ok"}
    # Before melo: importing it re-downloads these into the bundle if missing.
    check("nltk_zips", _nltk_zips)
    check("melo_import", _melo)

    def _qt_window():
        # The Qt window (tts_app.py --qt) on the real Windows platform plugin,
        # built but never shown: fails here if qwindows.dll or a Qt module is
        # missing from the bundle, instead of on the user's first launch.
        from PySide6.QtWidgets import QApplication
        from ui_qt import bridge, theme
        from ui_qt.app import MainWindow
        app = QApplication.instance() or QApplication([sys.argv[0]])
        app.setStyle("Fusion")
        app.setStyleSheet(theme.stylesheet())
        bridge.install()
        win = MainWindow(background=False)
        app.processEvents()
        info = {"platform": app.platformName(), "title": win.windowTitle(),
                "engines": win.engine_panel.engine_names(),
                "effects": len(win._effects_panel.get_active_effects())}
        win.close()
        app.processEvents()
        if info["platform"] != "windows" and sys.platform == "win32":
            raise RuntimeError(f"Qt platform plugin is {info['platform']!r}, not 'windows'")
        return info
    check("qt_window", _qt_window)

    def _writable():
        out = {"logs": str(paths.logs_dir()), "output": str(paths.output_dir())}
        for k, d in out.items():
            probe = os.path.join(d, ".write_probe")
            with open(probe, "w") as f:
                f.write("x")
            os.remove(probe)
        out["HF_HOME"] = os.environ.get("HF_HOME", "")
        return out
    check("user_dirs", _writable)

    report["tmp_dir"] = tmp
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    return 0 if report["ok"] else 1
