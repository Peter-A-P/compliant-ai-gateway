"""The Gateway: resolve, cap, ledger, send, parse, cost, record. Part of the frozen interface.

Order of operations for one call, and why the order is what it is:

1. Resolve the model (alias or explicit). Pass-through refuses aliases here.
2. Build the exact request bytes with the provider's adapter.
3. Check the spend caps with a pessimistic estimate. A refused call makes no request.
4. Insert the ledger row (in_flight, carrying the estimate) before anything leaves.
5. Send: one attempt in pass-through; the retry policy in standard mode.
6. Parse, cost from returned usage, write the raw record (pass-through only).
7. Complete the ledger row, then return or raise.

Nothing after step 4 can lose the row: a crash leaves an in_flight row with the estimate.
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
import random
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self

from opentelemetry.trace import Span

from boundary import __version__
from boundary.cache import ExactMatchCache
from boundary.config import (
    BoundaryConfig,
    CapsConfig,
    PriceEntry,
    PriceList,
    ProviderConfig,
    ProviderKind,
    latest_price_list,
    load_caps,
    load_config,
)
from boundary.env import find_dotenv, load_dotenv
from boundary.errors import (
    ConfigError,
    PassthroughViolation,
    ProviderError,
    SpendCapExceeded,
    UnknownPrice,
)
from boundary.ledger.prices import cost_usd, estimate_usd
from boundary.ledger.store import LedgerRow, LedgerStore, utc_now
from boundary.providers import ADAPTERS
from boundary.providers.base import Adapter, BuiltRequest, ParsedResponse
from boundary.rawstore import RawStore, sha256_hex
from boundary.routes import ModelRef, resolve
from boundary.telemetry import Telemetry
from boundary.transport import TRANSPORT_ERRORS, HttpResult, Transport
from boundary.types import ChatRequest, ChatResponse, Mode, Usage

RETRY_STATUSES = frozenset({408, 409, 425, 429, 500, 502, 503, 504})
ZERO_PRICE = PriceEntry(
    input=0.0, output=0.0, cache_read=0.0, cache_write=0.0, batch_multiplier=1.0
)
MALFORMED = "MalformedResponse"


@dataclass(frozen=True, slots=True)
class RawResponse:
    """What the escape hatch returns."""

    status: int | str
    headers: Mapping[str, str]
    body: bytes
    json: Any
    latency_ms: float | None
    usage: Usage
    cost_usd: float | None
    costed: bool
    ledger_id: int
    retries: int = 0


@dataclass(slots=True)
class _Call:
    request: ChatRequest
    ref: ModelRef
    adapter: Adapter
    built: BuiltRequest
    mode: Mode
    row: LedgerRow
    price_entry: PriceEntry | None
    span: Span
    cached: HttpResult | None = None
    error_detail: str = ""


class Gateway:
    def __init__(
        self,
        config: BoundaryConfig,
        *,
        project: str,
        ledger_path: Path | None = None,
        raw_store: Path | None = None,
        strict_cost: bool = False,
        transport: Transport | None = None,
        caps: CapsConfig | None = None,
        prices: PriceList | None = None,
        sleep: Callable[[float], None] = time.sleep,
        asleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.config = config
        self.project = project
        self.strict_cost = strict_cost
        self.caps = caps or load_caps(config.caps)
        self.prices = prices or latest_price_list(config.prices)
        self.ledger = LedgerStore(ledger_path or config.ledger.path)
        self.raw_store = RawStore(raw_store) if raw_store is not None else None
        self.cache = ExactMatchCache(config.cache.path) if config.cache.enabled else None
        self.transport = transport or Transport(config.defaults.timeouts)
        self.telemetry = Telemetry(config.telemetry, version=__version__)
        self._sleep = sleep
        self._asleep = asleep

    @classmethod
    def from_config(
        cls,
        path: str | Path,
        *,
        project: str,
        ledger_path: Path | None = None,
        raw_store: Path | None = None,
        strict_cost: bool = False,
        dotenv: Path | None = None,
    ) -> Self:
        """Load boundary.yaml and, for local use, a gitignored .env: the one given, or the
        first found in the working directory, the config directory's parent, or the config
        directory. Variables already set in the environment are never overridden."""
        path = Path(path)
        env_file = dotenv or find_dotenv(Path.cwd(), path.parent.parent, path.parent)
        if env_file is not None:
            load_dotenv(env_file)
        return cls(
            load_config(path),
            project=project,
            ledger_path=ledger_path,
            raw_store=raw_store,
            strict_cost=strict_cost,
        )

    # -- public -------------------------------------------------------------------------

    def resolve(self, model: str, mode: Mode = Mode.STANDARD) -> ModelRef:
        return resolve(model, self.config, mode)

    def chat(
        self,
        request: ChatRequest,
        *,
        purpose: str,
        run_id: str | None = None,
        mode: Mode = Mode.STANDARD,
    ) -> ChatResponse:
        call = self._prepare(request, purpose=purpose, run_id=run_id, mode=mode)
        if call.cached is not None:
            return self._finish(call, call.cached, None, 0, cached=True)
        result, error_type, retries = self._send_sync(call)
        return self._finish(call, result, error_type, retries, cached=False)

    async def achat(
        self,
        request: ChatRequest,
        *,
        purpose: str,
        run_id: str | None = None,
        mode: Mode = Mode.STANDARD,
    ) -> ChatResponse:
        call = self._prepare(request, purpose=purpose, run_id=run_id, mode=mode)
        if call.cached is not None:
            return self._finish(call, call.cached, None, 0, cached=True)
        result, error_type, retries = await self._send_async(call)
        return self._finish(call, result, error_type, retries, cached=False)

    def raw(
        self,
        provider: str,
        method: str,
        path: str,
        json: Any,
        *,
        purpose: str,
        run_id: str | None = None,
        mode: Mode = Mode.STANDARD,
    ) -> RawResponse:
        """Escape hatch for a vendor feature the library does not model. Traced and
        ledgered; costed only when the response carries usage the adapter recognises and a
        price exists for the model it names."""
        pc = self._provider(provider)
        adapter = self._adapter(pc)
        if mode is Mode.PASSTHROUGH and self.raw_store is None:
            raise PassthroughViolation("pass-through mode needs a raw store the caller owns")
        from boundary.providers._json import dumps

        built = BuiltRequest(
            method=method.upper(),
            url=pc.base_url.rstrip("/") + "/" + path.lstrip("/"),
            headers=self._auth_headers(pc, self._api_key(provider, pc)),
            body=dumps(json) if json is not None else b"",
        )
        row = LedgerRow(
            ts_utc=utc_now(),
            boundary_version=__version__,
            project=self.project,
            purpose=purpose,
            mode=mode.value,
            provider=provider,
            model_requested=f"{provider}/raw:{path}",
            run_id=run_id,
            region=pc.region,
            price_list=self.prices.name,
            request_sha256=sha256_hex(built.body),
        )
        self._check_caps(run_id, estimate=0.0)
        span = self.telemetry.start("boundary.raw")
        ids = Telemetry.ids(span)
        row.trace_id, row.span_id = ids.trace_id, ids.span_id
        self.ledger.begin(row)
        call = _Call(
            request=ChatRequest(
                model=f"{provider}/raw", messages=[{"role": "user", "content": ""}]
            ),
            ref=ModelRef(provider=provider, model="raw", provider_config=pc, region=pc.region),
            adapter=adapter,
            built=built,
            mode=mode,
            row=row,
            price_entry=None,
            span=span,
        )
        result, error_type, retries = self._send_sync(call)
        usage = Usage()
        body_json: Any = None
        cost: float | None = None
        if result is not None and 200 <= result.status < 300:
            try:
                from boundary.providers._json import loads

                body_json = loads(result.body)
            except ValueError:
                body_json = None
            usage = adapter.parse_usage(body_json)
            model = body_json.get("model") if isinstance(body_json, dict) else None
            entry = self._price_for(pc, provider, str(model)) if model else None
            cost = cost_usd(usage, entry) if entry is not None else None
        self._record(
            call, result, error_type, retries, cached=False, parsed=None, usage=usage, cost=cost
        )
        status: int | str = result.status if result is not None else (error_type or "error")
        return RawResponse(
            status=status,
            headers=result.headers if result is not None else {},
            body=result.body if result is not None else b"",
            json=body_json,
            latency_ms=result.elapsed_ms if result is not None else None,
            usage=usage,
            cost_usd=cost,
            costed=cost is not None,
            ledger_id=row.id or 0,
            retries=retries,
        )

    def close(self) -> None:
        self.transport.close()
        self.ledger.close()
        self.telemetry.shutdown()

    async def aclose(self) -> None:
        await self.transport.aclose()
        self.ledger.close()
        self.telemetry.shutdown()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- steps --------------------------------------------------------------------------

    def _provider(self, name: str) -> ProviderConfig:
        pc = self.config.providers.get(name)
        if pc is None:
            raise ConfigError(
                f"unknown provider {name!r}; known: {', '.join(sorted(self.config.providers))}"
            )
        return pc

    @staticmethod
    def _adapter(pc: ProviderConfig) -> Adapter:
        adapter = ADAPTERS.get(pc.kind)
        if adapter is None:
            raise ConfigError(
                f"provider kind {pc.kind.value!r} is not available in boundary {__version__}"
            )
        return adapter

    @staticmethod
    def _api_key(name: str, pc: ProviderConfig) -> str | None:
        if pc.api_key_env is None:
            return None
        value = os.environ.get(pc.api_key_env, "").strip()
        if not value:
            raise ConfigError(
                f"provider {name!r} needs {pc.api_key_env} in the environment (or in .env); it is unset or empty"
            )
        return value

    @staticmethod
    def _auth_headers(pc: ProviderConfig, api_key: str | None) -> dict[str, str]:
        headers: dict[str, str] = {"content-type": "application/json", **pc.headers}
        if api_key is None:
            return headers
        if pc.kind is ProviderKind.ANTHROPIC:
            headers["x-api-key"] = api_key
            headers["anthropic-version"] = pc.api_version or "2023-06-01"
        elif pc.kind is ProviderKind.GOOGLE:
            headers["x-goog-api-key"] = api_key
        else:
            headers["authorization"] = f"Bearer {api_key}"
        return headers

    def _price_for(self, pc: ProviderConfig, provider: str, model: str) -> PriceEntry | None:
        if pc.price_zero:
            return ZERO_PRICE
        return self.prices.lookup(provider, model)

    def _prepare(
        self, request: ChatRequest, *, purpose: str, run_id: str | None, mode: Mode
    ) -> _Call:
        ref = resolve(request.model, self.config, mode)
        pc = ref.provider_config
        if mode is Mode.PASSTHROUGH:
            if request.max_tokens is None:
                raise PassthroughViolation(
                    "pass-through mode requires an explicit max_tokens; filling a default would rewrite the request"
                )
            if self.raw_store is None:
                raise PassthroughViolation(
                    "pass-through mode needs a raw store the caller owns (Gateway.from_config(..., raw_store=Path))"
                )
            effective = request
        else:
            effective = (
                request
                if request.max_tokens is not None
                else dataclasses.replace(request, max_tokens=self.config.defaults.max_tokens)
            )
        adapter = self._adapter(pc)
        built = adapter.build_request(ref, effective, pc, self._api_key(ref.provider, pc))

        entry = self._price_for(pc, ref.provider, ref.model)
        max_tokens = effective.max_tokens or 0
        estimate = estimate_usd(len(built.body), max_tokens, entry) if entry is not None else 0.0
        self._check_caps(run_id, estimate=estimate)

        row = LedgerRow(
            ts_utc=utc_now(),
            boundary_version=__version__,
            project=self.project,
            purpose=purpose,
            mode=mode.value,
            provider=ref.provider,
            model_requested=ref.explicit,
            run_id=run_id,
            alias=ref.alias,
            region=ref.region,
            price_list=self.prices.name,
            cost_usd=estimate if entry is not None else None,
            request_sha256=sha256_hex(built.body),
        )
        span = self.telemetry.start("boundary.chat")
        ids = Telemetry.ids(span)
        row.trace_id, row.span_id = ids.trace_id, ids.span_id
        self.ledger.begin(row)

        cached: HttpResult | None = None
        if mode is Mode.STANDARD and self.cache is not None:
            cached = self.cache.get(built)
        return _Call(
            request=effective,
            ref=ref,
            adapter=adapter,
            built=built,
            mode=mode,
            row=row,
            price_entry=entry,
            span=span,
            cached=cached,
        )

    def _check_caps(self, run_id: str | None, *, estimate: float) -> None:
        month = utc_now()[:7]
        cap = self.caps.for_project(self.project)
        spent = self.ledger.spend_usd(project=self.project, year_month=month)
        if spent + estimate > cap.monthly_usd:
            raise SpendCapExceeded(
                f"project {self.project} monthly", cap.monthly_usd, spent, estimate
            )
        if run_id is not None and cap.per_run_usd is not None:
            spent_run = self.ledger.spend_usd(project=self.project, run_id=run_id)
            if spent_run + estimate > cap.per_run_usd:
                raise SpendCapExceeded(
                    f"project {self.project} run {run_id}", cap.per_run_usd, spent_run, estimate
                )
        spent_all = self.ledger.spend_usd(project=None, year_month=month)
        if spent_all + estimate > self.caps.portfolio_monthly_usd:
            raise SpendCapExceeded(
                "portfolio monthly", self.caps.portfolio_monthly_usd, spent_all, estimate
            )

    def _backoff(self, attempt: int, headers: Mapping[str, str] | None) -> float:
        policy = self.config.retry
        if headers is not None:
            ra = headers.get("retry-after")
            if ra is not None:
                try:
                    return min(float(ra), policy.max_delay_s)
                except ValueError:
                    pass
        delay = min(policy.max_delay_s, policy.base_delay_s * (2.0 ** (attempt - 1)))
        return delay + random.uniform(0, policy.base_delay_s)

    def _send_sync(self, call: _Call) -> tuple[HttpResult | None, str | None, int]:
        if call.mode is Mode.PASSTHROUGH:
            try:
                return self.transport.send(call.built), None, 0
            except TRANSPORT_ERRORS as e:
                call.error_detail = str(e)
                return None, type(e).__name__, 0
        attempts = self.config.retry.max_attempts
        error_type: str | None = None
        result: HttpResult | None = None
        for attempt in range(1, attempts + 1):
            try:
                result = self.transport.send(call.built)
                error_type = None
            except TRANSPORT_ERRORS as e:
                result, error_type = None, type(e).__name__
                call.error_detail = str(e)
            if result is not None and result.status not in RETRY_STATUSES:
                return result, None, attempt - 1
            if attempt < attempts:
                self._sleep(self._backoff(attempt, result.headers if result else None))
        return result, error_type, attempts - 1

    async def _send_async(self, call: _Call) -> tuple[HttpResult | None, str | None, int]:
        if call.mode is Mode.PASSTHROUGH:
            try:
                return await self.transport.asend(call.built), None, 0
            except TRANSPORT_ERRORS as e:
                call.error_detail = str(e)
                return None, type(e).__name__, 0
        attempts = self.config.retry.max_attempts
        error_type: str | None = None
        result: HttpResult | None = None
        for attempt in range(1, attempts + 1):
            try:
                result = await self.transport.asend(call.built)
                error_type = None
            except TRANSPORT_ERRORS as e:
                result, error_type = None, type(e).__name__
                call.error_detail = str(e)
            if result is not None and result.status not in RETRY_STATUSES:
                return result, None, attempt - 1
            if attempt < attempts:
                await self._asleep(self._backoff(attempt, result.headers if result else None))
        return result, error_type, attempts - 1

    def _finish(
        self,
        call: _Call,
        result: HttpResult | None,
        error_type: str | None,
        retries: int,
        *,
        cached: bool,
    ) -> ChatResponse:
        parsed: ParsedResponse | None = None
        failure: ProviderError | None = None
        if result is None:
            failure = ProviderError(
                call.ref.provider, error_type or "error", call.error_detail, retries
            )
        elif 200 <= result.status < 300:
            try:
                parsed = call.adapter.parse_response(result.status, result.headers, result.body)
            except ProviderError as e:
                failure = e
                error_type = MALFORMED
        else:
            failure = call.adapter.parse_error(
                call.ref.provider, result.status, result.headers, result.body
            )
            failure.retries = retries
            error_type = f"http_{result.status}"

        usage = parsed.usage if parsed is not None else Usage()
        cost: float | None = None
        if parsed is not None:
            model_for_price = parsed.model_returned or call.ref.model
            entry = self._price_for(call.ref.provider_config, call.ref.provider, model_for_price)
            cost = cost_usd(usage, entry) if entry is not None else None

        if cached and call.mode is Mode.STANDARD:
            # A cache hit made no upstream call; it costs nothing this time.
            cost = 0.0
        if (
            not cached
            and self.cache is not None
            and call.mode is Mode.STANDARD
            and result is not None
        ):
            self.cache.put(call.built, result)

        self._record(
            call, result, error_type, retries, cached=cached, parsed=parsed, usage=usage, cost=cost
        )

        if parsed is not None and cost is None and self.strict_cost:
            raise UnknownPrice(
                call.ref.provider, parsed.model_returned or call.ref.model, self.prices.name
            )
        if failure is not None and call.mode is Mode.STANDARD:
            raise failure

        status: int | str = result.status if result is not None else (error_type or "error")
        return ChatResponse(
            text=parsed.text if parsed is not None else None,
            finish_reason=parsed.finish_reason if parsed is not None else None,
            usage=usage,
            cost_usd=cost,
            costed=cost is not None,
            model_requested=call.ref.explicit,
            model_returned=parsed.model_returned if parsed is not None else None,
            provider=call.ref.provider,
            latency_ms=result.elapsed_ms if result is not None else 0.0,
            status=status,
            headers=result.headers if result is not None else {},
            raw=parsed.raw if parsed is not None else None,
            ledger_id=call.row.id or 0,
            mode=call.mode,
            retries=retries,
            cached=cached,
            price_list=self.prices.name if cost is not None else None,
            trace_id=call.row.trace_id,
        )

    def _record(
        self,
        call: _Call,
        result: HttpResult | None,
        error_type: str | None,
        retries: int,
        *,
        cached: bool,
        parsed: ParsedResponse | None,
        usage: Usage,
        cost: float | None,
    ) -> None:
        row = call.row
        raw_path: str | None = None
        if call.mode is Mode.PASSTHROUGH and self.raw_store is not None and row.id is not None:
            raw_path = str(
                self.raw_store.write(
                    ledger_id=row.id,
                    ts_utc=row.ts_utc,
                    run_id=row.run_id,
                    provider=row.provider,
                    built=call.built,
                    result=result,
                    error_type=error_type,
                )
            )
        row.model_returned = parsed.model_returned if parsed is not None else None
        row.input_tokens = usage.input_tokens
        row.output_tokens = usage.output_tokens
        row.cache_read_tokens = usage.cache_read_tokens
        row.cache_write_tokens = usage.cache_write_tokens
        row.cost_usd = cost
        row.costed = cost is not None
        row.cached = cached
        row.latency_ms = result.elapsed_ms if result is not None else None
        row.http_status = result.status if result is not None else None
        row.error_type = error_type
        row.retries = retries
        row.response_sha256 = sha256_hex(result.body) if result is not None else None
        row.raw_path = raw_path
        self.ledger.complete(row)
        self._end_span(call, parsed, error_type)

    def _end_span(self, call: _Call, parsed: ParsedResponse | None, error_type: str | None) -> None:
        row = call.row
        Telemetry.set_attributes(
            call.span,
            {
                "gen_ai.operation.name": "chat",
                "gen_ai.system": call.ref.provider_config.kind.value,
                "gen_ai.request.model": call.ref.model,
                "gen_ai.response.model": row.model_returned,
                "gen_ai.usage.input_tokens": row.input_tokens,
                "gen_ai.usage.output_tokens": row.output_tokens,
                "gen_ai.response.finish_reasons": parsed.finish_reason if parsed else None,
                "http.response.status_code": row.http_status,
                "boundary.project": row.project,
                "boundary.purpose": row.purpose,
                "boundary.run_id": row.run_id,
                "boundary.mode": row.mode,
                "boundary.alias": row.alias,
                "boundary.provider": row.provider,
                "boundary.cost_usd": row.cost_usd,
                "boundary.costed": row.costed,
                "boundary.cached": row.cached,
                "boundary.retries": row.retries,
                "boundary.latency_ms": row.latency_ms,
                "boundary.ledger_id": row.id,
                "boundary.version": row.boundary_version,
            },
        )
        if error_type is not None:
            Telemetry.mark_error(call.span, error_type)
        call.span.end()
