# Plan: Compliant AI Gateway

**Written:** 2026-09-06. **Status (2026-09-11):** Part A released as `v0.1.0` on 2026-09-10, three days ahead of the Sep 13 target, after one live call per provider. Version 0.2 is in progress on `main` (section 5.1). Part B is unchanged, May 2027.

Two parts, one repository, one Python package called `boundary`:

| Part | What | When | Section |
|---|---|---|---|
| **A. Version 0, the library** | Every model call in the portfolio goes through one library: provider adapters over raw HTTP, routing by configuration, an OpenTelemetry span and a cost record per call, spend caps, and a pass-through mode the drift runs can trust | One week, Sep 7 to Sep 13 2026, tagged `v0.1.0`; small follow-ups in October as projects need them | Part A |
| **B. The full gateway** | An OpenAI-compatible proxy on top of the library: reversible PII redaction, residency routing by data classification, semantic cache, prompt-injection screening, per-team budgets, a hash-chained audit log, a published latency-overhead budget, and the portfolio-wide observability dashboard at gateway.peterparker.ca | Three weeks, May 3 to May 23 2027, tagged `v1.0.0` | Part B |

Part A exists because the model landscape changes monthly and fifteen projects calling vendors
directly would mean ten places to change when a model is retired, repriced or replaced.
It is built first because the 03 drift runner is written against it from Sep 16 and the
first official drift run is Sep 27. Part B is the compliance layer regulated buyers
actually ask about, built with its overhead measured rather than promised.

**Fed by:** 03 (measurement; the gate measures the quality cost of redaction in Part B).
**Feeds:** every project that calls a model, from September 2026 (01 does not call models;
02, 03, 05, 06, 09, 10, 13, 14 do); 07 imports the redaction engine from Part B.

---

# Part A. Version 0: the library

## 1. What this produces

One importable library, `boundary`, that every project in the portfolio uses for every
model call, so that a model can be swapped in one file, every call has a trace and a cost
record, and no project can overspend its line without the library refusing first.

The numbers a stranger can check:

| Number | What it shows |
|---|---|
| Library overhead per call, p50 and p95 in milliseconds, against a local mock upstream, 1,000 calls, 95% bootstrap CI | The plumbing costs nothing worth arguing about |
| Ledger completeness under fault injection: cost records written divided by calls attempted, across upstream 500s, timeouts, malformed responses and a process kill mid-call | No call escapes the ledger |
| Ledger against invoice: monthly difference between the ledger's cost and each vendor's billing console, in percent, recorded in the portfolio's monthly budget review | The cost figures every other project reports are believable |
| Spend cap enforcement: calls attempted past a cap, and calls that reached the vendor past it (must be zero) | The cap is a cap |
| Pass-through fidelity: share of requests where the bytes sent to the vendor equal the bytes the caller built, on a corpus of 500 recorded requests (must be 100%) | The gateway cannot be a confound in the drift record |
| Redirect test: a one-line change to the routes file moves every call of an alias to a different model, shown in the ledger | The reason the library exists |

## 2. Design decisions

### 2.1 Library first, proxy second

Version 0 is a Python package, not a server. A server in September would put a deployment
on the critical path of the drift runs and add a network hop the drift record would have to
account for. A library is a dependency pin. The proxy arrives in Part B and is built on
the same package, so there is never a second code path.

### 2.2 Raw HTTP with pinned headers, no vendor SDKs

Every adapter builds the request body itself and sends it with a pinned `httpx` client and
pinned API version headers. Vendor SDKs change defaults (retries, timeouts, headers,
beta flags) between minor releases, and the 03 plan requires that an SDK release can never
change a request. The one exception is request signing: `botocore`'s SigV4 signer for
Bedrock and `google-auth` for Vertex tokens, used for the signature and the token only;
bodies are still built here. The exception is recorded in the README.

### 2.3 Two modes, and pass-through is inviolable

| Mode | Retries | Cache | Rewriting | Raw bytes stored | Used by |
|---|---|---|---|---|---|
| `standard` | On 429, 5xx and timeouts, exponential backoff with jitter, at most 4, honours `Retry-After` | Optional exact-match development cache | Alias resolution, defaults filled in | No | 01 to 14 in development and normal use |
| `passthrough` | None; an error is a result | Never | None; provider and model identifier are explicit, aliases are refused | Request and response bytes, and response headers, written to a caller-owned raw store | 03 drift runs; anything that is a measurement |

Pass-through is enforced by tests, not by convention: a test asserts byte equality between
what the caller built and what left the process, and another asserts that a pass-through
call with a cache configured makes exactly one upstream request.

### 2.4 The ledger is the product

Every call writes one row to a ledger before the response is returned to the caller,
including failed calls. The ledger is local-first: a SQLite file per environment (the
laptop, a GitHub Actions runner), merged by a CLI command, because a remote database is a
network dependency the Actions runner should not have on the drift-run path. Part B adds
central ingestion into Postgres on the VPS; the row schema does not change.

Cost is computed from the token counts the vendor returns and a versioned price list in
the repository, `config/prices/<date>.yaml`, and every row carries the price list version
it was costed with. If the returned model identifier has no price, the row is written
with `costed = false` and the call counts toward an "uncosted calls" figure that should be
zero; the library never guesses a price. Repricing is a new price file, never an edit.

### 2.5 Caps in the library are the second line, not the first

Hard spend caps in every vendor console come first, set before the first call. The library
adds a per-project monthly cap, a per-run cap, and a portfolio monthly cap in
`config/caps.yaml`. Before each call it sums the month's ledger for the project and adds a
conservative estimate of this call (input characters divided by 3.5 times the input price,
plus `max_tokens` times the output price); if the total would exceed the cap the call is
refused with `SpendCapExceeded` and no request is made. After the call the actual cost
replaces the estimate. The estimate is deliberately pessimistic; a refused call is cheap
and an unrefused overspend is not.

### 2.6 Aliases are for projects that want redirecting; identifiers are for projects that must not be

`config/routes.yaml` maps an alias such as `fast` or `judge` to a provider, a model
identifier, an API version and a region. A caller that passes an alias is redirected when
the file changes. A caller that passes `provider/model-id` explicitly is never redirected,
and pass-through mode requires the explicit form: the 03 plan treats model identifiers as
part of the data, and a routing change would be a change to the data.

### 2.7 No content in telemetry

