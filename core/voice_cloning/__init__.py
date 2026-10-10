"""
core/voice_cloning
───────────────────
Voice cloning (RVC via Applio, zero-shot via Seed-VC) for Adits Voice Studio.

The heavy work runs in separate processes inside their own Python venvs
(see runtime.py and vc_runtime/). This package only manages those
processes, so importing it never loads torch.
"""

import threading

_client = None
_client_lock = threading.Lock()


def get_client():
    """The shared VCClient, created on first use."""
    global _client
    with _client_lock:
        if _client is None:
            from core.voice_cloning.client import VCClient
            _client = VCClient()
        return _client


def is_training() -> bool:
    """True while a voice is training. Never creates the client."""
    with _client_lock:
        client = _client
    return client is not None and client.training_active


def shutdown_client():
    """Stop every worker process. Safe to call when none was ever started."""
    with _client_lock:
        client = _client
    if client is not None:
        client.shutdown()
