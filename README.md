# Compliant AI Gateway

Lets a bank, insurer, hospital or government department use frontier AI models without
personal data ever leaving the boundary, with a tamper-evident record of every call and a
hard cap on every team's spend. The blocker most regulated organisations cite for AI
adoption, removed, with the latency overhead measured and published rather than promised.

**Status: Part A in progress since 2026-09-07.** Nothing has been measured yet; the interface
draft is in [docs/interface.md](docs/interface.md) and freezes on Sep 9. Both parts are planned
in [PLAN.md](PLAN.md):
Part A, the `boundary` library that every project in this portfolio calls models through,
built Sep 7 to 13 2026; Part B, the full gateway with redaction, residency routing, audit
log, cache, budgets and the portfolio-wide observability dashboard, built May 2027.

## Result

Not yet measured. Part A fills the first table in September 2026; Part B fills the second
in May 2027.

**Library (Part A)**

| Overhead p50 / p95 (95% CI) | Ledger completeness under fault injection | Ledger vs invoice, monthly | Calls past a spend cap | Pass-through byte fidelity |
|---|---|---|---|---|
| _not yet_ | | | | |

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

## How it works

See [PLAN.md](PLAN.md). Part A: a Python library with raw-HTTP adapters for Anthropic,
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

One of ten projects built over twelve months. This one is the plumbing the others share:
every model call in the portfolio goes through it, the release-gate project measures the
quality cost of its redaction, and the access-to-information redaction project builds on
its redaction engine.

## How this was built

Design, methodology, evaluation choices and judgement are Peter Parker's. AI coding
assistants (Claude Code) were used for implementation and drafting, the way a senior
engineer uses them in 2026. Every number in the results tables is reproducible from this
repository with one command, and that reproducibility is the evidence that matters.