Spans carry provider, model requested and returned, token counts, cost, latency, status,
project, purpose and mode, following the OpenTelemetry generative-AI semantic conventions
where an attribute exists. Prompts and completions are never attached to a span. Request
and response bodies are stored only in pass-through mode, only in a raw store the caller
owns (03 commits its own), and only because the drift record needs them.

### 2.8 Out of scope in version 0, on purpose

- A server or proxy of any kind (Part B).
- Redaction, policy routing, semantic cache, audit chain, quotas by team (Part B).
- Streaming responses. Nothing in the portfolio needs streaming before May; the proxy
  adds it in Part B and the library gets it then.
- Typed tool calls. Tool-use fields pass through untouched inside the request body; typed
  support arrives in `v1.x` when 10, 13 and 14 need it (July 2027 onward).
- Embeddings endpoints. 05 runs embeddings locally; if a project needs a hosted embedding
  it goes through the raw escape hatch, traced and costed.
- Any attempt to classify or inspect content. Version 0 moves bytes and counts money.

## 3. Interface (frozen Sep 9)

```python
from boundary import Gateway, ChatRequest, Mode

gw = Gateway.from_config("config/boundary.yaml", project="ai-release-gate")

resp = gw.chat(
    ChatRequest(
        model="anthropic/claude-haiku-4-5-20251001",   # explicit; or an alias such as "fast" in standard mode
        system="Answer with the letter only.",
        messages=[{"role": "user", "content": "..."}],
        max_tokens=8,
        temperature=0.0,
        stop=None,
        extra={},                                      # vendor-specific fields, passed through verbatim
    ),
    purpose="drift-run",
    run_id="2026-09",
    mode=Mode.PASSTHROUGH,
)

resp.text              # first text block, or None on error
resp.finish_reason
resp.usage             # input, output, cache_read, cache_write token counts
resp.cost_usd          # None when costed is False
resp.costed
resp.model_returned    # the identifier the vendor reported
resp.latency_ms
resp.status            # HTTP status, or the error type
resp.headers           # response headers, for the drift record
resp.raw               # parsed response body
resp.ledger_id         # row id, so a caller can join its own records to the ledger
```

`await gw.achat(...)` is the async form; both share one adapter implementation. Concurrency
is the caller's business. `gw.raw(provider, method, path, json, mode=...)` is the escape
hatch for a vendor feature the library does not model: it is traced, costed when the
response carries usage in a shape the adapter knows, and otherwise written as uncosted.

Errors: `SpendCapExceeded`, `UnknownAlias`, `UnknownPrice` (raised only when the caller
asks for strict costing), `ProviderError` (status, body, retry count), `PassthroughViolation`
(alias or cache requested in pass-through mode).

The interface is frozen on Sep 9 so the 03 runner (written Sep 16 to 19) and the 02
runner (November) can be written against it. Changes after that are additive and versioned.

## 4. Architecture

```
boundary/
  __init__.py      Gateway, ChatRequest, ChatResponse, Mode, errors
  config.py        boundary.yaml loader (routes, prices, caps), pydantic-validated
  routes.py        alias resolution; explicit provider/model form; pass-through refuses aliases
  providers/       base.py (Adapter protocol: build_request, parse_response, parse_usage, parse_error)
                   anthropic.py        Messages API, anthropic-version pinned; batches in v0.2
                   openai_compat.py    chat completions; also every OpenAI-compatible host: the
                                       open-weights provider for 03's control arm, local llama.cpp
                                       or Ollama servers for 02 at price zero
                   google.py           Gemini API generateContent, version pinned
                   azure_foundry.py    Claude on Microsoft Foundry (v0.2)
                   aws_bedrock.py      Claude on Amazon Bedrock, SigV4 via botocore signer (v0.2)
                   gcp_vertex.py       Claude or Gemini on Vertex AI, token via google-auth (v0.2)
  transport.py     pinned httpx client, timeouts (connect 10 s, read 120 s), retry policy (standard only)
  ledger/          schema.sql (v1; v2 in 0.2 adds call_uid and env), store.py (SQLite, one row
                   per call, written before return; merge_from combines another environment's
                   file, keyed on call_uid, idempotent),
                   prices.py (versioned price files, cost arithmetic incl. cache and batch rates)
                   Revised 2026-09-10: merge and report were planned as ledger/merge.py and
                   ledger/report.py. Merge needs the schema, the connection and the same
                   insert path as a write, so it is a method on the store rather than a
                   module that reaches into it; report is a projection for the operator, so
                   it lives in cli.py with the other commands.
  caps.py          monthly, per-run and portfolio caps; pre-call estimate; post-call actual
  telemetry.py     one span per call, GenAI semantic-convention attributes, no content;
                   exporters: none, console, OTLP (OTLP used from Part B)
  cache.py         exact-match development cache keyed by sha256 of the canonical request; standard mode only
  rawstore.py      pass-through request and response bytes plus headers, JSONL, caller-owned path
  cli.py           boundary smoke <provider> | routes show | prices check | ledger report |
                   ledger merge | bench | experiment remote-ledger
  _mock.py         in-process upstream and request corpus shared by bench and experiment
  experiment/      the Rule C measurements behind docs/rejected.md
config/
  boundary.yaml    routes and defaults
  prices/2026-09-07.yaml
  caps.yaml
tests/
  mock upstream (respx) per provider with golden requests and responses, including error shapes
  test_passthrough.py   byte equality; exactly one upstream call; aliases refused
  test_ledger.py        completeness under fault injection (500, timeout, malformed body, SIGKILL
                        simulated by writing before return)
  test_merge.py         merge idempotence, in-flight rows completed by a later merge, the v1
                        upgrade, and a failed merge leaving the destination untouched
  test_experiment.py    the claims on docs/rejected.md, at a size CI can afford
  test_caps.py          refusal at the cap with zero upstream calls; estimate is never below actual on the golden corpus
  test_prices.py        cost arithmetic against each vendor's published pricing examples, to the cent
  test_adapters.py      request builder and response parser per adapter against goldens
  test_routes.py        alias change moves the model in the ledger
docs/
  interface.md     the frozen interface, with the version it changed in
  ledger.md        the row schema, field by field, and how merge matches rows
  explained.md     the library in plain language
  rejected.md      Rule C: the central remote ledger, and the numbers that rejected it
```

### Ledger row (schema v1; v2 in 0.2)

