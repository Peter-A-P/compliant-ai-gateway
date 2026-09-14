"""Chat completions over raw HTTP, for OpenAI and every OpenAI-compatible host: the
open-weights provider that serves 03's control arm, local llama.cpp or Ollama servers.

Request shape (POST {base_url}/chat/completions):
    {"model", "messages" (system prepended as a system message), "max_tokens" or
     "max_completion_tokens" (per provider configuration), "temperature"?, "stop"?, ...extra}
Response shape:
    {"id", "model", "choices": [{"message": {"role", "content"}, "finish_reason"}],
     "usage": {"prompt_tokens", "completion_tokens",
     "prompt_tokens_details": {"cached_tokens"}?}}
Error shape:
    {"error": {"message", "type", "code"}}

Usage note: OpenAI's prompt_tokens includes cached tokens. The adapter reports
input_tokens = prompt_tokens - cached_tokens and cache_read_tokens = cached_tokens, so the
cost arithmetic is the same for every provider.
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


class OpenAICompatAdapter:
    kind: ClassVar[ProviderKind] = ProviderKind.OPENAI_COMPAT

    def chat_body(
        self, ref: ModelRef, request: ChatRequest, provider: ProviderConfig
    ) -> dict[str, Any]:
        """The chat completions body for one request.

        Shared by the single-call path and the batch path so that a request costs the same
        bytes either way: the same prompt sent singly or inside a batch file has the same
        request hash in the ledger, and there is one implementation to keep correct rather
        than two to keep in step.
        """
        messages: list[dict[str, Any]] = []
        if request.system is not None:
            messages.append({"role": "system", "content": request.system})
        messages.extend(dict(m) for m in request.messages)
        body: dict[str, Any] = {"model": ref.model, "messages": messages}
        if request.max_tokens is not None:
            body[provider.max_tokens_field] = request.max_tokens
        if request.temperature is not None:
            body["temperature"] = request.temperature
        if request.stop:
            body["stop"] = list(request.stop)
        body.update(request.extra)
        return body

    def build_request(
        self,
        ref: ModelRef,
        request: ChatRequest,
        provider: ProviderConfig,
        api_key: str | None,
    ) -> BuiltRequest:
        headers: dict[str, str] = {"content-type": "application/json", **provider.headers}
        if api_key:
            headers["authorization"] = f"Bearer {api_key}"
        return BuiltRequest(
            method="POST",
            url=provider.base_url.rstrip("/") + "/chat/completions",
            headers=headers,
            body=dumps(self.chat_body(ref, request, provider)),
        )

    def parse_response(
        self, status: int, headers: Mapping[str, str], body: bytes
    ) -> ParsedResponse:
        try:
            raw = loads(body)
        except ValueError as e:
            raise ProviderError(
                "openai_compat", status, body.decode("utf-8", "replace"), headers=headers
            ) from e
        if not isinstance(raw, dict) or not isinstance(raw.get("choices"), list):
            raise ProviderError(
                "openai_compat", status, body.decode("utf-8", "replace")[:500], headers=headers
            )
        return self.parse_completion(raw)

    def parse_completion(self, raw: dict[str, Any]) -> ParsedResponse:
        """Read an already-decoded chat completion object.

        A batch results line carries the same object inline rather than as a body of its own,
        so both paths read usage and text through this one method.
        """
        text: str | None = None
        finish: str | None = None
        choices = raw.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            first = choices[0]
            msg = first.get("message")
            if isinstance(msg, dict):
                content = msg.get("content")
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    # Some hosts return content parts; take the first text part.
                    for part in content:
                        if isinstance(part, dict) and part.get("type") == "text":
                            text = str(part.get("text", ""))
                            break
            fr = first.get("finish_reason")
            finish = str(fr).lower() if fr is not None else None
        return ParsedResponse(
            text=text,
            finish_reason=finish,
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
        prompt = int(u.get("prompt_tokens") or 0)
        details = u.get("prompt_tokens_details")
        cached = int(details.get("cached_tokens") or 0) if isinstance(details, dict) else 0
        return Usage(
            input_tokens=max(prompt - cached, 0),
            output_tokens=int(u.get("completion_tokens") or 0),
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
                text = f"{err.get('type') or err.get('code') or 'error'}: {err.get('message', '')}"
        except ValueError:
            pass
        return ProviderError(provider_name, status, text, headers=headers)
