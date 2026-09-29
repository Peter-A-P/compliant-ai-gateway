# The load test

`boundary loadtest` (0.19) is PLAN.md B4's layered load test, built before the VPS it is
meant to run on so that the method is fixed, tested and exercised before the number that
matters is taken. **The overhead budget in the README comes from the VPS run only**, stated
with the VPS size, and nothing below is that number.

```
uv sync --extra server
boundary loadtest                                   # 3 layers x 50, 200, 500 rps x 5 runs x 10 s
boundary loadtest --generator k6 --levels 50,200,500 --machine "..." --published --out bench/loadtest.json
boundary loadtest --from bench/loadtest.json --write-readme   # fill the README from a stored run
```

## Method

- **The upstream is a mock** with a fixed 50 ms answer, an OpenAI-compatible server that echoes
  the user message, in its own process. The proxy is `boundary serve` in its own process, on
  loopback, with a fresh ledger for each layer. No vendor is called and nothing is spent.
- **Open loop**: requests go out at a fixed rate whatever the answers are doing, and each one's
  latency counts from the moment it was **scheduled**, not the moment it was sent. A closed-loop
  client that waits for an answer before sending the next hides a stall as a lower request
  rate; this one records it as latency, including for every request queued behind it
  (coordinated omission). A test stalls a mock for 300 ms and requires the stall to show.
- **Overhead is per run and differential**: every run measures the same client against the
  mock directly and then through the proxy, at the same rate for the same duration, and the
  overhead is the proxy's percentile minus the mock's. The client's own cost, the loopback
  and the mock's jitter are in both and cancel, up to the noise between two consecutive runs.
- **The interval is over runs**, a bootstrap of the per-run overheads, because requests inside
  one run share whatever the machine was doing and are not independent draws.
- **The generator** (0.26): `--generator k6` is B4's. Its constant-arrival-rate executor
  starts each request on schedule while it has a free virtual user, and counts one it could
  not start as a dropped iteration. So k6 times each request from its send, and a run with
  any dropped iteration against the mock is excluded as the client's. Dropped iterations
  against the proxy alone are the proxy's: its slow answers held every user. The default,
  `python`, is the open-loop client below, which counts from the schedule instead.
- **Layers, cumulative**: `routing` (team key, policy, routing, ledger; public data; no audit
  chain), `audit` (plus the chain appended as the proxy answers), `redaction` (no header, so
  personal: redacted before it leaves, rehydrated after), and since 0.27 `cache` (audit plus
  the semantic cache, on a miss: a bare question marked for the cache, each with its own
  number, public, because personal data is never cached, so it branches from `audit`).

The request is about a page of text naming a person, an email address, a phone number and a
file number, so the redaction layer has real work to do and the echo has a realistic size.

## The VPS run: the published figure (0.27)

2026-09-29, boundary 0.27.0, the same host, generator and settings as the 0.26 run below,
with the fourth layer added. `bench/loadtest.json`, milliseconds:

| Layer | 50 rps p50 | 50 rps p99 | 200 rps p50 | 200 rps p99 | 500 rps |
|---|---|---|---|---|---|
| routing | 3.7 (3.5 to 3.8) | 4.8 (4.3 to 5.6) | 4.0 (3.9 to 4.2) | 9.7 (7.7 to 12.4) | saturated, 7,145 of 10,001 failed |
| + audit | 3.6 (3.5 to 3.7) | 4.5 (4.2 to 4.8) | 4.4 (4.2 to 4.6) | 13.2 (9.3 to 20.1) | saturated, 7,245 of 10,000 |
| + redaction | 5.2 (5.1 to 5.3) | 5.9 (5.6 to 6.3) | saturated, 1,452 of 4,002 | | saturated |
| cache (audit + cache, miss path) | 17.9 (17.3 to 18.6) | 54.7 (27.5 to 94.6) | saturated, 2,315 of 4,001 | | saturated |

**What changed since 0.26, and why.** The 0.26 run found the proxy failing at 200 requests a
second once the audit chain was on. Every ledger write and every audit append was an `fsync`
on the event loop, and on this host a commit costs about 1 ms (the table below). 0.27 made
two changes and nothing else on the timed path:

- `Gateway.achat` and `achat_stream` write the ledger from a worker thread. A cap check and
  the row that spends against it are one step under a lock per ledger file, shared by every
  gateway on it. A test sends twelve calls at once against a cap that fits three and
  requires exactly three sent; without the lock it fails.
- The proxy group-commits the audit chain. A completed call schedules one seal 50 ms later,
  in a worker thread, and every call that completes before it runs shares the commit. The
  price is a window of up to 50 ms in which a call is in the ledger and not yet in the
  chain. `GET /audit/head` seals before it answers, so an anchor never misses a call that
  had completed.

With the audit chain on, the proxy now holds 200 requests a second (4.4 ms p50, 13.2 ms p99),
and its p99 at 50 fell from 6.0 to 4.5 ms. Routing alone and redaction moved within their
intervals or slightly down. At 500 every layer still saturates, and redaction still does at
200: what is left is CPU in one Python process, not waiting on the disk.