`id, ts_utc, boundary_version, project, purpose, run_id, mode, provider, alias,
model_requested, model_returned, region, input_tokens, output_tokens, cache_read_tokens,
cache_write_tokens, price_list, cost_usd, costed, cached, latency_ms, http_status,
error_type, retries, request_sha256, response_sha256, trace_id, span_id, raw_path`

Schema changes are additive only. A column is never renamed or removed.

v2 (2026-09-10, with `ledger merge`) adds `call_uid` and `env`. The plan did not name them
because it did not say how merge would identify a row across files. `id` cannot: it is
per file, so every environment holds an id 1 for a different call. `call_uid` is minted in
the process that makes the call, which is what lets merge be idempotent; `env` says which
environment a row came from, and therefore which machine's raw store its `raw_path` points
into. A v1 file is upgraded in place on open.

### Adapters in version 0

| Provider | API and pin | Auth | Version |
|---|---|---|---|
| Anthropic | Messages API, `anthropic-version: 2023-06-01`; Message Batches API in v0.2 for 02's half-price runs | API key from env | v0.1 |
| OpenAI | Chat completions. Chosen over the Responses API because the same adapter serves every OpenAI-compatible host; a Responses adapter is deferred until a project needs a feature only it has | API key from env | v0.1 |
| Google | Gemini API `generateContent`, `v1beta` pinned | API key from env | v0.1 |
| OpenAI-compatible hosts | The open-weights provider that serves 03's control arm; local llama.cpp or Ollama for 02, price file entry zero | Per host | v0.1 |
| Microsoft Foundry | Claude on Foundry. Request shape and endpoint verified against the current documentation in v0.2 week; Anthropic Messages shape expected | Azure key from env | v0.2 |
| Amazon Bedrock | Claude via InvokeModel with the Anthropic Messages body; SigV4 signing through the botocore signer only | AWS credentials from env | v0.2 |
| Vertex AI | Claude via rawPredict or Gemini via generateContent; OAuth2 token via google-auth | Service account from env | v0.2 |

The three hyperscaler adapters are the portfolio's one example of each ecosystem's model
platform. They wait for v0.2 because they need accounts with billing alerts first, and
nothing in September needs them. Each is exercised at least once
with the call recorded in the ledger; that is a definition-of-done item.

## 5. Week by week

| Dates | Built | Done when |
|---|---|---|
| Sep 7 (Mon) | Repository scaffold, `pyproject.toml`, config schema, `ChatRequest` and `ChatResponse`, `Adapter` protocol, `docs/interface.md` draft | Interface draft reviewed against what the 03 and 02 plans call |
| Sep 8 to 9 | Anthropic and OpenAI-compatible adapters with goldens; transport; pass-through mode and raw store; ledger schema and store | Both adapters pass golden tests; pass-through byte-equality test passes; **interface frozen Sep 9** |
| Sep 10 to 11 | Google adapter; price files and cost arithmetic; caps; telemetry spans (console exporter) | Cost matches vendor pricing examples to the cent; cap test refuses with zero upstream calls |
| Sep 12 (Sat) | Fault injection; overhead benchmark against the mock; `ledger report`; one smoke call per provider once the vendor console caps exist | Numbers from section 1 in the README |
| Sep 13 (Sun) | Docs, `v0.1.0` tag; 03 and 02 repositories pin it | Tagged; the 03 runner can start on Sep 16 |

First to drop if behind: the overhead benchmark and `ledger report` (to v0.2), and the
configurable telemetry exporter (console only). None is in the drift-run dependency. The
Google adapter cannot be dropped: 03 runs Google from the first dry run.

**Outcome (2026-09-11):** `v0.1.0` tagged 2026-09-10, three days early. Day 2 slipped a
day and Day 3 recovered it. The smoke calls ran from GitHub Actions through a manual
workflow in the 03 repository because the laptop's network inspects TLS (decided
2026-09-10). Ten live calls across four vendors cost US$0.0009 and found three
things the goldens could not: OpenAI returns dated identifiers, `gemini-2.5-flash-lite` is
closed to new users, and Gemini `extra` fields replaced `generationConfig` instead of
merging into it. All three were fixed before the tag. Nothing was dropped; the overhead
benchmark and `ledger report` shipped in 0.1.0.

### Version 0.2 follow-ups, October 2026

Each is a day or less, done alongside 01 before the project that needs it:

| Item | Needed by | When |
|---|---|---|
| `ledger merge` across environments and the monthly `ledger report` that feeds the STATUS budget review | The first budget review | Before Oct 1. **Done 2026-09-10**, brought forward because the Rule C experiment needed it to exist to be measured against |
| Foundry, Bedrock and Vertex adapters, exercised once each | Definition of done; nothing else this year | Mid October, once their accounts have billing alerts |
| Anthropic Message Batches: `batch_submit`, `batch_results`, ledger rows written at result time at the batch rate | 02 | Before Nov 1. **Pulled forward to the week of Sep 21** (section 5.1): 02 built its public-data half on 2026-09-11, seven weeks early, and its own-run panel is blocked on this |
| Local OpenAI-compatible hosts at price zero | 02 | Before Nov 1. The costing path exists (`price_zero` on a provider entry); what remains is one exercised call against a local server, with a golden for its response shape |

### 5.1 Version 0.2 in detail (written 2026-09-11)

**Where 0.2 stands (2026-09-11).** On `main`: ledger schema v2 and v3, `ledger merge`,
`env`, `boundary experiment remote-ledger` and `docs/rejected.md`; Anthropic Message
Batches; merge no longer writing to its sources; the local price-zero host under goldens; a
cap line for 02. Version `0.2.0.dev0`; 139 tests, `ruff` and `mypy --strict` clean. Left
before the tag: one live batch call and one live local call. Left after it: the three
hyperscaler adapters and the first ledger-against-invoice check.

**Ordering principle: by who is waiting.** 02 is waiting now. Nothing is waiting on the
hyperscalers, and Bedrock cannot start before the AWS account opens in the first week of
October. 03 needs 04 to stay still until its first official run on Sep 27, which the tag
pin already guarantees, so `main` moving costs it nothing. So the tag splits: `v0.2.0`
carries what 02 needs and goes out as soon as its two live calls are made; the hyperscalers
follow as `v0.2.1` and `v0.2.2` when their accounts allow. The CHANGELOG had all of it
under one October tag; this is the change, and the reason is that 02 should not wait on an
AWS account it does not use.

