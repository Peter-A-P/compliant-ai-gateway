# Changelog

Versions follow the plan's handover table (PLAN.md section 7). Interface changes within a
major version are additive only; see docs/interface.md.

## Unreleased

- **`ProviderError`'s message now follows its retry count.** An adapter's `parse_error` cannot
  know how many attempts were made, so it builds the error with zero and the gateway assigns
  the real count afterwards. The message was formatted in `__init__`, so that assignment never
  reached the text: a Vertex 429 retried three times reported "after 0 retries" while the
  ledger correctly recorded `retries = 3`. The message is what a person reads first when a call
  fails, and that one read as evidence the retry policy had not run.

- **Google credential minting for Vertex** (`boundary/credentials.py`), behind a new optional
  `vertex` extra: `uv sync --extra vertex`, or `pip install 'boundary[vertex]'`. A
  cloud-platform access token from Application Default Credentials, refreshed five minutes
  before it expires rather than after a call fails on it. This was the last code gap in the
  three hyperscaler adapters: a pasted token lasts about an hour, which is fine for a smoke
  call and useless for a drift run.

  A new `credentials` field on a provider entry (`env`, the default, or `google_adc`) says
  where the credential comes from. Under `google_adc` a set `api_key_env` variable still
  wins, so one checked-in entry serves a laptop with `gcloud auth application-default login`
  and a CI runner with a token from an earlier step and no gcloud.

  The token is fetched through this library's own pinned httpx client rather than
  google-auth's `requests` or `urllib3` transports, so the token call and the vendor call
  verify TLS against the same store. On a network that inspects TLS, the alternative fails
  the mint while every vendor call succeeds, and reads like a credentials problem.

  The adapter is unchanged and still pure. `VertexAdapter` receives a token string and knows
  nothing about where it came from.

- **Amazon Bedrock adapter** (`aws_bedrock`), a subclass of the Anthropic adapter with a
  different path and a residency guard. AWS serves the Anthropic Messages API at
  `POST {host}/anthropic/v1/messages` with a Bedrock API key in `x-api-key`, so there is no
  SigV4 and no botocore: PLAN.md section 2.2's SDK exception now names only `google-auth`.
  Exercised live from `ca-central-1` on 2026-09-15, and the response and error goldens in
  `tests/test_bedrock.py` are that call's bytes rather than documentation samples.

  The guard is most of the adapter. On Bedrock the processing geography is encoded in the
  model identifier's prefix and nowhere else, so `build_request` refuses a profile sent to
  the single-region endpoint, a bare id sent to the endpoint that cannot serve one, a region
  that differs from the one named in the host, and a model identifier that contradicts the
  provider entry's declared `residency`.

  Bedrock calls are **uncosted**: AWS publishes its own Claude rates and they were not
  readable from the published page on 2026-09-15, and an unknown price never becomes an
  estimate. Batches are not implemented; Bedrock's is `CreateModelInvocationJob` over S3, a
  different API, and a batch aimed at a bedrock provider is refused by name.

- **Ledger schema v4, additive: `residency`.** One nullable column recording how far a call
  was allowed to travel from its `region`, copied from the provider entry's declaration.

  It is configuration rather than observation because no vendor reports where a request was
  processed: Foundry hides the hosting version, Vertex hides the processing location, and
  Bedrock strips the routing profile out of the model identifier it echoes back. A row can
  say what the operator chose and cannot say what the vendor did. Null and `"global"` stay
  distinct, and no existing row is backfilled, because a value there would be a claim about
  where data went that nobody made.

- **`residency` on a provider entry** (`single-region`, `geo`, `global`), optional and
  additive. `aws_bedrock` enforces it against the model identifier; other kinds record it
  without checking, because their platforms offer no equivalent signal to check against.

- **Microsoft Foundry and Google Vertex adapters** (`azure_foundry`, `gcp_vertex`), both
  subclassing the Anthropic adapter because both platforms serve the Messages API. Only the
  envelope differs and only the envelope is overridden, so a change to how a response or its
  usage is read cannot drift between the direct vendor and a platform. Endpoint shapes verified
  against the vendors' documentation on 2026-09-14. Foundry takes the Azure key in `api-key`
  and keeps `model` in the body, where it is the deployment name; Vertex moves `model` into the
  URL and `anthropic_version` into the body as `vertex-2023-10-16`. 19 goldens, no network.
