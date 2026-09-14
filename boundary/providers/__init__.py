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
    UploadingBatchAdapter,
)
from boundary.providers.foundry import AzureFoundryAdapter
from boundary.providers.google import GoogleAdapter
from boundary.providers.google_batch import GoogleBatchAdapter
from boundary.providers.openai_batch import OpenAICompatBatchAdapter
from boundary.providers.openai_compat import OpenAICompatAdapter
from boundary.providers.vertex import VertexAdapter

# One adapter instance per kind. Adapters are pure, so sharing them is safe.
#
# The batch-capable OpenAI and Google adapters subclass the single-call ones and add the
# batch methods, so one instance of each serves both tables and a single call and a batched
# call cannot drift apart.
_ANTHROPIC = AnthropicAdapter()
_OPENAI_COMPAT = OpenAICompatBatchAdapter()
_GOOGLE = GoogleBatchAdapter()

ADAPTERS: dict[ProviderKind, Adapter] = {
    ProviderKind.ANTHROPIC: _ANTHROPIC,
    ProviderKind.OPENAI_COMPAT: _OPENAI_COMPAT,
    ProviderKind.GOOGLE: _GOOGLE,
    ProviderKind.AZURE_FOUNDRY: AzureFoundryAdapter(),
    ProviderKind.GCP_VERTEX: VertexAdapter(),
}

# Only the providers whose batch endpoints are implemented. A kind that is absent is
# refused by name when a batch is submitted, rather than failing somewhere deeper.
#
# Being in here is necessary and not sufficient: the provider entry also has to serve
# batches (ProviderConfig.serves_batches). A local Ollama server is `openai_compat` and has
# no /v1/batches, so the kind being listed here must not be read as every host of that kind
# having the endpoint.
#
# Foundry and Vertex are deliberately absent: the Message Batches API is on both platforms'
# unsupported lists, so a batch aimed at either is refused by name before anything is sent.
BATCH_ADAPTERS: dict[ProviderKind, BatchAdapter] = {
    ProviderKind.ANTHROPIC: _ANTHROPIC,
    ProviderKind.OPENAI_COMPAT: _OPENAI_COMPAT,
    ProviderKind.GOOGLE: _GOOGLE,
}

__all__ = [
    "ADAPTERS",
    "BATCH_ADAPTERS",
    "Adapter",
    "AnthropicAdapter",
    "AzureFoundryAdapter",
    "BatchAdapter",
    "BatchItem",
    "BatchItemResult",
    "BatchProgress",
    "BatchSubmitted",
    "BuiltRequest",
    "GoogleAdapter",
    "GoogleBatchAdapter",
    "OpenAICompatAdapter",
    "OpenAICompatBatchAdapter",
    "ParsedResponse",
    "UploadingBatchAdapter",
    "VertexAdapter",
]
