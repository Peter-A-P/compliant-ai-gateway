"""The load-test harness itself (0.19). The run is a command; these check the arithmetic and
the two properties the method rests on, without starting a server."""

from __future__ import annotations

import asyncio
import random
from pathlib import Path

import httpx
import pytest

from boundary.loadtest import Cell, LoadResults, _ci, _drive, _p


def test_percentiles_and_the_bootstrap_over_runs() -> None:
    assert _p([1, 2, 3, 4, 100], 0.5) == 3
    assert _p([1, 2, 3, 4, 100], 0.99) == 100
    lo, hi = _ci([1.0, 1.1, 0.9, 1.0, 1.05], random.Random(1))
    assert lo < 1.01 < hi


async def test_latency_counts_from_the_scheduled_time_not_the_send() -> None:
    """Coordinated omission: a server that stalls must show as latency, including for the
    requests that were waiting behind the stall."""
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            await asyncio.sleep(0.3)
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://x"
    ) as client:
        lat, errors = await _drive(client, "http://x/", rps=50, duration_s=0.4, headers={}, body={})
    assert errors == 0 and len(lat) == 20
    assert max(lat) >= 300, "the stalled request's wait is in its latency"


async def test_errors_are_counted_and_not_timed() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://x"
    ) as client:
        lat, errors = await _drive(client, "http://x/", rps=50, duration_s=0.2, headers={}, body={})
    assert lat == [] and errors == 10


def test_a_run_off_the_vps_says_it_is_not_the_budget() -> None:
    cell = Cell(
        "routing", 50, overhead_ms={"p50": [1.0, 1.2], "p95": [2.0, 2.1], "p99": [3.0, 3.3]}
    )
    dev = LoadResults("x", "t", "a laptop", False, 2, 10.0, 50.0, [cell])
    assert "NOT THE OVERHEAD BUDGET" in dev.table()
    pub = LoadResults("x", "t", "the VPS", True, 2, 10.0, 50.0, [cell])
    assert "NOT THE OVERHEAD BUDGET" not in pub.table()


def test_a_cell_whose_client_fell_behind_is_not_reported_as_a_measurement() -> None:
    cell = Cell("routing", 500, overhead_ms={}, send_lag_p99_ms=[250.0])
    assert cell.generator_bound
    out = LoadResults("x", "t", "a laptop", False, 1, 10.0, 50.0, [cell]).table()
    assert "generator-bound" in out


async def test_a_run_that_falls_far_behind_is_abandoned() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    now = [0.0]

    def slow_clock() -> float:
        now[0] += 0.05  # every look at the clock costs 50 ms: a hopelessly slow client
        return now[0]

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://x"
    ) as client:
        lags: list[float] = []
        await _drive(
            client, "http://x/", rps=1000, duration_s=5, headers={}, body={}, lags=lags,
            clock=slow_clock,
        )  # fmt: skip
    assert len(lags) < 100, "abandoned long before the 5,000 it was asked to send"


def test_one_stalled_run_is_dropped_not_the_cell() -> None:
    three = {"p50": [1.0, 1.1, 1.2], "p95": [2.0, 2.1, 2.2], "p99": [3.0, 3.1, 3.2]}
    cell = Cell("audit", 50, overhead_ms=three, send_lag_p99_ms=[2.0, 2.0, 97.0, 2.0])
    assert not cell.generator_bound and cell.excluded_runs == 1
    out = LoadResults("x", "t", "a laptop", False, 4, 10.0, 50.0, [cell]).table()
    assert "1 run dropped" in out


def test_the_readme_rows_carry_intervals_and_name_a_generator_bound_cell() -> None:
    ok = Cell(
        "audit",
        200,
        overhead_ms={"p50": [1.0, 1.2, 1.1], "p95": [2.0, 2.1, 2.2], "p99": [3.0, 3.3, 3.1]},
        requests=6000,
        errors=0,
    )
    bound = Cell("audit", 500, overhead_ms={}, send_lag_p99_ms=[250.0])
    rows = LoadResults("x", "t", "the VPS", True, 3, 10.0, 50.0, [ok, bound]).readme_rows()
    first, second = rows.splitlines()
    assert first.startswith("| audit | 200 | 1.1 (") and " to " in first
    assert first.endswith("| 0 of 6000 |")
    assert "generator-bound" in second and "250 ms" in second


def test_only_a_published_run_fills_the_readme(tmp_path: Path) -> None:
    import argparse

    from boundary.cli import _loadtest_readme

    (tmp_path / "config").mkdir()
    readme = tmp_path / "README.md"
    readme.write_text("<!-- loadtest:start -->\n<!-- loadtest:end -->\n", encoding="utf-8")
    three = [1.0, 1.0, 1.0]
    cell = Cell("routing", 50, overhead_ms={"p50": three, "p95": three, "p99": three})
    args = argparse.Namespace(write_readme=True, config=tmp_path / "config" / "boundary.yaml")
    dev = LoadResults("x", "t", "a laptop", False, 1, 10.0, 50.0, [cell])
    assert _loadtest_readme(args, dev) == 2
    assert "routing" not in readme.read_text(encoding="utf-8")
    pub = LoadResults("x", "t", "the VPS", True, 1, 10.0, 50.0, [cell])
    assert _loadtest_readme(args, pub) == 0
    assert "| routing | 50 | 1.0 (1.0 to 1.0)" in readme.read_text(encoding="utf-8")


