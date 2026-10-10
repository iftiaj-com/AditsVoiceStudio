"""
core/temp_cleanup.py
─────────────────────
Housekeeping for adits_vs_* temp directories.

Generation creates tempfile.mkdtemp(prefix="adits_vs_") directories.
Normal runs clean up after themselves, but a crash or force-quit leaves
orphans behind. sweep_orphan_temp_dirs() is called on startup to remove
them; the age threshold keeps a concurrently running second instance's
fresh directory safe (and on Windows, files opened by pygame in a live
instance can't be deleted anyway, so rmtree simply skips them).
"""

import os
import shutil
import tempfile
import time

TEMP_PREFIX = "adits_vs_"
LEGACY_PREFIXES = ("tts_studio_",)   # before the rebrand


def sweep_orphan_temp_dirs(max_age_seconds=3600):
    """Delete leftover adits_vs_* (and pre-rebrand tts_studio_*) dirs older than *max_age_seconds*.

    Returns the number of directories removed. Never raises.
    """
    root = tempfile.gettempdir()
    removed = 0
    try:
        names = os.listdir(root)
    except OSError:
        return 0

    now = time.time()
    for name in names:
        if not name.startswith((TEMP_PREFIX,) + LEGACY_PREFIXES):
            continue
        path = os.path.join(root, name)
        try:
            if not os.path.isdir(path):
                continue
            if now - os.path.getmtime(path) < max_age_seconds:
                continue  # recent — may belong to a live instance
            shutil.rmtree(path, ignore_errors=True)
            if not os.path.exists(path):
                removed += 1
        except OSError:
            continue
    return removed