| # | Item | When | Done when |
|---|---|---|---|
| 1 | **Stay still for 03.** No change to anything `v0.1.0` exposes; the tag never moves; a fix the Sep 27 run needs is a `v0.1.1` branched from the tag, not from `main` | Sep 12 to Sep 27 | The official run completes on the tag it pinned |
| 2 | **Done 2026-09-11. Merge never writes to a source.** `ledger merge` used to upgrade a v1 source in place, and 0.1.0 then refuses to write to that file. 03 pins 0.1.0 and commits one ledger per arm per month, so merging the checkout's copies would break the runner's next write to any file it reopens. Fix: merge copies a v1 source to a temporary file and upgrades the copy, or reads it without upgrading; `docs/ledger.md` and `tests/test_merge.py` change with it | Done 2026-09-11 | Done: rows are read from a temporary copy, and `test_merge_never_writes_to_a_source` asserts the source's bytes are unchanged and that it is still v1 without the v2 columns. A ledger that fails to open no longer leaks its file handle either, which on Windows had turned one file's error into another's |
| 3 | **Anthropic Message Batches**, the signatures frozen in `docs/interface.md` section 2. Raw HTTP with the pinned `anthropic-version` header: create the batch, poll its status, fetch the results file. Standard mode only. Cap check on the whole batch before submit, at the batch rate; one `in_flight` row per request at submit, so a process that dies between submit and results still has every call on record; rows completed at result time from returned usage times `batch_multiplier`. `BatchHandle` is rebuildable from the ledger alone, so 02 can submit in one process and collect hours later in another: schema v3 adds one nullable column, `batch_id`, additive. The development cache is not consulted for batch requests in 0.2, and the doc says so. Goldens from the Anthropic batch documentation, including a partial failure (one `errored` result among successes) and an expired batch | Done 2026-09-11 | Done, with 17 tests in `tests/test_batches.py`: submit, partial failure, expired, missing, not-ready, polling, collection by a second process, a results URL on another host refused before the key is sent, the cache never answering, and the cap refusing a batch with nothing sent. One live batch call is still owed before the tag |
| 4 | **Local price-zero host.** Ollama or llama.cpp on the laptop, so nothing leaves the machine. **Goldens done 2026-09-11** in `tests/test_local_host.py`: Ollama's response shape parses, the call is costed at zero and marked costed rather than left uncosted, no key is sent, and repeated local calls never move a cap. **Live call done 2026-09-11**: Ollama 0.34.0 with `llama3.2:3b`, ledger row 5, status 200, 32 and 2 tokens, `costed = 1` at cost 0.00. It is the one live call that can be made from an inspected network, because nothing leaves the machine | Done 2026-09-11 | A row in the ledger at cost zero with `costed = 1` |
| 5 | **Caps entry for 02.** **Added 2026-09-11** as `model-selection-tenth-cost`, US$40 a month and US$30 a run, with the reasoning in a comment beside it. **Confirmed by Peter 2026-09-11.** They were set so that 02 is not silently refused by the US$10 default, and the figures come from 02's own cost table | Done 2026-09-11 | 02's first call is admitted or refused by its own line, not by `default` |
| 6 | **Tag `v0.2.0`**, once items 3 and 4 have each been exercised live. Item 4 done 2026-09-11. Item 3 part done the same day: the four vendor keys went on as repository secrets, all four providers answered under 0.2, and a real two-request batch was submitted and polled, the vendor reporting its own counts. It had not ended within fifteen minutes, which is the vendor's right and not a fault, so `boundary batch collect` and a collect mode in the workflow were added to finish it rather than abandon it. **Left: that collect run, which proves the cross-process path live.** Then CHANGELOG dated, the `since` column in `docs/interface.md` checked, version `0.2.0`, 02 pins `>=0.2,<1` | As soon as the batch is collected | Tagged; 02 installs from the tag with the same read-only token 03 uses |
| 7 | **First ledger-against-invoice check, for September.** Sources: the laptop's `boundary.sqlite` (four zero-cost rows from the TLS-proxy attempt, which stay), and the 03 repository's per-arm ledgers for the dry runs and the Sep 27 run. The ten smoke calls from Actions (US$0.0009) count only if the 03 smoke workflow kept its ledger; if not, the gap is noted, not hidden. Merge into a central file kept outside git, run `ledger report`, compare per vendor with the four consoles, record the percent difference in the monthly budget review; above 5% is investigated before the October run (section 8). Same day: re-check the four price pages and write `config/prices/2026-10-01.yaml` even when nothing changed, so `prices check` stays quiet and every October row carries an October price date | Oct 1 to 3, then the first days of every month | A line in the budget review with the four differences |
| 8 | **Foundry and Vertex adapters.** The Azure and GCP budgets have existed since Sep 8 and Sep 10. Foundry: Claude on a Foundry endpoint with the Anthropic Messages body and the Azure key header, endpoint and shape verified against the current documentation first, as the adapter table says. Vertex: Claude via `rawPredict` or Gemini via `generateContent`, token from `google-auth`, used for the token only. Both in optional dependency groups so 03's install gains nothing. The `region` field is recorded on every row; the Canadian regions are the worked example, which is what Part B's residency policy builds on. Price files keyed by each platform's own model identifiers, with sources; an unknown identifier is an uncosted row, never a guess. One live call each from GitHub Actions or a non-inspected network, in the ledger | Mid October, one day each, alongside 01 | `v0.2.1` tagged; two costed rows in the ledger |
| 9 | **Bedrock adapter.** `InvokeModel` with the Anthropic Messages body and `anthropic_version: bedrock-2023-05-31`; SigV4 through the botocore signer only, in an optional dependency group. One live call recorded | After the AWS account opens (first week of October) and its budget alarms exist | `v0.2.2` tagged; a costed row in the ledger |
| 10 | **Rule C candidate 2 becomes cheap after Sep 27**, and is optional. The drift run leaves about 16,800 rows with returned usage and a raw store holding every request body, with no personal data by construction. Tokenizer estimates over those bodies against the returned counts, per vendor, is an afternoon. Rule C is already met; this would be a second row in "What did not work" only if the evidence is clear | October, if time allows | A dated table in `docs/rejected.md`, or nothing |

**Still not in 0.2:** streaming, typed tool calls, embeddings, any server, OTLP export,
content inspection (section 2.8). Nothing between now and May 2027 needs them.

