"""The OpenAI chat completions wire format, in and out. PLAN.md B2.1.

Pure functions with no I/O, so the translation is tested on its own and the app only moves
bytes. Two rules shape everything here.

**Nothing a client sent is dropped silently.** A field this gateway does not carry to every
vendor (`tools`, `response_format`, `logprobs`, `n` above one...) is refused with a 400
that names it, rather than accepted and ignored. A client that set `seed` and got an answer
would believe the seed was honoured; a gateway that measures things cannot let that belief
form. The accepted set is the one every adapter in the library can honour.

**The response says what answered.** `model` in the body is the identifier the vendor
returned, not the alias the client asked for, as OpenAI's own API does. The ledger's
`call_uid` rides in a header and in the completion id, so a team can join its own records to
the ledger without the ledger holding anything of theirs.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from boundary.types import ChatRequest, ChatResponse, Usage

# Fields a request may carry. Everything else is refused by name.
ACCEPTED = frozenset(
    {
        "model",
        "messages",
        "max_tokens",
        "max_completion_tokens",
        "temperature",
        "stop",
        "stream",
        "stream_options",
        "n",
    }
)
_ROLES = frozenset({"user", "assistant"})
_SYSTEM_ROLES = frozenset({"system", "developer"})

# Vendor finish reasons, normalised to lower case by the adapters, onto OpenAI's words.
# A reason not listed goes through as the vendor said it, rather than being guessed into one.
FINISH = {
    "stop": "stop",
    "end_turn": "stop",
    "stop_sequence": "stop",
    "length": "length",
    "max_tokens": "length",
    "content_filter": "content_filter",
    "safety": "content_filter",
    "refusal": "content_filter",
    "tool_calls": "tool_calls",
    "tool_use": "tool_calls",
}


class WireError(ValueError):
    """A request this gateway will not carry. `param` names the field, as OpenAI does."""

    def __init__(
        self, message: str, *, param: str | None = None, code: str = "invalid_request"
    ) -> None:
        super().__init__(message)
        self.param = param
        self.code = code


@dataclass(frozen=True, slots=True)
class Parsed:
    request: ChatRequest
    stream: bool
    include_usage: bool


def _text(content: Any, where: str) -> str:
    """A message's content as one string. OpenAI allows a string or a list of parts; only
    text parts are carried, because nothing here can redact an image (PLAN.md B2.9)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out: list[str] = []
        for i, part in enumerate(content):
            if not isinstance(part, Mapping) or part.get("type") != "text":
                kind = part.get("type") if isinstance(part, Mapping) else type(part).__name__
                raise WireError(
                    f"{where}.content[{i}] is a {kind!r} part; this gateway carries text only, "
                    "because it cannot inspect or redact anything else",
                    param=f"{where}.content",
                    code="unsupported_content",
                )
            text = part.get("text")
            if not isinstance(text, str):
                raise WireError(f"{where}.content[{i}].text must be a string", param=where)
            out.append(text)
        return "".join(out)
    raise WireError(f"{where}.content must be a string or a list of text parts", param=where)


