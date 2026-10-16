"""Speech-recognition backends.

``ciw.asr`` itself is cheap to import (no torch, no models); backends import their heavy
dependencies only inside ``transcribe``.
"""

from ciw.asr.base import (
    ASR_BACKENDS,
    ProgressFn,
    Transcriber,
    available_backends,
    get_transcriber,
    register_backend,
)

__all__ = [
    "ASR_BACKENDS",
    "ProgressFn",
    "Transcriber",
    "available_backends",
    "get_transcriber",
    "register_backend",
]