**Between 0.2 and Part B (November 2026 to April 2027).** Nothing is built here. Three
things accumulate or are decided elsewhere and matter in May: the shared VPS (needed by
project 03's dashboard in January anyway) is where the proxy, Postgres and Redis
will run; every month's drift run adds to the replayed-traffic corpus the semantic cache
is tuned on; and the monthly invoice check builds the ledger's credibility. In the last
week of April, before Part B starts: re-check Part B's prices, Presidio's current version
and the corpora licences (B3), and re-read B5 against what 03 Part B actually shipped in
January.

**Risks specific to 0.2**

| Risk | Handling |
|---|---|
| Version skew: 03 on 0.1.0, 02 on 0.2, one merge across both | Item 2 makes merge read-only on its sources; each repository owns its ledger files; the interface doc already says one library version per ledger file |
| Batch results arrive hours later, and the in-flight rows count against caps at the pessimistic estimate meanwhile | Correct by design, and documented: `ledger report` shows them as `in_flight` until `batch_results` completes them; a `SpendCapExceeded` in that window names the estimate |
| Hyperscaler model identifiers and prices differ from the vendors' direct APIs | Price files keyed by the platform's own identifiers; uncosted rows until they exist, and the `unc` column of `ledger report` would show it |
| October is 01's build, and 0.2 eats the month | Each item above is a day or less; items 8 to 10 drop first, and nothing this year needs them |

## 6. Cost

Version 0 costs almost nothing. The overhead benchmark runs against a mock. Smoke calls
are one short request per provider per adapter change, on the order of 200 calls at under
100 tokens each across the week, under US$2 total. The v0.2 hyperscaler smoke calls are
the same order, on free tiers where they exist. **Part A: under CA$10 of the CA$160 line**,
price lists as of 2026-09-06 (Anthropic list prices per million tokens: Haiku 4.5 at $1
in and $5 out, Sonnet 5 at $2 in and $10 out, Opus 5 at $5 in and $25 out; the other
vendors' prices are copied into the first price file on Sep 10 from their price pages, with
the date).

Actuals are recorded next to the estimate in the portfolio's monthly budget review, and
from October the ledger-against-invoice difference is recorded there too (section 1).

## 7. Handover

| Version | Date | What downstream imports |
|---|---|---|
| `boundary` v0.1.0 | Sep 13 2026 | `Gateway`, `ChatRequest`, `ChatResponse`, `Mode`, errors; ledger schema v1; four adapters |
| v0.2.0 | Early October 2026 (split proposed 2026-09-11; was one October tag) | `ledger merge` and `report`, ledger schema v2 (`call_uid`, `env`), `Gateway(env=)`, Anthropic batches with schema v3 (`batch_id`), local price-zero host exercised. Tagged as soon as batches land so 02 can pin `>=0.2` without waiting for the AWS account |
| v0.2.1, v0.2.2 | Mid to late October 2026 | Foundry and Vertex adapters (0.2.1); Bedrock after the AWS account opens (0.2.2). Optional dependency groups, so 03's install gains neither botocore nor google-auth |
| v1.0.0 | May 23 2027 | Everything in Part B; `boundary.redact` for 07; the proxy for 13 and 14 |

03 pins `boundary>=0.1,<0.3` for Part A and moves to `>=1.0` when its Part B is built
against the proxy's ledger ingestion. 02 pins `>=0.2,<1`. Interface changes are additive
within a major version; a breaking change is a major version and a note in the plans of
every project that pins the old one.

## 8. Risks

| Risk | Handling |
|---|---|
| The week slips and the 03 runner has nothing to call on Sep 16 | The interface is frozen Sep 9 regardless of implementation state, so the runner can be written against it; Anthropic and OpenAI adapters are built first because the dry run on Sep 16 to 19 needs them; Google follows by Sep 11 |
| A vendor changes an API shape under a pinned version header | Goldens catch it in CI; the adapter is fixed in a patch release; the ledger row records the `boundary_version` so any affected calls can be identified |
| The library becomes a confound in the drift record | Pass-through mode has no retries, no cache, no rewriting, and byte-equality tests; the 03 record stores the gateway version with every run |
| Cost figures drift from invoices | Monthly ledger-against-invoice check recorded in STATUS; a difference above 5% is investigated before the next run |
| Price list goes stale | `boundary prices check` compares the file's date with the current month and warns; repricing is a new dated file |
| A pre-call estimate refuses a legitimate call | The estimate is pessimistic by design; the caller sees a clear error with the numbers, and the cap is raised deliberately in `caps.yaml`, never bypassed in code |
| Secrets leak | Keys only from environment variables; `.env` gitignored; Actions secrets; a test greps the ledger and raw store fixtures for anything key-shaped |
| Employer boundary | Generic infrastructure with no counterpart in the employer's systems; nothing to mirror. Redaction for access-to-information is 07's problem, not this one's |

## 9. Rule C candidates

Three approaches expected not to work, each with the evidence it would leave behind:

1. **Vendor SDKs instead of raw HTTP.** Pin each vendor's SDK at the September version,
   then diff the outbound request of the SDK against the library's raw request across two
   SDK minor releases. Expected: at least one silent change in headers, retry behaviour or
   default fields, which is exactly what a drift record cannot tolerate.
2. **Estimating cost from a tokenizer before the call rather than from returned usage.**
   Run both on the 500-request golden corpus. Expected: tokenizer estimates differ from
   returned counts by more than 10% for at least one vendor, and by an unpredictable amount
   for system prompts and cached prefixes.
3. **A central remote ledger from day one instead of local-first with merge.** Simulate the
   Actions runner losing network to the ledger host mid-run. Expected: rows lost or the run
   aborted; the local-first design loses nothing and the merge is idempotent.

Whichever produces the clearest evidence becomes `docs/rejected.md`.

**Outcome (2026-09-10): candidate 3.** Written up in `docs/rejected.md` and measured by
`boundary experiment remote-ledger`: 100 runs per design, an outage in each, three designs
over the same corpus. The strict remote ledger finished none of the runs; the best-effort
one finished all of them and left 8.8% of the calls it had already paid for with no record
at all, and made 981 calls with no cap check, because a remote ledger cannot answer "what
has been spent" when it is the unreachable thing. Local-first lost nothing and a second
merge inserted nothing. Candidate 1 (vendor SDKs) needs two SDK releases to diff and
candidate 2 (tokenizer estimates) needs a corpus of live calls, so both move to v0.2 in
October; Rule C asks for one, and it is done.

## 10. Definition of done, Part A

- [x] `boundary` v0.1.0 tagged by Sep 13 2026; interface frozen Sep 9 and documented. **Tagged 2026-09-10; frozen 2026-09-08**
- [x] Anthropic, OpenAI, Google and OpenAI-compatible adapters pass golden tests, **and one live call each on 2026-09-10**
- [x] Pass-through mode: byte equality and single-upstream-call tests pass; aliases refused. **500/500 in the README**
- [x] Every call, including failures, writes a ledger row before returning; fault-injection completeness reported. **600/600 across six fault types, both modes**
- [x] Cost from returned usage and a dated price file; no guessed prices; uncosted count reported. **The `unc` column of `ledger report`; ten live calls, all costed**
- [x] Spend caps refuse with zero upstream calls; test proves it. **17 attempted past a cap, 0 reached the upstream**
- [x] One OpenTelemetry span per call with no content. **Attribute allow list enforced in code; a test asserts the prompt never appears in the export**
- [x] Overhead p50 and p95 with CIs in the README. **2.61 ms and 4.62 ms, n = 1,000, against an in-process mock**
- [ ] Used by the 03 dry runs (Sep 16 onward) and pinned by the 02 and 03 repositories. **Both pins done: 03 on `v0.1.0` 2026-09-10, first Actions install worked; 02 on `v0.2.0` 2026-09-11. Waiting only on the 03 dry runs**
- [ ] v0.2: Foundry, Bedrock and Vertex exercised once each, calls in the ledger; Anthropic batches; `ledger merge` and `report`. **`merge` and `report` done 2026-09-10; the rest is section 5.1**
- [ ] Ledger-against-invoice difference recorded in the monthly budget review from October (section 5.1, item 7)
- [x] One rejected approach documented with evidence (Rule C): `docs/rejected.md`, 2026-09-10
- [x] `v0.1.0` tagged. The repository was private at the tag (decided 2026-09-07): the 02
      and 03 runners installed it with a fine-grained read-only GitHub token held as an
      Actions secret, and it went public once its results could be checked.
      **Tagged 2026-09-10; 03's token install confirmed the same day. Public 2026-09-11**,
      after `v0.2.0` and a commit-by-commit audit of all 18 commits for credentials,
      employer identifiers and plan-repository links. The history was not rewritten, on
      purpose: 02 and 03 pin exact commit SHAs, and rewriting the library that produced
      September's measurements would weaken the reproducibility claim the repository exists
      to make. Both tokens and both CI authentication steps stay until after 03's Sep 27
      run, then come out.

---

# Part B. The full gateway, May 2027

## B1. What this produces

A drop-in, OpenAI-compatible gateway that a regulated organisation can put between its
teams and the model vendors: personal data is redacted before it leaves and restored when
the answer comes back, requests carrying sensitive classes can only reach approved
providers and regions, every call is in a tamper-evident audit log, every team has a
budget, repeated questions are served from a cache, and the overhead of all of that is
measured under load and published. Plus the portfolio-wide observability dashboard: every
project's calls, latency, errors and cost, by model and provider, live.

The numbers a stranger can check:

| Number | What it shows |
|---|---|
| Latency overhead p50, p95, p99 in milliseconds at 50, 200 and 500 requests per second, per feature layer (routing and ledger; plus audit; plus redaction; plus cache), 5 runs each, 95% CIs across runs, on a stated VPS size | Engineering, not assembly; the number an interviewer will probe |
| Redaction precision and recall per entity type on public PII corpora, and on a hand-built Canadian identifier set, with 95% CIs | Redaction accuracy measured, not asserted, including the failure modes |
| Rehydration fidelity: share of placeholders in model responses restored correctly, and the rate at which models mutate placeholders | Reversibility actually works, and where it does not |
| Quality cost of redaction: paired non-inferiority test through the 03 gate on 03's gold set, redacted against unredacted prompts, delta and interval | Redaction does not silently make answers worse, or it does and by how much |
| Residency policy violations under an adversarial suite of N requests (must be zero), plus refusals correctly raised | Policy is enforced, not documented |
| Semantic cache on replayed portfolio traffic: hit rate, dollars saved, and false-hit rate on 200 hand-labelled hits | Savings with the risk next to them |
| Injection screen: detection rate and false-positive rate on public sets and 03's suite | Screening measured against its cost in blocked legitimate requests |
| Audit chain: verification time for the full log, and detection of every injected corruption in a tamper test | Tamper-evident means detectable, and here is the detector |
| Dashboard completeness: rows in the central ledger against rows in every environment's local ledger | The dashboard shows everything, not most things |

## B2. Design decisions

### B2.1 An OpenAI-compatible proxy on the library

`POST /v1/chat/completions` and `GET /v1/models`, with server-sent-event streaming, so a
real client works by changing only the base URL and the key. The proxy calls the same
`Gateway` as Part A; every feature below is a layer inside that call path and can be
switched off per route, which is what makes the layered load test possible.

### B2.2 Data classification is an input, not an inference

Each request carries `X-Data-Class`: `public`, `internal`, `personal` or `sensitive`.
Absent means `personal`: fail closed. The policy file maps a class to allowed providers and
regions and to required layers (for example, `personal` requires redaction and forbids the
cache). Residency is expressed as region sets; the Canadian set is the worked example.
The gateway never guesses a classification from content; a gateway that did would be
making a compliance decision nobody reviewed. Violations are refused with a reason and
audited.

### B2.3 Reversible redaction

Detection is Presidio's analyzer plus custom recognisers for Canadian identifiers: Social
Insurance Numbers with the checksum, provincial health numbers by format, postal codes,
and place and organisation names for Newfoundland and Labrador as a gazetteer. Each
detected span becomes a typed, consistent placeholder (`<PERSON_1>` is the same person
everywhere in one request). The mapping is held in a per-request vault in Redis with a
short TTL, encrypted with AES-GCM under a per-team key, and never written to the audit
log or the ledger. Rehydration substitutes placeholders back, with tolerant matching for
the ways models mutate them (possessives, case, spacing); the mutation rate is measured
and reported because it is the honest limit of the approach.

### B2.4 A hash-chained, anchored audit log

Each audit record is `sha256(previous_hash || canonical_record)` over hashes of the
request and response, the policy decision, the team, the model, the cost and the time,
never content. Records live in a Postgres table with `UPDATE` and `DELETE` revoked and a
trigger that refuses both. Once a day a GitHub Action commits the chain head to this
public repository, so tampering by the operator, not only by an outsider, is detectable
by anyone who can read the repository. `boundary audit verify` recomputes the chain and
checks it against the anchors.

### B2.5 Semantic cache, with its risk measured

Local `bge-small` embeddings and pgvector; a cosine threshold tuned on replayed traffic
from the portfolio's own ledger and raw stores (eight months of real requests by May, none
containing personal data). Bypassed for `personal` and `sensitive` classes and always in
pass-through. The claim is hit rate and dollars saved on the workload it was measured on,
with the false-hit rate from hand-labelling 200 hits printed next to it. A cache that
returns a confident answer to a different question is worse than no cache, and the
number says how often that happens.

