"""The Gemini Batch API, with inlined requests.

Verified against the current documentation on 2026-09-14:
    https://ai.google.dev/gemini-api/docs/batch-api

One round trip to submit, like Anthropic and unlike OpenAI, because the requests go inline
rather than as an uploaded file:

    POST {base}/v1beta/models/{model}:batchGenerateContent
         {"batch": {"display_name", "input_config": {"requests": {"requests": [
             {"request": {...}, "metadata": {"key": "<custom_id>"}}, ...]}}}}
      -> {"name": "batches/123456789"}
    GET  {base}/v1beta/{batch_name}
      -> {"name", "state": "JOB_STATE_SUCCEEDED",
          "response": {"inlinedResponses": [{"response": {...}}, ...]}}

Inline requests are capped at 20 MB for the whole batch, which is a real limit rather than a
formality: the caller chunks, and a batch that exceeds it is refused here by name rather than
by a 400 from the vendor.

**One model per batch.** The model is in the URL, not in each request, so a batch is
single-model by construction. The gateway already groups by resolved model before submitting,
and this adapter refuses a mixed batch rather than silently sending every request to the first
item's model.

**The results come back in the status response.** There is no separate results file and no
results URL. To fit the shape the gateway already has, `results_url` carries the batch name and
`build_batch_results` fetches the same operation a second time. That is one redundant GET
against a free endpoint, and it buys a single code path for collecting every vendor's batch,
including the cross-process collection that project 02 depends on.

**Mapping a response back to a request is the risk here.** Anthropic and OpenAI both return the
`custom_id` on every result, so order does not matter. Google's documented shape puts the
caller's key in the *request* metadata and the documentation does not promise it comes back on
the response. So this adapter reads `metadata.key` when it is there and falls back to position
when it is not, and says which it used. Position is a correct fallback only because
`inlinedResponses` is documented as aligned with the request list, and the count is checked
before any of it is trusted: a response list of a different length is refused rather than
zipped, because a silently shifted mapping would put one call's usage on another call's row,
which is the worst thing a cost ledger can do.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, ClassVar

from boundary.config import ProviderConfig, ProviderKind
from boundary.errors import ProviderError
from boundary.providers._json import dumps, loads
from boundary.providers.base import (
    BatchItem,
    BatchItemResult,
    BatchProgress,
    BatchSubmitted,
    BuiltRequest,
)
from boundary.providers.google import GoogleAdapter

# Google's own words. Anything not running is terminal.
_RUNNING_STATES = frozenset({"JOB_STATE_PENDING", "JOB_STATE_RUNNING", "JOB_STATE_QUEUED"})
_SUCCEEDED = "JOB_STATE_SUCCEEDED"

# The documented ceiling for inlined requests, in bytes.
MAX_INLINE_BYTES = 20 * 1024 * 1024


class GoogleBatchAdapter(GoogleAdapter):
    """Gemini batches. Inherits every single-call method unchanged."""

    kind: ClassVar[ProviderKind] = ProviderKind.GOOGLE
    uploads_input_file: ClassVar[bool] = False

    # -- submit -------------------------------------------------------------------------

    def build_batch_submit(
        self,
        items: Sequence[BatchItem],
        provider: ProviderConfig,
        api_key: str | None,
    ) -> BuiltRequest:
        if not items:
            raise ProviderError("google", "batch_submit", "a batch needs at least one request")
        models = {ref.model for _, ref, _ in items}
        if len(models) > 1:
            raise ProviderError(
                "google",
                "batch_submit",
                "a Gemini batch names its model in the URL, so every request in it must use "
                f"the same model; got {sorted(models)}",
            )
        model = next(iter(models))

        requests: list[dict[str, Any]] = []
        for custom_id, ref, request in items:
            requests.append(
                {
                    "request": self.generate_body(ref, request),
                    "metadata": {"key": custom_id},
                }
            )
        body = dumps(
            {
                "batch": {
                    "display_name": "boundary",
                    "input_config": {"requests": {"requests": requests}},
                }
            }
        )
        if len(body) > MAX_INLINE_BYTES:
            raise ProviderError(
                "google",
                "batch_too_large",
                f"inlined Gemini batches are capped at {MAX_INLINE_BYTES} bytes and this one "
                f"is {len(body)}; submit fewer requests per batch",
            )
        version = provider.api_version or "v1beta"
        return BuiltRequest(
            method="POST",
            url=f"{provider.base_url.rstrip('/')}/{version}/models/{model}:batchGenerateContent",
            headers=self._headers(provider, api_key),
            body=body,
        )

    def parse_batch_submit(
        self, status: int, headers: Mapping[str, str], body: bytes
    ) -> BatchSubmitted:
        raw = self._object(status, headers, body)
        name = raw.get("name")
        if not isinstance(name, str) or not name:
            raise ProviderError(
                "google", status, "batch create returned no name", headers=headers
            )
        return BatchSubmitted(
            batch_id=name,
            processing_status=str(raw.get("state", "JOB_STATE_PENDING")),
            raw=raw,
        )

    # -- status -------------------------------------------------------------------------

    def build_batch_status(
        self, batch_id: str, provider: ProviderConfig, api_key: str | None
    ) -> BuiltRequest:
        return BuiltRequest(
            method="GET",
            url=self._batch_url(batch_id, provider),
            headers=self._headers(provider, api_key),
            body=b"",
        )

    def parse_batch_status(
        self, status: int, headers: Mapping[str, str], body: bytes
    ) -> BatchProgress:
        raw = self._object(status, headers, body)
        state = str(raw.get("state", ""))
        name = str(raw.get("name", ""))
        ended = state not in _RUNNING_STATES and state != ""
        counts: dict[str, int] = {}
        stats = raw.get("batchStats")
        if isinstance(stats, dict):
            counts = {k: int(v) for k, v in stats.items() if isinstance(v, int)}
        # There is no results file. The name is what build_batch_results fetches, and only a
        # batch that actually succeeded has responses to read.
        return BatchProgress(
            batch_id=name,
            processing_status=state,
            ended=ended,
            results_url=name if state == _SUCCEEDED and name else None,
            counts=counts,
            raw=raw,
        )

    # -- results ------------------------------------------------------------------------

    def build_batch_results(
        self, results_url: str, provider: ProviderConfig, api_key: str | None
    ) -> BuiltRequest:
        return BuiltRequest(
            method="GET",
            url=self._batch_url(results_url, provider),
            headers=self._headers(provider, api_key),
            body=b"",
        )

    def parse_batch_results(
        self, status: int, headers: Mapping[str, str], body: bytes
    ) -> list[BatchItemResult]:
        raw = self._object(status, headers, body)
        response = raw.get("response")
        inlined: Any = None
        if isinstance(response, dict):
            inlined = response.get("inlinedResponses")
        if not isinstance(inlined, list):
            raise ProviderError(
                "google",
                status,
                f"batch {raw.get('name')!r} ended as {raw.get('state')!r} with no inlined "
                "responses to read",
                headers=headers,
            )

        out: list[BatchItemResult] = []
        for i, entry in enumerate(inlined):
            if not isinstance(entry, dict):
                out.append(BatchItemResult(custom_id="", outcome="errored", error="bad entry"))
                continue
            custom_id = _key_of(entry)
            if custom_id is None:
                # Positional fallback, correct only because the list is documented as aligned
                # with the requests. The gateway checks the count before trusting any of it.
                custom_id = f"#{i}"
            err = entry.get("error")
            inner = entry.get("response")
            if err is not None or not isinstance(inner, dict):
                out.append(
                    BatchItemResult(
                        custom_id=custom_id,
                        outcome="errored",
                        error=_error_text(err) or "no response",
                    )
                )
                continue
            out.append(
                BatchItemResult(
                    custom_id=custom_id,
                    outcome="succeeded",
                    parsed=self.parse_generate(inner),
                )
            )
        return out

    # -- shared -------------------------------------------------------------------------

    def _batch_url(self, name: str, provider: ProviderConfig) -> str:
        """`name` is already `batches/{id}`, which the versioned path takes verbatim."""
        version = provider.api_version or "v1beta"
        safe = name.strip("/")
        if not safe.startswith("batches/") or "/" in safe[len("batches/") :]:
            raise ProviderError(
                "google",
                "batch_name",
                f"refusing a batch name that is not 'batches/<id>': {name!r}",
            )
        return f"{provider.base_url.rstrip('/')}/{version}/{safe}"

    def _object(
        self, status: int, headers: Mapping[str, str], body: bytes
    ) -> dict[str, Any]:
        if not 200 <= status < 300:
            raise ProviderError(
                "google", status, body.decode("utf-8", "replace")[:500], headers=headers
            )
        try:
            raw = loads(body)
        except ValueError as e:
            raise ProviderError(
                "google", status, body.decode("utf-8", "replace")[:500], headers=headers
            ) from e
        if not isinstance(raw, dict):
            raise ProviderError("google", status, "expected a JSON object", headers=headers)
        return raw


def _key_of(entry: Mapping[str, Any]) -> str | None:
    meta = entry.get("metadata")
    if isinstance(meta, dict):
        key = meta.get("key")
        if isinstance(key, str) and key:
            return key
    return None


def _error_text(err: object) -> str | None:
    if err is None:
        return None
    if isinstance(err, dict):
        message = err.get("message")
        code = err.get("code")
        if isinstance(message, str):
            return f"{code}: {message}" if code else message
    return str(err)


__all__ = ["GoogleBatchAdapter"]
