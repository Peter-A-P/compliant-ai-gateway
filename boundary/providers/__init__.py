"""Provider adapters. Each builds a request body itself and parses the response itself,
over raw HTTP with pinned API version headers. No vendor SDKs (PLAN.md section 2.2)."""

from boundary.config import ProviderKind
from boundary.providers.anthropic import AnthropicAdapter
from boundary.providers.base import (
    Adapter,
    BatchAdapter,
    BatchItem,
    BatchItemResult,
    BatchProgress,
    BatchSubmitted,
    BuiltRequest,
    ParsedResponse,
)
from boundary.providers.google import GoogleAdapter
from boundary.providers.openai_compat import OpenAICompatAdapter

# One adapter instance per kind. Adapters are pure, so sharing them is safe.
_ANTHROPIC = AnthropicAdapter()

ADAPTERS: dict[ProviderKind, Adapter] = {
    ProviderKind.ANTHROPIC: _ANTHROPIC,
    ProviderKind.OPENAI_COMPAT: OpenAICompatAdapter(),
    ProviderKind.GOOGLE: GoogleAdapter(),
}

# Only the providers whose batch endpoints are implemented. A kind that is absent is
# refused by name when a batch is submitted, rather than failing somewhere deeper.
# OpenAI has a batch API of its own shape; it is added when a project needs it.
BATCH_ADAPTERS: dict[ProviderKind, BatchAdapter] = {
    ProviderKind.ANTHROPIC: _ANTHROPIC,
}

__all__ = [
    "ADAPTERS",
    "BATCH_ADAPTERS",
    "Adapter",
    "AnthropicAdapter",
    "BatchAdapter",
    "BatchItem",
    "BatchItemResult",
    "BatchProgress",
    "BatchSubmitted",
    "BuiltRequest",
    "GoogleAdapter",
    "OpenAICompatAdapter",
    "ParsedResponse",
]
