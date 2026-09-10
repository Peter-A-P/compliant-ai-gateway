"""Anthropic Messages API over raw HTTP. `anthropic-version` pinned from configuration.

Request shape (POST {base_url}/v1/messages):
    {"model", "messages", "max_tokens", "system"?, "temperature"?, "stop_sequences"?, ...extra}
Response shape:
    {"id", "type": "message", "role", "model", "content": [{"type": "text", "text"}, ...],
     "stop_reason", "usage": {"input_tokens", "output_tokens",
     "cache_read_input_tokens"?, "cache_creation_input_tokens"?}}
Error shape:
    {"type": "error", "error": {"type", "message"}}
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from boundary.config import ProviderConfig, ProviderKind
from boundary.errors import ProviderError
from boundary.providers._json import dumps, loads
from boundary.providers.base import BuiltRequest, ParsedResponse
from boundary.routes import ModelRef
from boundary.types import ChatRequest, Usage

DEFAULT_API_VERSION = "2023-06-01"


class AnthropicAdapter:
    kind: ClassVar[ProviderKind] = ProviderKind.ANTHROPIC

    def build_request(
        self,
        ref: ModelRef,
        request: ChatRequest,
        provider: ProviderConfig,
        api_key: str | None,
    ) -> BuiltRequest:
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
        headers: dict[str, str] = {
            "content-type": "application/json",
            "anthropic-version": ref.api_version or provider.api_version or DEFAULT_API_VERSION,
            **provider.headers,
        }
        if api_key:
            headers["x-api-key"] = api_key
        return BuiltRequest(
            method="POST",
            url=provider.base_url.rstrip("/") + "/v1/messages",
            headers=headers,
            body=dumps(body),
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
