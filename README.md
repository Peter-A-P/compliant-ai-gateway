# Compliant AI Gateway

Lets a bank, insurer, hospital or government department use frontier AI models without
personal data ever leaving the boundary, with a tamper-evident record of every call and a
hard cap on every team's spend. The blocker most regulated organisations cite for AI
adoption, removed, with the latency overhead measured and published rather than promised.

**Status: Part A released as `v0.1.0` on 2026-09-10**, after one live call per provider. The interface is frozen
([docs/interface.md](docs/interface.md)); the library table below is measured. **`v0.2.0` followed on 2026-09-11**: ledger merge
across environments, Anthropic Message Batches collected at half price, and merging that no
longer writes to the ledgers it reads. Every provider and the batch path were exercised
live, and a local model server answers at price zero. Still to come are the adapters for the
three hyperscaler model platforms. Both parts are planned in [PLAN.md](PLAN.md):
Part A, the `boundary` library that every project in this portfolio calls models through,
built Sep 7 to 13 2026; Part B, the full gateway with redaction, residency routing, audit
log, cache, budgets and the portfolio-wide observability dashboard, built May 2027.

## Result

Part A's table is measured against an in-process mock; the live-call column fills from October.
Part B fills the second table in May 2027.

**Library (Part A)**

| Overhead p50 / p95 (95% CI) | Ledger completeness under fault injection | Ledger vs invoice, monthly | Calls past a spend cap | Pass-through byte fidelity |
|---|---|---|---|---|
<!-- bench:start -->
| 2.61 ms (2.58 to 2.64) / 4.62 ms (4.42 to 4.97), n = 1,000 | 600/600 = 100.0% across ok, 500, 400, timeout, malformed and a kill mid-call, both modes (100 rows left in flight by the kills, as designed) | _from October_ | 17 attempted, 0 reached the upstream | 500/500 = 100% |
<!-- bench:end -->

Filled by `boundary bench --write-readme` against an in-process mock upstream; the raw
results are in `bench/results.json`. Overhead is the library's own cost per call on the
machine that ran it: resolving, building, cap checks, two ledger writes, telemetry and
parsing, with the upstream answering instantly.

**Gateway (Part B)**

| Layer | Load (rps) | Overhead p50 / p95 / p99 ms (95% CI) |
|---|---|---|
| _not yet_ | | |

| Redaction precision / recall by entity (95% CI) | Rehydration fidelity | Quality cost of redaction (non-inferiority delta) | Residency violations | Cache hit rate / false-hit rate / saved | Audit tamper detection |
|---|---|---|---|---|---|
| _not yet_ | | | | | |

## What this does not do

- It does not classify data. The caller declares a data class on every request, and the
  gateway enforces the policy for that class. A gateway that guessed classifications would
  be making a compliance decision nobody reviewed.
- It does not run in more than one region. Residency here means controlling where requests
  are allowed to go, not where the proxy runs.
- It does not redact images or audio. Text only; non-text content is refused for anything
  but the public class.
- Redaction is not perfect, and the results table says by how much, per entity type.

## What did not work

A central ledger written over the network, instead of one file per environment combined by
`boundary ledger merge`. It would have closed a real gap: until a merge runs, the portfolio
spend cap is checked against one machine's view. Measured over 100 simulated runs with a
network outage in each (`bench/remote-ledger.json`), the central design either stopped
every one of them part way, or finished and left 8.8% of the calls it had already paid for
with no record at all, and made 981 calls with no cap check at all, because the cap cannot
be checked when the host holding the totals is the thing that is unreachable. Local-first
lost nothing. The evidence, the method and what was kept from the idea are in
[docs/rejected.md](docs/rejected.md), reproducible with
`boundary experiment remote-ledger`.

## How it works

In plain language: [docs/explained.md](docs/explained.md). In full: [PLAN.md](PLAN.md). Part A: a Python library with raw-HTTP adapters for Anthropic,
OpenAI, Google, OpenAI-compatible hosts and the three hyperscaler model platforms
(Microsoft Foundry, Amazon Bedrock, Vertex AI); routing by a configuration file; one
OpenTelemetry span and one cost-ledger row per call, computed from returned usage and a
dated price list; spend caps enforced before the call; a pass-through mode with no
retries, no cache and no rewriting, verified by byte-equality tests, for measurements
that must not be confounded. Part B: an OpenAI-compatible proxy on the same library with
reversible PII redaction, residency routing by declared data class, a semantic cache with
its false-hit rate measured, per-team budgets, a hash-chained audit log anchored daily in
this repository, a layered load test, and a read-only dashboard of every project's calls
and costs.

## Part of a portfolio

One of fifteen projects built over twelve months. This one is the plumbing the others share:
every model call in the portfolio goes through it, the release-gate project measures the
quality cost of its redaction, and the access-to-information redaction project builds on
its redaction engine.

## How this was built

Design, methodology, evaluation choices and judgement are Peter Parker's. AI coding
assistants (Claude Code) were used for implementation and drafting, the way a senior
engineer uses them in 2026. Every number in the results tables is reproducible from this
repository with one command, and that reproducibility is the evidence that matters.
