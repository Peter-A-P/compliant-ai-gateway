"""The OpenAI-compatible proxy on the library. PLAN.md B2.1, B2.2 (the header), B2.7.

`POST /v1/chat/completions` (streamed or not), `GET /v1/models` and `GET /healthz`, so a
client written for OpenAI works by changing its base URL and its key. Every call goes
through the same `Gateway` Part A built, one per team, so the proxy writes exactly the
ledger row a library caller would: same columns, same costing, same caps, same policy.

What the proxy adds is the boundary's side of three things the library records but cannot
decide:

- **Who is calling.** A bearer key maps to a team (`boundary.server.teams`), and the team is
  the row's `project`. An unknown key is refused before anything is read.
- **What the data is.** `X-Data-Class` is the caller's declaration. Absent means `personal`:
  the library records an absent claim as null, and the proxy, which is the compliance
  boundary, substitutes the class that fails closed and passes it on as a declaration, so
  the row says what the boundary decided (PLAN.md B2.2, amended 2026-09-19).
- **Whether it may go.** The proxy refuses to start without a data policy, so every request
  through it is judged by `boundary.enforce`. A refusal is a 403 and a `policy_refused` row.

Limits are 429 with a body that names the limit and when it resets: a team's monthly or
per-run budget (Part A caps over the shared ledger), the gateway's monthly ceiling, or the
team's requests per minute.

Not here yet, and PLAN.md says where each lands: redaction before a `personal` call leaves
(B2.3, stage 2), the semantic cache (B2.5), the audit chain appended as the proxy answers
rather than sealed after (B2.4), Postgres, and the layered load test.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import math
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from boundary import __version__
from boundary.config import BoundaryConfig
from boundary.enforce import DataPolicy, load_policy
from boundary.errors import (
    ConfigError,
    PolicyRefused,
    ProviderError,
    SpendCapExceeded,
    UnknownAlias,
)
from boundary.gateway import Gateway
from boundary.providers import STREAM_ADAPTERS
from boundary.routes import ModelRef
from boundary.server import wire
from boundary.server.redaction import (
    Redacted,
    RedactionRefused,
    StreamRehydrator,
    leak_counts,
    redact_request,
)
from boundary.server.teams import RequestQuota, TeamsConfig, hash_key
from boundary.transport import Transport
from boundary.types import ChatResponse, DataClass, Mode

# The class a request without the header is judged as. Fails closed (PLAN.md B2.2).
ABSENT_CLASS = DataClass.PERSONAL
DEFAULT_PURPOSE = "proxy"
_MAX_LABEL = 200


class Refusal(Exception):
    """A request answered with an error before, or instead of, a call."""

    def __init__(
        self, status: int, body: Mapping[str, Any], headers: Mapping[str, str] | None = None
    ) -> None:
        super().__init__(status)
        self.status = status
        self.body = dict(body)
        self.headers = dict(headers or {})


@dataclass
class ServerState:
    """What the app holds for its lifetime. `tasks` keeps a reference to every call in
    flight, so a call whose client has gone away still runs to its ledger row."""

    gateways: dict[str, Gateway]
    teams: TeamsConfig
    by_hash: dict[str, str]
    quota: RequestQuota
    wall: Callable[[], float]
    policy: DataPolicy
    tasks: set[asyncio.Task[ChatResponse]] = field(default_factory=set)

    async def close(self) -> None:
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        for gw in self.gateways.values():
            await gw.aclose()


def create_app(
    config: BoundaryConfig,
    teams: TeamsConfig,
    *,
    ledger_path: Path | None = None,
    env: str | None = None,
    policy: DataPolicy | None = None,
    transport: Transport | None = None,
    clock: Callable[[], float] = time.monotonic,
    wall: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    asleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> FastAPI:
    """The proxy as an ASGI app. Refuses to build without a data policy: a boundary that
    enforces nothing when its operator forgot a file is not a boundary."""
    if policy is None:
        if config.policy is None:
            raise ConfigError(
                "the proxy needs a data policy and none was given: name one with `policy:` "
                "in boundary.yaml or --policy. It fails closed rather than start without one"
            )
        policy = load_policy(config.policy)
    shared = transport or Transport(config.defaults.timeouts)
    caps = teams.caps()
    gateways = {
        name: Gateway(
            config,
            project=name,
            ledger_path=ledger_path,
            caps=caps,
            transport=shared,
            env=env,
            policy=policy,
            sleep=sleep,
            asleep=asleep,
        )
        for name in teams.teams
    }
    state = ServerState(
        gateways=gateways,
        teams=teams,
        by_hash=teams.by_hash(),
        quota=RequestQuota(clock),
        wall=wall,
        policy=policy,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        await state.close()

    app = FastAPI(
        title="boundary",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.boundary = state

    @app.exception_handler(Refusal)
    async def _refusal(_request: Request, exc: Refusal) -> Response:
        return JSONResponse(exc.body, status_code=exc.status, headers=exc.headers)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/v1/models")
    async def models(request: Request) -> dict[str, Any]:
        _team(state, request)
        return {
            "object": "list",
            "data": [
                {"id": alias, "object": "model", "created": 0, "owned_by": route.provider}
                for alias, route in sorted(config.routes.items())
            ],
        }

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request) -> Response:
        team = _team(state, request)
        try:
            body = await request.json()
        except ValueError:
            raise Refusal(
                400, wire.error_body("the body is not valid JSON", type_="invalid_request_error")
            ) from None
        try:
            parsed = wire.parse_request(body)
        except wire.WireError as e:
            raise Refusal(
                400,
                wire.error_body(str(e), type_="invalid_request_error", code=e.code, param=e.param),
            ) from None
        data_class, source = _data_class(request)
        purpose = _label(request, "x-boundary-purpose") or DEFAULT_PURPOSE
        run_id = _label(request, "x-boundary-run-id")

        quota = state.quota.take(team, state.teams.teams[team].requests_per_minute)
        if not quota.allowed:
            wait = max(1, math.ceil(quota.retry_after_s))
            raise Refusal(
                429,
                wire.error_body(
                    f"team {team} has made {quota.used} requests in the last minute, its "
                    f"quota of {quota.limit}; retry in {wait} s",
                    type_="rate_limit_exceeded",
                    code="team_requests_per_minute",
                    limit=quota.limit,
                    used=quota.used,
                    retry_after_s=round(quota.retry_after_s, 3),
                ),
                {"retry-after": str(wait)},
            )

        gw = state.gateways[team]
        ref = _resolve(gw, parsed.request.model)
        headers = {
            "x-boundary-data-class": data_class.value,
            "x-boundary-data-class-source": source,
            "x-boundary-version": __version__,
        }
        redaction = _redact(state.policy, data_class, parsed)
        headers["x-boundary-redacted"] = "true" if redaction is not None else "false"
        if redaction is not None:
            headers["x-boundary-placeholders"] = str(redaction.placeholders)
        call = _Call(state, gw, parsed, purpose, run_id, data_class, ref, headers, redaction)
        if not parsed.stream:
            resp = await call.run(call.plain())
            headers["x-boundary-call-uid"] = resp.call_uid or ""
            if redaction is not None:
                headers["x-boundary-unresolved"] = str(
                    len(redaction.policy.unresolved(resp.text or ""))
                )
            return JSONResponse(
                wire.completion_body(resp, created=int(state.wall()), text=call.restore(resp.text)),
                headers=headers,
            )
        if ref.provider_config.kind in STREAM_ADAPTERS:
            return await call.stream_native()
        return await call.stream_whole()

    return app


# -- request helpers ------------------------------------------------------------------------


def _team(state: ServerState, request: Request) -> str:
    auth = request.headers.get("authorization", "")
    scheme, _, key = auth.partition(" ")
    if scheme.lower() != "bearer" or not key.strip():
        raise Refusal(
            401,
            wire.error_body(
                "a bearer key is required: Authorization: Bearer <team key>",
                type_="invalid_request_error",
                code="missing_api_key",
            ),
        )
    team = state.by_hash.get(hash_key(key.strip()))
    if team is None:
        raise Refusal(
            401,
            wire.error_body(
                "the key is not one this gateway issued",
                type_="invalid_request_error",
                code="invalid_api_key",
            ),
        )
    return team


def _data_class(request: Request) -> tuple[DataClass, str]:
    raw = request.headers.get("x-data-class")
    if raw is None or not raw.strip():
        return ABSENT_CLASS, "absent"
    try:
        return DataClass(raw.strip().lower()), "header"
    except ValueError:
        known = ", ".join(c.value for c in DataClass)
        raise Refusal(
            400,
            wire.error_body(
                f"X-Data-Class {raw!r} is not one of {known}; the vocabulary is closed, and an "
                "absent header is judged as personal",
                type_="invalid_request_error",
                code="invalid_data_class",
                param="X-Data-Class",
            ),
        ) from None


def _label(request: Request, name: str) -> str | None:
    value = request.headers.get(name)
    if value is None or not value.strip():
        return None
    value = value.strip()
    if len(value) > _MAX_LABEL:
        raise Refusal(
            400,
            wire.error_body(
                f"{name} is longer than {_MAX_LABEL} characters",
                type_="invalid_request_error",
                param=name,
            ),
        )
    return value


def _resolve(gw: Gateway, model: str) -> ModelRef:
    try:
        return gw.resolve(model, Mode.STANDARD)
    except (UnknownAlias, ConfigError) as e:
        raise Refusal(
            404,
            wire.error_body(
                f"model {model!r} is not a route or provider on this gateway: {e}",
                type_="invalid_request_error",
                code="model_not_found",
                param="model",
            ),
        ) from None


def _redact(policy: DataPolicy, data_class: DataClass, parsed: wire.Parsed) -> Redacted | None:
    """Redact the request when its class's rule names a `redacted_as`, or refuse it with a
    422 when the guard will not vouch for the result. The refusal carries counts by kind and
    type, never a value."""
    rule = policy.classes.get(data_class)
    if rule is None or rule.redacted_as is None:
        return None
    try:
        return redact_request(parsed.request)
    except RedactionRefused as e:
        counts = leak_counts(e)
        raise Refusal(
            422,
            wire.error_body(
                f"the redaction guard would not vouch for this {data_class.value} request "
                f"({sum(counts.values())} finding(s) after redaction), so nothing was sent",
                type_="redaction_refused",
                code="leak_after_redaction",
                findings=counts,
            ),
        ) from None


def _next_month(now: dt.datetime) -> dt.datetime:
    first = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return (first + dt.timedelta(days=32)).replace(day=1)


def refusal_for(exc: BaseException, *, team: str, wall: Callable[[], float]) -> Refusal:
    """The HTTP answer for an exception a call raised. Anything not listed is re-raised by
    the caller, so an unexpected failure is a 500 with a traceback in the server log rather
    than a polite error that hides it."""
    if isinstance(exc, Refusal):
        return exc
    if isinstance(exc, PolicyRefused):
        return Refusal(
            403,
            wire.error_body(
                f"the data policy refuses {exc.data_class} data to {exc.provider}: {exc.reason}",
                type_="policy_refused",
                code="data_class_not_allowed",
                data_class=exc.data_class,
                provider=exc.provider,
                ledger_id=exc.ledger_id,
            ),
        )
    if isinstance(exc, SpendCapExceeded):
        return _budget(exc, team=team, wall=wall)
    if isinstance(exc, ProviderError):
        status = exc.status if isinstance(exc.status, int) else None
        if status == 429:
            code, http = "upstream_rate_limited", 429
        elif status is not None and 400 <= status < 500:
            code, http = "upstream_refused", status
        else:
            code, http = "upstream_failed", 502
        return Refusal(
            http,
            wire.error_body(
                f"{exc.provider} did not answer: {exc}",
                type_="upstream_error",
                code=code,
                upstream_status=exc.status,
                retries=exc.retries,
            ),
        )
    if isinstance(exc, NotImplementedError | ValueError):
        return Refusal(400, wire.error_body(str(exc), type_="invalid_request_error"))
    if isinstance(exc, ConfigError):
        return Refusal(500, wire.error_body(f"gateway configuration: {exc}", type_="server_error"))
    raise exc


def _budget(exc: SpendCapExceeded, *, team: str, wall: Callable[[], float]) -> Refusal:
    now = dt.datetime.fromtimestamp(wall(), dt.UTC)
    if exc.scope.startswith("portfolio"):
        code, what = "gateway_monthly_budget", "the gateway's monthly ceiling"
        resets: dt.datetime | None = _next_month(now)
    elif " run " in exc.scope:
        code, what = "team_run_budget", f"team {team}'s per-run budget"
        resets = None
    else:
        code, what = "team_monthly_budget", f"team {team}'s monthly budget"
        resets = _next_month(now)
    headers: dict[str, str] = {}
    when = "a new run id starts a new count"
    if resets is not None:
        headers["retry-after"] = str(max(1, math.ceil((resets - now).total_seconds())))
        when = f"it resets at {resets.isoformat().replace('+00:00', 'Z')}"
    return Refusal(
        429,
        wire.error_body(
            f"{what} is US${exc.cap_usd:g}; US${exc.spent_usd:.4f} is spent and this call "
            f"could cost up to US${exc.estimate_usd:.4f}, so it was not sent; {when}",
            type_="budget_exceeded",
            code=code,
            limit_usd=exc.cap_usd,
            spent_usd=round(exc.spent_usd, 6),
            estimate_usd=round(exc.estimate_usd, 6),
            resets_at=resets.isoformat().replace("+00:00", "Z") if resets else None,
        ),
        headers,
    )


# -- one call -------------------------------------------------------------------------------


class _Call:
    def __init__(
        self,
        state: ServerState,
        gw: Gateway,
        parsed: wire.Parsed,
        purpose: str,
        run_id: str | None,
        data_class: DataClass,
        ref: ModelRef,
        headers: dict[str, str],
        redaction: Redacted | None = None,
    ) -> None:
        self.state = state
        self.redaction = redaction
        # What is sent: the redacted request when the class calls for it.
        self.request = redaction.request if redaction is not None else parsed.request
        self.gw = gw
        self.parsed = parsed
        self.purpose = purpose
        self.run_id = run_id
        self.data_class = data_class
        self.ref = ref
        self.headers = headers

    def _task(self, coro: Awaitable[ChatResponse]) -> asyncio.Task[ChatResponse]:
        async def wrapped() -> ChatResponse:
            return await coro

        task = asyncio.create_task(wrapped())
        self.state.tasks.add(task)
        task.add_done_callback(self.state.tasks.discard)
        return task

    def restore(self, text: str | None) -> str | None:
        """The answer as the client sees it: rehydrated when the request was redacted."""
        if text is None or self.redaction is None:
            return text
        return self.redaction.policy.rehydrate(text)

    def plain(self) -> asyncio.Task[ChatResponse]:
        return self._task(
            self.gw.achat(
                self.request,
                purpose=self.purpose,
                run_id=self.run_id,
                data_class=self.data_class,
                redacted=self.redaction is not None,
            )
        )

    async def run(self, task: asyncio.Task[ChatResponse]) -> ChatResponse:
        """Await a call, shielded: a client that disconnects cancels this wait and not the
        call, which runs on to write its row."""
        try:
            return await asyncio.shield(task)
        except Exception as e:
            raise refusal_for(e, team=self.gw.project, wall=self.state.wall) from None

    def _streaming(self, events: AsyncIterator[bytes], how: str) -> StreamingResponse:
        self.headers["x-boundary-stream"] = how
        self.headers["cache-control"] = "no-cache"
        return StreamingResponse(events, media_type="text/event-stream", headers=self.headers)

    async def stream_whole(self) -> Response:
        """A route whose adapter cannot stream (Anthropic, Google and the hyperscalers in
        0.13): the call is made whole and sent as a stream of one content event, labelled
        `x-boundary-stream: whole`. A client that asked for a stream still gets one, and the
        row says `ttft_ms` is null, which is true: no first token was timed."""
        resp = await self.run(self.plain())
        created = int(self.state.wall())
        model = resp.model_returned or self.ref.model
        cid = wire.completion_id(resp.call_uid)
        include_usage = self.parsed.include_usage
        text = self.restore(resp.text)

        async def events() -> AsyncIterator[bytes]:
            yield wire.chunk(
                cid, created=created, model=model, delta={"role": "assistant", "content": ""}
            )
            if text:
                yield wire.chunk(cid, created=created, model=model, delta={"content": text})
            yield wire.chunk(
                cid, created=created, model=model, finish=wire.finish_reason(resp.finish_reason)
            )
            if include_usage:
                yield wire.chunk(cid, created=created, model=model, usage=resp.usage)
            yield wire.DONE

        self.headers["x-boundary-call-uid"] = resp.call_uid or ""
        return self._streaming(events(), "whole")

    async def stream_native(self) -> Response:
        """Text is passed on as the host sends it. Nothing is sent to the client until the
        first piece of text arrives or the call ends, so a refusal (policy, budget, an
        upstream error before the stream began) is still an HTTP status and not an event
        inside a 200. After that, a failure is an `error` event, which OpenAI's client
        raises, and the row records it either way."""
        queue: asyncio.Queue[str | None] = asyncio.Queue()

        async def on_text(piece: str) -> None:
            queue.put_nowait(piece)

        task = self._task(
            self.gw.achat_stream(
                self.request,
                purpose=self.purpose,
                run_id=self.run_id,
                data_class=self.data_class,
                on_text=on_text,
                redacted=self.redaction is not None,
            )
        )
        task.add_done_callback(lambda _t: queue.put_nowait(None))
        first = await queue.get()
        if first is None:
            # Ended before any text: either refused or failed, which is a status, or
            # answered with no text at all, which is a stream with no content event.
            await self.run(task)

        created = int(self.state.wall())
        # The call_uid is only known when the row completes, which is after the headers
        # are sent, so a stream's id is its own and the uid rides on the final event.
        cid = f"chatcmpl-stream-{secrets.token_hex(12)}"
        model = self.ref.model
        include_usage = self.parsed.include_usage
        team = self.gw.project
        wall = self.state.wall
        # A placeholder can arrive split across pieces, so a redacted answer is rehydrated
        # through a buffer that never releases half of one.
        rehydrator = StreamRehydrator(self.redaction.policy) if self.redaction is not None else None

        def out(piece: str) -> str:
            return rehydrator.feed(piece) if rehydrator is not None else piece

        async def events() -> AsyncIterator[bytes]:
            yield wire.chunk(
                cid, created=created, model=model, delta={"role": "assistant", "content": ""}
            )
            piece = first
            while piece is not None:
                text = out(piece)
                if text:
                    yield wire.chunk(cid, created=created, model=model, delta={"content": text})
                piece = await queue.get()
            tail = rehydrator.flush() if rehydrator is not None else ""
            if tail:
                yield wire.chunk(cid, created=created, model=model, delta={"content": tail})
            exc = task.exception()
            if exc is not None:
                refusal = refusal_for(exc, team=team, wall=wall)
                yield wire.error_event(refusal.body)
                return
            resp = task.result()
            returned = resp.model_returned or model
            # The ledger's call_uid rides on the last event with a choice.
            yield wire.chunk(
                cid,
                created=created,
                model=returned,
                finish=wire.finish_reason(resp.finish_reason),
                extra={"call_uid": resp.call_uid},
            )
            if include_usage:
                yield wire.chunk(cid, created=created, model=returned, usage=resp.usage)
            yield wire.DONE

        return self._streaming(events(), "native")
