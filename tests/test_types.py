"""The request and response types validate what they can without a network."""

from __future__ import annotations

import dataclasses

import pytest

from boundary import ChatRequest, ChatResponse, Mode, Usage, __version__


def test_version_is_a_dev_prerelease_until_the_tag() -> None:
    assert __version__.startswith("0.1.0")


def test_chat_request_minimal_and_explicit_flag() -> None:
    r = ChatRequest(model="fast", messages=[{"role": "user", "content": "hi"}])
    assert r.is_explicit is False
    assert r.max_tokens is None
    assert dict(r.extra) == {}
    e = ChatRequest(model="anthropic/claude-haiku-4-5-20251001", messages=r.messages, max_tokens=8)
    assert e.is_explicit is True


def test_chat_request_is_frozen() -> None:
    r = ChatRequest(model="fast", messages=[{"role": "user", "content": "hi"}])
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.model = "other"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"model": "", "messages": [{"role": "user", "content": "x"}]}, "non-empty"),
        ({"model": " fast", "messages": [{"role": "user", "content": "x"}]}, "whitespace"),
        ({"model": "fast", "messages": []}, "at least one"),
        ({"model": "fast", "messages": [{"role": "user"}]}, "'role' and 'content'"),
        (
            {"model": "fast", "messages": [{"role": "user", "content": "x"}], "max_tokens": 0},
            "positive",
        ),
        (
            {"model": "fast", "messages": [{"role": "user", "content": "x"}], "temperature": 3.0},
            "temperature",
        ),
    ],
)
def test_chat_request_rejects_bad_input(kwargs: dict[str, object], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        ChatRequest(**kwargs)  # type: ignore[arg-type]


def test_usage_total() -> None:
    u = Usage(input_tokens=10, output_tokens=5, cache_read_tokens=3, cache_write_tokens=2)
    assert u.total_tokens == 20
    assert Usage().total_tokens == 0


def test_mode_values_are_the_ledger_strings() -> None:
    assert Mode.STANDARD.value == "standard"
    assert Mode.PASSTHROUGH.value == "passthrough"


def _response(status: int | str) -> ChatResponse:
    return ChatResponse(
        text="A",
        finish_reason="end_turn",
        usage=Usage(input_tokens=1, output_tokens=1),
        cost_usd=None,
        costed=False,
        model_requested="anthropic/m",
        model_returned="m",
        provider="anthropic",
        latency_ms=1.0,
        status=status,
        headers={},
        raw=None,
        ledger_id=1,
        mode=Mode.STANDARD,
    )


def test_response_ok_only_for_2xx() -> None:
    assert _response(200).ok
    assert _response(201).ok
    assert not _response(429).ok
    assert not _response("ReadTimeout").ok
