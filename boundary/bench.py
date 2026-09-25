"""The numbers a stranger can check (PLAN.md section 1), measured against an in-process mock.

No network is involved. The mock upstream is an httpx MockTransport that answers instantly, so
the wall time of a call is the library's own overhead: resolving, building, cap checks, the
two ledger writes, telemetry and parsing.

Four measurements:
    overhead      p50 and p95 of gateway.chat() wall time, 95% bootstrap CI over the calls
    completeness  ledger rows written / calls attempted across 500s, timeouts, malformed
                  bodies, successes and a simulated process kill mid-call, in both modes
    caps          calls attempted past a spend cap and calls that reached the upstream past it
    fidelity      share of pass-through requests whose bytes on the wire equal the bytes the
                  adapter built, on a corpus of varied requests

`boundary bench` runs them and writes bench/results.json; `--write-readme` fills the README
row between the markers. The README is never edited by hand.
"""

from __future__ import annotations

import contextlib
import json
import random
import statistics
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx

from boundary import __version__
from boundary._mock import MODEL, MockUpstream, mock_gateway, ok_body, sample_request
from boundary.config import BoundaryConfig, CapsConfig, ProjectCap, load_config
from boundary.errors import ProviderError, SpendCapExceeded
from boundary.ledger.store import IN_FLIGHT
from boundary.providers.anthropic import AnthropicAdapter
from boundary.providers.base import BuiltRequest
from boundary.transport import HttpResult, Transport
from boundary.types import ChatRequest, Mode

README_START = "<!-- bench:start -->"
README_END = "<!-- bench:end -->"


class _KillSometimes(Transport):
    """Raises KeyboardInterrupt on chosen call indexes, as if the process died mid-call."""

    def __init__(self, inner: httpx.Client, kill_on: set[int], timeouts: Any) -> None:
        super().__init__(timeouts, sync_client=inner)
        self.kill_on = kill_on
        self.n = 0

    def send(self, built: BuiltRequest) -> HttpResult:
        i = self.n
        self.n += 1
        if i in self.kill_on:
            raise KeyboardInterrupt
        return super().send(built)


def _bootstrap_ci(
    values: list[float], stat: Callable[[list[float]], float], *, n: int, rng: random.Random
) -> tuple[float, float]:
    k = len(values)
    samples = sorted(stat([values[rng.randrange(k)] for _ in range(k)]) for _ in range(n))
    return samples[int(0.025 * n)], samples[int(0.975 * n) - 1]


def _p(values: list[float], q: float) -> float:
    s = sorted(values)
    return s[min(len(s) - 1, round(q * (len(s) - 1)))]


@dataclass
class OverheadResult:
    calls: int
    p50_ms: float
    p50_ci_ms: tuple[float, float]
    p95_ms: float
    p95_ci_ms: tuple[float, float]
    mean_ms: float


def measure_overhead(
    config: BoundaryConfig, work: Path, *, calls: int, seed: int
) -> OverheadResult:
    rng = random.Random(seed)
    upstream = MockUpstream()
    times: list[float] = []
    with mock_gateway(config, work / "overhead", upstream) as gw:
        for i in range(calls):
            req = sample_request(i, rng)
            t0 = time.perf_counter()
            gw.chat(req, purpose="bench", run_id="overhead", mode=Mode.STANDARD)
            times.append((time.perf_counter() - t0) * 1000.0)
    boot = random.Random(seed + 1)
    return OverheadResult(
        calls=calls,
        p50_ms=_p(times, 0.50),
        p50_ci_ms=_bootstrap_ci(times, lambda v: _p(v, 0.50), n=1000, rng=boot),
        p95_ms=_p(times, 0.95),
        p95_ci_ms=_bootstrap_ci(times, lambda v: _p(v, 0.95), n=1000, rng=boot),
        mean_ms=statistics.fmean(times),
    )


@dataclass
class CompletenessResult:
    attempted: int
    rows: int
    by_fault: dict[str, int]
    in_flight_rows: int

    @property
    def ratio(self) -> float:
        return self.rows / self.attempted if self.attempted else 0.0


FAULTS = ("ok", "http_500", "http_400", "timeout", "malformed", "kill")


