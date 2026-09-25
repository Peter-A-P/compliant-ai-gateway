"""The load-test harness itself (0.19). The run is a command; these check the arithmetic and
the two properties the method rests on, without starting a server."""

from __future__ import annotations

import asyncio
import random

import httpx

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
