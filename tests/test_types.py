"""The request and response types validate what they can without a network."""

from __future__ import annotations

import dataclasses
import re

import pytest

from boundary import ChatRequest, ChatResponse, Mode, Usage, __version__


def test_the_version_matches_the_packaging_metadata() -> None:
    """A ledger row records the library version that wrote it, so the version in the package
    and the version the wheel is built with have to be the same string. They sat in two files
    and drifted once already, when the lockfile still said the previous one."""
    import tomllib
    from pathlib import Path

    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    declared = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["version"]
    assert __version__ == declared

    # A release is a plain three-part version; work after one carries a .devN suffix until
    # the next tag, which is what keeps an untagged build from claiming to be a release.
    assert re.fullmatch(r"\d+\.\d+\.\d+(\.dev\d+)?", __version__), __version__


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
