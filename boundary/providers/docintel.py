"""Azure AI Document Intelligence over raw HTTP (1.6): a document in, its fields and words out.

Asked for by project 16, which measures a managed document service beside the vision models
and wants its cost in the same ledger. Verified against the REST reference for API version
2024-11-30 (v4.0 GA) on 2026-10-24:
    https://learn.microsoft.com/en-us/rest/api/aiservices/document-models/analyze-document

    POST {endpoint}/documentintelligence/documentModels/{modelId}:analyze?api-version=...
         {"base64Source": "<the document, base64>"}
      -> 202, header Operation-Location: .../documentModels/{modelId}/analyzeResults/{id}?...
    GET  {Operation-Location}
      -> {"status": "notStarted" | "running" | "succeeded" | "failed" | "canceled",
          "analyzeResult": {"pages": [...], "documents": [{"fields": {...}}], ...},
          "error"?: {"code", "message"}}

**Asynchronous only, so it is a batch of one.** An analysis is accepted, runs, and is read
back later, which is exactly the shape of the gateway's batch path: a submit that writes the
ledger row in flight, a status call, a results call that completes the row. A ChatRequest is
one document and a batch carries exactly one; the Operation-Location URL is the batch id. The
single-call path cannot hold it (the POST returns no answer), so `chat` refuses this kind by
name before anything is sent, rather than paying for an analysis whose result it would drop.

**How a ChatRequest becomes an analysis.** One user message whose content holds exactly one
document block, `{"type": "document", "source": {"type": "base64", "media_type", "data"}}`,
the shape Anthropic takes for a PDF. The model is `<tier>/<modelId>`: `prebuilt/prebuilt-
receipt`, `prebuilt/prebuilt-layout`, `custom/<a model this resource trained>`. The tier is
what the service bills by, so it is the price list's key (`price_key`), and the model id is
what goes in the URL. `extra["pages"]` states how many pages the document has; it is the
pre-call estimate, and the bill is read from the result (`analyzeResult.pages`). Nothing else
in the request means anything to the service, so a system prompt, a temperature or a second
message is refused rather than dropped.

**What comes back.** `ChatResponse.text` is `analyzeResult` as canonical JSON: the fields a
model extracted under `documents`, and the words and their positions under `pages`, which is
what a custom model is later trained against. The usage is pages as input tokens and nothing
as output, and the price list carries this provider's rates per million pages, so the
gateway's arithmetic costs a page the way the service bills it.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, ClassVar
from urllib.parse import quote

from boundary.config import ProviderConfig, ProviderKind
from boundary.errors import ProviderError
from boundary.providers._json import dumps, loads
from boundary.providers.base import (
    BatchItem,
    BatchItemResult,
    BatchProgress,
    BatchSubmitted,
    BuiltRequest,
    ParsedResponse,
)
from boundary.routes import ModelRef
from boundary.types import ChatRequest, Usage

API_VERSION = "2024-11-30"
#: The tiers the service bills by, and so the keys of this provider's price entries.
TIERS = frozenset({"read", "layout", "prebuilt", "custom"})
_RUNNING = frozenset({"notStarted", "running"})


def split_model(model: str) -> tuple[str, str]:
    """`<tier>/<modelId>` as (tier, modelId), or ValueError."""
    tier, sep, model_id = model.partition("/")
    if not sep or tier not in TIERS or not model_id:
        raise ValueError(
            f"a Document Intelligence model is <tier>/<modelId> with the tier one of "
            f"{', '.join(sorted(TIERS))}; got {model!r}"
        )
    return tier, model_id


def _document(request: ChatRequest) -> str:
    """The one document's base64 data, or ValueError for anything the service cannot take."""
    if request.system is not None or request.temperature is not None or request.stop:
        raise ValueError(
            "a Document Intelligence request is a document and nothing else: no system "
            "prompt, temperature or stop sequences"
        )
    if len(request.messages) != 1 or request.messages[0].get("role") != "user":
        raise ValueError("a Document Intelligence request carries exactly one user message")
    content = request.messages[0].get("content")
    blocks = [b for b in content if isinstance(b, Mapping)] if isinstance(content, list) else []
    documents = [b for b in blocks if b.get("type") == "document"]
    if len(documents) != 1 or len(blocks) != 1:
        raise ValueError(
            "a Document Intelligence request's message holds exactly one document block"
        )
    source = documents[0].get("source")
    if not isinstance(source, Mapping) or source.get("type") != "base64":
        raise ValueError("the document block's source is {'type': 'base64', 'data': ...}")
    data = source.get("data")
    if not isinstance(data, str) or not data:
        raise ValueError("the document block carries no data")
    return data


