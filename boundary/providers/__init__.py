"""Provider adapters. Each builds a request body itself and parses the response itself,
over raw HTTP with pinned API version headers. No vendor SDKs (PLAN.md section 2.2)."""

from boundary.config import ProviderKind
from boundary.providers.anthropic import AnthropicAdapter
from boundary.providers.base import Adapter, BuiltRequest, ParsedResponse
from boundary.providers.openai_compat import OpenAICompatAdapter

# One adapter instance per kind. Adapters are pure, so sharing them is safe.
ADAPTERS: dict[ProviderKind, Adapter] = {
    ProviderKind.ANTHROPIC: AnthropicAdapter(),
    ProviderKind.OPENAI_COMPAT: OpenAICompatAdapter(),
}

__all__ = [
    "ADAPTERS",
    "Adapter",
    "AnthropicAdapter",
    "BuiltRequest",
    "OpenAICompatAdapter",
    "ParsedResponse",
]
