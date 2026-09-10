"""Golden requests and responses per adapter, including error shapes. No network."""

from __future__ import annotations

import json

import pytest

from boundary.config import BoundaryConfig
from boundary.errors import ProviderError
from boundary.providers.anthropic import AnthropicAdapter
from boundary.providers.openai_compat import OpenAICompatAdapter
from boundary.routes import resolve
from boundary.types import ChatRequest, Mode

from .conftest import HAIKU


def _req(model: str, **kw: object) -> ChatRequest:
    base: dict[str, object] = {
        "model": model,
        "messages": [{"role": "user", "content": "Q"}],
        "max_tokens": 8,
    }
    base.update(kw)
    return ChatRequest(**base)  # type: ignore[arg-type]


# -- Anthropic -----------------------------------------------------------------------------


def test_anthropic_golden_request(repo_config: BoundaryConfig) -> None:
    ref = resolve(HAIKU, repo_config, Mode.PASSTHROUGH)
    req = _req(HAIKU, system="S", temperature=0.0, stop=["\n"])
    built = AnthropicAdapter().build_request(ref, req, ref.provider_config, "k")
    assert built.method == "POST"
    assert built.url == "https://api.anthropic.com/v1/messages"
    assert built.headers["anthropic-version"] == "2023-06-01"
    assert built.headers["x-api-key"] == "k"
    assert built.headers["content-type"] == "application/json"
    assert built.body == (
        b'{"max_tokens":8,"messages":[{"content":"Q","role":"user"}],'
        b'"model":"claude-haiku-4-5-20251001","stop_sequences":["\\n"],"system":"S","temperature":0.0}'
    )
    # Deterministic: the same inputs give the same bytes.
    assert AnthropicAdapter().build_request(ref, req, ref.provider_config, "k").body == built.body


def test_anthropic_extra_is_merged_verbatim_and_last(repo_config: BoundaryConfig) -> None:
    ref = resolve(HAIKU, repo_config, Mode.STANDARD)
    req = _req(HAIKU, extra={"metadata": {"user_id": "u1"}, "temperature": 0.7})
    body = json.loads(AnthropicAdapter().build_request(ref, req, ref.provider_config, "k").body)
    assert body["metadata"] == {"user_id": "u1"}
    assert body["temperature"] == 0.7


def test_anthropic_no_key_means_no_header(repo_config: BoundaryConfig) -> None:
    ref = resolve(HAIKU, repo_config, Mode.STANDARD)
    built = AnthropicAdapter().build_request(ref, _req(HAIKU), ref.provider_config, None)
    assert "x-api-key" not in built.headers


def test_anthropic_requires_max_tokens(repo_config: BoundaryConfig) -> None:
    ref = resolve(HAIKU, repo_config, Mode.STANDARD)
    with pytest.raises(ValueError, match="max_tokens"):
        AnthropicAdapter().build_request(
            ref, _req(HAIKU, max_tokens=None), ref.provider_config, "k"
        )


def test_anthropic_golden_response() -> None:
    body = json.dumps(
        {
            "id": "msg_01",
            "type": "message",
            "role": "assistant",
            "model": "claude-haiku-4-5-20251001",
            "content": [{"type": "text", "text": "B"}],
            "stop_reason": "END_TURN",
            "usage": {
                "input_tokens": 40,
                "output_tokens": 3,
                "cache_read_input_tokens": 10,
                "cache_creation_input_tokens": 5,
            },
        }
    ).encode()
    parsed = AnthropicAdapter().parse_response(200, {}, body)
    assert parsed.text == "B"
    assert parsed.finish_reason == "end_turn"
    assert parsed.model_returned == "claude-haiku-4-5-20251001"
    assert (parsed.usage.input_tokens, parsed.usage.output_tokens) == (40, 3)
    assert (parsed.usage.cache_read_tokens, parsed.usage.cache_write_tokens) == (10, 5)


def test_anthropic_malformed_success_is_an_error() -> None:
    with pytest.raises(ProviderError):
        AnthropicAdapter().parse_response(200, {}, b"not json")
    with pytest.raises(ProviderError):
        AnthropicAdapter().parse_response(200, {}, b'{"type": "something_else"}')


