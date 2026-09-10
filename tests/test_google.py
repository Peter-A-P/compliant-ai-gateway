"""Golden requests and responses for the Gemini adapter, and one end-to-end pass-through call."""

from __future__ import annotations

import json

import pytest
import respx

from boundary.config import BoundaryConfig
from boundary.errors import ProviderError
from boundary.gateway import Gateway
from boundary.providers.google import GoogleAdapter
from boundary.routes import resolve
from boundary.types import ChatRequest, Mode

MODEL = "google/gemini-2.5-flash"
URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"


def _req(**kw: object) -> ChatRequest:
    base: dict[str, object] = {
        "model": MODEL,
        "messages": [
            {"role": "user", "content": "Q"},
            {"role": "assistant", "content": "A"},
            {"role": "user", "content": "Q2"},
        ],
        "max_tokens": 8,
    }
    base.update(kw)
    return ChatRequest(**base)  # type: ignore[arg-type]


def _ok(*, prompt: int = 40, out: int = 3, cached: int = 0, thoughts: int = 0) -> dict[str, object]:
    usage: dict[str, int] = {
        "promptTokenCount": prompt,
        "candidatesTokenCount": out,
        "totalTokenCount": prompt + out,
    }
    if cached:
        usage["cachedContentTokenCount"] = cached
    if thoughts:
        usage["thoughtsTokenCount"] = thoughts
    return {
        "candidates": [
            {
                "content": {"parts": [{"text": "B"}], "role": "model"},
                "finishReason": "STOP",
                "index": 0,
            }
        ],
        "usageMetadata": usage,
        "modelVersion": "gemini-2.5-flash",
    }


def test_google_golden_request(repo_config: BoundaryConfig) -> None:
    ref = resolve(MODEL, repo_config, Mode.PASSTHROUGH)
    req = _req(system="S", temperature=0.0, stop=["\n"])
    built = GoogleAdapter().build_request(ref, req, ref.provider_config, "k")
    assert built.url == URL
    assert built.headers["x-goog-api-key"] == "k"
    body = json.loads(built.body)
    assert body["contents"] == [
        {"parts": [{"text": "Q"}], "role": "user"},
        {"parts": [{"text": "A"}], "role": "model"},
        {"parts": [{"text": "Q2"}], "role": "user"},
    ]
    assert body["systemInstruction"] == {"parts": [{"text": "S"}]}
    assert body["generationConfig"] == {
        "maxOutputTokens": 8,
        "stopSequences": ["\n"],
        "temperature": 0.0,
    }
    assert built.body == (
        b'{"contents":[{"parts":[{"text":"Q"}],"role":"user"},{"parts":[{"text":"A"}],"role":"model"},'
        b'{"parts":[{"text":"Q2"}],"role":"user"}],"generationConfig":{"maxOutputTokens":8,'
        b'"stopSequences":["\\n"],"temperature":0.0},"systemInstruction":{"parts":[{"text":"S"}]}}'
    )


def test_google_extra_generation_config_merges_instead_of_replacing(
    repo_config: BoundaryConfig,
) -> None:
    """A caller fixing thinkingConfig must not lose maxOutputTokens (seen 2026-09-10 when
    gemini-flash-latest spent the whole budget thinking). Top-level extras still merge last."""
    ref = resolve(MODEL, repo_config, Mode.PASSTHROUGH)
    req = _req(
        temperature=0.0,
        extra={
            "generationConfig": {"thinkingConfig": {"thinkingBudget": 0}, "temperature": 0.5},
            "safetySettings": [],
        },
    )
    body = json.loads(GoogleAdapter().build_request(ref, req, ref.provider_config, "k").body)
    assert body["generationConfig"] == {
        "maxOutputTokens": 8,
        "temperature": 0.5,  # the caller's extra wins, as extras do everywhere
        "thinkingConfig": {"thinkingBudget": 0},
    }
    assert body["safetySettings"] == []


def test_google_list_content_passes_through_as_parts(repo_config: BoundaryConfig) -> None:
    ref = resolve(MODEL, repo_config, Mode.STANDARD)
    parts = [{"text": "look"}, {"inlineData": {"mimeType": "image/png", "data": "AAAA"}}]
    req = _req(messages=[{"role": "user", "content": parts}])
    body = json.loads(GoogleAdapter().build_request(ref, req, ref.provider_config, "k").body)
    assert body["contents"][0]["parts"] == parts


def test_google_golden_response_separates_cached_and_adds_thoughts() -> None:
    parsed = GoogleAdapter().parse_response(
        200, {}, json.dumps(_ok(prompt=100, out=3, cached=60, thoughts=20)).encode()
    )
    assert parsed.text == "B" and parsed.finish_reason == "stop"
    assert parsed.model_returned == "gemini-2.5-flash"
    assert parsed.usage.input_tokens == 40
    assert parsed.usage.cache_read_tokens == 60
    assert parsed.usage.output_tokens == 23


def test_google_blocked_prompt_is_a_result() -> None:
    body = json.dumps(
        {
            "promptFeedback": {"blockReason": "SAFETY"},
            "usageMetadata": {"promptTokenCount": 12, "totalTokenCount": 12},
        }
    ).encode()
    parsed = GoogleAdapter().parse_response(200, {}, body)
    assert parsed.text is None and parsed.finish_reason == "blocked"
    assert parsed.usage.input_tokens == 12


def test_google_malformed_and_error_shape() -> None:
    with pytest.raises(ProviderError):
        GoogleAdapter().parse_response(200, {}, b'{"nope": 1}')
    err = GoogleAdapter().parse_error(
        "google",
        429,
        {},
        b'{"error":{"code":429,"message":"quota","status":"RESOURCE_EXHAUSTED"}}',
    )
    assert err.status == 429 and "RESOURCE_EXHAUSTED: quota" in err.body


def test_google_passthrough_end_to_end(gw: Gateway) -> None:
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(URL).mock(return_value=respx.MockResponse(200, json=_ok()))
        resp = gw.chat(_req(), purpose="drift-run", run_id="g1", mode=Mode.PASSTHROUGH)
        sent = route.calls.last.request
    assert sent.headers["x-goog-api-key"] == "test-google-key-000000000000"
    assert resp.ok and resp.text == "B" and resp.model_returned == "gemini-2.5-flash"
    # No Google price in the price file yet: uncosted, never guessed.
    row = gw.ledger.rows()[0]
    assert row["provider"] == "google" and row["http_status"] == 200
    assert resp.costed is False or resp.cost_usd is not None
