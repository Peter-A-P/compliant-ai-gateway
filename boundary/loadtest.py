"""The layered load test: the proxy's own latency, by layer and load level (0.19, PLAN.md B4).

The method is B4's. The upstream is a mock with a fixed 50 ms answer, so what is measured is
the proxy and not a vendor. Requests go out open-loop at a fixed rate, and each one's latency
is counted from the moment it was scheduled, not the moment it was sent, so a generator that
falls behind shows as latency rather than hiding it (coordinated omission). Every run
measures the same client against the mock directly as well, and **overhead is the proxy's
percentile minus the mock's**, per run. Layers are switched on cumulatively and each gets a
fresh proxy and ledger:

- `routing`: team key, policy, routing, the ledger; `X-Data-Class: public`, no audit chain.
- `audit`: the same, with the audit chain appended as the proxy answers.
- `redaction`: the same, with no header, so the request is `personal` and redacted, and the
  answer rehydrated.

B4's fourth layer, the cache, does not exist yet. The interval on each figure is a bootstrap
over runs, not over requests: requests within a run share whatever the machine was doing.

**Where it runs decides what it means.** B4 publishes the overhead budget from the first
measurement on the VPS, stated with its size. A run anywhere else is a development figure,
and the output says which machine it came from and that it is not the budget.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import platform
import random
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx
import yaml

# At module level, not inside `mock_app`: FastAPI resolves a handler's annotations by name
# in the module's globals, and a `Request` imported inside the function reads as a required
# query parameter, which answered every request with a 422 in the first trial of this file.
from fastapi import FastAPI, Request

LAYERS = ("routing", "audit", "redaction")
MOCK_DELAY_S = 0.050
KEY = "bnd_loadtest-key"
MODEL = "loadmock/m"
PERCENTILES = (0.50, 0.95, 0.99)
# How late the generator may send a request, at the 99th percentile, before the cell is its
# measurement rather than the proxy's. A Python client on one core cannot hold 500 requests a
# second: the first full run on the development laptop spent 26 minutes of CPU at that level
# and never finished. B4 names k6 for the VPS run for exactly this reason.
MAX_SEND_LAG_MS = 10.0
# Past this, a cell is abandoned rather than run to its end.
ABANDON_LAG_S = 1.0
# The fewest clean runs a cell needs to report an overhead at all.
MIN_RUNS = 3
# About a page of the kind of text the proxy exists for: a person, an email address, a phone
# number and a file number, so the redaction layer has real work and the echo has a size.
TEXT = (
    "Please draft a reply to Marie Chaulk (marie.chaulk@example.gov.nl.ca, 709-555-0142) "
    "about access request ATIPP-2024-0117. She asked on 3 March for the records held by the "
    "division concerning the decision of $1,250.00, and was told they would be released in "
    "part. The file was assigned to Gerald Penney, an analyst, who notified a third party. "
    "Confirm the request, the file number and that the review is under way."
)


def mock_app(delay_s: float = MOCK_DELAY_S) -> Any:
    """An OpenAI-compatible upstream that waits `delay_s` and echoes the user message."""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.post("/v1/chat/completions")
    async def complete(request: Request) -> dict[str, Any]:
        body = await request.json()
        await asyncio.sleep(delay_s)
        text = next(
            (m["content"] for m in reversed(body.get("messages", [])) if m.get("role") == "user"),
            "",
        )
        return {
            "id": "load",
            "object": "chat.completion",
            "model": "m",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 100, "completion_tokens": 100},
        }

    return app


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait(url: str, proc: subprocess.Popen[bytes], timeout_s: float = 30.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"{url} exited with {proc.returncode} before it answered")
        try:
            httpx.get(url, timeout=1.0)
            return
        except httpx.HTTPError:
            time.sleep(0.1)
    raise RuntimeError(f"{url} did not answer within {timeout_s} s")


def _config(work: Path, repo_config: Path, mock_port: int) -> Path:
    """The checked-in configuration plus a `loadmock` provider at price zero, declared
    single-region on localhost so personal data may reach it redacted or not."""
    raw = yaml.safe_load(repo_config.read_text(encoding="utf-8"))
    raw["providers"]["loadmock"] = {
        "kind": "openai_compat",
        "base_url": f"http://127.0.0.1:{mock_port}/v1",
        "price_zero": True,
        "region": "localhost",
        "residency": "single-region",
    }
    raw["ledger"] = {"path": str(work / "unused.sqlite"), "env": "loadtest"}
    raw["telemetry"] = {"exporter": "none"}
    for name in ("caps.yaml", "policy.yaml"):
        shutil.copy(repo_config.parent / name, work / name)
    out = work / "boundary.yaml"
    out.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    (work / "teams.yaml").write_text(
        "version: 1\ngateway_monthly_usd: 1000\nteams:\n  loadtest:\n"
        f"    key_sha256: ['{_hash(KEY)}']\n    monthly_usd: 1000\n"
        "    requests_per_minute: 100000000\n",
        encoding="utf-8",
    )
    return out


def _stop(proc: subprocess.Popen[bytes]) -> None:
    """Terminate, and kill if it will not go. A proxy still draining a backlog from a cell the
    generator abandoned held its graceful shutdown past 30 s in the first run with the abandon
    rule, and the exception took every finished cell's results with it."""
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