- **Vertex refuses a host and a region that disagree.** The URL's host and its `locations/`
  segment both name a geography and the host is what actually routes, so a provider entry
  pinned to `northamerica-northeast1` serving a route pinned to `global` would send data to
  another country while the ledger row recorded the region the route asked for. That pair is
  now a `ConfigError` naming both sides, not a request. Failing closed on residency is Part B's
  rule; it is cheaper to build in now than to retrofit around live rows.
- **`ProviderConfig.project`**, additive and optional: the Google Cloud project id, which Vertex
  carries in the URL rather than in a header. Every existing configuration still loads.
- **Neither platform has a price entry**, so a call through either writes an uncosted row. That
  is deliberate. The rates are copied from the vendor's page on the day the account exists and
  dated then; a rate written weeks early carries a date that lies about when it was checked.
  Foundry bills in Claude Consumption Units at US$0.01 per CCU rated at standard USD rates, so
  the invoice check divides the Azure line item by 100 before comparing.
- **Claude on Foundry is unavailable to a Free Trial subscription, and the error does not say
  so.** Deploying `claude-haiku-4-5` Global Standard in `eastus2` on 2026-09-14 was refused
  with "Insufficient quota" on **both** offered model versions. Microsoft's quota table gives
  the documented default for that subscription type: 80 RPM, on either version. **Measured
  through the ARM API rather than inferred from the error**: all 19 Claude quota entries in
  `eastus2` read zero, while 159 other AIServices quotas in the same region are non-zero, so
  the subscription is provisioned normally and only the Anthropic models are at zero. Same in
  `eastus`, `centralus` and `swedencentral`, on both hosting versions; `canadacentral` has no
  Claude quota entry at all, which confirms the region finding from the resource provider
  rather than the documentation. The documented default is a ceiling you may request, not an
  allowance you receive. `docs/hyperscaler-setup.md` step 3a has the one-line `az` command
  that reproduces it.

  **Two wrong explanations were published here first and are corrected rather than deleted**,
  because one of them reached the portfolio site. The first said the Azure-hosted version was
  rationed while the Anthropic-hosted one deployed freely; the table marks both allocatable
  with the same default. The second said the subscription must be a Free Trial, which gets
  zero for everything; it is pay-as-you-go. Both were inferred from the error message instead
  of read from the quota page the portal will simply show, which is the failure mode this
  repository keeps finding in other people's cost claims, appearing here twice in one day.

  What survives: quota on this platform is per subscription and shared across regions, so a
  per-deployment capacity message points away from its own cause and invites four changes
  (region, model, version, retry) that cannot move a subscription-level allocation.
- **Microsoft's own wording for the distinction**, quoted in the setup document because it is
  the vendor's rather than ours: "Data might be processed globally, outside the resource's
  Azure geography, but data storage remains in the AI resource's Azure geography." Processing
  is global, storage is regional. A requirement written about storage can be met; one written
  about processing cannot, on this deployment type, and the two are commonly written as if
  they were one.
- **Foundry offers no Claude model in a Canadian region, which is a finding rather than a
  blocker.** A Foundry resource created in Canada Central on 2026-09-14 had no Anthropic model
  to deploy, and Microsoft's own region table agrees: every `claude-*` row reads `-` against
  `canadacentral` and `canadaeast`, in both hosting versions. The Data Zone deployment type,
  the one narrower control on offer, exists only for the United States. So Foundry gives a
  Canadian buyer two choices for Claude, global routing or the US, and neither is Canada.
  `docs/hyperscaler-setup.md` carries the region lists and the source; PLAN.md B2.2 is amended
  because Part B's residency design had assumed the Canadian set was non-empty. It can be
  empty, and the worked example should show that refusal rather than design around it, which
  demonstrates failing closed better than a policy that always finds a route. The earlier
  wording in this repository said a Canadian deployment "is achievable"; for Claude on Foundry
  that was wrong and it is corrected.
