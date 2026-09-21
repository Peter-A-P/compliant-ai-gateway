"""Streaming for OpenAI-compatible hosts (0.3): time to first token, one row per call.

Project 06 measures time to first token against self-hosted vLLM and llama.cpp servers and
needs it from the same library every other call goes through, so the number lands in the
same ledger as the cost. The mock upstream here is an SSE stream with a known delay before
its first content event; the test is that the delay comes out as `ttft_ms`, and that the
usage in the FINAL event is what gets costed, because that is where the hosts put it.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import AsyncIterator, Iterator, Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from boundary.config import BoundaryConfig, Timeouts
from boundary.errors import PassthroughViolation, ProviderError, UnknownPrice
from boundary.gateway import Gateway
from boundary.providers.openai_compat import OpenAICompatAdapter
from boundary.providers.sse import SSEDecoder
from boundary.routes import resolve
from boundary.transport import POOL_LIMITS, Transport
from boundary.types import ChatRequest, Mode

from .conftest import OPENWEIGHTS_URL, make_gateway

# Together at list price, so the costing can be checked against known rates: 0.15 in, 0.60 out.
MODEL = "openweights/openai/gpt-oss-120b"
LOCAL_URL = "http://127.0.0.1:11434/v1/chat/completions"

# A stream's first event is a role-only delta with empty content, which OpenAI sends and
# which is not a token. It arrives immediately; the first CONTENT event arrives after the
# delay. The test is that the delay, not the role event, is what ttft_ms measures.
FIRST_TOKEN_DELAY_S = 0.15
# Generous, because the machine running the suite is not idle. The lower bound is the one
# that matters: a ttft below the delay would mean the wrong event was stamped.
TOLERANCE_S = 0.5


def _event(obj: dict[str, Any]) -> bytes:
    return b"data: " + json.dumps(obj).encode() + b"\n\n"


def _chunk(delta: dict[str, Any], finish: str | None = None, **extra: Any) -> dict[str, Any]:
    return {
        "id": "chatcmpl-s1",
        "object": "chat.completion.chunk",
        "model": "openai/gpt-oss-120b",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        **extra,
    }


USAGE = {"prompt_tokens": 100, "completion_tokens": 20}
EXPECTED_COST = 100 / 1e6 * 0.15 + 20 / 1e6 * 0.60


def openai_shaped_events() -> list[bytes]:
    """OpenAI and vLLM: a role event, content events, a finish event, then one final event
    with empty choices carrying the usage, then the sentinel."""
    return [
        _event(_chunk({"role": "assistant", "content": ""})),
        _event(_chunk({"content": "Hel"})),
        _event(_chunk({"content": "lo"})),
        _event(_chunk({}, finish="stop")),
        _event(
            {"id": "chatcmpl-s1", "model": "openai/gpt-oss-120b", "choices": [], "usage": USAGE}
        ),
        b"data: [DONE]\n\n",
    ]


def llamacpp_shaped_events() -> list[bytes]:
    """llama.cpp puts the usage on the last content event rather than on one of its own."""
    return [
        _event(_chunk({"content": "Hel"})),
        _event(_chunk({"content": "lo"}, finish="stop", usage=USAGE)),
        b"data: [DONE]\n\n",
    ]


class _SSE(httpx.SyncByteStream, httpx.AsyncByteStream):
    """A body that arrives in pieces, the first piece at once and the rest after a delay.

    The split point is where the delay goes, so a stream can put its role-only event before
    the delay and its first content event after it.
    """

    def __init__(self, events: Sequence[bytes], *, delay_s: float, delay_before: int = 0) -> None:
        self.events = list(events)
        self.delay_s = delay_s
        self.delay_before = delay_before

    def __iter__(self) -> Iterator[bytes]:
        for i, e in enumerate(self.events):
            if i == self.delay_before:
                time.sleep(self.delay_s)
            yield e

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for i, e in enumerate(self.events):
            if i == self.delay_before:
                await asyncio.sleep(self.delay_s)
            yield e


class _Breaks(httpx.SyncByteStream):
    """A stream that fails after its first event: the host started and then went away."""

    def __iter__(self) -> Iterator[bytes]:
        yield openai_shaped_events()[1]
        raise httpx.ReadTimeout("the host stopped mid-stream")


def _stream_response(stream: httpx.SyncByteStream) -> httpx.Response:
    return httpx.Response(
        200, stream=stream, headers={"content-type": "text/event-stream", "x-request-id": "s1"}
    )


def _req(model: str = MODEL, **kw: object) -> ChatRequest:
    base: dict[str, object] = {
        "model": model,
        "messages": [{"role": "user", "content": "Say hello."}],
        "max_tokens": 16,
    }
    base.update(kw)
    return ChatRequest(**base)  # type: ignore[arg-type]


# -- the request --------------------------------------------------------------------------


def test_the_stream_request_is_the_chat_request_plus_stream_and_usage(
    repo_config: BoundaryConfig,
) -> None:
    ref = resolve(MODEL, repo_config, Mode.STANDARD)
    adapter = OpenAICompatAdapter()
    plain = json.loads(adapter.build_request(ref, _req(), ref.provider_config, "k").body)
    built = adapter.build_stream_request(ref, _req(), ref.provider_config, "k")
    streamed = json.loads(built.body)
    assert streamed.pop("stream") is True
    assert streamed.pop("stream_options") == {"include_usage": True}
    assert streamed == plain, "nothing else about the request changes"
    assert built.headers["accept"] == "text/event-stream"
    assert built.headers["authorization"] == "Bearer k"
    assert built.url == OPENWEIGHTS_URL


def test_a_caller_cannot_turn_the_stream_off_from_extra(repo_config: BoundaryConfig) -> None:
    """`extra` merges last everywhere else. Here the method name wins, because a request
    that says chat_stream and stream: false has asked for two contradictory things."""
    ref = resolve(MODEL, repo_config, Mode.STANDARD)
    built = OpenAICompatAdapter().build_stream_request(
        ref, _req(extra={"stream": False, "stream_options": {}}), ref.provider_config, None
    )
    body = json.loads(built.body)
    assert body["stream"] is True and body["stream_options"] == {"include_usage": True}


# -- time to first token ------------------------------------------------------------------


def test_ttft_is_the_delay_before_the_first_content_event_not_the_role_event(
    gw: Gateway,
) -> None:
    events = openai_shaped_events()
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(OPENWEIGHTS_URL).mock(
            return_value=_stream_response(_SSE(events, delay_s=FIRST_TOKEN_DELAY_S, delay_before=1))
        )
        resp = gw.chat_stream(_req(), purpose="load-test", run_id="lt-1")
        assert route.call_count == 1
        sent = json.loads(route.calls.last.request.content)
    assert sent["stream"] is True and sent["stream_options"] == {"include_usage": True}

    assert resp.ok and resp.text == "Hello" and resp.finish_reason == "stop"
    assert resp.ttft_ms is not None
    assert FIRST_TOKEN_DELAY_S * 1000 <= resp.ttft_ms <= (FIRST_TOKEN_DELAY_S + TOLERANCE_S) * 1000
    assert resp.latency_ms >= resp.ttft_ms, "latency runs to the last byte"
    assert resp.model_returned == "openai/gpt-oss-120b"
    assert resp.headers["x-request-id"] == "s1"


def test_the_usage_in_the_final_event_is_what_gets_costed(gw: Gateway) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.post(OPENWEIGHTS_URL).mock(
            return_value=_stream_response(_SSE(openai_shaped_events(), delay_s=0))
        )
        resp = gw.chat_stream(_req(), purpose="load-test")
    assert resp.usage.input_tokens == 100 and resp.usage.output_tokens == 20
    assert resp.costed and resp.cost_usd == pytest.approx(EXPECTED_COST)
    assert resp.price_list == gw.prices.name and resp.price_sha256 == gw.prices.rates_sha256


def test_llamacpp_puts_usage_on_the_last_content_event_and_is_costed_the_same(
    gw: Gateway,
) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.post(OPENWEIGHTS_URL).mock(
            return_value=_stream_response(_SSE(llamacpp_shaped_events(), delay_s=0))
        )
        resp = gw.chat_stream(_req(), purpose="load-test")
    assert resp.text == "Hello" and resp.costed and resp.cost_usd == pytest.approx(EXPECTED_COST)
    assert resp.ttft_ms is not None and resp.ttft_ms >= 0


def test_one_ledger_row_per_call_with_ttft_and_the_hash_of_every_byte(gw: Gateway) -> None:
    events = openai_shaped_events()
    with respx.mock(assert_all_called=True) as mock:
        mock.post(OPENWEIGHTS_URL).mock(
            return_value=_stream_response(_SSE(events, delay_s=FIRST_TOKEN_DELAY_S, delay_before=1))
        )
        resp = gw.chat_stream(_req(), purpose="load-test", run_id="lt-1")
    rows = gw.ledger.rows()
    assert len(rows) == 1, "one row per call, not one per event"
    row = rows[0]
    assert row["id"] == resp.ledger_id and row["error_type"] is None and row["http_status"] == 200
    assert row["ttft_ms"] == pytest.approx(resp.ttft_ms)
    assert row["ttft_ms"] >= FIRST_TOKEN_DELAY_S * 1000
    assert row["latency_ms"] >= row["ttft_ms"]
    assert (row["input_tokens"], row["output_tokens"]) == (100, 20)
    assert row["cost_usd"] == pytest.approx(EXPECTED_COST) and row["costed"] == 1
    assert row["response_sha256"] == hashlib.sha256(b"".join(events)).hexdigest()
    assert row["mode"] == "standard" and row["run_id"] == "lt-1" and row["cached"] == 0
    assert gw.ledger.uncosted_count() == 0


def test_a_stream_with_no_content_has_no_ttft(gw: Gateway) -> None:
    """Nothing arrived first, so there is no first-token time to report. None, not zero."""
    only_usage = [openai_shaped_events()[4], b"data: [DONE]\n\n"]
    with respx.mock(assert_all_called=True) as mock:
        mock.post(OPENWEIGHTS_URL).mock(return_value=_stream_response(_SSE(only_usage, delay_s=0)))
        resp = gw.chat_stream(_req(), purpose="load-test")
    assert resp.ok and resp.text is None and resp.ttft_ms is None
    assert gw.ledger.rows()[0]["ttft_ms"] is None
    assert resp.costed, "the usage still arrived, so the row is still costed"


# -- the async twin -----------------------------------------------------------------------


async def test_the_async_twin_measures_the_same_delay(gw: Gateway) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.post(OPENWEIGHTS_URL).mock(
            return_value=_stream_response(
                _SSE(openai_shaped_events(), delay_s=FIRST_TOKEN_DELAY_S, delay_before=1)
            )
        )
        resp = await gw.achat_stream(_req(), purpose="load-test", run_id="lt-a")
    assert resp.ok and resp.text == "Hello" and resp.ttft_ms is not None
    assert FIRST_TOKEN_DELAY_S * 1000 <= resp.ttft_ms <= (FIRST_TOKEN_DELAY_S + TOLERANCE_S) * 1000
    assert resp.costed and resp.cost_usd == pytest.approx(EXPECTED_COST)
    row = gw.ledger.rows()[0]
    assert row["run_id"] == "lt-a" and row["ttft_ms"] == pytest.approx(resp.ttft_ms)


async def test_sixty_four_concurrent_streams_each_write_a_row_with_a_ttft(gw: Gateway) -> None:
    """The load client holds 64 streams open at once. Each must be timed and ledgered on
    its own, and the whole batch must not serialise behind the ledger or the parser."""
    n = 64
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(OPENWEIGHTS_URL).mock(
            side_effect=lambda request: _stream_response(
                _SSE(openai_shaped_events(), delay_s=FIRST_TOKEN_DELAY_S, delay_before=1)
            )
        )
        t0 = time.perf_counter()
        responses = await asyncio.gather(
            *(gw.achat_stream(_req(), purpose="load-test", run_id="lt-64") for _ in range(n))
        )
        wall = time.perf_counter() - t0
        assert route.call_count == n
    assert all(r.ok and r.text == "Hello" and r.ttft_ms is not None for r in responses)
    assert gw.ledger.count() == n
    assert all(r["ttft_ms"] is not None and r["costed"] == 1 for r in gw.ledger.rows())
    # Serialised, this would take at least 64 x 150 ms = 9.6 s. Concurrent, about one delay.
    #
    # Half of that rather than a quarter (2026-09-20): the quarter is 2.4 s, and a loaded
    # hosted runner took 2.46 s on a run where nothing was serial, which made a timing
    # margin into a flake and the flake into a red build on an unrelated change. What this
    # assertion is for is telling concurrent from serial, and 4.8 s against a serial floor
    # of 9.6 s still does that without ambiguity. A tighter bound measures the runner.
    assert wall < n * FIRST_TOKEN_DELAY_S / 2, f"streams ran serially: {wall:.2f}s for {n}"


def test_the_pool_admits_at_least_sixty_four_connections() -> None:
    assert POOL_LIMITS.max_connections is not None and POOL_LIMITS.max_connections >= 64
    t = Transport(Timeouts())
    try:
        pool = t._async._transport._pool  # type: ignore[attr-defined]
        assert pool._max_connections >= 64
    finally:
        t.close()


# -- refusals -----------------------------------------------------------------------------


def test_passthrough_is_refused_before_anything_is_built(gw: Gateway) -> None:
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(OPENWEIGHTS_URL).mock(
            return_value=_stream_response(_SSE(openai_shaped_events(), delay_s=0))
        )
        with pytest.raises(PassthroughViolation, match="standard mode only"):
            gw.chat_stream(_req(), purpose="t", mode=Mode.PASSTHROUGH)
        assert route.call_count == 0
    assert gw.ledger.count() == 0, "a refused call is not a call"


async def test_the_async_twin_refuses_passthrough_too(gw: Gateway) -> None:
    with pytest.raises(PassthroughViolation):
        await gw.achat_stream(_req(), purpose="t", mode=Mode.PASSTHROUGH)
    assert gw.ledger.count() == 0


def test_other_provider_kinds_are_not_implemented_yet_and_say_so(gw: Gateway) -> None:
    with pytest.raises(NotImplementedError, match=r"'anthropic'.*openai_compat"):
        gw.chat_stream(_req(model="anthropic/claude-haiku-4-5-20251001"), purpose="t")
    assert gw.ledger.count() == 0


def test_a_stream_never_reads_or_writes_the_development_cache(
    repo_config: BoundaryConfig, tmp_path: Path, keys: None
) -> None:
    """A cached answer has no first token to time, so a stream is always an upstream call."""
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
            route = mock.post(OPENWEIGHTS_URL).mock(
                side_effect=lambda request: _stream_response(
                    _SSE(openai_shaped_events(), delay_s=0)
                )
            )
            first = g.chat_stream(_req(), purpose="t")
            second = g.chat_stream(_req(), purpose="t")
            assert route.call_count == 2
        assert first.cached is False and second.cached is False
        assert not (tmp_path / "cache").exists(), "a stream must never write the cache"
    finally:
        g.close()


# -- when the host does not play along ----------------------------------------------------


def test_a_stream_without_usage_is_written_uncosted_not_at_zero(
    repo_config: BoundaryConfig, tmp_path: Path, keys: None
) -> None:
    """A host that ignores stream_options streams text and no counts. Zero tokens at a real
    rate is US$0.00, which is not what the call cost; it is what the library could not see.
    The row says uncosted, and strict costing raises, exactly as for an unknown price."""
    no_usage = [e for e in openai_shaped_events() if b'"usage"' not in e]
    g = make_gateway(repo_config, tmp_path)
    try:
        with respx.mock(assert_all_called=True) as mock:
            mock.post(OPENWEIGHTS_URL).mock(
                return_value=_stream_response(_SSE(no_usage, delay_s=0))
            )
            resp = g.chat_stream(_req(), purpose="t")
        assert resp.ok and resp.text == "Hello"
        assert resp.costed is False and resp.cost_usd is None and resp.price_list is None
        row = g.ledger.rows()[0]
        assert row["costed"] == 0 and row["cost_usd"] is None
        assert row["price_list"] == g.prices.name, "the list was consulted; it had no counts to use"
        assert g.ledger.uncosted_count() == 1
    finally:
        g.close()
    strict = make_gateway(repo_config, tmp_path / "strict", strict_cost=True)
    try:
        with respx.mock(assert_all_called=True) as mock:
            mock.post(OPENWEIGHTS_URL).mock(
                return_value=_stream_response(_SSE(no_usage, delay_s=0))
            )
            with pytest.raises(UnknownPrice):
                strict.chat_stream(_req(), purpose="t")
        assert strict.ledger.count() == 1
    finally:
        strict.close()


def test_a_retryable_status_is_retried_and_the_stream_that_follows_is_timed(
    gw: Gateway,
) -> None:
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(OPENWEIGHTS_URL)
        route.side_effect = [
            httpx.Response(503, json={"error": {"message": "busy", "type": "overloaded"}}),
            _stream_response(
                _SSE(openai_shaped_events(), delay_s=FIRST_TOKEN_DELAY_S, delay_before=1)
            ),
        ]
        resp = gw.chat_stream(_req(), purpose="t")
        assert route.call_count == 2
    assert resp.ok and resp.retries == 1 and resp.text == "Hello"
    assert resp.ttft_ms is not None and resp.ttft_ms >= FIRST_TOKEN_DELAY_S * 1000
    assert gw.ledger.rows()[0]["retries"] == 1


def test_a_stream_that_breaks_after_it_began_is_not_retried(gw: Gateway) -> None:
    """The host produced tokens it may bill for and the library cannot count them. A second
    attempt would put two hosts' worth of work on one row, so the failure is recorded and
    the caller asks again on a new row."""
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(OPENWEIGHTS_URL).mock(return_value=_stream_response(_Breaks()))
        with pytest.raises(ProviderError) as ei:
            gw.chat_stream(_req(), purpose="t")
        assert route.call_count == 1, "no retry once the stream has begun"
    assert ei.value.status == "ReadTimeout" and ei.value.retries == 0
    row = gw.ledger.rows()[0]
    assert row["error_type"] == "ReadTimeout" and row["http_status"] is None
    assert row["costed"] == 0 and row["cost_usd"] is None and row["retries"] == 0


def test_a_transport_failure_before_any_byte_is_retried_like_chat(gw: Gateway) -> None:
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(OPENWEIGHTS_URL).mock(side_effect=httpx.ConnectTimeout("nope"))
        with pytest.raises(ProviderError):
            gw.chat_stream(_req(), purpose="t")
        assert route.call_count == gw.config.retry.max_attempts
    row = gw.ledger.rows()[0]
    assert row["error_type"] == "ConnectTimeout"
    assert row["retries"] == gw.config.retry.max_attempts - 1


def test_a_vendor_error_status_is_parsed_as_an_error_not_as_events(gw: Gateway) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.post(OPENWEIGHTS_URL).mock(
            return_value=httpx.Response(
                400, json={"error": {"message": "bad model", "type": "invalid_request_error"}}
            )
        )
        with pytest.raises(ProviderError, match="invalid_request_error: bad model"):
            gw.chat_stream(_req(), purpose="t")
    row = gw.ledger.rows()[0]
    assert row["http_status"] == 400 and row["error_type"] == "http_400" and row["ttft_ms"] is None


def test_an_error_event_inside_a_200_stream_is_a_malformed_response(gw: Gateway) -> None:
    events = [_event({"error": {"message": "context length exceeded", "type": "invalid"}})]
    with respx.mock(assert_all_called=True) as mock:
        mock.post(OPENWEIGHTS_URL).mock(return_value=_stream_response(_SSE(events, delay_s=0)))
        with pytest.raises(ProviderError, match="context length exceeded"):
            gw.chat_stream(_req(), purpose="t")
    row = gw.ledger.rows()[0]
    assert row["http_status"] == 200 and row["error_type"] == "MalformedResponse"


def test_an_empty_200_stream_is_a_malformed_response(gw: Gateway) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.post(OPENWEIGHTS_URL).mock(return_value=_stream_response(_SSE([], delay_s=0)))
        with pytest.raises(ProviderError, match="no events"):
            gw.chat_stream(_req(), purpose="t")
    assert gw.ledger.rows()[0]["error_type"] == "MalformedResponse"


def test_the_span_carries_the_first_token_time_and_no_content(
    repo_config: BoundaryConfig, tmp_path: Path, keys: None, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = repo_config.model_copy(
        update={"telemetry": repo_config.telemetry.model_copy(update={"exporter": "console"})}
    )
    g = make_gateway(cfg, tmp_path)
    try:
        with respx.mock(assert_all_called=True) as mock:
            mock.post(OPENWEIGHTS_URL).mock(
                return_value=_stream_response(_SSE(openai_shaped_events(), delay_s=0))
            )
            g.chat_stream(
                _req(messages=[{"role": "user", "content": "Patient Jane Q Public"}]),
                purpose="t",
            )
    finally:
        g.close()
    out = capsys.readouterr().out
    assert '"name": "boundary.chat_stream"' in out and "boundary.ttft_ms" in out
    assert "Jane" not in out and "Hello" not in out


# -- the decoder --------------------------------------------------------------------------


def test_the_sse_decoder_handles_split_chunks_crlf_comments_and_multiline_data() -> None:
    d = SSEDecoder()
    assert d.feed(b'data: {"a":') == []
    assert d.feed(b" 1}\n\n: a comment\r\nevent: ping\r\ndata: x\r\ndata: y\r\n\r\n") == [
        '{"a": 1}',
        "x\ny",
    ]
    assert d.feed(b"data:no-space\n") == []
    assert d.finish() == ["no-space"]
    assert d.finish() == []


def test_the_parser_reads_the_openai_and_llamacpp_shapes_alike() -> None:
    adapter = OpenAICompatAdapter()
    for events in (openai_shaped_events(), llamacpp_shaped_events()):
        parser = adapter.stream_parser()
        decoder = SSEDecoder()
        firsts = [parser.feed(d) for e in events for d in decoder.feed(e)]
        assert any(firsts), "at least one event carried content"
        assert parser.usage_seen
        parsed = parser.result()
        assert parsed.text == "Hello" and parsed.finish_reason == "stop"
        assert parsed.usage.input_tokens == 100 and parsed.usage.output_tokens == 20
        assert parsed.model_returned == "openai/gpt-oss-120b"
        assert parsed.raw["choices"][0]["message"]["content"] == "Hello"
        assert parsed.raw["assembled_from_stream_events"] == len(events) - 1


def test_the_role_only_event_is_not_a_token() -> None:
    parser = OpenAICompatAdapter().stream_parser()
    assert parser.feed(json.dumps(_chunk({"role": "assistant", "content": ""}))) is False
    assert parser.feed(json.dumps(_chunk({"content": "H"}))) is True
    assert parser.feed("[DONE]") is False
