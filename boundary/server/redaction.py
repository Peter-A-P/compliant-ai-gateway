"""Redaction in the proxy (0.15). The implementation moved to `boundary.redact.request` in
0.16 so it can run without the server extra; this module keeps the 0.15 import path."""

from boundary.redact.preserve import PRESERVE_LINE
from boundary.redact.request import (
    HOLD,
    Redacted,
    RedactionRefused,
    StreamRehydrator,
    leak_counts,
    redact_request,
)

__all__ = [
    "HOLD",
    "PRESERVE_LINE",
    "Redacted",
    "RedactionRefused",
    "StreamRehydrator",
    "leak_counts",
    "redact_request",
]
