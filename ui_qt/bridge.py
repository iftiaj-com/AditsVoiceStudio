"""
ui_qt/bridge.py
───────────────
Run a function on the Qt GUI thread from any thread.

Qt widgets may only be touched on the GUI thread; doing it from a worker can
crash the app. Workers call ui(fn, *args) instead (the Qt counterpart of
Tk's after(0, ...)): the call is sent through a queued signal and runs on the
GUI thread at its next event-loop turn.
"""

from PySide6.QtCore import QObject, Qt, QThread, Signal, Slot
from PySide6.QtWidgets import QApplication


class _Dispatcher(QObject):
    _call = Signal(object)

    def __init__(self):
        super().__init__()
        # Queued even from the GUI thread, so ui() always runs later, like after(0).
        self._call.connect(self._run, Qt.QueuedConnection)

    @Slot(object)
    def _run(self, item):
        fn, args = item
        try:
            fn(*args)
        except RuntimeError as e:
            # "Internal C++ object already deleted": a widget closed meanwhile
            if "already deleted" not in str(e):
                raise


_dispatcher = None


def install():
    """Create the dispatcher on the GUI thread. Call once after QApplication()."""
    global _dispatcher
    app = QApplication.instance()
    assert app is not None and QThread.currentThread() is app.thread()
    _dispatcher = _Dispatcher()


def ui(fn, *args):
    """Run fn(*args) on the GUI thread at its next event-loop turn."""
    if _dispatcher is not None:
        _dispatcher._call.emit((fn, args))
