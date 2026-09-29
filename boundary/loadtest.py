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
- `cache` (0.27): `audit` plus the semantic cache, on the path a miss takes: every request
  is a bare question marked for the cache (`QUESTION`, not the page the other layers send,
  because a page is never cached) carrying its own number, so every one is embedded, looked
  up, missed and stored. Public
  data, because personal data is never cached, so this layer branches from `audit` rather
  than stacking on `redaction`. A hit skips the upstream altogether, so its latency is not
  an overhead on it; the miss is the price every request pays for the cache being on.
 The interval on each figure is a bootstrap
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
import secrets
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

LAYERS = ("routing", "audit", "redaction", "cache")
# The cache layer's threshold: 1.0, so that no two requests, each with its own number, can
# hit, and every request pays the miss path.
CACHE_MISS_THRESHOLD = "1.0"
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
# A run in which more than this share of the proxy's requests failed measured a proxy that
# could not hold the rate, and its surviving latencies are not an overhead (0.26: the first
# VPS run labelled such a cell generator-bound, which blamed the client for the proxy).
MAX_ERROR_SHARE = 0.01
# Saturated runs after which a cell stops: more runs only take longer to say so.
SATURATED_STOP = 2
# A run whose mock p99 sits this far above its own p50 had a pause on the machine while the
# baseline was taken (0.27). The mock does nothing but wait 50 ms, so a 200 ms p99 is the
# host, and subtracting it from the proxy's p99 gave a negative overhead in the first 0.27
# run. Such a run is dropped and counted, as a stalled client run is. A pause during the
# proxy's own run is not detectable apart from the proxy, and stays in the figure.
MOCK_STALL_MS = 20.0
# About a page of the kind of text the proxy exists for: a person, an email address, a phone
# number and a file number, so the redaction layer has real work and the echo has a size.
TEXT = (
    "Please draft a reply to Marie Chaulk (marie.chaulk@example.gov.nl.ca, 709-555-0142) "
    "about access request ATIPP-2024-0117. She asked on 3 March for the records held by the "
    "division concerning the decision of $1,250.00, and was told they would be released in "
    "part. The file was assigned to Gerald Penney, an analyst, who notified a third party. "
    "Confirm the request, the file number and that the review is under way."
)

