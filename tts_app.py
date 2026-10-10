"""
tts_app.py  –  Adits Voice Studio entry point
──────────────────────────────────────────────
This file is intentionally tiny. All logic lives in the packages below:

  core/       – constants, design tokens, voice maps
  engines/    – TTS engine adapters + registry
  effects/    – audio DSP effects + registry
  ui/         – panels, theme helpers, and the main app window

Run with:
    python tts_app.py
    python tts_app.py --selftest report.json   (headless engine check)
"""

import sys

# Must run before ui.app imports torch / transformers / huggingface_hub,
# which read HF_HOME at import time.
from core.paths import configure_runtime
configure_runtime()

_MUTEX_NAME = "AditsVoiceStudioSingleInstance"   # installer.iss AppMutex uses the same name


def _already_running():
    """Hold a named mutex for the app's lifetime; True if another copy has it.

    Startup takes a minute or more, so users click the icon again; each extra
    copy costs over 1 GB of RAM. The installer also checks this mutex so it
    does not overwrite files of a running app.
    """
    if sys.platform != "win32":
        return False
    import ctypes
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    global _mutex_handle
    _mutex_handle = kernel32.CreateMutexW(None, False, _MUTEX_NAME)
    return kernel32.GetLastError() == 183   # ERROR_ALREADY_EXISTS


def _close_splash():
    try:
        import pyi_splash   # only exists in builds made with --splash
        pyi_splash.close()
    except Exception:
        pass


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--selftest":
        _close_splash()
        from core.selftest import run
        sys.exit(run(sys.argv[2]))

    if _already_running():
        _close_splash()
        import ctypes
        from core.version import APP_TITLE
        ctypes.windll.user32.MessageBoxW(
            None, f"{APP_TITLE} is already running.\n\nIt can take a minute to open "
                  "the first time, please wait for its window.", APP_TITLE, 0x40)
        sys.exit(0)

    from core.temp_cleanup import sweep_orphan_temp_dirs
    from ui.app import AditsVoiceStudioApp

    # Remove adits_vs_* temp dirs orphaned by a previous crash/force-quit.
    sweep_orphan_temp_dirs()
    app = AditsVoiceStudioApp()
    _close_splash()
    app.mainloop()