def test_anthropic_error_shape() -> None:
    body = b'{"type":"error","error":{"type":"rate_limit_error","message":"slow down"}}'
    err = AnthropicAdapter().parse_error("anthropic", 429, {"retry-after": "2"}, body)
    assert err.status == 429
    assert "rate_limit_error: slow down" in err.body
    assert err.headers["retry-after"] == "2"


# -- OpenAI-compatible ---------------------------------------------------------------------


def test_openai_golden_request_uses_max_completion_tokens(repo_config: BoundaryConfig) -> None:
    ref = resolve("openai/gpt-x", repo_config, Mode.PASSTHROUGH)
    req = _req("openai/gpt-x", system="S", temperature=0.0, stop=["\n"])
    built = OpenAICompatAdapter().build_request(ref, req, ref.provider_config, "k")
    assert built.url == "https://api.openai.com/v1/chat/completions"
    assert built.headers["authorization"] == "Bearer k"
    assert built.body == (
        b'{"max_completion_tokens":8,"messages":[{"content":"S","role":"system"},'
        b'{"content":"Q","role":"user"}],"model":"gpt-x","stop":["\\n"],"temperature":0.0}'
    )


def test_openweights_host_uses_max_tokens_and_keeps_slashes(repo_config: BoundaryConfig) -> None:
    model = "openweights/meta-llama/Llama-3.3-70B-Instruct"
    ref = resolve(model, repo_config, Mode.PASSTHROUGH)
    built = OpenAICompatAdapter().build_request(ref, _req(model), ref.provider_config, "k")
    body = json.loads(built.body)
    assert body["model"] == "meta-llama/Llama-3.3-70B-Instruct"
    assert body["max_tokens"] == 8 and "max_completion_tokens" not in body
    assert built.url == "https://api.together.xyz/v1/chat/completions"


def test_local_host_has_no_auth_header(repo_config: BoundaryConfig) -> None:
    ref = resolve("local/llama", repo_config, Mode.STANDARD)
    built = OpenAICompatAdapter().build_request(ref, _req("local/llama"), ref.provider_config, None)
    assert "authorization" not in built.headers
    assert built.url.startswith("http://127.0.0.1:11434/v1/")


def test_openai_golden_response_and_cached_tokens() -> None:
    body = json.dumps(
        {
            "id": "chatcmpl-1",
            "model": "gpt-x-2026-01-01",
            "choices": [
                {"message": {"role": "assistant", "content": "B"}, "finish_reason": "STOP"}
            ],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 3,
                "prompt_tokens_details": {"cached_tokens": 60},
            },
        }
    ).encode()
    parsed = OpenAICompatAdapter().parse_response(200, {}, body)
    assert parsed.text == "B"
    assert parsed.finish_reason == "stop"
    assert parsed.model_returned == "gpt-x-2026-01-01"
    # prompt_tokens includes cached tokens; the adapter separates them.
    assert parsed.usage.input_tokens == 40
    assert parsed.usage.cache_read_tokens == 60
    assert parsed.usage.output_tokens == 3


def test_openai_content_parts_and_null_content() -> None:
    parts = {"choices": [{"message": {"content": [{"type": "text", "text": "P"}]}}], "model": "m"}
    assert OpenAICompatAdapter().parse_response(200, {}, json.dumps(parts).encode()).text == "P"
    null = {"choices": [{"message": {"content": None}, "finish_reason": "length"}], "model": "m"}
    parsed = OpenAICompatAdapter().parse_response(200, {}, json.dumps(null).encode())
    assert parsed.text is None and parsed.finish_reason == "length"


def test_openai_malformed_and_error_shape() -> None:
    with pytest.raises(ProviderError):
        OpenAICompatAdapter().parse_response(200, {}, b'{"no": "choices"}')
    err = OpenAICompatAdapter().parse_error(
        "openai",
        401,
        {},
        b'{"error":{"message":"bad key","type":"invalid_request_error","code":null}}',
    )
    assert err.status == 401 and "invalid_request_error: bad key" in err.body
