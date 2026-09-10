# Changelog

Versions follow the plan's handover table (PLAN.md section 7). Interface changes within a
major version are additive only; see docs/interface.md.

## 0.1.0 (unreleased; tag after one live call per provider)

First version of the `boundary` library, Part A of the Compliant AI Gateway.

- Adapters over raw HTTP with pinned API version headers, no vendor SDKs: Anthropic
  Messages, OpenAI-compatible chat completions (OpenAI, Together, local servers) and
  Gemini generateContent. Golden request and response tests including error shapes.
- Routing by configuration: aliases redirected by one line in the routes file; explicit
  `provider/model-id` never redirected.
- Two modes. Standard retries 429, 5xx and timeouts with backoff and `Retry-After`, and
  can fill defaults. Pass-through never retries, never caches, never rewrites, requires
  an explicit model and `max_tokens`, and records request and response bytes plus headers
  to a caller-owned raw store; byte equality and single-upstream-call tests.
- Ledger, schema v1, in SQLite. Two-phase rows: inserted before the request leaves with
  the pessimistic estimate, completed with the actual. Cost only from returned usage and a
  dated price file; unknown prices write uncosted rows; `strict_cost` raises instead.
- Spend caps per project month, per run and per portfolio, refused before any request.
- One OpenTelemetry span per call with an attribute allow list enforced in code and no
  content; console exporter. Trace and span ids written to the ledger row.
- Exact-match development cache, standard mode only.
- Live calls from GitHub Actions on 2026-09-10 (the laptop's network inspects TLS): Anthropic,
  OpenAI and Together answered; Google refused `gemini-2.5-flash-lite` as closed to new
  users, so the smoke default is `gemini-3.5-flash-lite`. OpenAI returned a dated id for an
  undated request, which the price list did not know: the ledger now prices by the returned
  id and falls back to the requested id, never further. Price file `2026-09-10.yaml` adds
  the current Gemini and GPT lines. The smoke call sends `reasoning_effort: minimal` to
  OpenAI so a reasoning model produces text within the smoke budget.
- TLS verification against the operating system trust store.
- Price files 2026-09-07 (Anthropic list prices from the plan) and 2026-09-09 (Anthropic,
  OpenAI, Google and Together, copied from their price pages).
- Command line: `boundary smoke`, `routes show`, `prices check`, `ledger report`, `bench`.
- Measured against an in-process mock (`boundary bench`): overhead p50 and p95 with
  bootstrap intervals, ledger completeness under injected faults including a kill
  mid-call, cap enforcement, pass-through fidelity. Results in the README and
  `bench/results.json`.

Deferred to 0.2 (October 2026): Foundry, Bedrock and Vertex adapters; Anthropic Message
Batches; `ledger merge`; OTLP exporter arrives with Part B.