def test_a_proxy_that_cannot_hold_the_rate_is_saturated_not_generator_bound() -> None:
    # The first VPS run: the client kept to schedule, the proxy failed 9,386 of 10,000.
    cell = Cell(
        "audit",
        200,
        requests=10000,
        errors=9386,
        send_lag_p99_ms=[1.1, 1.1],
        saturated_runs=2,
    )
    assert cell.generator_bound and cell.saturated
    out = LoadResults("x", "t", "the VPS", True, 5, 10.0, 50.0, [cell]).table()
    assert "saturated: 9386 of 10000" in out and "generator-bound" not in out
    row = LoadResults("x", "t", "the VPS", True, 5, 10.0, 50.0, [cell]).readme_rows()
    assert "saturated" in row and row.endswith("| 9386 of 10000 |")


def _summary(
    p50: float, p99: float, *, requests: int = 500, failed: int = 0, dropped: int = 0
) -> dict[str, float]:
    return {"p50": p50, "p95": p50 + 1, "p99": p99, "requests": requests, "failed": failed,
            "dropped": dropped, "s0": 0, "s4xx": 0, "s5xx": 0}  # fmt: skip


def test_k6_runs_are_sorted_into_measured_stalled_dropped_and_saturated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The k6 cell's rules, on canned summaries (0.26, 0.27): a clean pair is an overhead; a
    pause in the mock's baseline drops the run; dropped against the mock is the client's
    limit; failures against the proxy while the mock is clean are saturation."""
    from boundary import loadtest

    script = iter(
        [
            _summary(52, 55), _summary(52, 55),  # warm-up, proxy then mock
            _summary(52, 53), _summary(55, 58),  # run 1: mock, proxy: clean, 3 ms p50
            _summary(52, 240), _summary(55, 58),  # run 2: the host paused in the baseline
            _summary(52, 53, dropped=4), _summary(55, 58),  # run 3: the client's limit
            _summary(52, 53), _summary(55, 59),  # run 4: clean
            _summary(52, 53), _summary(55, 60),  # run 5: clean
        ]
    )  # fmt: skip
    monkeypatch.setattr(loadtest, "_k6", lambda *a, **k: next(script))
    cell = Cell("audit", 50)
    loadtest._cell_k6(cell, 1, 2, {}, {}, 5, 10.0, tmp_path)
    assert cell.overhead_ms["p50"] == [3.0, 3.0, 3.0]
    assert cell.mock_stalled_runs == 1 and cell.dropped_iterations.count(4) == 1
    assert not cell.generator_bound and not cell.saturated

    script = iter([_summary(52, 55)] * 2 + [_summary(52, 53), _summary(900, 5000, failed=100)] * 2)
    monkeypatch.setattr(loadtest, "_k6", lambda *a, **k: next(script))
    cell = Cell("audit", 200)
    loadtest._cell_k6(cell, 1, 2, {}, {}, 5, 10.0, tmp_path)
    assert cell.saturated and cell.saturated_runs == 2, "stops after two"


def test_a_cache_cell_that_sheds_is_not_reported_as_the_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """0.29: a request sent upstream past a busy cache is answered, so it is not a failure;
    but its latency is the routing layer's, so a cache cell that sheds more than 1% gets no
    overhead figure, only how much it shed. Shedding under 1% is still a measurement."""
    from boundary import loadtest

    def shedding(n: int) -> dict[str, float]:
        return {**_summary(55, 58), "shed": n}

    few = [_summary(52, 55)] * 2 + [_summary(52, 53), shedding(3)] * 3
    script = iter(few)
    monkeypatch.setattr(loadtest, "_k6", lambda *a, **k: next(script))
    cell = Cell("cache", 50)
    loadtest._cell_k6(cell, 1, 2, {}, {}, 3, 10.0, tmp_path)
    assert cell.shed == 9 and not cell.shedding and not cell.generator_bound

    script = iter([_summary(52, 55)] * 2 + [_summary(52, 53), shedding(400)] * 3)
    monkeypatch.setattr(loadtest, "_k6", lambda *a, **k: next(script))
    cell = Cell("cache", 500)
    loadtest._cell_k6(cell, 1, 2, {}, {}, 3, 10.0, tmp_path)
    assert cell.shedding and cell.errors == 0
    results = LoadResults("x", "t", "the VPS", True, 3, 10.0, 50.0, [cell])
    assert "shedding: 1200 of 1500" in results.table()
    row = results.readme_rows()
    assert "no cache figure" in row and row.endswith("| 0 of 1500 |")


async def test_the_python_client_counts_what_the_proxy_shed() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={}, headers={"x-boundary-cache": "skip: busy"})

    shed = [0]
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://x"
    ) as client:
        lat, errors = await _drive(
            client, "http://x/", rps=50, duration_s=0.2, headers={}, body={}, shed=shed
        )
    assert errors == 0 and len(lat) == 10 and shed == [10]
