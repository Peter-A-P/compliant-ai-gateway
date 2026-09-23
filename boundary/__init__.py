"""boundary: the one library every model call in the portfolio goes through.

Public interface (frozen 2026-09-08, see docs/interface.md):
    Gateway, ChatRequest, ChatResponse, Usage, Mode, DataClass (0.4), RawResponse, and the
    errors below.
"""

__version__ = "0.7.0"

from boundary.errors import (
    BatchNotReady,
    BoundaryError,
    ConfigError,
    PassthroughViolation,
    ProviderError,
    SpendCapExceeded,
    UnknownAlias,
    UnknownPrice,
)
from boundary.gateway import Gateway, RawResponse
from boundary.providers.base import BatchItemResult, BatchProgress
from boundary.types import BatchHandle, ChatRequest, ChatResponse, DataClass, Mode, Usage

__all__ = [
    "BatchHandle",
    "BatchItemResult",
    "BatchNotReady",
    "BatchProgress",
    "BoundaryError",
    "ChatRequest",
    "ChatResponse",
    "ConfigError",
    "DataClass",
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
