"""A local OpenAI-compatible server at price zero.

Project 02's panel includes two or three small models running on the laptop, which spread
the ability range downward where item calibration needs it. They cost nothing, and the
ledger has to say nothing rather than say unknown: a local row that came back uncosted
would show up in the figure that must stay at zero and hide a real missing price somewhere
else.

No network and no server here. The goldens are Ollama's OpenAI-compatible response shape;
the live exercise against a real server is a separate step and is recorded in the ledger
when it happens.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from boundary.config import BoundaryConfig
from boundary.providers.openai_compat import OpenAICompatAdapter
from boundary.routes import resolve
from boundary.types import ChatRequest, Mode

from .conftest import make_gateway

LOCAL_URL = "http://127.0.0.1:11434/v1/chat/completions"
LOCAL_MODEL = "local/llama3.2:3b"


def _ollama_response() -> httpx.Response:
    """What Ollama returns from /v1/chat/completions. Kept verbatim as the golden."""
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-604",
            "object": "chat.completion",
            "created": 1789200000,
            "model": "llama3.2:3b",
            "system_fingerprint": "fp_ollama",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "OK"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 26, "completion_tokens": 2, "total_tokens": 28},
        },
    )


def test_the_local_entry_needs_no_key_and_sends_no_authorization(
    repo_config: BoundaryConfig,
) -> None:
    ref = resolve(LOCAL_MODEL, repo_config, Mode.PASSTHROUGH)
    assert ref.provider == "local" and ref.model == "llama3.2:3b"
    assert ref.provider_config.api_key_env is None
    assert ref.provider_config.price_zero is True

    built = OpenAICompatAdapter().build_request(ref, _request(), ref.provider_config, None)
    assert built.url == LOCAL_URL
    assert "authorization" not in {k.lower() for k in built.headers}
    assert json.loads(built.body)["model"] == "llama3.2:3b"


def test_the_ollama_response_shape_parses(repo_config: BoundaryConfig) -> None:
    body = _ollama_response().content
    parsed = OpenAICompatAdapter().parse_response(200, {}, body)
    assert parsed.text == "OK"
    assert parsed.finish_reason == "stop"
    assert parsed.model_returned == "llama3.2:3b"
    assert parsed.usage.input_tokens == 26
    assert parsed.usage.output_tokens == 2
    assert parsed.usage.cache_read_tokens == 0


def test_a_local_call_is_costed_at_zero_rather_than_left_uncosted(
    repo_config: BoundaryConfig, tmp_path: Path, keys: None
) -> None:
    gw = make_gateway(repo_config, tmp_path)
    try:
        with respx.mock(assert_all_called=True) as mock:
            mock.post(LOCAL_URL).mock(return_value=_ollama_response())
            resp = gw.chat(_request(), purpose="own-run", run_id="local-1")

        assert resp.ok and resp.text == "OK"
        assert resp.cost_usd == pytest.approx(0.0)
        assert resp.costed is True, "free is a price; unknown is not"

        row = gw.ledger.rows()[0]
        assert row["provider"] == "local"
        assert row["model_requested"] == LOCAL_MODEL
        assert row["cost_usd"] == pytest.approx(0.0) and row["costed"] == 1
        assert row["input_tokens"] == 26 and row["output_tokens"] == 2
        # The figure the README reports stays at zero for a local call.
        assert gw.ledger.uncosted_count() == 0
    finally:
        gw.close()


def test_a_local_call_never_moves_a_cap(
    repo_config: BoundaryConfig, tmp_path: Path, keys: None
) -> None:
    """Free calls must not consume a project's month, or an evening of local runs would
    refuse the vendor call that actually matters."""
    gw = make_gateway(repo_config, tmp_path)
    try:
        with respx.mock(assert_all_called=True) as mock:
            mock.post(LOCAL_URL).mock(return_value=_ollama_response())
            for _ in range(5):
                gw.chat(_request(), purpose="own-run", run_id="local-1")
        assert gw.ledger.count() == 5
        assert gw.ledger.spend_usd(project="ai-release-gate") == pytest.approx(0.0)
    finally:
        gw.close()


def _request() -> ChatRequest:
    return ChatRequest(
        model=LOCAL_MODEL,
        messages=[{"role": "user", "content": "Reply with the single word OK."}],
        max_tokens=16,
    )
