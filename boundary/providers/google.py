"""Gemini API generateContent over raw HTTP, API version pinned from configuration.

Request shape (POST {base_url}/{api_version}/models/{model}:generateContent):
    {"contents": [{"role": "user" | "model", "parts": [{"text": ...}]}],
     "systemInstruction": {"parts": [{"text": ...}]}?,
     "generationConfig": {"maxOutputTokens", "temperature"?, "stopSequences"?}, ...extra}
Response shape:
    {"candidates": [{"content": {"parts": [{"text"}], "role": "model"}, "finishReason"}],
     "usageMetadata": {"promptTokenCount", "candidatesTokenCount", "totalTokenCount",
     "cachedContentTokenCount"?, "thoughtsTokenCount"?}, "modelVersion"}
Error shape:
    {"error": {"code", "message", "status"}}

Role mapping: the vendor-neutral "assistant" becomes "model". String content becomes one
text part; list content is passed through as parts.

Usage note: promptTokenCount includes cached tokens, which the adapter separates into
cache_read_tokens. Output tokens are candidatesTokenCount plus thoughtsTokenCount, since
both are billed as output.
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

DEFAULT_API_VERSION = "v1beta"
_ROLE = {"user": "user", "assistant": "model", "model": "model"}


def _parts(content: Any) -> list[Any]:
    if isinstance(content, str):
        return [{"text": content}]
    if isinstance(content, list):
        return list(content)
    return [{"text": str(content)}]


class GoogleAdapter:
    kind: ClassVar[ProviderKind] = ProviderKind.GOOGLE

    def build_request(
        self,
        ref: ModelRef,
        request: ChatRequest,
        provider: ProviderConfig,
        api_key: str | None,
    ) -> BuiltRequest:
        contents: list[dict[str, Any]] = []
        for m in request.messages:
            role = _ROLE.get(str(m.get("role", "user")), "user")
            contents.append({"role": role, "parts": _parts(m.get("content"))})
        body: dict[str, Any] = {"contents": contents}
        if request.system is not None:
            body["systemInstruction"] = {"parts": [{"text": request.system}]}
        gen: dict[str, Any] = {}
        if request.max_tokens is not None:
            gen["maxOutputTokens"] = request.max_tokens
        if request.temperature is not None:
            gen["temperature"] = request.temperature
        if request.stop:
            gen["stopSequences"] = list(request.stop)
        if gen:
            body["generationConfig"] = gen
        # Extra fields merge verbatim and last, like every adapter. Gemini nests the fields a
        # caller most often needs to fix (thinkingConfig, responseMimeType) under
        # generationConfig, so that one key merges into the built generationConfig instead of
        # replacing it and silently dropping maxOutputTokens and temperature.
        extra = dict(request.extra)
        extra_gen = extra.pop("generationConfig", None)
        if isinstance(extra_gen, dict):
            body["generationConfig"] = {**gen, **extra_gen}
        body.update(extra)
        headers: dict[str, str] = {"content-type": "application/json", **provider.headers}
        if api_key:
            headers["x-goog-api-key"] = api_key
        version = ref.api_version or provider.api_version or DEFAULT_API_VERSION
        return BuiltRequest(
            method="POST",
            url=f"{provider.base_url.rstrip('/')}/{version}/models/{ref.model}:generateContent",
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
                "google", status, body.decode("utf-8", "replace"), headers=headers
            ) from e
        if not isinstance(raw, dict) or not isinstance(raw.get("candidates"), list):
            # A prompt blocked by safety filters returns promptFeedback and no candidates;
            # that is a vendor decision, not a malformed body.
            if isinstance(raw, dict) and "promptFeedback" in raw:
                return ParsedResponse(
                    text=None,
                    finish_reason="blocked",
                    model_returned=raw.get("modelVersion"),
                    usage=self.parse_usage(raw),
                    raw=raw,
                )
            raise ProviderError(
                "google", status, body.decode("utf-8", "replace")[:500], headers=headers
            )
        text: str | None = None
        finish: str | None = None
        candidates = raw["candidates"]
        if candidates and isinstance(candidates[0], dict):
            first = candidates[0]
            content = first.get("content")
            if isinstance(content, dict):
                for part in content.get("parts") or []:
                    if isinstance(part, dict) and "text" in part and not part.get("thought"):
                        text = str(part["text"])
                        break
            fr = first.get("finishReason")
            finish = str(fr).lower() if fr is not None else None
        return ParsedResponse(
            text=text,
            finish_reason=finish,
            model_returned=raw.get("modelVersion"),
            usage=self.parse_usage(raw),
            raw=raw,
        )

    def parse_usage(self, raw: Any) -> Usage:
        if not isinstance(raw, dict):
            return Usage()
        u = raw.get("usageMetadata")
        if not isinstance(u, dict):
            return Usage()
        prompt = int(u.get("promptTokenCount") or 0)
        cached = int(u.get("cachedContentTokenCount") or 0)
        out = int(u.get("candidatesTokenCount") or 0) + int(u.get("thoughtsTokenCount") or 0)
        return Usage(
            input_tokens=max(prompt - cached, 0),
            output_tokens=out,
            cache_read_tokens=cached,
            cache_write_tokens=0,
        )

    def parse_error(
        self, provider_name: str, status: int, headers: Mapping[str, str], body: bytes
    ) -> ProviderError:
        text = body.decode("utf-8", "replace")
        try:
            raw = loads(body)
            err = raw.get("error") if isinstance(raw, dict) else None
            if isinstance(err, dict):
                text = (
                    f"{err.get('status') or err.get('code') or 'error'}: {err.get('message', '')}"
                )
        except ValueError:
            pass
        return ProviderError(provider_name, status, text, headers=headers)