def parse_request(body: Any) -> Parsed:
    """An OpenAI chat completions body as a library `ChatRequest`, or a WireError."""
    if not isinstance(body, dict):
        raise WireError("the request body must be a JSON object")
    unknown = sorted(set(body) - ACCEPTED)
    if unknown:
        raise WireError(
            f"unsupported parameter {unknown[0]!r}: this gateway refuses a field it would "
            "otherwise have to drop, so that no caller believes it was honoured"
            + (f" (also: {', '.join(unknown[1:])})" if len(unknown) > 1 else ""),
            param=unknown[0],
            code="unsupported_parameter",
        )
    model = body.get("model")
    if not isinstance(model, str) or not model:
        raise WireError("model is required and must be a string", param="model")
    n = body.get("n", 1)
    if n != 1:
        raise WireError("n must be 1; one call writes one ledger row", param="n")

    raw_messages = body.get("messages")
    if not isinstance(raw_messages, list) or not raw_messages:
        raise WireError("messages must be a non-empty list", param="messages")
    system: list[str] = []
    messages: list[dict[str, str]] = []
    for i, m in enumerate(raw_messages):
        where = f"messages[{i}]"
        if not isinstance(m, Mapping):
            raise WireError(f"{where} must be an object", param="messages")
        extra = sorted(set(m) - {"role", "content"})
        if extra:
            raise WireError(
                f"{where}.{extra[0]} is not supported; a message carries role and content",
                param=f"{where}.{extra[0]}",
                code="unsupported_parameter",
            )
        role = m.get("role")
        if role in _SYSTEM_ROLES:
            if messages:
                # Anthropic and Google take one system prompt, before the conversation; a
                # system message part way through has no faithful place to go.
                raise WireError(
                    f"{where} is a {role} message after the conversation began; system and "
                    "developer messages must come first",
                    param="messages",
                )
            system.append(_text(m.get("content"), where))
            continue
        if role not in _ROLES:
            raise WireError(
                f"{where}.role {role!r} is not supported; use system, developer, user or assistant",
                param=f"{where}.role",
            )
        messages.append({"role": str(role), "content": _text(m.get("content"), where)})
    if not messages:
        raise WireError("messages must contain a user or assistant message", param="messages")

    if "max_tokens" in body and "max_completion_tokens" in body:
        raise WireError(
            "give max_tokens or max_completion_tokens, not both", param="max_completion_tokens"
        )
    max_tokens = body.get("max_completion_tokens", body.get("max_tokens"))
    if max_tokens is not None and (isinstance(max_tokens, bool) or not isinstance(max_tokens, int)):
        raise WireError("max_tokens must be an integer", param="max_tokens")
    temperature = body.get("temperature")
    if temperature is not None and (
        isinstance(temperature, bool) or not isinstance(temperature, int | float)
    ):
        raise WireError("temperature must be a number", param="temperature")
    stop_raw = body.get("stop")
    stop: Sequence[str] | None
    if stop_raw is None:
        stop = None
    elif isinstance(stop_raw, str):
        stop = [stop_raw]
    elif isinstance(stop_raw, list) and all(isinstance(s, str) for s in stop_raw):
        stop = list(stop_raw)
    else:
        raise WireError("stop must be a string or a list of strings", param="stop")

    stream = body.get("stream", False)
    if not isinstance(stream, bool):
        raise WireError("stream must be a boolean", param="stream")
    options = body.get("stream_options")
    include_usage = False
    if options is not None:
        if not stream:
            raise WireError(
                "stream_options is only allowed when stream is true", param="stream_options"
            )
        if not isinstance(options, Mapping) or set(options) - {"include_usage"}:
            raise WireError("stream_options may carry include_usage only", param="stream_options")
        include_usage = bool(options.get("include_usage", False))

    try:
        request = ChatRequest(
            model=model,
            messages=messages,
            system="\n\n".join(system) if system else None,
            max_tokens=max_tokens,
            temperature=float(temperature) if temperature is not None else None,
            stop=stop,
        )
    except ValueError as e:
        raise WireError(str(e)) from None
    return Parsed(request=request, stream=stream, include_usage=include_usage)


def finish_reason(vendor: str | None) -> str | None:
    if vendor is None:
        return None
    return FINISH.get(vendor, vendor)


def usage_body(usage: Usage) -> dict[str, Any]:
    """OpenAI counts cached input inside `prompt_tokens`; the library keeps it apart."""
    prompt = usage.input_tokens + usage.cache_read_tokens + usage.cache_write_tokens
    return {
        "prompt_tokens": prompt,
        "completion_tokens": usage.output_tokens,
        "total_tokens": prompt + usage.output_tokens,
        "prompt_tokens_details": {"cached_tokens": usage.cache_read_tokens},
    }


def completion_id(call_uid: str | None) -> str:
    return f"chatcmpl-{call_uid or 'unrecorded'}"


def completion_body(resp: ChatResponse, *, created: int) -> dict[str, Any]:
    return {
        "id": completion_id(resp.call_uid),
        "object": "chat.completion",
        "created": created,
        "model": resp.model_returned or resp.model_requested,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": resp.text or ""},
                "finish_reason": finish_reason(resp.finish_reason),
            }
        ],
        "usage": usage_body(resp.usage),
    }


def chunk(
    cid: str,
    *,
    created: int,
    model: str,
    delta: Mapping[str, Any] | None = None,
    finish: str | None = None,
    usage: Usage | None = None,
    extra: Mapping[str, Any] | None = None,
) -> bytes:
    """One server-sent event. A usage chunk has no choices, as OpenAI sends it. `extra`
    adds top-level fields, which a client that does not know them ignores."""
    obj: dict[str, Any] = {
        "id": cid,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
    }
    if usage is not None:
        obj["choices"] = []
        obj["usage"] = usage_body(usage)
    else:
        obj["choices"] = [{"index": 0, "delta": dict(delta or {}), "finish_reason": finish}]
    if extra:
        obj.update(extra)
    return b"data: " + json.dumps(obj, separators=(",", ":")).encode() + b"\n\n"


DONE = b"data: [DONE]\n\n"


def error_body(
    message: str, *, type_: str, code: str | None = None, param: str | None = None, **extra: Any
) -> dict[str, Any]:
    """OpenAI's error shape, which its client reads into the exception it raises. Extra
    keys ride inside `error`, where a client that does not know them ignores them."""
    err: dict[str, Any] = {"message": message, "type": type_, "code": code, "param": param}
    err.update(extra)
    return {"error": err}


def error_event(body: Mapping[str, Any]) -> bytes:
    return b"data: " + json.dumps(body, separators=(",", ":")).encode() + b"\n\n"