**The cache layer.** This is audit plus the semantic cache on the path a miss takes. Every
request is a bare question marked for the cache, carrying its own number, so each one is
embedded, looked up, missed and stored. It is the price every request pays for having the
cache on. A hit instead skips the upstream altogether. bge-small takes about 5 ms to embed
a twelve-word question on this host (22 ms for the page the other layers send), and with
several at once on four cores the miss costs about 14 ms more than the audit layer at the
median. It saturates at 200.

**Two things the runs before this one found.**

- **BLAS threads.** The first cache run's overhead jumped from 16 ms to 90 ms after two runs,
  and stayed there. The store's scores were a numpy matrix product. Past about a thousand
  entries BLAS fans a product out over every core, and with a lookup in each of several
  worker threads they fought for the same four. The store now scores with `einsum`, whose
  loop stays on the calling thread. At the same size a six-run probe held 16 to 18 ms in
  every run. The embedding's own threads were not the cause: one, two or every core gave
  the same jump.
- **A pause on the host during the baseline.** In an interim 0.27 run, one run's mock p99 was
  244 ms against a 52 ms median, and subtracting it gave redaction a p99 overhead of
  -32 ms. The mock does nothing but wait 50 ms, so such a pause is the host's. A run whose mock
  p99 sits more than 20 ms above its own median is now dropped and counted
  (`MOCK_STALL_MS`), as a stalled client run is. This rule was written after seeing that
  run, and is stated as such. The published run above dropped none. A pause during the
  proxy's own run cannot be told apart from the proxy, and stays in the figure: the routing
  cell's wide p99 interval at 200 is that.

## The first VPS run (0.26)

2026-09-29, boundary 0.26.0, on the host that serves gateway.peterparker.ca: OVHcloud VPS-2,
4 vCPU (AMD EPYC-Milan), 8 GB, Beauharnois, Ubuntu 24.04 (docs/deploy.md). The generator, the
mock and the proxy share the host, in a throwaway container beside the live proxy, which
served no traffic during the run. `boundary loadtest --generator k6 --levels 50,200,500 --runs
5 --duration 10 --published`, stored in `bench/loadtest-026.json`, milliseconds:

| Layer | 50 rps p50 | 50 rps p99 | 200 rps p50 | 200 rps p99 | 500 rps |
|---|---|---|---|---|---|
| routing | 3.1 (3.0 to 3.3) | 3.9 (3.7 to 4.1) | 3.5 (3.4 to 3.5) | 7.7 (6.8 to 9.2) | saturated, 7,075 of 10,001 failed |
| + audit | 4.9 (4.7 to 5.0) | 6.0 (5.8 to 6.2) | saturated, 1,760 of 4,000 failed | | saturated |
| + redaction | 6.3 (6.3 to 6.4) | 7.4 (7.1 to 7.7) | saturated, 2,151 of 4,000 failed | | saturated |

**Where the proxy holds the rate, the overhead is inside B4's provisional budget** (p99
under 20 ms for routing plus audit, under 100 ms with redaction): 6.0 ms and 7.4 ms at 50
requests a second. **Where it does not hold the rate, there is no overhead to report.** One
proxy process holds 200 requests a second on routing alone, not 500, and holds 50 with the
audit chain on, not 200. The cells stopped after two saturated runs each, as the method says,
which is why they count 4,000 requests rather than 10,000.

