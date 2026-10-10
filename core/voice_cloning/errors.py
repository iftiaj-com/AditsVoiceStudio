"""
core/voice_cloning/errors.py
─────────────────────────────
Exceptions raised by the voice-cloning layer. Every message is written to
be shown to the user as-is.
"""


class VCError(Exception):
    def __init__(self, message, hint=None):
        super().__init__(message)
        self.message = message
        self.hint = hint

    def user_text(self):
        return f"{self.message}\n{self.hint}" if self.hint else self.message


class VCNotInstalled(VCError):
    pass


class VCBusy(VCError):
    pass


class VCCancelled(VCError):
    def __init__(self, message="Cancelled"):
        super().__init__(message)


class VCWorkerError(VCError):
    """A request failed inside a worker. kind mirrors vc_runtime/vc_common.TaskError."""

    def __init__(self, kind, message, hint=None):
        super().__init__(message, hint)
        self.kind = kind


class InstallError(VCError):
    pass