def measure_completeness(
    config: BoundaryConfig, work: Path, *, per_fault: int, seed: int
) -> CompletenessResult:
    rng = random.Random(seed)
    plan: list[str] = [f for f in FAULTS for _ in range(per_fault)]
    rng.shuffle(plan)

    def script(i: int) -> httpx.Response | Exception:
        fault = plan_for_upstream[i]
        if fault == "http_500":
            return httpx.Response(
                500, json={"type": "error", "error": {"type": "api_error", "message": "x"}}
            )
        if fault == "http_400":
            return httpx.Response(
                400,
                json={"type": "error", "error": {"type": "invalid_request_error", "message": "x"}},
            )
        if fault == "timeout":
            return httpx.ReadTimeout("slow")
        if fault == "malformed":
            return httpx.Response(200, content=b"<html>")
        return httpx.Response(200, json=ok_body())

    by_fault: dict[str, int] = dict.fromkeys(FAULTS, 0)
    attempted = 0
    total_rows = 0
    in_flight = 0
    for mode in (Mode.STANDARD, Mode.PASSTHROUGH):
        # Standard mode retries 500s and timeouts, so the upstream sees several requests per
        # call; the script indexes by upstream request, so it is rebuilt per gateway call.
        upstream = MockUpstream(script)
        kill_indexes: set[int] = set()
        transport = _KillSometimes(upstream.client(), kill_indexes, config.defaults.timeouts)
        with mock_gateway(
            config, work / f"completeness-{mode.value}", upstream, transport=transport
        ) as gw:
            for i, fault in enumerate(plan):
                plan_for_upstream = [fault] * (config.retry.max_attempts + 1)
                upstream.calls = 0
                if fault == "kill":
                    transport.kill_on = {transport.n}
                attempted += 1
                with contextlib.suppress(ProviderError, KeyboardInterrupt):
                    gw.chat(
                        sample_request(i, rng), purpose="bench", run_id=f"c-{mode.value}", mode=mode
                    )
                by_fault[fault] += 1
            rows = gw.ledger.rows()
            total_rows += len(rows)
            in_flight += sum(1 for r in rows if r["error_type"] == IN_FLIGHT)
    return CompletenessResult(
        attempted=attempted, rows=total_rows, by_fault=by_fault, in_flight_rows=in_flight
    )


@dataclass
class CapsResult:
    attempted_past_cap: int
    reached_upstream_past_cap: int
    calls_before_cap: int


def measure_caps(config: BoundaryConfig, work: Path, *, attempts: int, seed: int) -> CapsResult:
    rng = random.Random(seed)
    upstream = MockUpstream()
    entry_in, entry_out = 1.0, 5.0  # haiku list price in the checked-in file
    # A cap that admits roughly three calls: three times the actual cost of a 40/3 call plus
    # a little, so the fourth pessimistic estimate trips it.
    actual = 40 / 1e6 * entry_in + 3 / 1e6 * entry_out
    caps = CapsConfig(
        version=1,
        portfolio_monthly_usd=1000.0,
        projects={"bench": ProjectCap(monthly_usd=actual * 3.5, per_run_usd=None)},
    )
    before = 0
    past = 0
    reached_after = 0
    tripped = False
    with mock_gateway(config, work / "caps", upstream, caps=caps) as gw:
        for i in range(attempts):
            seen = upstream.calls
            try:
                gw.chat(
                    ChatRequest(
                        model=MODEL,
                        messages=[{"role": "user", "content": f"{i} {rng.random()}"}],
                        max_tokens=8,
                    ),
                    purpose="bench",
                    run_id="caps",
                )
                if tripped:
                    reached_after += upstream.calls - seen
                else:
                    before += 1
            except SpendCapExceeded:
                tripped = True
                past += 1
                reached_after += upstream.calls - seen
    return CapsResult(
        attempted_past_cap=past, reached_upstream_past_cap=reached_after, calls_before_cap=before
    )


@dataclass
class FidelityResult:
    requests: int
    identical: int

    @property
    def share(self) -> float:
        return self.identical / self.requests if self.requests else 0.0


def measure_fidelity(
    config: BoundaryConfig, work: Path, *, requests: int, seed: int
) -> FidelityResult:
    rng = random.Random(seed)
    upstream = MockUpstream()
    identical = 0
    adapter = AnthropicAdapter()
    with mock_gateway(config, work / "fidelity", upstream) as gw:
        ref = gw.resolve(MODEL, Mode.PASSTHROUGH)
        for i in range(requests):
            req = sample_request(i, rng)
            expected = adapter.build_request(ref, req, ref.provider_config, "bench-key").body
            gw.chat(req, purpose="bench", run_id="fidelity", mode=Mode.PASSTHROUGH)
            if gw.transport.last_sent_body == expected:
                identical += 1
    return FidelityResult(requests=requests, identical=identical)