- **Batches for OpenAI, Together and Gemini**, which until now only Anthropic had. Project
  02's own-run panel batched 9,292 of its 9,298 Anthropic calls and none of the other 18,000,
  because there was nothing to batch them with: US$11.24 of that run went at full price where
  a batch rate would have been about US$5.62.
- **The OpenAI shape submits in two round trips**, uploading the requests as a JSONL file and
  then creating a batch that names it. `BatchAdapter` gains `uploads_input_file`, and a new
  `UploadingBatchAdapter` carries the upload methods. The ledger rows are written before the
  *upload*, not before the create, because the upload is the request the prompts leave in.
  Neither round trip is billed, so a failure in either completes every row as a failure at no
  cost rather than leaving it in flight at an estimate.
- **Gemini batches submit inline** and return their results inside the status response, so
  there is no results file. To keep one collection path for every vendor, the batch name is
  carried as the results URL and the operation is fetched a second time. A Gemini batch is
  single-model by construction, because the model is in the URL, and a mixed batch is refused
  rather than silently sent to one of them.
- **A response is never matched to the wrong request.** Anthropic and OpenAI return the
  `custom_id` on every result, so order cannot matter. Gemini's documented shape does not
  promise the key comes back, so the adapter reads `metadata.key` when present and falls back
  to position when it is not. A silently shifted mapping would put one call's usage on another
  call's row, which is the worst thing a cost ledger can do.
- **`batches` on a provider entry**, additive and optional. `openai_compat` defaults to OFF
  because most compatible hosts (Ollama, vLLM) answer `/v1/chat/completions` and have no
  `/v1/batches`; a batch aimed at the local server used to be a confusing parse failure and is
  now a `ConfigError` naming the provider. `anthropic` and `google` default on.
- **A vendor-supplied file id cannot become a path.** The OpenAI results request carries the
  API key and builds its URL from an id the vendor returned, so an id that is not a plain id
  is refused before the request is built. Anthropic's equivalent guard checks a returned URL
  against the configured host; this is the same defence for a shape that has no URL.
- **`chat_body`/`parse_completion` and `generate_body`/`parse_generate` extracted** from the
  OpenAI-compatible and Google adapters, so the batch path builds byte-identical bodies to the
  single-call path and reads usage through the same parser. Tested both ways. No behaviour
  change to either single-call path.
- **Together's batch discount is per model**, which no other vendor here does:
  `meta-llama/Llama-3.3-70B-Instruct-Turbo` runs at half price and `openai/gpt-oss-120b` runs
  at the standard rate, so the price file carries `batch_multiplier: 1.0` for the latter.
  Writing 0.5 there would have understated its invoice by half on every batched call.
- **Price files ship with the library**, at `boundary/prices/`, and a configuration asks for
  them with `prices: builtin`. A directory path still works, for trying a rate before it is
  released. Until now this repository held three dated files and project 02 held a fourth of
  its own, so September's costing could not be reproduced from either repository alone; the
  invoice check found it. A project now pins a version and the version determines the rates.
  02's `2026-09-12.yaml` moved here byte for byte, so nothing it has already costed changes.
  `docs/prices.md` has the reasoning and the steps for moving 02 across.
- **`boundary/prices/2026-09-14.yaml`** supersedes `2026-09-12` and adds the `foundry` provider
  at the standard per-model USD rates, which is what Foundry meters before converting to Claude
  Consumption Units at US$0.01 each. No `batch_multiplier`, because Foundry does not offer the
  Message Batches API and an absent multiplier correctly leaves a batched call uncosted. No
  `vertex` block: Google publishes its own Claude rates and they were not readable from the
  published page on 2026-09-14, so a Vertex call is uncosted until someone reads them.
- **`docs/hyperscaler-setup.md`**, the step-by-step for creating the Foundry resource and the
  Vertex project, and the two things that bite: a Canadian regional endpoint may not serve the
  newest models at all, and the Vertex bearer token expires hourly with no refresh layer built
  yet.
