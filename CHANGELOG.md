# Changelog

Versions follow the plan's handover table (PLAN.md section 7). Interface changes within a
major version are additive only; see docs/interface.md.

## Unreleased (0.2.0, October 2026)

- **Ledger schema v2, additive.** Two columns: `call_uid`, a uid minted in the process that
  makes the call, and `env`, which environment made it. A v1 file is upgraded in place the
  first time this version opens it, with `call_uid` backfilled from each row's own contents
  so that two copies of one v1 file still merge to one row per call. No value a call
  recorded is changed, and no column moved.
- **`boundary ledger merge --into <dest> <sources...>`.** Combines one ledger per
  environment into a central file, matching on `call_uid` and never on `id`. Idempotent:
  merging the same source again inserts nothing, which is what makes "run it again" the
  answer to a merge that failed. A row held as `in_flight` is completed when the source has
  since completed it, so a central file settles on actual costs; a completed row is never
  reverted by an older copy. One source is one transaction. `--dry-run` reports without
  writing.
- **`env` on the gateway and in the configuration.** `Gateway(..., env=...)`, else
  `BOUNDARY_ENV`, else `ledger.env` in `boundary.yaml` (default `local`). Written to every
  ledger row and carried on the span as `boundary.env`. `ledger report` groups by it.
- **Rule C: `docs/rejected.md`.** A central ledger written over the network, measured
  against local-first plus merge over 100 runs with an outage in each, by
  `boundary experiment remote-ledger`. The strict remote design finished none of the runs;
  the best-effort one finished them all and left 8.8% of the calls it had paid for with no
  record, and made 981 calls with no cap check, because the cap cannot be checked when the
  host holding the totals is unreachable. Local-first lost nothing. The table in the doc
  and the figures in the README are filled from `bench/remote-ledger.json` and a test
  fails if they drift.
- The mock upstream and the request corpus behind `boundary bench` moved to
  `boundary/_mock.py` so the experiment measures the same code path against the same
  corpus. The three deterministic bench numbers are unchanged by the move.
- Version is `0.2.0.dev0` until the 0.2.0 tag, so a ledger row says which library wrote it.

Still deferred to the 0.2.0 tag: Foundry, Bedrock and Vertex adapters; Anthropic Message
Batches; local OpenAI-compatible hosts at price zero. OTLP export arrives with Part B.

## 0.1.0 (2026-09-10)

First version of the `boundary` library, Part A of the Compliant AI Gateway. Tagged after
one live call per provider succeeded and was costed (run from GitHub Actions on
2026-09-10: Anthropic, OpenAI, Google, Together; ten calls in all, US$0.0009).

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
  OpenAI so a reasoning model produces text within the smoke budget (the gpt-5.4 family
  wants `none` instead; the value belongs to the caller, not the library).
- Gemini `extra` fields under `generationConfig` merge into the built one instead of
  replacing it, so a caller can fix `thinkingConfig` without losing `maxOutputTokens`.
  Found when `gemini-flash-latest` (resolving to `gemini-3.8-flash`) spent a 64-token
  budget on thinking and returned no text.
- TLS verification against the operating system trust store.
- Price files 2026-09-07 (Anthropic list prices from the plan) and 2026-09-09 (Anthropic,
  OpenAI, Google and Together, copied from their price pages).
- Command line: `boundary smoke`, `routes show`, `prices check`, `ledger report`, `bench`.
- Measured against an in-process mock (`boundary bench`): overhead p50 and p95 with
  bootstrap intervals, ledger completeness under injected faults including a kill
  mid-call, cap enforcement, pass-through fidelity. Results in the README and
  `bench/results.json`.

Deferred to 0.2 (October 2026): Foundry, Bedrock and Vertex adapters; Anthropic Message
Batches; `ledger merge` (done, see above); OTLP exporter arrives with Part B.
