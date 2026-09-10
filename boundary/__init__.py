"""boundary: the one library every model call in the portfolio goes through.

Public interface (frozen 2026-09-08, see docs/interface.md):
    Gateway, ChatRequest, ChatResponse, Usage, Mode, RawResponse, and the errors below.
"""

__version__ = "0.1.0.dev2"

from boundary.errors import (
    BoundaryError,
    ConfigError,
    PassthroughViolation,
    ProviderError,
    SpendCapExceeded,
    UnknownAlias,
    UnknownPrice,
)
from boundary.gateway import Gateway, RawResponse
from boundary.types import ChatRequest, ChatResponse, Mode, Usage

__all__ = [
    "BoundaryError",
    "ChatRequest",
    "ChatResponse",
    "ConfigError",
    "Gateway",
    "Mode",
    "PassthroughViolation",
    "ProviderError",
    "RawResponse",
    "SpendCapExceeded",
    "UnknownAlias",
    "UnknownPrice",
    "Usage",
    "__version__",
]
