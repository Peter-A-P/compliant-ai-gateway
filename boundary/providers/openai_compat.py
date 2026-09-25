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

    # -- streaming (0.3) ------------------------------------------------------------------

    def build_stream_request(
        self,
        ref: ModelRef,
        request: ChatRequest,
        provider: ProviderConfig,
        api_key: str | None,
    ) -> BuiltRequest:
        """The chat completions request with streaming on and usage asked for.

        The non-streaming body, then `stream` and `stream_options` on top, AFTER the caller's
        `extra`. The order is deliberate: `extra` is merged last everywhere else so that a
        caller can reach any vendor field, but a caller who set `stream: false` in `extra` on
        a call to `chat_stream` has asked for two contradictory things, and the one the method
        name says wins. `include_usage` is what makes the final event carry the token counts
        the row is costed from; without it the stream is text and no counts, and the row is
        written uncosted.
        """
        body = self.chat_body(ref, request, provider)
        body["stream"] = True
        body["stream_options"] = {"include_usage": True}
        headers: dict[str, str] = {
            "content-type": "application/json",
            "accept": "text/event-stream",
            **provider.headers,
        }
        if api_key:
            headers["authorization"] = f"Bearer {api_key}"
        return BuiltRequest(
            method="POST",
            url=provider.base_url.rstrip("/") + "/chat/completions",
            headers=headers,
            body=dumps(body),
        )

    def stream_parser(self) -> OpenAIStreamParser:
        return OpenAIStreamParser(self)

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


class OpenAIStreamParser:
    """Assembles one streamed chat completion from its `data:` events.

    Stream shape (one `data:` event per line, closed by `data: [DONE]`):
        {"id", "model", "choices": [{"index", "delta": {"role"?, "content"?}, "finish_reason"}],
         "usage"?: {...}}
    With `stream_options.include_usage`, OpenAI and vLLM send one final event whose
    `choices` is empty and whose `usage` is the whole call's; llama.cpp puts the usage on
    its last content event instead. Both are read the same way: the last usage object seen
    is the call's, and whether one was seen at all is recorded rather than assumed.

    Time to first token is the first event whose delta carries a non-empty `content`
    string. A leading role-only delta (`{"role": "assistant", "content": ""}`, which OpenAI
    sends) is not a token and does not count. A `reasoning_content` delta does not count
    either: the caller asked when visible text began, and a reasoning model that thinks for
    ten seconds before its first visible token has a ten-second time to first token, which
    is the honest number for a user waiting on it.
    """

    def __init__(self, adapter: OpenAICompatAdapter) -> None:
        self._adapter = adapter
        self._text: list[str] = []
        self._finish: str | None = None
        self._model: str | None = None
        self._id: str | None = None
        self._usage: Usage = Usage()
        self._events = 0
        self._error: str | None = None
        self.usage_seen = False
        self._drained = 0

    def feed(self, data: str) -> bool:
        if data.strip() == "[DONE]":
            return False
        try:
            raw = loads(data.encode("utf-8"))
        except ValueError:
            # A host that sends a line the library cannot read has sent a malformed stream.
            # Recorded and surfaced at result(), so the row says so.
            self._error = f"unreadable event: {data[:200]}"
            return False
        if not isinstance(raw, dict):
            self._error = f"event is not an object: {data[:200]}"
            return False
        self._events += 1
        err = raw.get("error")
        if isinstance(err, dict):
            self._error = (
                f"{err.get('type') or err.get('code') or 'error'}: {err.get('message', '')}"
            )
            return False
        if isinstance(err, str):
            self._error = err
            return False
        if raw.get("model") and self._model is None:
            self._model = str(raw["model"])
        if raw.get("id") and self._id is None:
            self._id = str(raw["id"])
        if isinstance(raw.get("usage"), dict):
            self._usage = self._adapter.parse_usage(raw)
            self.usage_seen = True
        content = False
        choices = raw.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            first = choices[0]
            delta = first.get("delta")
            if isinstance(delta, dict):
                piece = delta.get("content")
                if isinstance(piece, str) and piece:
                    self._text.append(piece)
                    content = True
            fr = first.get("finish_reason")
            if fr is not None:
                self._finish = str(fr).lower()
        return content

    def drain(self) -> str:
        piece = "".join(self._text[self._drained :])
        self._drained = len(self._text)
        return piece

    def result(self) -> ParsedResponse:
        if self._error is not None:
            raise ProviderError("openai_compat", 200, self._error)
        if self._events == 0:
            raise ProviderError("openai_compat", 200, "the stream carried no events")
        text = "".join(self._text)
        # The completion in the non-streaming shape, assembled from the events. It is what a
        # caller reading `raw` expects to find, and it is labelled as assembled so that
        # nobody mistakes it for bytes a host sent.
        raw: dict[str, Any] = {
            "id": self._id,
            "object": "chat.completion",
            "model": self._model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": self._finish,
                }
            ],
            "assembled_from_stream_events": self._events,
        }
        if self.usage_seen:
            raw["usage"] = {
                "prompt_tokens": self._usage.input_tokens + self._usage.cache_read_tokens,
                "completion_tokens": self._usage.output_tokens,
                "prompt_tokens_details": {"cached_tokens": self._usage.cache_read_tokens},
            }
        return ParsedResponse(
            text=text if self._text else None,
            finish_reason=self._finish,
            model_returned=self._model,
            usage=self._usage,
            raw=raw,
        )