### B2.6 Injection screening is advisory by default

A classifier over user-supplied and retrieved content flags likely prompt injection.
Default action is to flag in the audit record; blocking is a per-policy choice, because
the false-positive rate is a real cost and the policy owner should choose it knowingly.
Evaluated on 03's injection red-team suite (Rule F: reuse) and a public set.

### B2.7 Teams, budgets and quotas

API keys map to teams; each team has a monthly budget and a rate quota; budgets extend
Part A's caps and use the same ledger. At the limit the proxy returns 429 with a body that
says which limit and when it resets.

### B2.8 Measured by 03

The gate from project 03 answers the question a buyer will ask: does redaction hurt the
answers? Redacted and unredacted prompts over 03's gold set, three models, paired
non-inferiority with the gate's default delta. It is the same harness every other project
uses, which is the point.

### B2.9 Out of scope in Part B, on purpose

- Single sign-on and role-based access. Keys per team only; anything more is enterprise
  integration work with no number attached.
- Multi-region deployment. The gateway runs on one VPS; residency is about where requests
  are allowed to go, not where the proxy runs, and the README says so.
- Prompt management, agent orchestration, model hosting. Other projects, or nobody's.
- Images and audio. Text only. Non-text content is passed through only for the `public`
  class and refused otherwise, because it cannot be redacted here.