def _hash(key: str) -> str:
    import hashlib

    return hashlib.sha256(key.encode()).hexdigest()


def _p(values: Sequence[float], q: float) -> float:
    s = sorted(values)
    return s[min(len(s) - 1, round(q * (len(s) - 1)))]


async def _drive(
    client: httpx.AsyncClient,
    url: str,
    *,
    rps: int,
    duration_s: float,
    headers: dict[str, str],
    body: dict[str, Any],
    lags: list[float] | None = None,
    clock: Callable[[], float] = time.perf_counter,
) -> tuple[list[float], int]:
    """Open loop at `rps` for `duration_s`. Latency runs from each request's scheduled time,
    so falling behind is measured rather than hidden. Returns latencies in ms and errors.
    `lags`, when given, receives how late each request was dispatched, in ms; once that
    passes `ABANDON_LAG_S` the rest of the run is not sent."""
    latencies: list[float] = []
    errors = 0
    n = int(rps * duration_s)
    interval = 1.0 / rps
    start = clock() + 0.05

    async def one(scheduled: float) -> None:
        nonlocal errors
        try:
            r = await client.post(url, json=body, headers=headers)
            ok = r.status_code == 200
        except httpx.HTTPError:
            ok = False
        if ok:
            latencies.append((clock() - scheduled) * 1000.0)
        else:
            errors += 1

    tasks = []
    for i in range(n):
        scheduled = start + i * interval
        delay = scheduled - clock()
        if delay > 0:
            await asyncio.sleep(delay)
        elif -delay > ABANDON_LAG_S:
            break
        if lags is not None:
            lags.append(max(0.0, (clock() - scheduled) * 1000.0))
        tasks.append(asyncio.create_task(one(scheduled)))
    await asyncio.gather(*tasks)
    return latencies, errors


@dataclass
class Cell:
    layer: str
    rps: int
    # One entry per run: overhead at each percentile, proxy minus mock, in ms.
    overhead_ms: dict[str, list[float]] = field(default_factory=dict)
    proxy_ms: dict[str, list[float]] = field(default_factory=dict)
    mock_ms: dict[str, list[float]] = field(default_factory=dict)
    requests: int = 0
    errors: int = 0
    # The generator's own lag at the 99th percentile, per run, in ms. Above MAX_SEND_LAG_MS
    # the cell measures the client, and is reported as generator-bound instead.
    send_lag_p99_ms: list[float] = field(default_factory=list)

    @property
    def excluded_runs(self) -> int:
        """Runs whose generator lagged: their overhead is not recorded."""
        return sum(1 for v in self.send_lag_p99_ms if v > MAX_SEND_LAG_MS)

    @property
    def generator_bound(self) -> bool:
        """Fewer than MIN_RUNS runs the generator kept up with. One stall in one run is the
        machine, and that run alone is dropped; the first version dropped the whole cell for
        it, which on the development laptop discarded a layer's 50 rps cell over a single
        97 ms pause in its fifth run."""
        return len(self.overhead_ms.get("p50", [])) < MIN_RUNS


