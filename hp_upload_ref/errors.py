class UploadError(RuntimeError):
    """Base exception for the reference uploader."""


class RetryableUploadError(UploadError):
    """A transient failure for which reconnecting is appropriate."""


class FatalUploadError(UploadError):
    """A protocol, identity, or authorization failure that must not loop forever."""


class AuthenticationFailed(FatalUploadError):
    pass


class ChunkConflict(FatalUploadError):
    pass


class SessionError(FatalUploadError):
    pass


class ProtocolViolation(FatalUploadError):
    pass


class FlowStopped(FatalUploadError):
    """The server requested STOP_SAFELY."""


class FlowPaused(UploadError):
    """The server requested PAUSE_AFTER_CURRENT_CHUNK."""


class LocalChunkCorruption(FatalUploadError):
    """A locally durable .ready object no longer matches its manifest."""