# The cache layer's question. The cache only ever sees a message its caller marked as a bare
# question (docs/cache.md), so that is what this layer sends, not the page the others send.
# Embedding cost grows with length: on the VPS, bge-small takes about 5 ms for this and 22 ms
# for TEXT.
QUESTION = "How long do I have to dispute a charge on my credit card statement?"


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
    unique: bool = False,
) -> tuple[list[float], int]:
    """Open loop at `rps` for `duration_s`. Latency runs from each request's scheduled time,
    so falling behind is measured rather than hidden. Returns latencies in ms and errors.
    `lags`, when given, receives how late each request was dispatched, in ms; once that
    passes `ABANDON_LAG_S` the rest of the run is not sent."""
    latencies: list[float] = []
    errors = 0
    nonce = secrets.token_hex(6)
    n = int(rps * duration_s)
    interval = 1.0 / rps
    start = clock() + 0.05

    async def one(scheduled: float, i: int) -> None:
        nonlocal errors
        payload = body
        if unique:
            # Every request its own question (the cache layer).
            msgs = [dict(m) for m in body["messages"]]
            msgs[0]["content"] = f"{msgs[0]['content']} Reference {nonce}-{i}."
            payload = {**body, "messages": msgs}
        try:
            r = await client.post(url, json=payload, headers=headers)
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
        tasks.append(asyncio.create_task(one(scheduled, i)))
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
    # k6 (0.26) has no per-request lag to report. Its arrival-rate executor starts each
    # request on schedule while it has a free virtual user and counts one it could not start
    # as a dropped iteration, so a run with any dropped is the client's, and is excluded.
    dropped_iterations: list[int] = field(default_factory=list)
    # Runs the generator kept up with in which the proxy failed more than MAX_ERROR_SHARE.
    saturated_runs: int = 0
    # k6 only: what the proxy's failures were, summed over runs. `timeout` is no answer in
    # 30 s or no connection, `dropped` a request k6 could not start for want of a free user.
    failures: dict[str, int] = field(default_factory=dict)
    # Runs dropped because the mock's own p99 showed a pause on the host (MOCK_STALL_MS).
    mock_stalled_runs: int = 0

    @property
    def excluded_runs(self) -> int:
        """Runs whose generator lagged: their overhead is not recorded."""
        return sum(1 for v in self.send_lag_p99_ms if v > MAX_SEND_LAG_MS) + sum(
            1 for d in self.dropped_iterations if d > 0
        )

    @property
    def saturated(self) -> bool:
        """Unmeasured because the proxy could not hold the rate, not because the client
        could not send it."""
        return self.generator_bound and self.saturated_runs > 0

    def bound_reason(self) -> str:
        if self.saturated:
            return (
                f"saturated: {self.errors} of {self.requests} requests failed, so the proxy "
                "cannot hold this rate"
            )
        if any(self.dropped_iterations):
            return (
                f"k6 could not start {max(self.dropped_iterations)} requests on schedule in a run"
            )
        if not self.send_lag_p99_ms:
            return f"fewer than {MIN_RUNS} clean runs"
        worst = max(self.send_lag_p99_ms)
        return f"the client sent up to {worst:.0f} ms late at p99"

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
    # `python`, the open-loop client in this module, or `k6` (0.26, B4's generator).
    generator: str = "python"

    def table(self) -> str:
        rng = random.Random(20260925)
        lines = [
            f"boundary {self.boundary_version}, layered load test on {self.machine}: "
            f"{self.runs} runs of {self.duration_s:g} s per cell, mock upstream "
            f"{self.mock_delay_ms:g} ms, generator {self.generator}",
            ""
            if self.published
            else "A DEVELOPMENT FIGURE, NOT THE OVERHEAD BUDGET: PLAN.md B4 publishes that from "
            "the VPS.",
            "",
            f"{'layer':<11}{'rps':>5}  {'overhead p50 ms':<22}{'p95 ms':<22}{'p99 ms':<22}"
            f"{'errors':>8}",
        ]
        for c in self.cells:
            if c.saturated:
                lines.append(f"{c.layer:<11}{c.rps:>5}  {c.bound_reason()}")
                continue
            if c.generator_bound:
                lines.append(
                    f"{c.layer:<11}{c.rps:>5}  generator-bound: {c.bound_reason()}, so this "
                    "cell measures the client, not the proxy"
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
            ) + (
                f"  ({c.mock_stalled_runs} run dropped: host paused during the baseline)"
                if c.mock_stalled_runs
                else ""
            )
            lines.append(
                f"{c.layer:<11}{c.rps:>5}  {cols[0]:<22}{cols[1]:<22}{cols[2]:<22}"
                f"{c.errors:>5}/{c.requests}{dropped}"
            )
        lines += [
            "",
            "Overhead is the proxy's percentile minus the mock's, per run; the interval is a "
            "bootstrap over runs. "
            + (
                "k6 times each request from its send and starts it on schedule; a run with a "
                "request it could not start on time is dropped."
                if self.generator == "k6"
                else "Latency counts from each request's scheduled time."
            ),
        ]
        return "\n".join(lines)

    def readme_rows(self) -> str:
        """The README's overhead table (0.26): one row per cell, a generator-bound cell named
        as such rather than given a number."""
        rng = random.Random(20260925)
        rows = []
        for c in self.cells:
            if c.saturated:
                rows.append(
                    f"| {c.layer} | {c.rps} | saturated: the proxy cannot hold this rate | "
                    f"{c.errors} of {c.requests} |"
                )
                continue
            if c.generator_bound:
                rows.append(
                    f"| {c.layer} | {c.rps} | generator-bound: {c.bound_reason()}, so no figure | |"
                )
                continue
            cols = []
            for q in ("p50", "p95", "p99"):
                vals = c.overhead_ms.get(q, [])
                mean = sum(vals) / len(vals)
                lo, hi = _ci(vals, rng) if len(vals) > 1 else (mean, mean)
                cols.append(f"{mean:.1f} ({lo:.1f} to {hi:.1f})")
            rows.append(
                f"| {c.layer} | {c.rps} | {' / '.join(cols)} | {c.errors} of {c.requests} |"
            )
        return "\n".join(rows)

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=1) + "\n"


