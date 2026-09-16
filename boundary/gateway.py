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
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self, cast

from opentelemetry.trace import Span

from boundary import __version__
from boundary.cache import ExactMatchCache
from boundary.config import (
    BoundaryConfig,
    CapsConfig,
    CredentialSource,
    PriceEntry,
    PriceList,
    ProviderConfig,
    ProviderKind,
    latest_price_list,
    load_caps,
    load_config,
)
from boundary.credentials import GoogleADCToken
from boundary.env import find_dotenv, load_dotenv
from boundary.errors import (
    BatchNotReady,
    BoundaryError,
    ConfigError,
    PassthroughViolation,
    ProviderError,
    SpendCapExceeded,
    UnknownPrice,
)
from boundary.ledger.prices import cost_usd, estimate_usd
from boundary.ledger.store import LedgerRow, LedgerStore, new_call_uid, utc_now
from boundary.providers import ADAPTERS, BATCH_ADAPTERS, UploadingBatchAdapter
from boundary.providers.base import (
    Adapter,
    BatchAdapter,
    BatchItem,
    BatchItemResult,
    BatchProgress,
    BuiltRequest,
    ParsedResponse,
)
from boundary.rawstore import RawStore, sha256_hex
from boundary.routes import ModelRef, resolve, split_explicit
from boundary.telemetry import Telemetry
from boundary.transport import TRANSPORT_ERRORS, HttpResult, Transport
from boundary.types import BatchHandle, ChatRequest, ChatResponse, Mode, Usage

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


