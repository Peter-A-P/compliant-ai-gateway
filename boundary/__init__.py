"""boundary: the one library every model call in the portfolio goes through.

Public interface (frozen 2026-09-09, see docs/interface.md):
    Gateway, ChatRequest, ChatResponse, Usage, Mode, and the errors below.

`Gateway` is exported once the adapters land (Sep 8 to 9); the types and errors are
final from day one so that callers can be written against them now.
"""

from boundary.errors import (
    BoundaryError,
    ConfigError,
    PassthroughViolation,
    ProviderError,
    SpendCapExceeded,
    UnknownAlias,
    UnknownPrice,
)
from boundary.types import ChatRequest, ChatResponse, Mode, Usage

__version__ = "0.1.0.dev1"

__all__ = [
    "BoundaryError",
    "ChatRequest",
    "ChatResponse",
    "ConfigError",
    "Mode",
    "PassthroughViolation",
    "ProviderError",
    "SpendCapExceeded",
    "UnknownAlias",
    "UnknownPrice",
    "Usage",
    "__version__",
]
