"""The OpenAI-compatible proxy (0.13, PLAN.md B2.1, B2.2's header, B2.7).

The conformance tests drive the proxy with OpenAI's own client, changing only its base URL
and key, because B10 asks that "a real client library works" and nothing short of the real
client tests that. The upstream vendors are respx mocks behind the proxy; the client reaches
the proxy through an in-process ASGI transport, which respx does not intercept.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from collections.abc import AsyncIterator, Iterator, Sequence
from pathlib import Path
from typing import Any

import httpx
import httpx2
import openai
import pytest
import respx

from boundary.config import BoundaryConfig
from boundary.enforce import load_policy
from boundary.errors import ConfigError
from boundary.ledger.store import LedgerStore
from boundary.server import create_app
from boundary.server.teams import RequestQuota, Team, TeamsConfig, hash_key, load_teams, new_key
from boundary.server.wire import WireError, parse_request

from .conftest import ANTHROPIC_URL, CONFIG_DIR, OPENWEIGHTS_URL, anthropic_ok

KEY = "bnd_test-key-for-team-alpha-000000000000"
OTHER = "bnd_test-key-for-team-beta-0000000000000"
MODEL = "openweights/openai/gpt-oss-120b"
LOCAL_URL = "http://127.0.0.1:11434/v1/chat/completions"
PUBLIC = {"X-Data-Class": "public"}
# 2026-09-25 12:00 UTC, so the budget's reset is 2026-10-01 00:00 UTC.
NOW = dt.datetime(2026, 9, 25, 12, 0, tzinfo=dt.UTC).timestamp()

USAGE = {"prompt_tokens": 100, "completion_tokens": 20}


def completion(text: str = "Hello", model: str = "openai/gpt-oss-120b") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-up",
            "object": "chat.completion",
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": "stop",
                }
            ],
            "usage": USAGE,
        },
    )


def _event(obj: dict[str, Any]) -> bytes:
    return b"data: " + json.dumps(obj).encode() + b"\n\n"


def _chunk(delta: dict[str, Any], finish: str | None = None) -> dict[str, Any]:
    return {
        "id": "chatcmpl-s1",
        "object": "chat.completion.chunk",
        "model": "openai/gpt-oss-120b",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }


def sse_events(pieces: Sequence[str] = ("Hel", "lo")) -> list[bytes]:
    return [
        _event(_chunk({"role": "assistant", "content": ""})),
        *(_event(_chunk({"content": p})) for p in pieces),
        _event(_chunk({}, finish="stop")),
        _event(
            {"id": "chatcmpl-s1", "model": "openai/gpt-oss-120b", "choices": [], "usage": USAGE}
        ),
        b"data: [DONE]\n\n",
    ]


class _Stream(httpx.AsyncByteStream):
    def __init__(self, events: Sequence[bytes], *, break_after: int | None = None) -> None:
        self.events = list(events)
        self.break_after = break_after

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for i, e in enumerate(self.events):
            if self.break_after is not None and i == self.break_after:
                raise httpx.ReadTimeout("the host stopped mid-stream")
            await asyncio.sleep(0)
            yield e


def streamed(events: Sequence[bytes], *, break_after: int | None = None) -> httpx.Response:
    return httpx.Response(
        200,
        stream=_Stream(events, break_after=break_after),
        headers={"content-type": "text/event-stream"},
    )


def teams(**overrides: Any) -> TeamsConfig:
    alpha: dict[str, Any] = {
        "key_sha256": [hash_key(KEY)],
        "monthly_usd": 5.0,
        "per_run_usd": 1.0,
        "requests_per_minute": 100,
    }
    alpha.update(overrides)
    return TeamsConfig(
        version=1,
        gateway_monthly_usd=10.0,
        teams={
            "alpha": Team(**alpha),
            "beta": Team(key_sha256=[hash_key(OTHER)], monthly_usd=1.0, requests_per_minute=100),
        },
    )


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


async def _no_sleep(_: float) -> None:
    return None


class Proxy:
    def __init__(self, repo_config: BoundaryConfig, tmp_path: Path, team_cfg: TeamsConfig) -> None:
        self.ledger_path = tmp_path / "proxy.sqlite"
        self.clock = Clock()
        self.app = create_app(
            repo_config,
            team_cfg,
            ledger_path=self.ledger_path,
            policy=load_policy(CONFIG_DIR / "policy.yaml"),
            clock=self.clock,
            wall=lambda: NOW,
            sleep=lambda _s: None,
            asleep=_no_sleep,
        )
        self.http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://proxy"
        )
        # OpenAI's client 3.x is built on httpx2, a separate package from the httpx this
        # library pins, so it gets a client of its own kind over the same app.
        self.http2 = httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=self.app), base_url="http://proxy"
        )

    def client(self, key: str = KEY, headers: dict[str, str] | None = None) -> openai.AsyncOpenAI:
        return openai.AsyncOpenAI(
            base_url="http://proxy/v1",
            api_key=key,
            max_retries=0,
            http_client=self.http2,
            default_headers=headers,
        )

    async def post(
        self, body: dict[str, Any], *, key: str | None = KEY, headers: dict[str, str] | None = None
    ) -> httpx.Response:
        h = dict(headers or {})
        if key is not None:
            h["authorization"] = f"Bearer {key}"
        return await self.http.post("/v1/chat/completions", json=body, headers=h)

    def rows(self) -> list[dict[str, Any]]:
        store = LedgerStore(self.ledger_path)
        try:
            return store.rows()
        finally:
            store.close()

    async def close(self) -> None:
        await self.http.aclose()
        await self.http2.aclose()
        await self.app.state.boundary.close()


@pytest.fixture
def upstream() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        yield router


@pytest.fixture
async def proxy(
    repo_config: BoundaryConfig, tmp_path: Path, keys: None, upstream: respx.MockRouter
) -> AsyncIterator[Proxy]:
    p = Proxy(repo_config, tmp_path, teams())
    yield p
    await p.close()


def body(**kw: Any) -> dict[str, Any]:
    b: dict[str, Any] = {"model": MODEL, "messages": [{"role": "user", "content": "Say hello."}]}
    b.update(kw)
    return b


# -- conformance: OpenAI's own client --------------------------------------------------------


async def test_real_client_non_streaming(proxy: Proxy, upstream: respx.MockRouter) -> None:
    route = upstream.post(OPENWEIGHTS_URL).mock(return_value=completion("Hello"))
    client = proxy.client(headers=PUBLIC)
    raw = await client.chat.completions.with_raw_response.create(
        model=MODEL, messages=[{"role": "user", "content": "Say hello."}], max_tokens=16
    )
    out = raw.parse()
    assert out.choices[0].message.content == "Hello"
    assert out.choices[0].finish_reason == "stop"
    assert out.model == "openai/gpt-oss-120b"
    assert out.usage is not None and out.usage.prompt_tokens == 100
    assert out.usage.completion_tokens == 20
    assert route.call_count == 1

    (row,) = proxy.rows()
    assert row["project"] == "alpha"
    assert row["purpose"] == "proxy"
    assert row["data_class"] == "public"
    assert row["http_status"] == 200
    assert row["costed"] == 1 and row["cost_usd"] > 0
    assert raw.headers["x-boundary-call-uid"] == row["call_uid"]
    assert out.id == f"chatcmpl-{row['call_uid']}"
    assert raw.headers["x-boundary-data-class-source"] == "header"


async def test_real_client_streaming(proxy: Proxy, upstream: respx.MockRouter) -> None:
    upstream.post(OPENWEIGHTS_URL).mock(return_value=streamed(sse_events(("Hel", "lo", "!"))))
    client = proxy.client(headers=PUBLIC)
    stream = await client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": "Say hello."}],
        max_tokens=16,
        stream=True,
        stream_options={"include_usage": True},
    )
    pieces: list[str] = []
    finish: str | None = None
    usage = None
    call_uid = None
    async for ch in stream:
        if ch.usage is not None:
            usage = ch.usage
        for c in ch.choices:
            if c.delta.content:
                pieces.append(c.delta.content)
            if c.finish_reason:
                finish = c.finish_reason
                call_uid = getattr(ch, "call_uid", None)
    assert "".join(pieces) == "Hello!"
    assert finish == "stop"
    assert usage is not None and usage.prompt_tokens == 100 and usage.completion_tokens == 20

    (row,) = proxy.rows()
    assert row["ttft_ms"] is not None
    assert row["costed"] == 1
    assert call_uid == row["call_uid"]


async def test_streaming_writes_the_same_row_as_not_streaming(
    proxy: Proxy, upstream: respx.MockRouter
) -> None:
    """PLAN.md B4's test. Timing, hashes and identifiers differ by nature; ttft exists only
    on the stream. Everything that says what the call was and what it cost must match."""
    route = upstream.post(OPENWEIGHTS_URL)
    route.side_effect = [completion("Hello"), streamed(sse_events(("Hel", "lo")))]
    await proxy.post(body(max_tokens=16), headers=PUBLIC)
    await proxy.post(body(max_tokens=16, stream=True), headers=PUBLIC)
    plain, stream = proxy.rows()
    differ = {
        "id",
        "call_uid",
        "ts_utc",
        "latency_ms",
        "ttft_ms",
        "request_sha256",
        "response_sha256",
        "trace_id",
        "span_id",
    }
    assert {k: v for k, v in plain.items() if k not in differ} == {
        k: v for k, v in stream.items() if k not in differ
    }
    assert plain["ttft_ms"] is None and stream["ttft_ms"] is not None


async def test_a_route_that_cannot_stream_is_sent_whole(
    proxy: Proxy, upstream: respx.MockRouter
) -> None:
    """Anthropic has no streaming adapter in 0.13. A client that asks for a stream still
    gets one, labelled, and the row does not pretend a first token was timed."""
    upstream.post(ANTHROPIC_URL).mock(return_value=anthropic_ok(text="Hi there"))
    r = await proxy.post(body(model="fast", max_tokens=16, stream=True), headers=PUBLIC)
    assert r.status_code == 200
    assert r.headers["x-boundary-stream"] == "whole"
    lines = [ln for ln in r.text.split("\n\n") if ln.startswith("data: ")]
    assert lines[-1] == "data: [DONE]"
    text = "".join(
        c["delta"].get("content") or "" for ln in lines[:-1] for c in json.loads(ln[6:])["choices"]
    )
    assert text == "Hi there"
    (row,) = proxy.rows()
    assert row["provider"] == "anthropic" and row["ttft_ms"] is None


async def test_models_lists_the_routes(proxy: Proxy) -> None:
    models = await proxy.client().models.list()
    ids = {m.id for m in models.data}
    assert {"fast", "balanced", "judge", "frontier"} <= ids


async def test_system_messages_become_the_system_prompt(
    proxy: Proxy, upstream: respx.MockRouter
) -> None:
    route = upstream.post(ANTHROPIC_URL).mock(return_value=anthropic_ok())
    r = await proxy.post(
        body(
            model="fast",
            messages=[
                {"role": "system", "content": "Be brief."},
                {"role": "user", "content": [{"type": "text", "text": "Hi"}]},
            ],
        ),
        headers=PUBLIC,
    )
    assert r.status_code == 200
    sent = json.loads(route.calls.last.request.content)
    assert sent["system"] == "Be brief."
    assert sent["messages"] == [{"role": "user", "content": "Hi"}]


# -- the data class header: absent means personal ------------------------------------------


async def test_absent_header_is_personal_and_refused_off_shore(
    proxy: Proxy, upstream: respx.MockRouter
) -> None:
    route = upstream.post(OPENWEIGHTS_URL).mock(return_value=completion())
    with pytest.raises(openai.PermissionDeniedError) as e:
        await proxy.client().chat.completions.create(
            model=MODEL, messages=[{"role": "user", "content": "x"}]
        )
    assert route.call_count == 0
    err = e.value.body
    assert isinstance(err, dict)
    assert err["type"] == "policy_refused" and err["data_class"] == "personal"
    (row,) = proxy.rows()
    assert row["error_type"] == "policy_refused"
    assert row["data_class"] == "personal"
    assert row["project"] == "alpha"


async def test_personal_data_reaches_the_local_model(
    proxy: Proxy, upstream: respx.MockRouter
) -> None:
    """The Canadian worked example leaves personal data one provider: the local model."""
    route = upstream.post(LOCAL_URL).mock(return_value=completion(model="llama3.2:3b"))
    r = await proxy.post(body(model="local/llama3.2:3b"))
    assert r.status_code == 200, r.text
    assert r.headers["x-boundary-data-class"] == "personal"
    assert r.headers["x-boundary-data-class-source"] == "absent"
    assert route.call_count == 1
    (row,) = proxy.rows()
    assert row["data_class"] == "personal" and row["region"] == "localhost"


async def test_a_streamed_refusal_is_a_status_not_an_event(
    proxy: Proxy, upstream: respx.MockRouter
) -> None:
    route = upstream.post(OPENWEIGHTS_URL).mock(return_value=streamed(sse_events()))
    r = await proxy.post(body(stream=True))
    assert r.status_code == 403
    assert r.json()["error"]["type"] == "policy_refused"
    assert route.call_count == 0


@pytest.mark.parametrize("value", ["secret", "personal,public", "pii"])
async def test_unknown_data_class_is_refused(proxy: Proxy, value: str) -> None:
    r = await proxy.post(body(), headers={"X-Data-Class": value})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_data_class"
    assert proxy.rows() == []


async def test_case_and_space_are_forgiven_the_vocabulary_is_not(
    proxy: Proxy, upstream: respx.MockRouter
) -> None:
    upstream.post(OPENWEIGHTS_URL).mock(return_value=completion())
    r = await proxy.post(body(), headers={"X-Data-Class": " Public "})
    assert r.status_code == 200
    assert proxy.rows()[0]["data_class"] == "public"


# -- keys --------------------------------------------------------------------------------


@pytest.mark.parametrize("key", [None, "", "bnd_not-a-key-this-gateway-issued"])
async def test_no_key_or_a_wrong_key_is_refused_before_anything(
    proxy: Proxy, upstream: respx.MockRouter, key: str | None
) -> None:
    route = upstream.post(OPENWEIGHTS_URL).mock(return_value=completion())
    r = await proxy.post(body(), key=key, headers=PUBLIC)
    assert r.status_code == 401
    assert route.call_count == 0
    assert proxy.rows() == []


async def test_models_needs_a_key(proxy: Proxy) -> None:
    assert (await proxy.http.get("/v1/models")).status_code == 401


async def test_each_team_is_its_own_project(proxy: Proxy, upstream: respx.MockRouter) -> None:
    upstream.post(OPENWEIGHTS_URL).mock(return_value=completion())
    await proxy.post(body(), key=KEY, headers=PUBLIC)
    await proxy.post(body(), key=OTHER, headers=PUBLIC)
    assert [r["project"] for r in proxy.rows()] == ["alpha", "beta"]


# -- nothing is dropped silently -----------------------------------------------------------


@pytest.mark.parametrize(
    ("extra", "param"),
    [
        ({"tools": [{"type": "function", "function": {"name": "f"}}]}, "tools"),
        ({"seed": 7}, "seed"),
        ({"response_format": {"type": "json_object"}}, "response_format"),
        ({"n": 2}, "n"),
    ],
)
async def test_a_field_that_would_be_dropped_is_refused(
    proxy: Proxy, upstream: respx.MockRouter, extra: dict[str, Any], param: str
) -> None:
    route = upstream.post(OPENWEIGHTS_URL).mock(return_value=completion())
    r = await proxy.post(body(**extra), headers=PUBLIC)
    assert r.status_code == 400
    assert r.json()["error"]["param"] == param
    assert route.call_count == 0


def test_image_parts_are_refused() -> None:
    with pytest.raises(WireError, match="text only"):
        parse_request(
            {
                "model": "fast",
                "messages": [
                    {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "x"}}]}
                ],
            }
        )


def test_a_system_message_after_the_conversation_began_is_refused() -> None:
    with pytest.raises(WireError, match="must come first"):
        parse_request(
            {
                "model": "fast",
                "messages": [
                    {"role": "user", "content": "a"},
                    {"role": "system", "content": "b"},
                ],
            }
        )


async def test_an_unknown_model_is_404(proxy: Proxy) -> None:
    r = await proxy.post(body(model="no-such-alias"), headers=PUBLIC)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "model_not_found"


# -- limits: 429 names the limit and when it resets ----------------------------------------


async def test_team_budget_429_names_the_limit_and_the_reset(
    repo_config: BoundaryConfig, tmp_path: Path, keys: None, upstream: respx.MockRouter
) -> None:
    route = upstream.post(OPENWEIGHTS_URL).mock(return_value=completion())
    p = Proxy(repo_config, tmp_path, teams(monthly_usd=0.000001, per_run_usd=None))
    try:
        r = await p.post(body(max_tokens=1000), headers=PUBLIC)
    finally:
        await p.close()
    assert r.status_code == 429
    err = r.json()["error"]
    assert err["type"] == "budget_exceeded"
    assert err["code"] == "team_monthly_budget"
    assert err["limit_usd"] == 0.000001
    assert err["resets_at"] == "2026-10-01T00:00:00Z"
    assert int(r.headers["retry-after"]) == int(
        dt.datetime(2026, 10, 1, tzinfo=dt.UTC).timestamp() - NOW
    )
    assert route.call_count == 0


async def test_run_budget_has_no_reset(
    repo_config: BoundaryConfig, tmp_path: Path, keys: None, upstream: respx.MockRouter
) -> None:
    upstream.post(OPENWEIGHTS_URL).mock(return_value=completion())
    p = Proxy(repo_config, tmp_path, teams(per_run_usd=0.000001))
    try:
        r = await p.post(body(max_tokens=1000), headers={**PUBLIC, "X-Boundary-Run-Id": "run-1"})
    finally:
        await p.close()
    assert r.status_code == 429
    err = r.json()["error"]
    assert err["code"] == "team_run_budget" and err["resets_at"] is None
    assert "retry-after" not in r.headers


async def test_request_quota(
    repo_config: BoundaryConfig, tmp_path: Path, keys: None, upstream: respx.MockRouter
) -> None:
    upstream.post(OPENWEIGHTS_URL).mock(return_value=completion())
    p = Proxy(repo_config, tmp_path, teams(requests_per_minute=2))
    try:
        assert (await p.post(body(), headers=PUBLIC)).status_code == 200
        p.clock.t += 10
        assert (await p.post(body(), headers=PUBLIC)).status_code == 200
        r = await p.post(body(), headers=PUBLIC)
        assert r.status_code == 429
        err = r.json()["error"]
        assert err["code"] == "team_requests_per_minute" and err["limit"] == 2
        assert r.headers["retry-after"] == "50"
        # The other team is not charged for alpha's loop.
        assert (await p.post(body(), key=OTHER, headers=PUBLIC)).status_code == 200
        p.clock.t += 50
        assert (await p.post(body(), headers=PUBLIC)).status_code == 200
    finally:
        await p.close()


def test_quota_window_slides() -> None:
    clock = Clock()
    q = RequestQuota(clock)
    assert q.take("t", 2).allowed
    clock.t += 30
    assert q.take("t", 2).allowed
    clock.t += 29
    refused = q.take("t", 2)
    assert not refused.allowed and refused.retry_after_s == pytest.approx(1.0)
    clock.t += 1
    assert q.take("t", 2).allowed


# -- upstream failures ---------------------------------------------------------------------


@pytest.mark.parametrize(("upstream_status", "proxy_status"), [(500, 502), (400, 400), (429, 429)])
async def test_upstream_errors(
    proxy: Proxy, upstream: respx.MockRouter, upstream_status: int, proxy_status: int
) -> None:
    upstream.post(OPENWEIGHTS_URL).mock(
        return_value=httpx.Response(upstream_status, json={"error": {"message": "no"}})
    )
    r = await proxy.post(body(), headers=PUBLIC)
    assert r.status_code == proxy_status
    assert r.json()["error"]["type"] == "upstream_error"
    (row,) = proxy.rows()
    assert row["error_type"] == f"http_{upstream_status}"


async def test_a_stream_that_breaks_is_an_error_event_and_a_row(
    proxy: Proxy, upstream: respx.MockRouter
) -> None:
    upstream.post(OPENWEIGHTS_URL).mock(return_value=streamed(sse_events(), break_after=2))
    stream = await proxy.client(headers=PUBLIC).chat.completions.create(
        model=MODEL, messages=[{"role": "user", "content": "x"}], stream=True
    )
    got: list[str] = []
    with pytest.raises(openai.APIError):
        async for ch in stream:
            got.extend(c.delta.content or "" for c in ch.choices)
    assert "".join(got) == "Hel"
    (row,) = proxy.rows()
    assert row["error_type"] == "ReadTimeout"
    assert row["costed"] == 0


# -- configuration -------------------------------------------------------------------------


def test_the_proxy_will_not_start_without_a_policy(
    repo_config: BoundaryConfig, tmp_path: Path
) -> None:
    assert repo_config.policy is None
    with pytest.raises(ConfigError, match="data policy"):
        create_app(repo_config, teams(), ledger_path=tmp_path / "l.sqlite")


def test_a_raw_key_in_the_teams_file_is_refused() -> None:
    with pytest.raises(ValueError, match="raw key"):
        Team(key_sha256=[KEY], monthly_usd=1, requests_per_minute=1)


def test_one_key_cannot_be_two_teams() -> None:
    h = hash_key(KEY)
    with pytest.raises(ValueError, match="exactly one team"):
        TeamsConfig(
            version=1,
            gateway_monthly_usd=10,
            teams={
                "a": Team(key_sha256=[h], monthly_usd=1, requests_per_minute=1),
                "b": Team(key_sha256=[h], monthly_usd=1, requests_per_minute=1),
            },
        )


def test_budgets_cannot_exceed_the_ceiling() -> None:
    with pytest.raises(ValueError, match="above gateway_monthly_usd"):
        teams().model_copy(update={"gateway_monthly_usd": 1.0}).model_validate(
            {**teams().model_dump(), "gateway_monthly_usd": 1.0}
        )


def test_the_example_teams_file_loads_and_admits_nobody() -> None:
    cfg = load_teams(CONFIG_DIR / "teams.example.yaml")
    assert set(cfg.teams) == {"demo", "analytics"}
    for _ in range(20):
        assert hash_key(new_key()) not in cfg.by_hash()


def test_keys_are_prefixed_and_distinct() -> None:
    keys = {new_key() for _ in range(100)}
    assert len(keys) == 100 and all(k.startswith("bnd_") and len(k) > 40 for k in keys)


# -- over a real socket --------------------------------------------------------------------


class _Pausing(httpx.AsyncByteStream):
    """An upstream that sends its first piece, pauses, then sends the rest."""

    def __init__(self, pause_s: float) -> None:
        self.pause_s = pause_s

    async def __aiter__(self) -> AsyncIterator[bytes]:
        events = sse_events(("Hel", "lo"))
        for i, e in enumerate(events):
            if i == 2:
                await asyncio.sleep(self.pause_s)
            yield e


def test_tokens_reach_a_real_client_before_the_upstream_finishes(
    repo_config: BoundaryConfig, tmp_path: Path, keys: None
) -> None:
    """Uvicorn on a loopback port and OpenAI's synchronous client, so the stream crosses a
    real socket. The first piece must arrive while the upstream is still paused: a proxy
    that buffered the answer would pass every other test here and fail this one."""
    import socket
    import threading
    import time

    import uvicorn

    pause_s = 0.6
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    app = create_app(
        repo_config,
        teams(),
        ledger_path=tmp_path / "proxy.sqlite",
        policy=load_policy(CONFIG_DIR / "policy.yaml"),
        asleep=_no_sleep,
    )
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    with respx.mock(assert_all_called=False) as router:
        router.route(host="127.0.0.1", port=port).pass_through()
        router.post(OPENWEIGHTS_URL).mock(
            return_value=httpx.Response(
                200, stream=_Pausing(pause_s), headers={"content-type": "text/event-stream"}
            )
        )
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started:
                assert time.monotonic() < deadline, "uvicorn did not start"
                time.sleep(0.01)
            client = openai.OpenAI(
                base_url=f"http://127.0.0.1:{port}/v1",
                api_key=KEY,
                max_retries=0,
                default_headers=PUBLIC,
            )
            t0 = time.monotonic()
            arrivals: list[tuple[float, str]] = []
            for ch in client.chat.completions.create(
                model=MODEL, messages=[{"role": "user", "content": "hi"}], stream=True
            ):
                for c in ch.choices:
                    if c.delta.content:
                        arrivals.append((time.monotonic() - t0, c.delta.content))
            client.close()
        finally:
            server.should_exit = True
            thread.join(timeout=10)
    assert [p for _, p in arrivals] == ["Hel", "lo"]
    first, second = arrivals[0][0], arrivals[1][0]
    assert first < pause_s / 2, f"first piece at {first:.3f}s: the proxy buffered the stream"
    assert second >= pause_s * 0.9
    # Uvicorn ran the lifespan, which closed every team's gateway after the call finished.
    store = LedgerStore(tmp_path / "proxy.sqlite")
    try:
        (row,) = store.rows()
    finally:
        store.close()
    assert row["http_status"] == 200 and row["ttft_ms"] is not None


# -- the command line ----------------------------------------------------------------------


def test_teams_key_prints_a_key_once_and_its_hash(capsys: pytest.CaptureFixture[str]) -> None:
    from boundary.cli import main

    assert main(["teams", "key", "--team", "alpha"]) == 0
    lines = capsys.readouterr().out.splitlines()
    key = lines[1].strip()
    assert key.startswith("bnd_")
    assert lines[-1].strip() == f"- {hash_key(key)}"


def test_serve_keeps_its_own_ledger_and_refuses_without_teams(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import uvicorn

    import boundary.cli
    from boundary.cli import PROXY_LEDGER, main
    from boundary.config import load_config

    started: list[Any] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: started.append((app, kw)))
    # The checked-in configuration with its ledger moved here, so the default beside it is
    # created in the test's directory and not in the repository.
    moved = load_config(CONFIG_DIR / "boundary.yaml")
    moved = moved.model_copy(
        update={"ledger": moved.ledger.model_copy(update={"path": tmp_path / "boundary.sqlite"})}
    )
    monkeypatch.setattr(boundary.cli, "load_config", lambda _p: moved)
    teams_file = tmp_path / "teams.yaml"
    teams_file.write_text(
        "version: 1\ngateway_monthly_usd: 1\nteams:\n  t:\n"
        f"    key_sha256: ['{hash_key(KEY)}']\n    monthly_usd: 1\n    requests_per_minute: 1\n",
        encoding="utf-8",
    )
    config = str(CONFIG_DIR / "boundary.yaml")
    assert main(["--config", config, "serve", "--teams", str(teams_file)]) == 0
    ((app, kw),) = started
    assert kw == {"host": "127.0.0.1", "port": 8080, "log_level": "info"}
    gw = app.state.boundary.gateways["t"]
    assert gw.ledger.path == tmp_path / PROXY_LEDGER
    assert gw.policy is not None
    asyncio.run(app.state.boundary.close())
    assert f"ledger {gw.ledger.path}" in capsys.readouterr().err

    assert main(["--config", config, "serve", "--teams", str(tmp_path / "missing.yaml")]) == 2


# -- the adversarial suite through the proxy -----------------------------------------------


def test_the_adversarial_suite_through_the_proxy_sends_nothing_forbidden(
    repo_config: BoundaryConfig,
) -> None:
    from boundary import enforce_eval
    from boundary.cli import SMOKE_MODELS

    results = enforce_eval.run_proxy(repo_config, CONFIG_DIR / "policy.yaml", models=SMOKE_MODELS)
    assert results.violations.hits == 0 and results.violations.total > 200
    assert results.false_refusals.hits == 0
    assert results.audited.hits == results.audited.total > 0
    allowed = [o for o in results.outcomes if o.expected_allowed]
    assert allowed and all(o.sent for o in allowed), "an allowed case never reached the upstream"
    # Absent and blank headers are judged personal, so on this policy they reach only the
    # local model.
    for o in results.outcomes:
        if o.data_class is None or not o.data_class.strip():
            assert o.expected_allowed == (o.target.provider == "local")


def test_the_suite_catches_a_proxy_that_lets_an_absent_header_through(
    repo_config: BoundaryConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The suite's oracle knows the door's rule independently; a proxy that stopped failing
    closed has to show up as violations, not pass quietly."""
    from boundary import enforce_eval
    from boundary.cli import SMOKE_MODELS
    from boundary.server import app as server_app
    from boundary.types import DataClass

    monkeypatch.setattr(server_app, "ABSENT_CLASS", DataClass.PUBLIC)
    results = enforce_eval.run_proxy(repo_config, CONFIG_DIR / "policy.yaml", models=SMOKE_MODELS)
    assert results.violations.hits > 0