def _residency(pc: ProviderConfig) -> str | None:
    """The residency a provider entry declares, as a ledger value.

    None when the entry declares nothing, which is honestly different from "global": one
    says the operator made no claim, the other says the operator claimed the weakest one.
    """
    return pc.residency.value if pc.residency is not None else None


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
        env: str | None = None,
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
        # A runner labels itself with BOUNDARY_ENV rather than by editing a checked-in
        # configuration file; an explicit argument beats both.
        self.env = env or os.environ.get("BOUNDARY_ENV", "").strip() or config.ledger.env
        self._sleep = sleep
        self._asleep = asleep
        # One token source per provider entry that mints rather than reads its credential.
        # Held for the gateway's lifetime so the token is refreshed, not re-minted per call.
        self._token_sources: dict[str, GoogleADCToken] = {}

    @classmethod
    def from_config(
        cls,
        path: str | Path,
        *,
        project: str,
        ledger_path: Path | None = None,
        raw_store: Path | None = None,
        strict_cost: bool = False,
        env: str | None = None,
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
            env=env,
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
            residency=_residency(pc),
            price_list=self.prices.name,
            request_sha256=sha256_hex(built.body),
            env=self.env,
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

    # -- batches ------------------------------------------------------------------------

    def batch_submit(
        self,
        requests: Sequence[ChatRequest],
        *,
        purpose: str,
        run_id: str | None = None,
    ) -> BatchHandle:
        """Submit many requests as one vendor batch, billed at the batch rate.

        Standard mode by definition: a batch fills in defaults and resolves aliases, both of
        which pass-through forbids, and nothing measured should be batched. The development
        cache is not consulted, because a cache hit inside a batch would make the rows say a
        request was billed when it was not.

        One ledger row per request is written before the submit leaves the process, in
        flight and carrying the batch-rate estimate, so that a process which dies between
        the submit and its answer still leaves a record of work the vendor may have
        accepted. The rows are named with the batch id once the vendor returns one.

        The caps are checked once, for the whole batch: a vendor bills for every request the
        moment it accepts the batch, so there is no such thing as being refused half way in.
        """
        if len(requests) == 0:
            raise ValueError("a batch needs at least one request")
        refs = [resolve(r.model, self.config, Mode.STANDARD) for r in requests]
        providers = sorted({ref.provider for ref in refs})
        if len(providers) != 1:
            raise ConfigError(
                "one batch goes to one provider, because a batch is one request to one "
                f"endpoint; got {', '.join(providers)}"
            )
        provider, pc = refs[0].provider, refs[0].provider_config
        batch_adapter = self._batch_adapter(provider, pc)
        adapter = self._adapter(pc)
        api_key = self._api_key(provider, pc)

        items: list[BatchItem] = []
        rows: list[LedgerRow] = []
        total_estimate = 0.0
        for ref, request in zip(refs, requests, strict=True):
            effective = (
                request
                if request.max_tokens is not None
                else dataclasses.replace(request, max_tokens=self.config.defaults.max_tokens)
            )
            # Built as a single request too, so the row's request hash is the same whether a
            # prompt was sent alone or in a batch, and so the estimate is measured against
            # the same bytes.
            single = adapter.build_request(ref, effective, pc, api_key)
            entry = self._price_for(pc, ref.provider, ref.model)
            estimate = 0.0
            if entry is not None:
                estimate = estimate_usd(len(single.body), effective.max_tokens or 0, entry)
                # At the batch rate where the price list publishes one. Where it does not,
                # the estimate stays at the full rate, which is the pessimistic way round
                # and the only one that never under-reserves against a cap.
                if entry.batch_multiplier is not None:
                    estimate *= entry.batch_multiplier
            total_estimate += estimate
            custom_id = new_call_uid()
            items.append((custom_id, ref, effective))
            rows.append(
                LedgerRow(
                    ts_utc=utc_now(),
                    boundary_version=__version__,
                    project=self.project,
                    purpose=purpose,
                    mode=Mode.STANDARD.value,
                    provider=ref.provider,
                    model_requested=ref.explicit,
                    run_id=run_id,
                    alias=ref.alias,
                    region=ref.region,
                    residency=_residency(pc),
                    price_list=self.prices.name,
                    cost_usd=estimate if entry is not None else None,
                    request_sha256=sha256_hex(single.body),
                    call_uid=custom_id,
                    env=self.env,
                )
            )

        self._check_caps(run_id, estimate=total_estimate)
        span = self.telemetry.start("boundary.batch_submit")
        for row in rows:
            self.ledger.begin(row)

        if batch_adapter.uploads_input_file:
            # OpenAI-shaped hosts name a file rather than carrying the requests inline, so
            # the prompts leave the process during this upload and not during the create
            # that follows. The rows above are already in flight, which is what "no call
            # escapes the ledger" means when submitting takes two round trips.
            #
            # Neither round trip is billed, so a failure in either completes every row as a
            # failure with no cost. That is the honest record: nothing is chargeable until
            # the vendor has accepted the batch.
            uploading = cast("UploadingBatchAdapter", batch_adapter)
            upload_built = uploading.build_batch_upload(items, pc, api_key)
            try:
                upload_id = self._upload_batch_input(provider, uploading, upload_built)
            except BoundaryError as e:
                self._fail_batch_rows(rows, getattr(e, "status", None), "batch_upload")
                self._end_batch_span(span, "submit", provider, len(rows), None, "batch_upload")
                raise
            built = uploading.build_batch_create(upload_id, pc, api_key)
        else:
            built = batch_adapter.build_batch_submit(items, pc, api_key)

        result, error_type, retries, detail = self._send_retrying(built)

        if result is None or not (200 <= result.status < 300):
            if result is None:
                failure = ProviderError(provider, error_type or "error", detail, retries)
            else:
                failure = adapter.parse_error(provider, result.status, result.headers, result.body)
                failure.retries = retries
                error_type = f"http_{result.status}"
            for row in rows:
                row.cost_usd = None
                row.costed = False
                row.error_type = error_type
                row.http_status = result.status if result is not None else None
                row.latency_ms = result.elapsed_ms if result is not None else None
                row.response_sha256 = sha256_hex(result.body) if result is not None else None
                row.retries = retries
                self.ledger.complete(row)
            self._end_batch_span(span, "submit", provider, len(rows), None, error_type)
            raise failure

        # A 2xx whose body cannot be read is the one case where the rows are left in flight:
        # the vendor has accepted the batch and will bill for it, so recording the requests
        # as failed would understate the month. They stay in flight at their estimate, which
        # is what counts against the caps, and `ledger report` shows them.
        submitted = batch_adapter.parse_batch_submit(result.status, result.headers, result.body)
        ids = [row.id for row in rows if row.id is not None]
        self.ledger.set_batch_id(ids, submitted.batch_id)
        self._end_batch_span(span, "submit", provider, len(rows), submitted.batch_id, None)
        return BatchHandle(
            provider=provider,
            batch_id=submitted.batch_id,
            project=self.project,
            purpose=purpose,
            run_id=run_id,
            custom_ids=tuple(custom_id for custom_id, _, _ in items),
            ledger_ids=tuple(ids),
        )

    def batch_handle(self, batch_id: str) -> BatchHandle:
        """Rebuild a handle from the ledger, for a batch this process did not submit.

        This is what makes "submit now, collect hours later" work across processes: the
        batch id is the only thing a caller has to keep, and everything else is already a
        column.
        """
        rows = self.ledger.rows_for_batch(batch_id)
        if not rows:
            raise ValueError(
                f"no rows for batch {batch_id!r} in {self.ledger.path}; a batch is collected "
                "from the ledger that submitted it"
            )
        first = rows[0]
        return BatchHandle(
            provider=str(first["provider"]),
            batch_id=batch_id,
            project=str(first["project"]),
            purpose=str(first["purpose"]),
            run_id=first["run_id"],
            custom_ids=tuple(str(r["call_uid"]) for r in rows),
            ledger_ids=tuple(int(r["id"]) for r in rows),
        )

    def batch_status(self, handle: BatchHandle) -> BatchProgress:
        """Where the vendor has got to with a batch. Makes no ledger change."""
        pc = self._provider(handle.provider)
        batch_adapter = self._batch_adapter(handle.provider, pc)
        built = batch_adapter.build_batch_status(
            handle.batch_id, pc, self._api_key(handle.provider, pc)
        )
        result = self._fetch(handle.provider, pc, built)
        return batch_adapter.parse_batch_status(result.status, result.headers, result.body)

    def batch_results(
        self,
        handle: BatchHandle,
        *,
        wait_s: float = 0.0,
        poll_s: float = 30.0,
    ) -> list[ChatResponse]:
        """Collect a finished batch, completing one ledger row per request at the batch rate.

        Does not wait by default: a batch runs for as long as the vendor takes, and a
        library call that blocked for hours would be the wrong shape. `BatchNotReady` is the
        ordinary answer to "is it done", not a failure. Pass `wait_s` to poll for a while.

        Responses come back in submission order. A request the results file does not mention
        is completed as `batch_missing` rather than left in flight for ever, because a batch
        that has ended will not mention it later either.
        """
        pc = self._provider(handle.provider)
        batch_adapter = self._batch_adapter(handle.provider, pc)

        progress = self.batch_status(handle)
        deadline = time.monotonic() + max(wait_s, 0.0)
        while not progress.ended and time.monotonic() < deadline:
            self._sleep(max(0.0, min(poll_s, deadline - time.monotonic())))
            progress = self.batch_status(handle)
        if not progress.ended:
            raise BatchNotReady(handle.batch_id, progress.processing_status, progress.counts)
        if progress.results_url is None:
            raise ProviderError(
                handle.provider,
                "results_url",
                f"batch {handle.batch_id} ended as {progress.processing_status!r} without a "
                "results URL, so its rows cannot be completed from the vendor",
            )

        built = batch_adapter.build_batch_results(
            progress.results_url, pc, self._api_key(handle.provider, pc)
        )
        result = self._fetch(handle.provider, pc, built)
        by_custom_id = {
            item.custom_id: item
            for item in batch_adapter.parse_batch_results(
                result.status, result.headers, result.body
            )
        }
        records = {str(r["call_uid"]): r for r in self.ledger.rows_for_batch(handle.batch_id)}

        responses: list[ChatResponse] = []
        unpriced: tuple[str, str] | None = None
        for custom_id in handle.custom_ids:
            record = records.get(custom_id)
            if record is None:
                raise ValueError(
                    f"batch {handle.batch_id} names call {custom_id} but {self.ledger.path} "
                    "has no such row; the handle and the ledger are not the same batch"
                )
            response = self._complete_batch_row(handle, pc, record, by_custom_id.get(custom_id))
            if response.raw is not None and not response.costed and unpriced is None:
                unpriced = (handle.provider, response.model_returned or response.model_requested)
            responses.append(response)

        # Raised only after every row has been completed: strict costing is a report about
        # the price list, and it must not leave half a batch in flight.
        if unpriced is not None and self.strict_cost:
            raise UnknownPrice(unpriced[0], unpriced[1], self.prices.name)
        return responses

    def _batch_adapter(self, provider: str, pc: ProviderConfig) -> BatchAdapter:
        batch_adapter = BATCH_ADAPTERS.get(pc.kind)
        if batch_adapter is None:
            raise ConfigError(
                f"provider {provider!r} (kind {pc.kind.value!r}) has no batch support in "
                f"boundary {__version__}; send the requests one at a time with chat()"
            )
        if not pc.serves_batches:
            # The kind has an adapter; this host has not said it has the endpoint. Every
            # OpenAI-compatible server answers /v1/chat/completions and most have no
            # /v1/batches, so assuming one would turn a missing feature into an HTML 404
            # parsed as a batch. Callers that fall back on ConfigError, which is what
            # project 02 does, get the same clean signal as an unsupported kind.
            raise ConfigError(
                f"provider {provider!r} does not serve batches: its kind "
                f"({pc.kind.value!r}) has an adapter, but the provider entry has not set "
                f"`batches: true`. Set it if this host has a batch endpoint, and leave it "
                f"unset for a local server that does not"
            )
        return batch_adapter

    def _upload_batch_input(
        self,
        provider: str,
        adapter: UploadingBatchAdapter,
        built: BuiltRequest,
    ) -> str:
        """Send the input file and return the id the create call will name."""
        result, error_type, retries, detail = self._send_retrying(built)
        if result is None:
            raise ProviderError(provider, error_type or "error", detail, retries)
        return adapter.parse_batch_upload(result.status, result.headers, result.body)

    def _fail_batch_rows(
        self, rows: Sequence[LedgerRow], status: int | None, error_type: str
    ) -> None:
        """Complete every row of a batch that never reached the vendor, at no cost."""
        for row in rows:
            row.cost_usd = None
            row.costed = False
            row.error_type = error_type
            row.http_status = status
            self.ledger.complete(row)

    def _fetch(self, provider: str, pc: ProviderConfig, built: BuiltRequest) -> HttpResult:
        """Send a batch control request and insist on a 2xx. These calls carry no tokens and
        write no ledger row of their own: the rows are the batch's, and they already exist."""
        result, error_type, retries, detail = self._send_retrying(built)
        if result is None:
            raise ProviderError(provider, error_type or "error", detail, retries)
        if not (200 <= result.status < 300):
            failure = self._adapter(pc).parse_error(
                provider, result.status, result.headers, result.body
            )
            failure.retries = retries
            raise failure
        return result

    def _complete_batch_row(
        self,
        handle: BatchHandle,
        pc: ProviderConfig,
        record: Mapping[str, Any],
        item: BatchItemResult | None,
    ) -> ChatResponse:
        from boundary.providers._json import dumps

        row = LedgerRow.from_columns(dict(record))
        span = self.telemetry.start("boundary.batch_result")
        span_ids = Telemetry.ids(span)
        row.trace_id, row.span_id = span_ids.trace_id, span_ids.span_id

        parsed = item.parsed if item is not None else None
        if item is None:
            outcome, error_type = "missing", "batch_missing"
        elif item.succeeded and parsed is not None:
            outcome, error_type = "succeeded", None
        else:
            outcome, error_type = item.outcome, f"batch_{item.outcome}"

        usage = parsed.usage if parsed is not None else Usage()
        cost: float | None = None
        if parsed is not None:
            entry = None
            if parsed.model_returned:
                entry = self._price_for(pc, handle.provider, parsed.model_returned)
            if entry is None:
                requested = split_explicit(row.model_requested)
                entry = (
                    self._price_for(pc, handle.provider, requested[1])
                    if requested is not None
                    else None
                )
            cost = cost_usd(usage, entry, batch=True) if entry is not None else None

        row.model_returned = parsed.model_returned if parsed is not None else None
        row.input_tokens = usage.input_tokens
        row.output_tokens = usage.output_tokens
        row.cache_read_tokens = usage.cache_read_tokens
        row.cache_write_tokens = usage.cache_write_tokens
        row.cost_usd = cost
        row.costed = cost is not None
        row.cached = False
        # A batched request has no wall time of its own: it waited in the vendor's queue for
        # as long as the batch did. A number here would be an invention.
        row.latency_ms = None
        # A request the vendor answered inside a batch is recorded as a 200 even though it
        # had no response of its own, so that an uncosted success still shows up in
        # `uncosted_count`, which is the figure that must stay at zero.
        row.http_status = 200 if error_type is None else None
        row.error_type = error_type
        row.retries = 0
        row.response_sha256 = (
            sha256_hex(dumps(parsed.raw)) if parsed is not None and parsed.raw is not None else None
        )
        self.ledger.complete(row)
        self._end_batch_span(
            span, "result", handle.provider, 1, handle.batch_id, error_type, row=row
        )
        return ChatResponse(
            text=parsed.text if parsed is not None else None,
            finish_reason=parsed.finish_reason if parsed is not None else None,
            usage=usage,
            cost_usd=cost,
            costed=cost is not None,
            model_requested=row.model_requested,
            model_returned=row.model_returned,
            provider=handle.provider,
            latency_ms=0.0,
            status=200 if error_type is None else outcome,
            headers={},
            raw=parsed.raw if parsed is not None else None,
            ledger_id=row.id or 0,
            mode=Mode.STANDARD,
            retries=0,
            cached=False,
            price_list=self.prices.name if cost is not None else None,
            trace_id=row.trace_id,
        )

    def _end_batch_span(
        self,
        span: Span,
        operation: str,
        provider: str,
        requests: int,
        batch_id: str | None,
        error_type: str | None,
        row: LedgerRow | None = None,
    ) -> None:
        attributes: dict[str, Any] = {
            "gen_ai.operation.name": f"batch.{operation}",
            "boundary.env": self.env,
            "boundary.project": self.project,
            "boundary.provider": provider,
            "boundary.batch_id": batch_id,
            "boundary.batch_requests": requests,
            "boundary.version": __version__,
        }
        if row is not None:
            attributes.update(
                {
                    "gen_ai.request.model": row.model_requested,
                    "gen_ai.response.model": row.model_returned,
                    "gen_ai.usage.input_tokens": row.input_tokens,
                    "gen_ai.usage.output_tokens": row.output_tokens,
                    "boundary.purpose": row.purpose,
                    "boundary.run_id": row.run_id,
                    "boundary.mode": row.mode,
                    "boundary.cost_usd": row.cost_usd,
                    "boundary.costed": row.costed,
                    "boundary.ledger_id": row.id,
                }
            )
        Telemetry.set_attributes(span, attributes)
        if error_type is not None:
            Telemetry.mark_error(span, error_type)
        span.end()

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

    def _api_key(self, name: str, pc: ProviderConfig) -> str | None:
        """The credential for one provider entry, however that entry gets one.

        For `credentials: google_adc` a pasted variable still wins when it is set. That is
        how one checked-in configuration serves both a laptop, which has
        `gcloud auth application-default login` and no variable, and CI, which has a token
        from an earlier step and no gcloud. The precedence is documented on the field rather
        than discovered, and the token is minted only when nothing was pasted.
        """
        if pc.credentials is CredentialSource.GOOGLE_ADC:
            pasted = os.environ.get(pc.api_key_env, "").strip() if pc.api_key_env else ""
            return pasted or self._google_token(name, pc)
        if pc.api_key_env is None:
            return None
        value = os.environ.get(pc.api_key_env, "").strip()
        if not value:
            raise ConfigError(
                f"provider {name!r} needs {pc.api_key_env} in the environment (or in .env); it is unset or empty"
            )
        return value

    def _google_token(self, name: str, pc: ProviderConfig) -> str:
        """A cloud-platform access token, minted once per provider entry and kept fresh.

        The source is cached on the gateway rather than made per call, which is the whole
        point: a run of ten thousand calls mints a handful of tokens instead of ten thousand.
        """
        source = self._token_sources.get(name)
        if source is None:
            source = GoogleADCToken(client=self.transport.sync_client)
            self._token_sources[name] = source
        return source.token()

    @staticmethod
    def _auth_headers(pc: ProviderConfig, api_key: str | None) -> dict[str, str]:
        headers: dict[str, str] = {"content-type": "application/json", **pc.headers}
        if api_key is None:
            return headers
        if pc.kind is ProviderKind.ANTHROPIC or pc.kind is ProviderKind.AWS_BEDROCK:
            # Bedrock's Anthropic-native route takes a Bedrock API key in the same header
            # the direct vendor uses, and wants the same version pinned, because it is the
            # same API. No SigV4 and no botocore; see boundary/providers/bedrock.py.
            headers["x-api-key"] = api_key
            headers["anthropic-version"] = pc.api_version or "2023-06-01"
        elif pc.kind is ProviderKind.AZURE_FOUNDRY:
            # Foundry takes an Azure-issued key under `api-key`, and still wants the
            # Anthropic version pinned, because the body is the Messages body.
            headers["api-key"] = api_key
            headers["anthropic-version"] = pc.api_version or "2023-06-01"
        elif pc.kind is ProviderKind.GOOGLE:
            headers["x-goog-api-key"] = api_key
        else:
            # Vertex included: a Google OAuth bearer token.
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
            residency=_residency(ref.provider_config),
            price_list=self.prices.name,
            cost_usd=estimate if entry is not None else None,
            request_sha256=sha256_hex(built.body),
            env=self.env,
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

    def _send_retrying(self, built: BuiltRequest) -> tuple[HttpResult | None, str | None, int, str]:
        """The standard-mode retry loop for one built request: result, error type, retries
        made, and the transport's own message when no response arrived.

        Shared by chat and by the batch endpoints, which are standard mode by definition.
        Pass-through never comes here: it makes exactly one attempt, and that is enforced by
        the call sites rather than by a flag, so no future edit to this loop can give a
        measurement a retry.
        """
        attempts = self.config.retry.max_attempts
        error_type: str | None = None
        result: HttpResult | None = None
        detail = ""
        for attempt in range(1, attempts + 1):
            try:
                result = self.transport.send(built)
                error_type = None
            except TRANSPORT_ERRORS as e:
                result, error_type = None, type(e).__name__
                detail = str(e)
            if result is not None and result.status not in RETRY_STATUSES:
                return result, None, attempt - 1, detail
            if attempt < attempts:
                self._sleep(self._backoff(attempt, result.headers if result else None))
        return result, error_type, attempts - 1, detail

    def _send_sync(self, call: _Call) -> tuple[HttpResult | None, str | None, int]:
        if call.mode is Mode.PASSTHROUGH:
            try:
                return self.transport.send(call.built), None, 0
            except TRANSPORT_ERRORS as e:
                call.error_detail = str(e)
                return None, type(e).__name__, 0
        result, error_type, retries, detail = self._send_retrying(call.built)
        if detail:
            call.error_detail = detail
        return result, error_type, retries

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
            # Price by the identifier the vendor returned; if the price list does not know it,
            # by the identifier that was requested. OpenAI answers a request for `gpt-5-nano`
            # with `gpt-5-nano-2025-08-07` and prices both the same; the ledger row keeps both
            # identifiers, so which one priced the call is always auditable. Never guessed
            # beyond that: a returned id and a requested id both unknown is an uncosted row.
            entry = None
            if parsed.model_returned:
                entry = self._price_for(
                    call.ref.provider_config, call.ref.provider, parsed.model_returned
                )
            if entry is None:
                entry = self._price_for(call.ref.provider_config, call.ref.provider, call.ref.model)
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
                "boundary.env": row.env,
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
