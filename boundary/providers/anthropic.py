"""Anthropic Messages API over raw HTTP. `anthropic-version` pinned from configuration.

Request shape (POST {base_url}/v1/messages):
    {"model", "messages", "max_tokens", "system"?, "temperature"?, "stop_sequences"?, ...extra}
Response shape:
    {"id", "type": "message", "role", "model", "content": [{"type": "text", "text"}, ...],
     "stop_reason", "usage": {"input_tokens", "output_tokens",
     "cache_read_input_tokens"?, "cache_creation_input_tokens"?}}
Error shape:
    {"type": "error", "error": {"type", "message"}}

Message Batches (v0.2), the same message body at half the price where latency does not
matter, which is the whole of project 02:
    POST   {base_url}/v1/messages/batches      {"requests": [{"custom_id", "params"}, ...]}
    GET    {base_url}/v1/messages/batches/{id} -> {"processing_status", "results_url", ...}
    GET    results_url                         -> JSON Lines, one object per request
Each results line is {"custom_id", "result": {"type": "succeeded", "message": {...}}}, or
type "errored", "canceled" or "expired". The params of a batched request are byte for byte
the body a single call would have sent, which is what `message_body` below guarantees.

No beta header is sent: batches are generally available under the pinned version above. A
vendor that later requires one takes it from `headers` on the provider entry, which is
configuration rather than a code change.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, ClassVar
from urllib.parse import urlsplit

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

DEFAULT_API_VERSION = "2023-06-01"

MESSAGES_PATH = "/v1/messages"
BATCHES_PATH = "/v1/messages/batches"

# The vendor's words for a batch that will not change again.
_ENDED = "ended"
_TERMINAL_OUTCOMES = frozenset({"succeeded", "errored", "canceled", "expired"})


class AnthropicAdapter:
    kind: ClassVar[ProviderKind] = ProviderKind.ANTHROPIC

    # -- shared -------------------------------------------------------------------------

    def message_body(self, ref: ModelRef, request: ChatRequest) -> dict[str, Any]:
        """The Messages body for one request.

        Shared by the single-call path and the batch path so that a request costs the same
        bytes either way: the same prompt submitted singly or in a batch has the same
        request hash in the ledger, and the pass-through byte-equality argument extends to
        batches without a second implementation to keep in step.
        """
        if request.max_tokens is None:
            raise ValueError(
                "Anthropic requires max_tokens; the gateway fills the default in standard mode"
            )
        body: dict[str, Any] = {
            "model": ref.model,
            "messages": [dict(m) for m in request.messages],
            "max_tokens": request.max_tokens,
        }
        if request.system is not None:
            body["system"] = request.system
        if request.temperature is not None:
            body["temperature"] = request.temperature
        if request.stop:
            body["stop_sequences"] = list(request.stop)
        body.update(request.extra)
        return body

    def _headers(
        self, provider: ProviderConfig, api_key: str | None, api_version: str | None = None
    ) -> dict[str, str]:
        headers: dict[str, str] = {
            "content-type": "application/json",
            "anthropic-version": api_version or provider.api_version or DEFAULT_API_VERSION,
            **provider.headers,
        }
        if api_key:
            headers["x-api-key"] = api_key
        return headers

    # -- single calls -------------------------------------------------------------------

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

    def parse_response(
        self, status: int, headers: Mapping[str, str], body: bytes
    ) -> ParsedResponse:
        try:
            raw = loads(body)
        except ValueError as e:
            raise ProviderError(
                "anthropic", status, body.decode("utf-8", "replace"), headers=headers
            ) from e
        if not isinstance(raw, dict) or raw.get("type") != "message":
            raise ProviderError(
                "anthropic", status, body.decode("utf-8", "replace")[:500], headers=headers
            )
        return self.parse_message(raw)

    def parse_message(self, raw: dict[str, Any]) -> ParsedResponse:
        """Read an already-decoded message object. A batch result carries the same object
        inline rather than as a body of its own."""
        text: str | None = None
        for block in raw.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "text":
                text = str(block.get("text", ""))
                break
        stop = raw.get("stop_reason")
        return ParsedResponse(
            text=text,
            finish_reason=str(stop).lower() if stop is not None else None,
            model_returned=raw.get("model"),
            usage=self.parse_usage(raw),
            raw=raw,
        )

    def parse_usage(self, raw: Any) -> Usage:
        if not isinstance(raw, dict):
            return Usage()
        u = raw.get("usage")
        if not isinstance(u, dict):
            return Usage()
        return Usage(
            input_tokens=int(u.get("input_tokens") or 0),
            output_tokens=int(u.get("output_tokens") or 0),
            cache_read_tokens=int(u.get("cache_read_input_tokens") or 0),
            cache_write_tokens=int(u.get("cache_creation_input_tokens") or 0),
        )

    def parse_error(
        self, provider_name: str, status: int, headers: Mapping[str, str], body: bytes
    ) -> ProviderError:
        text = body.decode("utf-8", "replace")
        try:
            raw = loads(body)
            err = raw.get("error") if isinstance(raw, dict) else None
            if isinstance(err, dict):
                text = f"{err.get('type', 'error')}: {err.get('message', '')}"
        except ValueError:
            pass
        return ProviderError(provider_name, status, text, headers=headers)

    # -- batches ------------------------------------------------------------------------

    def build_batch_submit(
        self,
        items: Sequence[BatchItem],
        provider: ProviderConfig,
        api_key: str | None,
    ) -> BuiltRequest:
        body = {
            "requests": [
                {"custom_id": custom_id, "params": self.message_body(ref, request)}
                for custom_id, ref, request in items
            ]
        }
        return BuiltRequest(
            method="POST",
            url=provider.base_url.rstrip("/") + BATCHES_PATH,
            headers=self._headers(provider, api_key),
            body=dumps(body),
        )

    def parse_batch_submit(
        self, status: int, headers: Mapping[str, str], body: bytes
    ) -> BatchSubmitted:
        raw = self._batch_object(status, headers, body)
        batch_id = raw.get("id")
        if not isinstance(batch_id, str) or not batch_id:
            raise ProviderError(
                "anthropic", status, "batch accepted without an id", headers=headers
            )
        return BatchSubmitted(
            batch_id=batch_id,
            processing_status=str(raw.get("processing_status") or "unknown"),
            raw=raw,
        )

    def build_batch_status(
        self, batch_id: str, provider: ProviderConfig, api_key: str | None
    ) -> BuiltRequest:
        return BuiltRequest(
            method="GET",
            url=f"{provider.base_url.rstrip('/')}{BATCHES_PATH}/{batch_id}",
            headers=self._headers(provider, api_key),
            body=b"",
        )

    def parse_batch_status(
        self, status: int, headers: Mapping[str, str], body: bytes
    ) -> BatchProgress:
        raw = self._batch_object(status, headers, body)
        counts = raw.get("request_counts")
        processing = str(raw.get("processing_status") or "unknown")
        results_url = raw.get("results_url")
        return BatchProgress(
            batch_id=str(raw.get("id") or ""),
            processing_status=processing,
            ended=processing == _ENDED,
            results_url=results_url if isinstance(results_url, str) and results_url else None,
            counts={k: int(v) for k, v in counts.items()} if isinstance(counts, dict) else {},
            raw=raw,
        )

    def build_batch_results(
        self, results_url: str, provider: ProviderConfig, api_key: str | None
    ) -> BuiltRequest:
        # The results URL comes back from the vendor and the request that fetches it carries
        # the API key, so it is checked against the configured host before it is used. A
        # results URL pointing somewhere else would hand the key to that somewhere else.
        want, got = urlsplit(provider.base_url), urlsplit(results_url)
        if got.scheme != want.scheme or got.netloc != want.netloc:
            raise ProviderError(
                "anthropic",
                "results_url",
                f"batch results URL {results_url!r} is not on the configured host "
                f"{want.scheme}://{want.netloc}; refusing to send the API key to it",
            )
        return BuiltRequest(
            method="GET",
            url=results_url,
            headers=self._headers(provider, api_key),
            body=b"",
        )

    def parse_batch_results(
        self, status: int, headers: Mapping[str, str], body: bytes
    ) -> list[BatchItemResult]:
        """One JSON object per line. A line that cannot be read at all is an error for the
        whole fetch: a results file half understood would complete some rows and silently
        leave others in flight."""
        out: list[BatchItemResult] = []
        for number, line in enumerate(body.decode("utf-8", "replace").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                raw = loads(line.encode("utf-8"))
            except ValueError as e:
                raise ProviderError(
                    "anthropic", status, f"batch results line {number} is not JSON", headers=headers
                ) from e
            if not isinstance(raw, dict):
                raise ProviderError(
                    "anthropic",
                    status,
                    f"batch results line {number} is not an object",
                    headers=headers,
                )
            custom_id = raw.get("custom_id")
            result = raw.get("result")
            if not isinstance(custom_id, str) or not isinstance(result, dict):
                raise ProviderError(
                    "anthropic",
                    status,
                    f"batch results line {number} has no custom_id and result pair",
                    headers=headers,
                )
            outcome = str(result.get("type") or "unknown")
            if outcome not in _TERMINAL_OUTCOMES:
                # A shape the vendor has added since. Recorded as an error rather than
                # guessed at, so the row says something true and the count is visible.
                out.append(
                    BatchItemResult(custom_id, outcome, error=f"unknown result type {outcome!r}")
                )
                continue
            message = result.get("message")
            if outcome == "succeeded" and isinstance(message, dict):
                out.append(BatchItemResult(custom_id, outcome, parsed=self.parse_message(message)))
            else:
                out.append(BatchItemResult(custom_id, outcome, error=self._result_error(result)))
        return out

    # -- helpers ------------------------------------------------------------------------

    def _batch_object(self, status: int, headers: Mapping[str, str], body: bytes) -> dict[str, Any]:
        try:
            raw = loads(body)
        except ValueError as e:
            raise ProviderError(
                "anthropic", status, body.decode("utf-8", "replace")[:500], headers=headers
            ) from e
        if not isinstance(raw, dict) or raw.get("type") != "message_batch":
            raise ProviderError(
                "anthropic", status, body.decode("utf-8", "replace")[:500], headers=headers
            )
        return raw

    @staticmethod
    def _result_error(result: Mapping[str, Any]) -> str | None:
        """The vendor's message for a request that did not succeed. The error object nests
        one level ({"error": {"error": {"type", "message"}}}), and older shapes do not, so
        both are unwrapped before giving up and stringifying."""
        err: Any = result.get("error")
        for _ in range(2):
            if not isinstance(err, dict):
                break
            if "message" in err:
                return f"{err.get('type', 'error')}: {err.get('message')}"
            err = err.get("error")
        return None if result.get("error") is None else str(result.get("error"))
