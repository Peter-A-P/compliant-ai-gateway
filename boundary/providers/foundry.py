"""Claude on Microsoft Foundry: the Anthropic Messages body on an Azure-hosted endpoint.

Verified against the current documentation on 2026-09-14
(https://platform.claude.com/docs/en/build-with-claude/claude-in-microsoft-foundry), as the
adapter table in PLAN.md section 4 requires before any adapter is written.

What differs from the Anthropic adapter, and nothing else does:

    URL      POST {base_url}/v1/messages, where base_url is
             https://{resource}.services.ai.azure.com/anthropic
    Auth     `api-key: {key}`, an Azure-issued key, rather than `x-api-key`. Foundry also
             accepts `x-api-key`, and Entra ID bearer tokens; the key header is what the
             configuration here uses, and an Entra deployment sets `authorization` through
             the provider entry's `headers` instead.
    Version  `anthropic-version` is still sent and still pinned from configuration.
    Body     identical, including `model`, which on Foundry is the *deployment* name. A
             deployment may be named anything, so the price file is keyed by the deployment
             name the ledger records, and an unknown one writes an uncosted row.

There is no `api-version` query parameter. That is an Azure OpenAI convention and Foundry's
Claude endpoints do not use it, so sending one would be cargo cult.

The response is byte-identical in shape to the Claude API's, including the `usage` object,
so parsing is inherited rather than reimplemented: one parser means a change to how usage is
read cannot drift between the direct vendor and the platform.

Batches are not offered here (the Message Batches API is on Foundry's unsupported list), so
this adapter deliberately does not implement BatchAdapter. A batch submitted to a Foundry
provider is refused by name in boundary.providers, before anything is sent.

Billing is in Azure Marketplace Claude Consumption Units rather than in dollars per token,
which is why `config/prices/` carries a separate Foundry price file: the same model on
Foundry and on the Claude API are two price entries, not one.
"""

from __future__ import annotations

from typing import ClassVar

from boundary.config import ProviderConfig, ProviderKind
from boundary.providers._json import dumps
from boundary.providers.anthropic import DEFAULT_API_VERSION, MESSAGES_PATH, AnthropicAdapter
from boundary.providers.base import BuiltRequest
from boundary.routes import ModelRef
from boundary.types import ChatRequest


class AzureFoundryAdapter(AnthropicAdapter):
    """Anthropic's Messages API, reached through an Azure Foundry resource.

    Subclasses the Anthropic adapter rather than copying it. The body builder and every
    parser are inherited unchanged, because they are the same API; only the URL and the
    authentication header differ, and those are the two methods overridden below.
    """

    kind: ClassVar[ProviderKind] = ProviderKind.AZURE_FOUNDRY

    def _headers(
        self, provider: ProviderConfig, api_key: str | None, api_version: str | None = None
    ) -> dict[str, str]:
        headers: dict[str, str] = {
            "content-type": "application/json",
            "anthropic-version": api_version or provider.api_version or DEFAULT_API_VERSION,
            **provider.headers,
        }
        if api_key:
            headers["api-key"] = api_key
        return headers

    def build_request(
        self,
        ref: ModelRef,
        request: ChatRequest,
        provider: ProviderConfig,
        api_key: str | None,
    ) -> BuiltRequest:
        return BuiltRequest(
            method="POST",
            url=provider.base_url.rstrip("/") + MESSAGES_PATH,
            headers=self._headers(provider, api_key, ref.api_version),
            body=dumps(self.message_body(ref, request)),
        )