def _ci(values: Sequence[float], rng: random.Random, n: int = 2000) -> tuple[float, float]:
    k = len(values)
    means = sorted(sum(values[rng.randrange(k)] for _ in range(k)) / k for _ in range(n))
    return means[int(0.025 * n)], means[int(0.975 * n) - 1]


@dataclass
class LoadResults:
    boundary_version: str
    ran_utc: str
    machine: str
    published: bool
    runs: int
    duration_s: float
    mock_delay_ms: float
    cells: list[Cell]

    def table(self) -> str:
        rng = random.Random(20260925)
        lines = [
            f"boundary {self.boundary_version}, layered load test on {self.machine}: "
            f"{self.runs} runs of {self.duration_s:g} s per cell, mock upstream "
            f"{self.mock_delay_ms:g} ms",
            ""
            if self.published
            else "A DEVELOPMENT FIGURE, NOT THE OVERHEAD BUDGET: PLAN.md B4 publishes that from "
            "the VPS.",
            "",
            f"{'layer':<11}{'rps':>5}  {'overhead p50 ms':<22}{'p95 ms':<22}{'p99 ms':<22}"
            f"{'errors':>8}",
        ]
        for c in self.cells:
            if c.generator_bound:
                worst = max(c.send_lag_p99_ms, default=float("nan"))
                lines.append(
                    f"{c.layer:<11}{c.rps:>5}  generator-bound: the client sent up to "
                    f"{worst:.0f} ms late at p99, so this cell measures the client, not the proxy"
                )
                continue
            cols = []
            for q in ("p50", "p95", "p99"):
                vals = c.overhead_ms.get(q, [])
                if not vals:
                    cols.append("n/a")
                    continue
                mean = sum(vals) / len(vals)
                lo, hi = _ci(vals, rng) if len(vals) > 1 else (mean, mean)
                cols.append(f"{mean:.1f} ({lo:.1f} to {hi:.1f})")
            dropped = (
                f"  ({c.excluded_runs} run dropped: client stalled)" if c.excluded_runs else ""
            )
            lines.append(
                f"{c.layer:<11}{c.rps:>5}  {cols[0]:<22}{cols[1]:<22}{cols[2]:<22}"
                f"{c.errors:>5}/{c.requests}{dropped}"
            )
        lines += [
            "",
            "Overhead is the proxy's percentile minus the mock's, per run; the interval is a "
            "bootstrap over runs. Latency counts from each request's scheduled time.",
        ]
        return "\n".join(lines)

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=1) + "\n"