- Training or fine-tuning on logs. Never.

## B3. Data

| Source | Size | What it gives | Access and terms |
|---|---|---|---|
| ai4privacy PII masking corpus (English subset) | Hundreds of thousands of annotated spans | Precision and recall per entity type on varied free text | Hugging Face; licence read and recorded in `docs/data.md` before use |
| Presidio research synthetic generator | Templated, unlimited | Controlled evaluation by entity type and format | MIT |
| Own Canadian identifier set | 500 hand-built items | SIN, health numbers, postal codes, NL place and organisation names, which public corpora lack | Written here, CC0, no real people |
| deepset prompt-injections | About 660 labelled prompts | Detection and false-positive rates for the injection screen | Hugging Face; licence recorded |
| 03 red-team suites (injection, PII) | From the 03 repository | Reuse; same items the gate uses | Own |
| Replayed portfolio traffic | Eight months of ledger rows and pass-through raw stores | Semantic cache tuning and savings on a real workload | Own; contains no personal data by construction |
| 03 gold set, 300 items | From the 03 repository | Quality cost of redaction | Own |

Not used: clinical de-identification corpora (i2b2, n2c2) require data-use agreements and
are out; the README names them as the standard the health domain would use.

Nothing raw is committed; loaders verify checksums; every licence is recorded.

## B4. Architecture

Additions to the Part A package:

```
boundary/
  server/          FastAPI: /v1/chat/completions (SSE streaming), /v1/models, /healthz;
                   team key auth; X-Data-Class handling; layered call path
  policy.py        class -> allowed providers and regions -> required layers; fail closed
  redact/          analyzer.py (Presidio + recognisers/ca.py), placeholders.py, vault.py
                   (Redis, AES-GCM, per-team key, TTL), rehydrate.py (tolerant matching)
                   Public API for 07: Redactor.analyze(text) -> spans; redact(text) -> (text, token);
                   rehydrate(text, token) -> text
  audit/           chain.py, store.py (Postgres append-only), anchor.py (daily head commit), verify
  semcache/        embed.py (bge-small, local), store.py (pgvector), threshold.py, metrics.py
  screen/          injection classifier and rules; advisory or blocking per policy
  quotas.py        team budgets and rate quotas on the shared ledger
  ingest.py        accepts ledger rows from remote environments into central Postgres
  dashboard/       read-only FastAPI plus htmx pages over the central ledger:
                   calls, cost, latency, errors by project, model, provider, day; completeness panel
loadtest/          k6 scripts; mock upstream with fixed latency; layer toggles; result tables with CIs
eval/              redaction on corpora; rehydration fidelity; quality A/B via the 03 gate;
                   cache replay and false-hit labelling; injection eval; tamper test
deploy/            docker compose: gateway, postgres with pgvector, redis, otel collector, caddy;
                   gateway.peterparker.ca on the shared VPS next to gate.peterparker.ca
```

### Load-test method

k6 against the proxy on the VPS, upstream replaced by a mock with a fixed 50 ms response,
so overhead is the proxy's own. Four layers switched on cumulatively, three load levels,
five runs each, overhead defined as the proxy percentile minus the mock's. Bootstrap CIs
across runs. The overhead budget is set from the first measurement in week 1 and published
before the feature list grows, so the budget drives the architecture (provisional target:
p99 under 20 ms for routing plus audit, under 100 ms with redaction on 2,000-token
prompts). Whatever the final numbers are, they are the numbers.

### Tests that matter

Rehydration round-trip property test (any text, any spans, redact then rehydrate is the
identity when the model echoes placeholders unchanged); vault never persists to disk;
policy fail-closed test (no header means `personal`); audit trigger refuses `UPDATE` and
`DELETE`; chain verify detects a single-bit corruption anywhere; cache bypass for
`personal` and pass-through; proxy conformance against a real OpenAI client library;
streaming produces the same ledger row as non-streaming.

## B5. Week by week

