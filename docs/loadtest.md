# The load test

`boundary loadtest` (0.19) is PLAN.md B4's layered load test, built before the VPS it is
meant to run on so that the method is fixed, tested and exercised before the number that
matters is taken. **The overhead budget in the README comes from the VPS run only**, stated
with the VPS size, and nothing below is that number.

```
uv sync --extra server
boundary loadtest                                   # 3 layers x 50, 200, 500 rps x 5 runs x 10 s
boundary loadtest --machine "VPS, 2 vCPU, 4 GB" --published --out bench/loadtest.json
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
- **Layers, cumulative**: `routing` (team key, policy, routing, ledger; public data; no audit
  chain), `audit` (plus the chain appended as the proxy answers), `redaction` (no header, so
  personal: redacted before it leaves, rehydrated after). The cache, B4's fourth layer, does
  not exist yet.

The request is about a page of text naming a person, an email address, a phone number and a
file number, so the redaction layer has real work to do and the echo has a realistic size.

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