def run(
    repo_config: Path,
    *,
    levels: Sequence[int] = (50, 200),
    runs: int = 5,
    duration_s: float = 10.0,
    layers: Sequence[str] = LAYERS,
    machine: str | None = None,
    published: bool = False,
) -> LoadResults:
    from boundary import __version__

    cells: list[Cell] = []
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        mock_port = _free_port()
        env = {**os.environ, "PYTHONUNBUFFERED": "1"}
        mock = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import uvicorn; from boundary.loadtest import mock_app; "
                f"uvicorn.run(mock_app({MOCK_DELAY_S}), host='127.0.0.1', port={mock_port}, "
                "log_level='warning')",
            ],
            env=env,
        )
        try:
            # Any HTTP answer, even a 404, means the mock is up.
            _wait(f"http://127.0.0.1:{mock_port}/", mock)
            cfg = _config(work, repo_config, mock_port)
            for layer in layers:
                port = _free_port()
                ledger = work / f"{layer}.sqlite"
                cmd = [
                    sys.executable, "-m", "boundary.cli", "--config", str(cfg), "serve",
                    "--teams", str(work / "teams.yaml"), "--port", str(port),
                    "--ledger", str(ledger),
                ]  # fmt: skip
                if layer == "routing":
                    cmd.append("--no-audit")
                proxy = subprocess.Popen(
                    cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
                try:
                    _wait(f"http://127.0.0.1:{port}/healthz", proxy)
                    headers = {"authorization": f"Bearer {KEY}"}
                    if layer != "redaction":
                        headers["x-data-class"] = "public"
                    body = {
                        "model": MODEL,
                        "messages": [{"role": "user", "content": TEXT}],
                        "max_tokens": 200,
                    }
                    for rps in levels:
                        cell = Cell(layer, rps)
                        asyncio.run(_cell(cell, port, mock_port, headers, body, runs, duration_s))
                        cells.append(cell)
                        # Progress as it goes, so a run that dies later has still said something.
                        print(
                            f"  {layer} {rps} rps: overhead p50 per run "
                            f"{cell.overhead_ms.get('p50', [])}, send lag p99 "
                            f"{cell.send_lag_p99_ms}",
                            file=sys.stderr,
                            flush=True,
                        )
                finally:
                    _stop(proxy)
        finally:
            _stop(mock)
    return LoadResults(
        boundary_version=__version__,
        ran_utc=dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        machine=machine or f"{platform.node()} ({platform.machine()}, {os.cpu_count()} cores)",
        published=published,
        runs=runs,
        duration_s=duration_s,
        mock_delay_ms=MOCK_DELAY_S * 1000,
        cells=cells,
    )


async def _cell(
    cell: Cell,
    port: int,
    mock_port: int,
    headers: dict[str, str],
    body: dict[str, Any],
    runs: int,
    duration_s: float,
) -> None:
    limits = httpx.Limits(max_connections=2000, max_keepalive_connections=2000)
    async with httpx.AsyncClient(limits=limits, timeout=30.0) as client:
        proxy_url = f"http://127.0.0.1:{port}/v1/chat/completions"
        mock_url = f"http://127.0.0.1:{mock_port}/v1/chat/completions"
        # Warm both: connections, the proxy's first-call costs, the mock's.
        await _drive(client, proxy_url, rps=20, duration_s=1.0, headers=headers, body=body)
        await _drive(client, mock_url, rps=20, duration_s=1.0, headers={}, body=body)
        for _ in range(runs):
            lags: list[float] = []
            mock_lat, _mock_errors = await _drive(
                client,
                mock_url,
                rps=cell.rps,
                duration_s=duration_s,
                headers={},
                body=body,
                lags=lags,
            )
            proxy_lat, proxy_err = await _drive(
                client, proxy_url, rps=cell.rps, duration_s=duration_s, headers=headers,
                body=body, lags=lags,
            )  # fmt: skip
            lag = round(_p(lags, 0.99), 3) if lags else float("inf")
            cell.send_lag_p99_ms.append(lag)
            if max(lags, default=float("inf")) > ABANDON_LAG_S * 1000:
                # The generator gave up part way: this level is beyond it, and more runs would
                # only take longer to say so.
                break
            if lag > MAX_SEND_LAG_MS:
                continue
            cell.requests += int(cell.rps * duration_s)
            cell.errors += proxy_err
            if not proxy_lat or not mock_lat:
                continue
            for q in PERCENTILES:
                name = f"p{int(q * 100)}"
                pm, mm = _p(proxy_lat, q), _p(mock_lat, q)
                cell.proxy_ms.setdefault(name, []).append(round(pm, 3))
                cell.mock_ms.setdefault(name, []).append(round(mm, 3))
                cell.overhead_ms.setdefault(name, []).append(round(pm - mm, 3))


__all__ = ["LAYERS", "Cell", "LoadResults", "mock_app", "run"]