@dataclass
class BenchResults:
    boundary_version: str
    ran_utc: str
    overhead: OverheadResult
    completeness: CompletenessResult
    caps: CapsResult
    fidelity: FidelityResult

    def readme_row(self) -> str:
        o = self.overhead
        c = self.completeness
        return (
            f"| {o.p50_ms:.2f} ms ({o.p50_ci_ms[0]:.2f} to {o.p50_ci_ms[1]:.2f}) / "
            f"{o.p95_ms:.2f} ms ({o.p95_ci_ms[0]:.2f} to {o.p95_ci_ms[1]:.2f}), n = {o.calls:,} "
            f"| {c.rows}/{c.attempted} = {c.ratio:.1%} across ok, 500, 400, timeout, malformed and a kill mid-call, "
            f"both modes ({c.in_flight_rows} rows left in flight by the kills, as designed) "
            f"| _from October_ "
            f"| {self.caps.attempted_past_cap} attempted, {self.caps.reached_upstream_past_cap} reached the upstream "
            f"| {self.fidelity.identical}/{self.fidelity.requests} = {self.fidelity.share:.0%} |"
        )


def run(
    config_path: Path,
    work: Path,
    *,
    calls: int = 1000,
    per_fault: int = 50,
    cap_attempts: int = 20,
    fidelity_requests: int = 500,
    seed: int = 20260907,
) -> BenchResults:
    import datetime as dt
    import os

    # Bench keys are dummies; the mock never checks them. Never read the real .env here.
    os.environ.setdefault("ANTHROPIC_API_KEY", "bench-key")
    config = load_config(config_path)
    work.mkdir(parents=True, exist_ok=True)
    return BenchResults(
        boundary_version=__version__,
        ran_utc=dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        overhead=measure_overhead(config, work, calls=calls, seed=seed),
        completeness=measure_completeness(config, work, per_fault=per_fault, seed=seed),
        caps=measure_caps(config, work, attempts=cap_attempts, seed=seed),
        fidelity=measure_fidelity(config, work, requests=fidelity_requests, seed=seed),
    )


def spend_query_scaling(
    work: Path, sizes: Sequence[int] = (10_000, 100_000, 1_000_000), *, seed: int = 1
) -> list[tuple[int, float, float, bool]]:
    """What the spend-cap check costs per call as a ledger grows, before and after v9 (0.19).

    Before: the two sums the caps made on every call, the project's month and the whole
    month for the ceiling, filtered on substr(ts_utc, 1, 7), which no index serves. After:
    `spend_usd`, reading `ledger_spend`. Rows `(size, before_ms, after_ms, totals_agree)`.
    """
    from boundary.ledger.store import LedgerStore, new_call_uid

    out: list[tuple[int, float, float, bool]] = []
    for n in sizes:
        path = work / f"spend-{n}.sqlite"
        store = LedgerStore(path)
        rng = random.Random(seed)
        conn = store._conn
        conn.execute("BEGIN")
        conn.executemany(
            "INSERT INTO ledger (ts_utc, boundary_version, project, purpose, mode, provider, "
            "model_requested, cost_usd, costed, call_uid) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
            (
                (
                    f"2026-09-{rng.randint(10, 28)}T00:00:00.000Z", "x",
                    rng.choice(["alpha", "beta", "gamma"]), "t", "standard", "p", "p/m",
                    rng.random() / 1000, new_call_uid(),
                )
                for _ in range(n)
            ),
        )  # fmt: skip
        conn.execute("COMMIT")

        before, after = _spend_timings(store, conn)
        total = conn.execute("SELECT SUM(cost_usd) FROM ledger").fetchone()[0]
        agree = abs(store.spend_usd(project=None, year_month="2026-09") - total) < 1e-6
        out.append((n, before, after, agree))
        store.close()
        path.unlink()
    return out


def _timed(fn: Callable[[], object], k: int) -> float:
    t0 = time.perf_counter()
    for _ in range(k):
        fn()
    return (time.perf_counter() - t0) / k * 1000.0


def _spend_timings(store: Any, conn: Any) -> tuple[float, float]:
    """Milliseconds per call for the caps' two sums, the old way and through `spend_usd`."""
    old = "SELECT COALESCE(SUM(cost_usd), 0) FROM ledger WHERE cost_usd IS NOT NULL"
    before = _timed(
        lambda: conn.execute(
            old + " AND project = ? AND substr(ts_utc, 1, 7) = ?", ("alpha", "2026-09")
        ).fetchone(),
        10,
    ) + _timed(
        lambda: conn.execute(old + " AND substr(ts_utc, 1, 7) = ?", ("2026-09",)).fetchone(), 10
    )
    after = _timed(lambda: store.spend_usd(project="alpha", year_month="2026-09"), 200) + _timed(
        lambda: store.spend_usd(project=None, year_month="2026-09"), 200
    )
    return before, after


def write_readme(readme: Path, row: str) -> None:
    text = readme.read_text(encoding="utf-8")
    start = text.index(README_START)
    end = text.index(README_END)
    if start < 0 or end < start:
        raise ValueError("README markers missing")
    text = text[: start + len(README_START)] + "\n" + row + "\n" + text[end:]
    readme.write_text(text, encoding="utf-8")


def to_json(results: BenchResults) -> str:
    return json.dumps(asdict(results), indent=2)
