"""Pass-through is inviolable: byte equality, one upstream call, no aliases, no cache."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from boundary.config import BoundaryConfig
from boundary.errors import PassthroughViolation
from boundary.gateway import Gateway
from boundary.providers.anthropic import AnthropicAdapter
from boundary.types import ChatRequest, Mode

from .conftest import ANTHROPIC_URL, HAIKU, anthropic_ok, make_gateway


def _req(**kw: object) -> ChatRequest:
    base: dict[str, object] = {
        "model": HAIKU,
        "messages": [{"role": "user", "content": "Answer with the letter only. Q?"}],
        "max_tokens": 8,
        "temperature": 0.0,
        "system": "Be brief.",
    }
    base.update(kw)
    return ChatRequest(**base)  # type: ignore[arg-type]


def test_bytes_sent_equal_bytes_built(gw: Gateway, repo_config: BoundaryConfig) -> None:
    req = _req()
    ref = gw.resolve(HAIKU, Mode.PASSTHROUGH)
    expected = AnthropicAdapter().build_request(
        ref, req, ref.provider_config, "test-anthropic-key-000000000000"
    )
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(ANTHROPIC_URL).mock(return_value=anthropic_ok())
        resp = gw.chat(req, purpose="drift-run", run_id="2026-09", mode=Mode.PASSTHROUGH)
        sent = route.calls.last.request
    assert bytes(sent.content) == expected.body
    assert gw.transport.last_sent_body == expected.body
    for k, v in expected.headers.items():
        assert sent.headers[k] == v
    assert resp.ok and resp.text == "B" and resp.retries == 0 and resp.cached is False
    assert resp.mode is Mode.PASSTHROUGH
    assert resp.headers["request-id"] == "req_abc"


def test_alias_refused_before_anything_leaves(gw: Gateway) -> None:
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(ANTHROPIC_URL).mock(return_value=anthropic_ok())
        with pytest.raises(PassthroughViolation, match="explicit"):
            gw.chat(_req(model="fast"), purpose="t", mode=Mode.PASSTHROUGH)
        assert route.call_count == 0
    assert gw.ledger.count() == 0


def test_missing_max_tokens_refused(gw: Gateway) -> None:
    with pytest.raises(PassthroughViolation, match="max_tokens"):
        gw.chat(_req(max_tokens=None), purpose="t", mode=Mode.PASSTHROUGH)
    assert gw.ledger.count() == 0


def test_no_raw_store_refused(repo_config: BoundaryConfig, tmp_path: Path, keys: None) -> None:
    g = make_gateway(repo_config, tmp_path, raw_store=False)
    try:
        with pytest.raises(PassthroughViolation, match="raw store"):
            g.chat(_req(), purpose="t", mode=Mode.PASSTHROUGH)
    finally:
        g.close()


def test_cache_configured_still_exactly_one_upstream_call_per_passthrough(
    repo_config: BoundaryConfig, tmp_path: Path, keys: None
) -> None:
    cfg = repo_config.model_copy(
        update={
            "cache": repo_config.cache.model_copy(
                update={"enabled": True, "path": tmp_path / "cache"}
            )
        }
    )
    g = make_gateway(cfg, tmp_path)
    try:
        with respx.mock(assert_all_called=True) as mock:
            route = mock.post(ANTHROPIC_URL).mock(return_value=anthropic_ok())
            g.chat(_req(), purpose="t", mode=Mode.PASSTHROUGH)
            g.chat(_req(), purpose="t", mode=Mode.PASSTHROUGH)
            assert route.call_count == 2
        assert not (tmp_path / "cache").exists(), "pass-through must never write the cache"
        # The same cache serves standard mode: second identical call is answered locally.
        with respx.mock(assert_all_called=True) as mock:
            route = mock.post(ANTHROPIC_URL).mock(return_value=anthropic_ok())
            first = g.chat(_req(), purpose="dev")
            second = g.chat(_req(), purpose="dev")
            assert route.call_count == 1
        assert first.cached is False and second.cached is True
        assert second.text == "B" and second.cost_usd == 0.0
    finally:
        g.close()


def test_vendor_error_is_a_result_not_an_exception(gw: Gateway) -> None:
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(
                500, json={"type": "error", "error": {"type": "api_error", "message": "boom"}}
            )
        )
        resp = gw.chat(_req(), purpose="drift-run", run_id="r1", mode=Mode.PASSTHROUGH)
        assert route.call_count == 1, "pass-through never retries"
    assert resp.status == 500 and resp.text is None and resp.retries == 0 and not resp.ok
    assert resp.costed is False and resp.cost_usd is None
    row = gw.ledger.rows()[0]
    assert row["http_status"] == 500 and row["error_type"] == "http_500"


def test_transport_failure_is_a_result_and_is_recorded(gw: Gateway) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.post(ANTHROPIC_URL).mock(side_effect=httpx.ReadTimeout("slow"))
        resp = gw.chat(_req(), purpose="drift-run", run_id="r1", mode=Mode.PASSTHROUGH)
    assert resp.status == "ReadTimeout" and resp.text is None and resp.retries == 0
    row = gw.ledger.rows()[0]
    assert row["error_type"] == "ReadTimeout" and row["http_status"] is None
    record = json.loads(Path(row["raw_path"]).read_text(encoding="utf-8").splitlines()[-1])
    assert record["response"] == {"error": "ReadTimeout"}


def test_raw_store_record_matches_ledger_and_redacts_the_key(gw: Gateway) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.post(ANTHROPIC_URL).mock(return_value=anthropic_ok())
        resp = gw.chat(_req(), purpose="drift-run", run_id="2026-09", mode=Mode.PASSTHROUGH)
    row = gw.ledger.rows()[0]
    path = Path(row["raw_path"])
    assert path.name == "anthropic.jsonl" and path.parent.name == "2026-09"
    record = json.loads(path.read_text(encoding="utf-8").splitlines()[-1])
    assert record["ledger_id"] == resp.ledger_id == row["id"]
    assert record["request"]["sha256"] == row["request_sha256"]
    assert record["response"]["sha256"] == row["response_sha256"]
    assert record["request"]["headers"]["x-api-key"] == "<redacted>"
    assert "test-anthropic-key" not in path.read_text(encoding="utf-8")
    assert record["response"]["headers"]["anthropic-ratelimit-requests-remaining"] == "49"
    assert json.loads(record["request"]["body"]["text"])["max_tokens"] == 8