README_START = "<!-- loadtest:start -->"
README_END = "<!-- loadtest:end -->"


def write_readme(readme: Path, rows: str) -> None:
    text = readme.read_text(encoding="utf-8")
    a, b = text.index(README_START), text.index(README_END)
    readme.write_text(
        text[: a + len(README_START)] + "\n" + rows + "\n" + text[b:], encoding="utf-8"
    )


def run(
    repo_config: Path,
    *,
    levels: Sequence[int] = (50, 200),
    runs: int = 5,
    duration_s: float = 10.0,
    layers: Sequence[str] = LAYERS,
    machine: str | None = None,
    published: bool = False,
    generator: str = "python",
) -> LoadResults:
    from boundary import __version__

    if generator not in ("python", "k6"):
        raise ValueError(f"unknown generator {generator!r}")
    if generator == "k6" and shutil.which("k6") is None:
        raise RuntimeError("the k6 generator needs k6 on PATH (https://grafana.com/docs/k6/)")

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
                if layer == "cache":
                    cmd += ["--semantic-cache", CACHE_MISS_THRESHOLD]
                proxy = subprocess.Popen(
                    cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
                try:
                    # The cache layer loads its embedding model before it answers.
                    _wait(f"http://127.0.0.1:{port}/healthz", proxy, timeout_s=180.0)
                    headers = {"authorization": f"Bearer {KEY}"}
                    if layer != "redaction":
                        headers["x-data-class"] = "public"
                    body = {
                        "model": MODEL,
                        "messages": [{"role": "user", "content": TEXT}],
                        "max_tokens": 200,
                    }
                    unique = layer == "cache"
                    if unique:
                        headers["x-boundary-cache"] = "question"
                        body["messages"] = [{"role": "user", "content": QUESTION}]
                    for rps in levels:
                        cell = Cell(layer, rps)
                        if generator == "k6":
                            _cell_k6(
                                cell, port, mock_port, headers, body, runs, duration_s, work,
                                unique=unique,
                            )  # fmt: skip
                        else:
                            asyncio.run(
                                _cell(
                                    cell,
                                    port,
                                    mock_port,
                                    headers,
                                    body,
                                    runs,
                                    duration_s,
                                    unique=unique,
                                )
                            )
                        cells.append(cell)
                        # Progress as it goes, so a run that dies later has still said something.
                        print(
                            f"  {layer} {rps} rps: overhead p50 per run "
                            f"{cell.overhead_ms.get('p50', [])}, send lag p99 "
                            f"{cell.send_lag_p99_ms}, dropped {cell.dropped_iterations}",
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
        generator=generator,
    )


async def _cell(
    cell: Cell,
    port: int,
    mock_port: int,
    headers: dict[str, str],
    body: dict[str, Any],
    runs: int,
    duration_s: float,
    *,
    unique: bool = False,
) -> None:
    limits = httpx.Limits(max_connections=2000, max_keepalive_connections=2000)
    async with httpx.AsyncClient(limits=limits, timeout=30.0) as client:
        proxy_url = f"http://127.0.0.1:{port}/v1/chat/completions"
        mock_url = f"http://127.0.0.1:{mock_port}/v1/chat/completions"
        # Warm both: connections, the proxy's first-call costs, the mock's.
        await _drive(
            client, proxy_url, rps=20, duration_s=1.0, headers=headers, body=body, unique=unique
        )
        await _drive(client, mock_url, rps=20, duration_s=1.0, headers={}, body=body, unique=unique)
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
                unique=unique,
            )
            proxy_lat, proxy_err = await _drive(
                client, proxy_url, rps=cell.rps, duration_s=duration_s, headers=headers,
                body=body, lags=lags, unique=unique,
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
            if not proxy_lat or proxy_err > MAX_ERROR_SHARE * cell.rps * duration_s:
                cell.saturated_runs += 1
                if cell.saturated_runs >= SATURATED_STOP:
                    break
                continue
            if not mock_lat:
                continue
            if _p(mock_lat, 0.99) - _p(mock_lat, 0.50) > MOCK_STALL_MS:
                cell.mock_stalled_runs += 1
                continue
            for q in PERCENTILES:
                name = f"p{int(q * 100)}"
                pm, mm = _p(proxy_lat, q), _p(mock_lat, q)
                cell.proxy_ms.setdefault(name, []).append(round(pm, 3))
                cell.mock_ms.setdefault(name, []).append(round(mm, 3))
                cell.overhead_ms.setdefault(name, []).append(round(pm - mm, 3))


# One k6 run: an open-loop arrival rate, so a slow answer never slows the next request, with
# enough virtual users preallocated that none is started late for want of one. The summary
# keeps only what the cell needs, exact percentiles of every request's duration.
K6_SCRIPT = """
import http from "k6/http";
import { Counter } from "k6/metrics";
const statuses = { s0: new Counter("s0"), s4xx: new Counter("s4xx"), s5xx: new Counter("s5xx") };
const cfg = JSON.parse(open(__ENV.BOUNDARY_K6_CFG));
export const options = {
  discardResponseBodies: true,
  summaryTrendStats: ["p(50)", "p(95)", "p(99)"],
  scenarios: { load: {
    executor: "constant-arrival-rate", rate: cfg.rps, timeUnit: "1s",
    duration: cfg.duration, preAllocatedVUs: cfg.vus, maxVUs: cfg.vus * 4,
  } },
};
const body = JSON.stringify(cfg.body);
let n = 0;
export default function () {
  let payload = body;
  if (cfg.unique) {
    // Every request its own question, so none repeats another (the cache layer), in this
    // run or any other: the nonce is fresh for every k6 process.
    n += 1;
    const b = JSON.parse(body);
    b.messages[0].content += " Reference " + cfg.nonce + "-" + __VU + "-" + n + ".";
    payload = JSON.stringify(b);
  }
  const r = http.post(cfg.url, payload, { headers: cfg.headers, timeout: "30s" });
  if (r.status === 0) statuses.s0.add(1);
  else if (r.status >= 500) statuses.s5xx.add(1);
  else if (r.status >= 400) statuses.s4xx.add(1);
}
export function handleSummary(data) {
  const m = data.metrics;
  const d = m.http_req_duration.values;
  const out = {
    p50: d["p(50)"], p95: d["p(95)"], p99: d["p(99)"],
    requests: m.http_reqs.values.count,
    failed: m.http_req_failed ? m.http_req_failed.values.passes : 0,
    dropped: m.dropped_iterations ? m.dropped_iterations.values.count : 0,
    s0: m.s0 ? m.s0.values.count : 0,
    s4xx: m.s4xx ? m.s4xx.values.count : 0,
    s5xx: m.s5xx ? m.s5xx.values.count : 0,
  };
  return { [__ENV.BOUNDARY_K6_OUT]: JSON.stringify(out) };
}
"""


def _k6(
    work: Path, url: str, *, rps: int, duration_s: float, headers: dict[str, str],
    body: dict[str, Any], unique: bool = False,
) -> dict[str, float]:  # fmt: skip
    """One k6 run against `url`; its summary. k6 times each request from its send, and a
    request it could not send on time is a dropped iteration rather than a late one."""
    script = work / "k6.js"
    if not script.exists():
        script.write_text(K6_SCRIPT, encoding="utf-8")
    cfg, out = work / "k6-cfg.json", work / "k6-out.json"
    out.unlink(missing_ok=True)
    cfg.write_text(
        json.dumps(
            {
                "url": url,
                "rps": rps,
                "duration": f"{duration_s:g}s",
                # Little's law with a wide margin: rps x (50 ms upstream + overhead) in flight.
                "vus": max(20, rps // 2),
                "headers": {"content-type": "application/json", **headers},
                "body": body,
                "unique": unique,
                "nonce": secrets.token_hex(6),
            }
        ),
        encoding="utf-8",
    )
    subprocess.run(
        ["k6", "run", "--quiet", "--no-usage-report", str(script)],
        # Not K6_*: k6 reads every K6_ variable as its own option, and took K6_OUT for an
        # output type in the first trial.
        env={**os.environ, "BOUNDARY_K6_CFG": str(cfg), "BOUNDARY_K6_OUT": str(out)},
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    result: dict[str, float] = json.loads(out.read_text(encoding="utf-8"))
    return result


def _cell_k6(
    cell: Cell,
    port: int,
    mock_port: int,
    headers: dict[str, str],
    body: dict[str, Any],
    runs: int,
    duration_s: float,
    work: Path,
    *,
    unique: bool = False,
) -> None:
    """`_cell` with k6 as the generator: the same pairing of a mock run and a proxy run, the
    same overhead per run, and a run the generator could not keep to schedule excluded."""
    proxy_url = f"http://127.0.0.1:{port}/v1/chat/completions"
    mock_url = f"http://127.0.0.1:{mock_port}/v1/chat/completions"
    _k6(work, proxy_url, rps=20, duration_s=1.0, headers=headers, body=body, unique=unique)
    _k6(work, mock_url, rps=20, duration_s=1.0, headers={}, body=body, unique=unique)
    for _ in range(runs):
        mock = _k6(
            work,
            mock_url,
            rps=cell.rps,
            duration_s=duration_s,
            headers={},
            body=body,
            unique=unique,
        )
        proxy = _k6(
            work,
            proxy_url,
            rps=cell.rps,
            duration_s=duration_s,
            headers=headers,
            body=body,
            unique=unique,
        )
        # Dropped against the mock is the client's limit. Dropped against the proxy alone is
        # the proxy's: its slow answers held every virtual user, so none was free on time.
        if int(mock["dropped"]):
            cell.dropped_iterations.append(int(mock["dropped"]))
            continue
        cell.dropped_iterations.append(0)
        for kind, key in (
            ("timeout", "s0"),
            ("4xx", "s4xx"),
            ("5xx", "s5xx"),
            ("dropped", "dropped"),
        ):
            if int(proxy.get(key, 0)):
                cell.failures[kind] = cell.failures.get(kind, 0) + int(proxy[key])
        cell.requests += int(proxy["requests"]) + int(proxy["dropped"])
        cell.errors += int(proxy["failed"]) + int(proxy["dropped"])
        failed = int(proxy["failed"]) + int(proxy["dropped"])
        if failed > MAX_ERROR_SHARE * max(1, int(proxy["requests"]) + int(proxy["dropped"])):
            cell.saturated_runs += 1
            if cell.saturated_runs >= SATURATED_STOP:
                break
            continue
        if float(mock["p99"]) - float(mock["p50"]) > MOCK_STALL_MS:
            cell.mock_stalled_runs += 1
            continue
        for name in ("p50", "p95", "p99"):
            pm, mm = float(proxy[name]), float(mock[name])
            cell.proxy_ms.setdefault(name, []).append(round(pm, 3))
            cell.mock_ms.setdefault(name, []).append(round(mm, 3))
            cell.overhead_ms.setdefault(name, []).append(round(pm - mm, 3))


__all__ = ["LAYERS", "Cell", "LoadResults", "mock_app", "run", "write_readme"]