| Dates | Built | Done when |
|---|---|---|
| May 3 to 9 | Proxy with streaming and team keys; policy engine; audit chain, store and daily anchor; central ledger ingest; first load test on the core layer | A real OpenAI client works by changing the base URL; `audit verify` passes; overhead budget published in the README |
| May 10 to 16 | Redaction: recognisers, vault, rehydration; redaction eval on corpora and the Canadian set; quality A/B through the 03 gate; semantic cache with replay and hand labelling; injection screen | Precision and recall table; rehydration fidelity; non-inferiority result; hit rate with false-hit rate |
| May 17 to 23 | Dashboard; deployment at gateway.peterparker.ca; full layered load test; tamper test; Rule C write-up; README | Live URL; every results table filled with intervals; `v1.0.0` tagged |
| May 24 to 31 | Slack | |

First to drop if behind: the injection screen (03 already measures injection; here it is
advisory), then hand-labelled cache hits reduced from 200 to 100. Neither is in the
definition of done as written; the cache hit rate itself is, and stays.

## B6. Cost

Prices as of 2026-09-06; re-checked in May.

| Item | Calls | Tokens | Cost |
|---|---|---|---|
| Quality-cost-of-redaction A/B: 300 items, 2 arms, 3 models, 3 repeats | 5,400 | About 3.5M in and 0.5M out | About US$12 |
| Development and conformance testing against real vendors | About 2,000 short calls | Under 1M | About US$5 |
| Hosted demo traffic, June to August 2027, capped per key | Capped at US$10 per month | | US$30 |
| Load tests | Against a mock; zero API cost | | 0 |
| Redis, Postgres, pgvector, collector | On the shared VPS; the VPS line in BUDGET covers 03, 04 and 09 | | 0 new |
| PII corpora, injection sets, embeddings | Free; embeddings run locally | | 0 |

Part B about **CA$65**; with Part A under CA$10 the project uses about CA$75 of the
CA$160 line. The remainder buys, in order: a larger Canadian identifier set with a second
labeller and agreement reported; a longer demo uptime with a higher cap; a second
open-weights provider in the residency demonstration. Actuals go to STATUS.

## B7. Handover

`boundary` v1.0.0 on May 23 2027: the proxy image; `boundary.redact` as an importable
engine with `analyze`, `redact` and `rehydrate`, which 07 builds its access-to-information
workflow on in August; the ledger ingestion endpoint 03's Part B dashboard reads from; the
policy and audit modules 13 and 14 inherit by pointing their gateway base URL here. 10 reads
the ledger and the spans as its production signal.

## B8. Risks

| Risk | Handling |
|---|---|
| Redaction recall on names and addresses in free text is mediocre | Report it per entity type with the failure modes named; a gateway reporting 94% with a documented failure mode is more credible than one claiming 100% |
| Latency overhead is embarrassing with redaction on | Measured in week 1 before the feature list grows; if the analyzer is the bottleneck, the smaller spaCy model and a span cache are the first moves, and the trade-off is published |
| Models mutate placeholders so rehydration fails | Tolerant matching, consistent placeholders, and a measured mutation rate; a system prompt line asking the model to preserve placeholders is tested as an intervention with its effect reported |
| Redis vault loss mid-request | TTL is short and a lost vault fails the request with a clear error rather than returning redacted text as if final |
| Crowded category | Differentiate on the audit anchor, the residency policy and the published overhead, not on feature parity; the README says which commercial gateways exist |
| The dashboard turns into a product of its own | It is read-only pages over the ledger; anything beyond calls, cost, latency and errors by project and model is out |
| VPS not ready | Needed by January for 03 anyway; if it slips, Part B deploys on a free-tier container host for the demo month and the load test notes the host |
| Employer boundary | Generic compliance infrastructure with public data; no internal architecture, prompts or thresholds, and nothing that mirrors a system built at work |

## B9. Rule C candidates

1. **Regex-only PII detection.** Run the regex recogniser set alone against the corpora.
   Expected: high precision on formatted identifiers, recall on names and addresses far
   below the analyzer, with the numbers per entity type.
2. **A loose semantic-cache threshold.** Sweep the cosine threshold on replayed traffic.
   Expected: hit rate rises and the false-hit rate rises faster past a point; the curve is
   the evidence, and the chosen threshold sits before the knee.
3. **An LLM as the redactor.** Ask a model to rewrite text with personal data removed,
   then measure recall against the corpora and latency against the analyzer. Expected:
   recall competitive on obvious entities, misses on formatted identifiers, latency an
   order of magnitude worse, and no reversibility because there is no span mapping.

## B10. Definition of done, Part B

- [ ] Everything in Part A's definition of done
- [ ] OpenAI-compatible: a real client library works by changing only the base URL and key, streaming included
- [ ] Data classification header enforced, absent means `personal`; residency violations zero on the adversarial suite, with correct refusals
- [ ] Reversible redaction round-trips under property tests; rehydration fidelity and mutation rate reported
- [ ] Redaction precision and recall per entity type on public corpora and the Canadian set, with CIs
- [ ] Quality cost of redaction measured through the 03 gate, with interval
- [ ] Semantic cache hit rate, dollars saved and false-hit rate on replayed traffic
- [ ] Injection screen detection and false-positive rates reported (unless dropped, and then said so)
- [ ] Audit log chain verification tool included; daily anchors in the public repository; tamper test detects every injected corruption
- [ ] Per-team budgets and quotas enforced; 429 body names the limit
- [ ] Load test published: p50, p95, p99 overhead by layer and load level, with CIs and the VPS size stated
- [ ] Observability dashboard shows every project's calls and costs live; completeness panel against local ledgers
- [ ] Hosted demo live at gateway.peterparker.ca
- [ ] Foundry, Bedrock and Vertex adapters each exercised with calls recorded
- [ ] `boundary.redact` importable and documented for 07
- [ ] One rejected approach documented with evidence (Rule C)
- [ ] Repository public, `v1.0.0` tagged

## B11. Deferred

| Deferred | Kept so the door stays open |
|---|---|
| OpenAI Responses API adapter | The `Adapter` protocol; add when a project needs a Responses-only feature |
| Typed tool calls in the library | Tool fields pass through untouched today; typed support in `v1.x` for 10, 13 and 14 |
| Single sign-on, role-based access | Teams are a table keyed by API key; an identity provider can populate it later |
| Multi-region deployment | Region is already a field on every route and every ledger row |
| Image and audio redaction | The policy engine already refuses non-text for non-public classes; a modality-specific redactor would slot in as a layer |
| Publishing to PyPI | Package name `boundary` may be taken; if so it publishes as `ai-boundary` and the import name stays. Until then, install from the repository tag |
| Managed OpenTelemetry backend | The OTLP exporter is configured; pointing it at a hosted backend is a config change |
