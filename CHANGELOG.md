# Changelog

Versions follow the plan's handover table (PLAN.md section 7). Interface changes within a
major version are additive only; see docs/interface.md.

## Unreleased (0.2.0)

- **Anthropic Message Batches.** `Gateway.batch_submit(requests, purpose=, run_id=)` returns
  a `BatchHandle`; `batch_results(handle, wait_s=, poll_s=)` completes the rows;
  `batch_status(handle)` asks after one without changing anything; `batch_handle(batch_id)`
  rebuilds a handle from the ledger so a batch can be collected by a process that did not
  submit it. Raw HTTP under the pinned `anthropic-version`, no beta header, because batches
  are generally available; a vendor that later wants one takes it from `headers` on the
  provider entry. Standard mode only, and the development cache is never consulted, because
  a cache hit inside a batch would make a row say a request was billed when it was not. The
  `custom_id` sent per request is the row's `call_uid`, so a result maps back to exactly one
  row whatever order the results file is in, and the params of a batched request are byte
  for byte the body a single call would have sent. Brought forward from November: project 02
  built its public-data half on 2026-09-11, seven weeks early, and its own-run panel was the
  thing waiting.
- **Ledger schema v3, additive: `batch_id`.** One nullable column, null for every ordinary
  call. A batch is submitted in one process and collected in another, so the rows written at
  submit have to be findable again by something the vendor also knows. The migration now
  runs one version at a time, so a uid is invented only for a file that predates the column:
  a null `call_uid` in a v2 or later file was put there by hand, and inventing one would let
  the same call merge twice.
- **Batch accounting.** One row per request, written before the submit leaves the process,
  in flight and carrying the estimate at the batch rate, because the vendor bills for every
  request the moment it accepts the batch. The caps are checked once for the whole batch: a
  vendor does not accept half of one. A refused submit completes every row as a failure with
  no cost; an accepted submit whose body cannot be read leaves the rows in flight at their
  estimate, because recording billed work as failed would understate the month. At result
  time each row is completed from the returned usage at the price entry's
  `batch_multiplier`, and an entry with no batch rate leaves the row uncosted rather than
  costed at the full rate. A request the results file never mentions is completed as
  `batch_missing`.
- **Merging never writes to a source.** `ledger merge` used to upgrade a v1 source in place,
  and an older library then refuses to write to the upgraded file by design, so merging an
  environment's ledger would have stopped that environment appending to it. Project 03 pins
  0.1.0 and commits one ledger per arm per month, and the monthly invoice check merges
  exactly those files. The rows are now read from a temporary copy and the copy is what gets
  upgraded; a test asserts the source's bytes are unchanged and that it is still readable and
  writable by the version that wrote it. A v1 source still merges to the same uids, because
  the backfill derives them from the rows rather than inventing them.
- **A ledger that fails to open no longer leaks the file handle**, which on Windows turned a
  clear error about one file into a confusing one about another.
- **The batch results URL is checked against the configured host** before the request that
  fetches it is sent, because that request carries the API key.
- **A spend cap for project 02** in `config/caps.yaml`, so its first own-run call is admitted
  or refused by its own line rather than by the US$10 default. Provisional amounts, to be
  confirmed before that call.
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

- **A local OpenAI-compatible host, exercised live.** Ollama 0.34.0 with `llama3.2:3b` on
  the laptop, answering through the `local` provider entry at price zero: ledger row 5,
  status 200, 32 input and 2 output tokens, `costed = 1` and cost 0.00, uncosted count
  still zero. It is the one live call that can be made from a network that inspects TLS,
  because nothing leaves the machine. `boundary smoke local` now has that model as its
  default.
- **`boundary smoke <provider> --batch`** submits two short requests as a real vendor batch
  and collects them, so the batch path can be exercised live the way a single call already
  could. `--wait` and `--poll` control how long it will sit there.
- **A `smoke` workflow in this repository**, manual only, running one call per vendor and
  the batch path on GitHub's runners, because the laptop's usual network inspects TLS and a
  vendor call from there would hand a personal key and a prompt to the employer's proxy. It
  needs the four vendor keys as repository secrets before it will do anything; until now
  those lived only on the release-gate repository.
- A CLI test reached a real local server and wrote to this repository's own ledger once
  `smoke local` gained a default model. It no longer does, and the test that covers "a
  provider with no default model is refused" now uses one that really has none.

Still deferred past the 0.2.0 tag, to 0.2.1 and 0.2.2: the Foundry, Vertex and Bedrock
adapters, which need their accounts and billing alerts first and which nothing is waiting
on. OTLP export arrives with Part B.

Before the 0.2.0 tag: one live batch and one live local call, both recorded in the ledger.
Version stays at `0.2.0.dev0` until then, as `v0.1.0` did until its smoke calls ran.

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