**What "failed" is.** Almost all are dropped iterations: k6 had every one of its virtual users
(four times Little's law for a 50 ms upstream) waiting on the proxy, so it could not start
the next request on time. The rest, at 500 requests a second, are requests with no answer in
30 s. None was a 4xx or 5xx. The mock alone never dropped one at any rate, so the limit is
the proxy's, not the client's.

**Why, as far as measured.** Every ledger write and every audit append is a synchronous SQLite
commit on the event loop. On this host a commit costs more than on the laptop, because the
laptop's `fsync` does not reach the disk and the VPS's does (1,000-byte row, 300 commits):

| Journal mode, `synchronous` | VPS p50 | VPS p99 | Laptop p50 |
|---|---|---|---|
| rollback journal (the audit log), FULL | 1.06 ms | 1.49 ms | 0.21 ms |
| WAL (the ledger), FULL | 0.52 ms | 0.62 ms | 0.05 ms |
| WAL, NORMAL | 0.01 ms | 0.04 ms | 0.01 ms |

A call is at least three commits: the ledger row before the call, the ledger row after it, and
the audit record. At 200 requests a second the audit append alone holds the loop for about
a fifth of each second, before any of the proxy's own work. That fits audit being where the
proxy stops holding 200, but it is a hypothesis, not a profile. The fix and its measurement
are the next step. Two candidates: move the commits off the loop, and group-commit the audit
record on a short timer instead of one commit per call. Either one changes what the budget
says, so the table above stays as the first measurement, as B4 says it should.

**The Python client agrees where it can hold the rate.** The same grid with the open-loop
Python client, earlier the same day on boundary 0.25.0 (`bench/loadtest-vps-python.json`):
routing 3.1 (3.1 to 3.2) at 50 and 4.1 (4.0 to 4.1) at 200, audit 4.8 (4.7 to 4.9) at 50,
redaction 6.4 (6.3 to 6.5) at 50, all p50. At 500 it fell behind itself, up to 824 ms at p99.
Its figure at 200 is 0.6 ms above k6's, which is what a client that times from the schedule
while competing with the proxy for the same cores should show.

**What the first VPS run found in the harness.** The Python run labelled audit at 200
requests a second generator-bound, although its client never fell more than 1.3 ms behind:
the proxy failed 9,386 of 10,000 requests, and a run with no successful latency was
discarded as if the client had lagged. That blamed the client for the proxy. A cell whose
proxy fails more than 1% of a run's requests while the client keeps up is now `saturated`
(`Cell.saturated`, with a test built from that run), and says so in the table.

**One observation not reproduced.** In a k6 run before the failure breakdown was added,
redaction at 50 requests a second had 13 failed requests of 2,502, under the 1% threshold. It
was the first cell after the audit layer's collapse at 500. A three-run rerun and the
published run both had 0. The 13 were never classified, so the cause is unknown.

**The laptop's pattern does not survive.** On the laptop, overhead was lower at 200 requests
a second than at 50 in every layer. On the VPS, routing is 3.1 at 50 and 3.5 at 200. That is
consistent with the power-management explanation offered below, but nothing here tests it.

## Development run

2026-09-25, boundary 0.19.0, on the development laptop (MacBook Pro, arm64, 10 cores,
16 GB), `boundary loadtest --levels 50,200,500 --runs 5 --duration 10`, stored in
`bench/loadtest-dev.json`. Median overhead per run and its bootstrap interval over runs, in
milliseconds:

| Layer | 50 rps p50 | 50 rps p99 | 200 rps p50 | 200 rps p99 | Errors |
|---|---|---|---|---|---|
| routing | 2.2 (2.1 to 2.3) | 3.1 (2.7 to 3.5) | 0.3 (0.3 to 0.4) | 0.9 (0.0 to 2.4) | 0 of 12,500 |
| + audit | 2.1 (2.0 to 2.1) | 3.5 (2.9 to 4.2) | 0.5 (0.5 to 0.5) | 1.7 (1.1 to 2.1) | 0 of 12,500 |
| + redaction | 2.6 (2.5 to 2.6) | 4.0 (2.6 to 5.4) | 1.0 (0.9 to 1.0) | 2.7 (1.8 to 3.9) | 0 of 12,500 |

At 500 rps every layer is **generator-bound**: the client sent requests up to 800 ms late, so
those cells measure the client and are reported as such, not as numbers.

**What it says, as far as a laptop can.** The proxy's own cost is a few milliseconds against
a 50 ms upstream, redaction adds about half a millisecond at the median over the audit layer
at both rates, and nothing errored. **One pattern is unexplained**: overhead is lower at 200 rps than at 50, in
every layer and in both runs taken. A plausible cause is the laptop's power management, which
parks cores between requests at the lower rate and adds wake-up time that the proxy's
two-hop path pays twice; nothing here tests that, and the VPS run will say whether it
survives on a machine that does not sleep.

## What building it found

- **The client is the limit before the proxy is.** A Python client on one core cannot hold
  500 requests a second. The first full attempt at that level spent 26 minutes of CPU and
  never finished. The harness now measures its own lag, abandons a cell once it falls a second
  behind, and reports a cell as generator-bound unless at least three runs were clean. B4
  named k6 for the VPS run; this is why that matters.
- **One stall is the machine, not the cell.** Early runs lost whole cells to a single 100 to
  500 ms pause in one run of five. Such a run is now dropped and counted in the output, and
  the cell stands on the clean runs.
- **The spend-cap check scanned the whole ledger on every call.** Reading the query while
  looking for why overhead rose from run to run in one cell found that `spend_usd` could use
  no index, so its cost grew with the ledger: 1.5 ms per call at 10,000 rows, 19 ms at 100,000,
  202 ms at a million (`boundary bench --spend`). Ledger v9 keeps a spend table by trigger and
  the check is now 0.005 ms at any size (docs/ledger.md). The rise that prompted the reading
  was mostly the laptop; the scan was real regardless, and would have dominated a busy month.
- **Three faults in the harness itself**, each caught by a run that produced nothing: a mock
  whose handler FastAPI read as taking a query parameter, because its type was imported
  inside the function; a generator-bound rule that counted "no overhead recorded yet" and so
  stopped every cell after one empty run; and a proxy whose graceful shutdown outlasted the
  harness's wait and took the finished cells' results with it. Each has a test or a guard.