class AzureDocIntelAdapter:
    kind: ClassVar[ProviderKind] = ProviderKind.AZURE_DOCINTEL
    uploads_input_file: ClassVar[bool] = False
    #: The single-call path cannot carry an asynchronous analysis; see the module docstring.
    async_only: ClassVar[bool] = True
    #: The results are read from the batch id itself (the Operation-Location URL).
    results_at_batch_id: ClassVar[bool] = True

    # -- what the gateway asks of every adapter -------------------------------------------

    def build_request(
        self,
        ref: ModelRef,
        request: ChatRequest,
        provider: ProviderConfig,
        api_key: str | None,
    ) -> BuiltRequest:
        _, model_id = split_model(ref.model)
        version = provider.api_version or API_VERSION
        url = (
            provider.base_url.rstrip("/")
            + f"/documentintelligence/documentModels/{quote(model_id, safe='')}:analyze"
            + f"?api-version={version}&stringIndexType=unicodeCodePoint"
        )
        headers: dict[str, str] = {"content-type": "application/json", **provider.headers}
        if api_key:
            headers["ocp-apim-subscription-key"] = api_key
        return BuiltRequest(
            method="POST",
            url=url,
            headers=headers,
            body=dumps({"base64Source": _document(request)}),
        )

    def parse_response(
        self, status: int, headers: Mapping[str, str], body: bytes
    ) -> ParsedResponse:
        """A finished analysis, as the GET on its Operation-Location returns it."""
        try:
            raw = loads(body)
        except ValueError as e:
            raise ProviderError(
                "azure_docintel", status, body.decode("utf-8", "replace")[:500], headers=headers
            ) from e
        result = raw.get("analyzeResult") if isinstance(raw, dict) else None
        if not isinstance(result, dict):
            raise ProviderError(
                "azure_docintel", status, body.decode("utf-8", "replace")[:500], headers=headers
            )
        return ParsedResponse(
            text=json.dumps(result, sort_keys=True, ensure_ascii=False),
            finish_reason="stop",
            model_returned=result.get("modelId")
            if isinstance(result.get("modelId"), str)
            else None,
            usage=self.parse_usage(raw),
            raw=raw,
        )

    def parse_usage(self, raw: Any) -> Usage:
        """Pages analysed, as input tokens: what the service bills."""
        result = raw.get("analyzeResult") if isinstance(raw, dict) else None
        pages = result.get("pages") if isinstance(result, dict) else None
        return Usage(input_tokens=len(pages) if isinstance(pages, list) else 0, output_tokens=0)

    def parse_error(
        self, provider_name: str, status: int, headers: Mapping[str, str], body: bytes
    ) -> ProviderError:
        text = body.decode("utf-8", "replace")
        try:
            raw = loads(body)
        except ValueError:
            return ProviderError(provider_name, status, text, headers=headers)
        err = raw.get("error") if isinstance(raw, dict) else None
        if isinstance(err, dict):
            inner = err.get("innererror")
            text = f"{err.get('code') or 'error'}: {err.get('message', '')}"
            if isinstance(inner, dict) and inner.get("message"):
                text += f" ({inner.get('code')}: {inner['message']})"
        return ProviderError(provider_name, status, text, headers=headers)

    # -- hooks the gateway reads where an adapter has them ---------------------------------

    def estimate_usage(self, request: ChatRequest) -> Usage:
        """The pre-call estimate: the pages the caller says the document has. Required,
        because the base64 of a scanned page says nothing useful about its page count."""
        pages = request.extra.get("pages")
        if not isinstance(pages, int) or isinstance(pages, bool) or pages < 1:
            raise ValueError(
                "a Document Intelligence request needs extra['pages'], the document's page "
                "count, for the pre-call estimate"
            )
        return Usage(input_tokens=pages, output_tokens=0)

    def price_key(self, model: str) -> str:
        """The price list key for a model: the tier the service bills it at. The model id
        the service returns carries no tier, so it is left as it is, finds no entry, and the
        gateway falls back to the requested model's, which does."""
        try:
            return split_model(model)[0]
        except ValueError:
            return model

    # -- the batch of one ------------------------------------------------------------------

    def build_batch_submit(
        self, items: Sequence[BatchItem], provider: ProviderConfig, api_key: str | None
    ) -> BuiltRequest:
        if len(items) != 1:
            raise ValueError(
                f"a Document Intelligence analysis is one document; submit {len(items)} "
                "documents as that many batches of one"
            )
        _, ref, request = items[0]
        return self.build_request(ref, request, provider, api_key)

    def parse_batch_submit(
        self, status: int, headers: Mapping[str, str], body: bytes
    ) -> BatchSubmitted:
        location = next((v for k, v in headers.items() if k.lower() == "operation-location"), None)
        if status != 202 or not location:
            raise ProviderError(
                "azure_docintel",
                status,
                f"an accepted analysis is a 202 with an Operation-Location header; got "
                f"{status} {body.decode('utf-8', 'replace')[:300]}",
                headers=headers,
            )
        return BatchSubmitted(batch_id=location, processing_status="notStarted")

    def _get(self, url: str, provider: ProviderConfig, api_key: str | None) -> BuiltRequest:
        if not url.startswith(provider.base_url.rstrip("/") + "/"):
            # The batch id is a URL the service returned; the key is only ever sent to the
            # host this provider entry names.
            raise ValueError(f"{url!r} is not on {provider.base_url}")
        headers: dict[str, str] = dict(provider.headers)
        if api_key:
            headers["ocp-apim-subscription-key"] = api_key
        return BuiltRequest(method="GET", url=url, headers=headers, body=b"")

    def build_batch_status(
        self, batch_id: str, provider: ProviderConfig, api_key: str | None
    ) -> BuiltRequest:
        return self._get(batch_id, provider, api_key)

    def parse_batch_status(
        self, status: int, headers: Mapping[str, str], body: bytes
    ) -> BatchProgress:
        raw = loads(body)
        state = str(raw.get("status")) if isinstance(raw, dict) else "unknown"
        ended = state not in _RUNNING
        return BatchProgress(
            batch_id="",
            processing_status=state,
            ended=ended,
            results_url=None,
            counts={"succeeded": 1} if state == "succeeded" else {},
            raw=raw,
        )

    def build_batch_results(
        self, results_url: str, provider: ProviderConfig, api_key: str | None
    ) -> BuiltRequest:
        return self._get(results_url, provider, api_key)

    def parse_batch_results(
        self, status: int, headers: Mapping[str, str], body: bytes
    ) -> list[BatchItemResult]:
        """The one result, by position: the batch holds one request, so `#0` is it."""
        raw = loads(body)
        state = raw.get("status") if isinstance(raw, dict) else None
        if state == "succeeded":
            return [
                BatchItemResult(
                    custom_id="#0",
                    outcome="succeeded",
                    parsed=self.parse_response(status, headers, body),
                )
            ]
        err = raw.get("error") if isinstance(raw, dict) else None
        message = (
            f"{err.get('code')}: {err.get('message')}" if isinstance(err, dict) else str(state)
        )
        outcome = "canceled" if state == "canceled" else "errored"
        return [BatchItemResult(custom_id="#0", outcome=outcome, error=message)]