- **`boundary experiment token-estimates`**, Rule C candidate 2, measured over 15,996 real calls
  from project 03's first official drift run. Three estimators against what the vendors actually
  returned. The finding is not that estimates are inaccurate but that they are accurate in
  aggregate and wrong per call: chars/4 gets the month's open-weights input count right to +0.1
  percent while getting the typical call wrong by -31.2 percent. A spend cap is checked per
  call, so an estimator that cancels out over a month is useless for the thing the number is
  for. Written up in `docs/rejected.md`; the costing path is unchanged.
- **`boundary.sqlite-wal` and `-shm` are no longer committed.** `.gitignore` covered
  `*.sqlite` and not its sidecars, so the 2026-09-12 commit carried a write-ahead log for a
  database that is itself ignored: a checkout got a WAL with no database beside it. Untracked,
  and the ignore rule now covers `-wal`, `-shm` and `-journal`. Found while writing the invoice
  check, which turned on exactly this distinction.
- **`docs/invoice-check.md`**, the first ledger-against-invoice check, run early because 03's
  first official run moved from Sep 27 to Sep 13. 70,834 calls across three projects merged into
  one ledger, US$61.3551 to 2026-09-14, reconciling with 03's own independent accounting to
  within US$0.000001 over 35,728 of those calls. The four vendor console figures wait for
  October. Two things the check found about itself are recorded there: gathering ledgers with a
  plain `cp` drops rows still in the write-ahead log, which `ledger merge` itself handles
  correctly and an operator tidying files beforehand does not; and September was priced from two
  repositories, so the costing cannot be reproduced from this one alone.

## 0.2.0 (2026-09-11)

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
- **Live calls, 2026-09-11, which are what the tag waited on.** All four providers answered
  under 0.2 from GitHub Actions. A real two-request Anthropic batch was submitted and polled,
  the vendor reporting its own counts; it had not ended within fifteen minutes, so it was
  collected by a second run in collect mode, from the first run's ledger artefact and with
  nothing carried between them but the batch id. Both rows completed at 16 input and 4 output
  tokens for US$0.000018 each, which is Haiku 4.5's list price times the 0.5 batch
  multiplier, computed from the returned usage and not estimated. Zero errors and zero
  uncosted rows in the report. A local Ollama server answered at price zero, costed rather
  than left unknown. Ten live calls in all this day, well under a cent.

- **A local OpenAI-compatible host, exercised live.** Ollama 0.34.0 with `llama3.2:3b` on
  the laptop, answering through the `local` provider entry at price zero: ledger row 5,
  status 200, 32 input and 2 output tokens, `costed = 1` and cost 0.00, uncosted count
  still zero. It is the one live call that can be made from a network that inspects TLS,
  because nothing leaves the machine. `boundary smoke local` now has that model as its
  default.
- **`boundary smoke <provider> --batch`** submits two short requests as a real vendor batch
  and collects them, so the batch path can be exercised live the way a single call already
  could. `--wait` and `--poll` control how long it will sit there.
- **`boundary batch status <id>` and `boundary batch collect <id>`.** A batch the vendor has
  not finished is the ordinary case, not a failure, so there has to be a way to come back to
  one. `collect` rebuilds the handle from the ledger and completes the rows; `--ledger`
  points at the ledger that submitted it, which for a hosted runner means one restored from
  that run's artefact. `status` exits non-zero until the batch has ended, so a script can
  wait on it. Added after the first live run: the vendor had not finished a two-request batch
  within fifteen minutes, and abandoning it would have meant paying for work with no record
  and no result.
- The smoke workflow gained a collect mode for the same reason, and lost the
  `continue-on-error` on its batch step. A green tick that meant "the batch was submitted"
  when nothing had been collected is worse than a red one.
- **A `smoke` workflow in this repository**, manual only, running one call per vendor and
  the batch path on GitHub's runners, because the laptop's usual network inspects TLS and a
  vendor call from there would pass a personal key and a prompt through an intermediary that
  should see neither. It
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
