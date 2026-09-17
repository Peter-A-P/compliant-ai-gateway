"""Every call writes a ledger row before returning, including failures and a kill mid-call."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from boundary.config import BoundaryConfig
from boundary.errors import ProviderError, UnknownPrice
from boundary.gateway import Gateway
from boundary.ledger.store import IN_FLIGHT
from boundary.providers.base import BuiltRequest
from boundary.transport import HttpResult, Transport
from boundary.types import ChatRequest

from .conftest import ANTHROPIC_URL, HAIKU, OPENAI_URL, anthropic_ok, make_gateway, openai_ok


def _req(model: str = HAIKU, **kw: object) -> ChatRequest:
    base: dict[str, object] = {
        "model": model,
        "messages": [{"role": "user", "content": "Q?"}],
        "max_tokens": 8,
    }
    base.update(kw)
    return ChatRequest(**base)  # type: ignore[arg-type]


def test_success_row_is_complete_and_costed(gw: Gateway) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.post(ANTHROPIC_URL).mock(return_value=anthropic_ok(input_tokens=100, output_tokens=10))
        resp = gw.chat(_req(), purpose="dev", run_id="r1")
    rows = gw.ledger.rows()
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == resp.ledger_id
    assert row["error_type"] is None and row["http_status"] == 200
    assert row["model_requested"] == HAIKU and row["model_returned"] == "claude-haiku-4-5-20251001"
    assert (row["input_tokens"], row["output_tokens"]) == (100, 10)
    # Haiku 4.5 list price: $1 in, $5 out per million tokens.
    assert row["cost_usd"] == pytest.approx(100 / 1e6 * 1.0 + 10 / 1e6 * 5.0)
    assert row["costed"] == 1 and row["price_list"] == gw.prices.name
    assert resp.cost_usd == pytest.approx(row["cost_usd"]) and resp.costed
    assert row["request_sha256"] and row["response_sha256"] and row["boundary_version"]
    assert row["mode"] == "standard" and row["alias"] is None and row["run_id"] == "r1"


def test_alias_is_recorded_alongside_the_resolved_model(gw: Gateway) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.post(ANTHROPIC_URL).mock(return_value=anthropic_ok())
        gw.chat(_req(model="fast"), purpose="dev")
    row = gw.ledger.rows()[0]
    assert row["alias"] == "fast" and row["model_requested"] == HAIKU


@pytest.mark.parametrize(
    ("response", "expected_error", "expected_status"),
    [
        (
            httpx.Response(
                500, json={"type": "error", "error": {"type": "api_error", "message": "x"}}
            ),
            "http_500",
            500,
        ),
        (
            httpx.Response(
                400,
                json={"type": "error", "error": {"type": "invalid_request_error", "message": "x"}},
            ),
            "http_400",
            400,
        ),
        (httpx.Response(200, content=b"<html>not json</html>"), "MalformedResponse", 200),
    ],
)
def test_failures_in_standard_mode_raise_after_writing_the_row(
    gw: Gateway, response: httpx.Response, expected_error: str, expected_status: int
) -> None:
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(ANTHROPIC_URL).mock(return_value=response)
        with pytest.raises(ProviderError) as ei:
            gw.chat(_req(), purpose="dev")
        # 500s are retried up to max_attempts; 400 and malformed 200 are not.
        expected_calls = gw.config.retry.max_attempts if expected_status == 500 else 1
        assert route.call_count == expected_calls
    rows = gw.ledger.rows()
    assert len(rows) == 1
    assert rows[0]["error_type"] == expected_error
    assert rows[0]["http_status"] == expected_status
    assert rows[0]["retries"] == expected_calls - 1 == ei.value.retries
    assert rows[0]["costed"] == 0 and rows[0]["cost_usd"] is None


def test_timeout_in_standard_mode_retries_then_raises_and_records(gw: Gateway) -> None:
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(ANTHROPIC_URL).mock(side_effect=httpx.ConnectTimeout("nope"))
        with pytest.raises(ProviderError) as ei:
            gw.chat(_req(), purpose="dev")
        assert route.call_count == gw.config.retry.max_attempts
    assert ei.value.status == "ConnectTimeout"
    row = gw.ledger.rows()[0]
    assert row["error_type"] == "ConnectTimeout" and row["http_status"] is None
    assert row["retries"] == gw.config.retry.max_attempts - 1


def test_retry_recovers_and_counts_retries(gw: Gateway) -> None:
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(ANTHROPIC_URL)
        route.side_effect = [
            httpx.Response(429, headers={"retry-after": "0"}, json={"type": "error", "error": {}}),
            httpx.Response(503, json={"type": "error", "error": {}}),
            anthropic_ok(),
        ]
        resp = gw.chat(_req(), purpose="dev")
        assert route.call_count == 3
    assert resp.ok and resp.retries == 2
    assert gw.ledger.rows()[0]["retries"] == 2


class _KilledTransport(Transport):
    """Simulates the process dying while the request is in flight."""

    def send(self, built: BuiltRequest) -> HttpResult:
        raise KeyboardInterrupt


def test_kill_mid_call_leaves_an_in_flight_row_with_the_estimate(
    repo_config: BoundaryConfig, tmp_path: Path, keys: None
) -> None:
    g = make_gateway(
        repo_config, tmp_path, transport=_KilledTransport(repo_config.defaults.timeouts)
    )
    try:
        with pytest.raises(KeyboardInterrupt):
            g.chat(_req(), purpose="dev", run_id="r1")
        rows = g.ledger.rows()
        assert len(rows) == 1
        assert rows[0]["error_type"] == IN_FLIGHT
        assert rows[0]["cost_usd"] is not None and rows[0]["cost_usd"] > 0
        assert rows[0]["http_status"] is None
        # The estimate counts against the caps until the row is completed.
        assert g.ledger.spend_usd(project="ai-release-gate", run_id="r1") == rows[0]["cost_usd"]
    finally:
        g.close()


def test_unknown_price_writes_an_uncosted_row_unless_strict(
    repo_config: BoundaryConfig, tmp_path: Path, keys: None
) -> None:
    g = make_gateway(repo_config, tmp_path)
    try:
        with respx.mock(assert_all_called=True) as mock:
            mock.post(ANTHROPIC_URL).mock(return_value=anthropic_ok(model="claude-unpriced-9"))
            # Neither the returned nor the requested identifier is priced: uncosted, no guess.
            resp = g.chat(_req(model="anthropic/claude-unpriced-9"), purpose="dev")
        assert (
            resp.ok and resp.costed is False and resp.cost_usd is None and resp.price_list is None
        )
        row = g.ledger.rows()[0]
        assert row["costed"] == 0 and row["cost_usd"] is None
        assert g.ledger.uncosted_count() == 1
    finally:
        g.close()
    strict = make_gateway(repo_config, tmp_path / "strict", strict_cost=True)
    try:
        with respx.mock(assert_all_called=True) as mock:
            mock.post(ANTHROPIC_URL).mock(return_value=anthropic_ok(model="claude-unpriced-9"))
            with pytest.raises(UnknownPrice):
                strict.chat(_req(model="anthropic/claude-unpriced-9"), purpose="dev")
        assert strict.ledger.count() == 1, "the row is written before UnknownPrice is raised"
    finally:
        strict.close()


def test_price_zero_host_is_costed_at_zero(gw: Gateway) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.post("http://127.0.0.1:11434/v1/chat/completions").mock(return_value=openai_ok())
        resp = gw.chat(_req(model="local/llama"), purpose="dev")
    assert resp.costed and resp.cost_usd == 0.0


def test_openai_cached_tokens_need_a_cache_rate_to_be_costed(
    repo_config: BoundaryConfig, tmp_path: Path, keys: None
) -> None:
    """The price file has no OpenAI entry yet (Sep 10), so an OpenAI call is uncosted, and
    the row says so rather than guessing."""
    g = make_gateway(repo_config, tmp_path)
    try:
        with respx.mock(assert_all_called=True) as mock:
            mock.post(OPENAI_URL).mock(return_value=openai_ok(prompt=100, cached=60))
            resp = g.chat(_req(model="openai/gpt-x"), purpose="dev")
        assert resp.usage.input_tokens == 40 and resp.usage.cache_read_tokens == 60
        assert resp.costed is False
    finally:
        g.close()


def test_returned_id_without_a_price_falls_back_to_the_requested_id(
    repo_config: BoundaryConfig, tmp_path: Path, keys: None
) -> None:
    """OpenAI answers a request for gpt-5-nano with gpt-5-nano-2025-08-07; the price list
    names the undated id. The call is costed by the requested id, and the row keeps both
    identifiers (seen on the first live call, 2026-09-10)."""
    g = make_gateway(repo_config, tmp_path)
    try:
        with respx.mock(assert_all_called=True) as mock:
            # The fixture returns model "gpt-x-2026-01-01" for any request.
            mock.post(OPENAI_URL).mock(return_value=openai_ok(prompt=100, cached=0))
            resp = g.chat(_req(model="openai/gpt-5-nano"), purpose="dev")
        assert resp.model_returned == "gpt-x-2026-01-01"
        assert resp.costed is True and resp.cost_usd is not None and resp.cost_usd > 0
        row = g.ledger.rows()[-1]
        assert row["model_requested"] == "openai/gpt-5-nano"
        assert row["model_returned"] == "gpt-x-2026-01-01"
    finally:
        g.close()


def test_redirect_moves_alias_callers_in_the_ledger_not_explicit_ones(
    repo_config: BoundaryConfig, tmp_path: Path, keys: None
) -> None:
    """The reason the library exists: one line in the routes file moves every alias call."""
    moved = repo_config.model_copy(
        update={
            "routes": {
                **repo_config.routes,
                "fast": repo_config.routes["fast"].model_copy(
                    update={"provider": "openai", "model": "gpt-x"}
                ),
            }
        }
    )
    g = make_gateway(moved, tmp_path)
    try:
        with respx.mock(assert_all_called=True) as mock:
            mock.post(OPENAI_URL).mock(return_value=openai_ok())
            mock.post(ANTHROPIC_URL).mock(return_value=anthropic_ok())
            g.chat(_req(model="fast"), purpose="dev")
            g.chat(_req(model=HAIKU), purpose="dev")
        rows = g.ledger.rows()
        assert rows[0]["alias"] == "fast" and rows[0]["model_requested"] == "openai/gpt-x"
        assert rows[1]["alias"] is None and rows[1]["model_requested"] == HAIKU
    finally:
        g.close()


async def test_async_path_writes_the_same_row(gw: Gateway) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.post(ANTHROPIC_URL).mock(return_value=anthropic_ok())
        resp = await gw.achat(_req(), purpose="dev", run_id="async")
    row = gw.ledger.rows()[0]
    assert resp.ok and row["run_id"] == "async" and row["error_type"] is None


def test_a_provider_error_message_reports_the_retries_the_gateway_assigned() -> None:
    """The message is built when it is read, not when the error is constructed.

    An adapter's parse_error cannot know how many attempts were made, so it constructs the
    error with zero and the gateway assigns the real count afterwards. Freezing the text in
    __init__ meant a call that was retried three times reported "after 0 retries", which
    reads as evidence that the retry policy never ran.
    """
    err = ProviderError("vertex", 429, "error: Quota exceeded", headers={})
    assert "after 0 retries" in str(err)
    err.retries = 3
    assert "after 3 retries" in str(err)
    assert err.retries == 3
