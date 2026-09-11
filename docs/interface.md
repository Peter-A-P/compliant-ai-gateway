# The `boundary` interface

**Status: frozen 2026-09-08** (draft written 2026-09-07, reviewed and frozen by Peter a day
early). From here, changes within a major version are additive only: new optional fields, new methods, new
error subclasses. Anything removed or renamed is a major version and a note in the plan of
every project that pins the old one.

This page is what the 02 and 03 repositories are written against. The 03 drift runner is
written from Sep 16 and needs `Gateway.chat` in pass-through mode, the raw store and the
ledger row. The 02 runner (November) needs standard mode, batches (v0.2) and the local
price-zero host.

Column "since" is the version each item first appeared in. Everything added after the
freeze is listed here with its version: 0.2 (in progress, September 2026) adds the `env`
argument and configuration key, ledger schema v2's two columns and v3's one, Anthropic
Message Batches, `boundary ledger merge` and `boundary experiment`. Nothing that 0.1.0
offered has changed shape.

## 1. Importing

```python
from boundary import Gateway, ChatRequest, ChatResponse, Usage, Mode
from boundary import (
    BoundaryError,
    ConfigError,
    UnknownAlias,
    UnknownPrice,
    SpendCapExceeded,
    PassthroughViolation,
    ProviderError,
)
```

## 2. `Gateway`

| Item | Signature | Since | Notes |
|---|---|---|---|
| Construct from a file | `Gateway.from_config(path, *, project, ledger_path=None, raw_store=None, strict_cost=False, env=None)` | 0.1, `env` 0.2 | `project` is the ledger's project column and the key into `caps.yaml`. `ledger_path` overrides the config's ledger path (the Actions runner passes a path inside the checkout). `raw_store` is a directory the caller owns; required for any pass-through call. `strict_cost=True` raises `UnknownPrice` instead of writing an uncosted row. `env` labels the rows this gateway writes, so a merged ledger says where a call was made; it defaults to `BOUNDARY_ENV`, then `ledger.env` in the configuration |
| Synchronous call | `gw.chat(request, *, purpose, run_id=None, mode=Mode.STANDARD) -> ChatResponse` | 0.1 | `purpose` is a short free-text label for the ledger ("drift-run", "grader-dev"). `run_id` groups rows and is what the per-run cap is measured against |
| Asynchronous call | `await gw.achat(request, *, purpose, run_id=None, mode=Mode.STANDARD) -> ChatResponse` | 0.1 | Same adapter code path as `chat`; concurrency is the caller's business |
| Escape hatch | `gw.raw(provider, method, path, json, *, purpose, run_id=None, mode=Mode.STANDARD) -> RawResponse` | 0.1 | For a vendor feature the library does not model. Traced and ledgered; costed when the body carries usage in a shape the adapter knows, otherwise written uncosted |
| Resolve without calling | `gw.resolve(model, mode=Mode.STANDARD) -> ModelRef` | 0.1 | What an alias points at right now. Lets a runner print the identifiers it is about to use |
| Close | `gw.close()`, and `with Gateway.from_config(...) as gw:` | 0.1 | Flushes the ledger and telemetry, closes the HTTP client |
| Submit a batch | `gw.batch_submit(requests, *, purpose, run_id=None) -> BatchHandle` | 0.2 | Anthropic Message Batches. Standard mode by definition; the development cache is not consulted. One ledger row per request, written before the submit leaves and carrying the estimate at the batch rate. The caps are checked once, for the whole batch |
| Collect a batch | `gw.batch_results(handle, *, wait_s=0.0, poll_s=30.0) -> list[ChatResponse]` | 0.2 | Completes one row per request from the returned usage at the batch rate. Does not wait by default: `BatchNotReady` is the ordinary answer to "is it done". Responses come back in submission order |
| Ask after a batch | `gw.batch_status(handle) -> BatchProgress` | 0.2 | `processing_status`, `ended`, `results_url` and the vendor's counts. Writes nothing |
| Rebuild a handle | `gw.batch_handle(batch_id) -> BatchHandle` | 0.2 | From the ledger, so a batch can be collected by a process that did not submit it. The batch id is the only thing a caller has to keep |

## 3. `ChatRequest`

Frozen dataclass. Vendor-neutral; the adapter places each field where the vendor expects it.

| Field | Type | Default | Since | Notes |
|---|---|---|---|---|
| `model` | `str` | required | 0.1 | An alias from the routes file (standard mode only), or the explicit `provider/model-id`. The provider part is a key in `providers` in `boundary.yaml`. Model identifiers may themselves contain `/` (open-weights hosts); the split is on the first `/` |
| `messages` | `Sequence[Mapping[str, Any]]` | required | 0.1 | `{"role": ..., "content": ...}` each. Content is a string or a vendor-shaped list of blocks, passed through untouched |
| `system` | `str \| None` | `None` | 0.1 | Placed where the vendor expects a system prompt |
| `max_tokens` | `int \| None` | `None` | 0.1 | **Required in pass-through mode**; filling a default would be rewriting. Standard mode uses `defaults.max_tokens` when None |
| `temperature` | `float \| None` | `None` | 0.1 | Passed through when set; 0.0 to 2.0 |
| `stop` | `Sequence[str] \| None` | `None` | 0.1 | Passed through when set |
| `extra` | `Mapping[str, Any]` | `{}` | 0.1 | Vendor-specific fields merged into the body verbatim, last. Part of "what the caller built" in the byte-equality sense |
| `is_explicit` | property `bool` | | 0.1 | True when `model` has the `provider/model-id` form |

Validation at construction: non-empty model with no surrounding whitespace, at least one
message, every message has `role` and `content`, positive `max_tokens`, temperature in range.

## 4. `ChatResponse`

Frozen dataclass. Returned for every call that produced a ledger row, including vendor
errors in pass-through mode (where an error is a result).

| Field | Type | Since | Notes |
|---|---|---|---|
| `text` | `str \| None` | 0.1 | First text block; None on error |
| `finish_reason` | `str \| None` | 0.1 | Vendor's reason, lower case |
| `usage` | `Usage` | 0.1 | `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens`, property `total_tokens`. As returned by the vendor, never estimated |
| `cost_usd` | `float \| None` | 0.1 | From usage and the dated price list; None when `costed` is False |
| `costed` | `bool` | 0.1 | False when the returned model has no price. The library never guesses |
| `model_requested` | `str` | 0.1 | Explicit `provider/model-id` after alias resolution |
| `model_returned` | `str \| None` | 0.1 | The identifier the vendor reported |
| `provider` | `str` | 0.1 | The `providers` key the call went to |
| `latency_ms` | `float` | 0.1 | Wall time of the upstream call, retries included |
| `status` | `int \| str` | 0.1 | HTTP status, or the exception type name when no response arrived (`"ReadTimeout"`) |
| `headers` | `Mapping[str, str]` | 0.1 | Response headers in full, for the drift record: request ids, model or version headers, rate-limit headers |
| `raw` | `Any` | 0.1 | Parsed response body, or None if unparseable |
| `ledger_id` | `int` | 0.1 | Row id, so a caller can join its own records to the ledger |
| `mode` | `Mode` | 0.1 | The mode the call ran in |
| `retries` | `int` | 0.1 | Retries made; always 0 in pass-through |
| `cached` | `bool` | 0.1 | Development cache answered; never in pass-through |
| `price_list` | `str \| None` | 0.1 | Date of the price file used, for example `"2026-09-07"` |
| `trace_id` | `str \| None` | 0.1 | OpenTelemetry trace id, hex, or None when telemetry is off |
| `ok` | property `bool` | 0.1 | 2xx status |

## 4a. `BatchHandle`

Frozen dataclass, returned by `batch_submit` and rebuildable with `batch_handle`. Every
field is also a ledger column, which is what makes "submit now, collect hours later in
another process" work.

| Field | Type | Since | Notes |
|---|---|---|---|
| `provider` | `str` | 0.2 | The providers key the batch went to. One batch goes to one provider |
| `batch_id` | `str` | 0.2 | The vendor's identifier, written to every row of the batch |
| `project`, `purpose`, `run_id` | `str`, `str`, `str \| None` | 0.2 | As passed at submit |
| `custom_ids` | `tuple[str, ...]` | 0.2 | The `call_uid` of each row, in submission order. Sent to the vendor as `custom_id`, which is how a result is matched back to exactly one row |
| `ledger_ids` | `tuple[int, ...]` | 0.2 | Row ids in the same order |

`len(handle)` is the number of requests.

`BatchProgress` (from `batch_status`) carries `batch_id`, `processing_status`, `ended`,
`results_url` and `counts`. Each `ChatResponse` from `batch_results` has `latency_ms` of
0.0 and no headers: a batched request has no wall time or response of its own. Its `status`
is 200 when the vendor answered it, and otherwise the vendor's word for what happened
(`errored`, `expired`, `canceled`) or `missing`.

## 5. `Mode`

| Value | Retries | Cache | Rewriting | Raw bytes stored | Aliases |
|---|---|---|---|---|---|
| `Mode.STANDARD` (`"standard"`) | 429, 5xx, timeouts; backoff with jitter; at most `retry.max_attempts`; honours `Retry-After` | Optional exact-match development cache | Alias resolution, defaults filled in | No | Allowed |
| `Mode.PASSTHROUGH` (`"passthrough"`) | None; an error is a result | Never | None | Request and response bytes plus response headers, to the caller-owned raw store | Refused with `PassthroughViolation` |

Pass-through is enforced by tests: byte equality between the request the adapter built
and the bytes that left the process, and exactly one upstream request even when a cache is
configured.

## 6. Errors

All subclass `BoundaryError`.

| Error | Raised when | Carries | Since |
|---|---|---|---|
| `ConfigError` | A configuration file is missing, malformed or inconsistent; an explicit provider is unknown | message | 0.1 |
| `UnknownAlias` | An alias is not in the routes file | `alias`, `known` | 0.1 |
| `UnknownPrice` | Strict costing asked for and the returned model has no price | `provider`, `model`, `price_list` | 0.1 |
| `SpendCapExceeded` | The pre-call estimate would pass a cap. **No request was made** | `scope`, `cap_usd`, `spent_usd`, `estimate_usd` | 0.1 |
| `PassthroughViolation` | Alias, cache, or missing `max_tokens` in pass-through mode; pass-through without a raw store | message | 0.1 |
| `BatchNotReady` | Results were asked for before the vendor finished the batch. Not a failure: "not yet" is the ordinary answer | `batch_id`, `processing_status`, `counts` | 0.2 |
| `ProviderError` | Non-2xx after retries (standard) or transport failure. In pass-through the same information is returned as a `ChatResponse` instead | `provider`, `status`, `body`, `retries`, `headers` | 0.1 |

## 7. The ledger row (schema v3)

One row per call, written before the response is returned, including failures. Columns
are additive only; never renamed or removed. Field-by-field notes in `docs/ledger.md`
(Sep 8, v2 Sep 10).

```
id, ts_utc, boundary_version, project, purpose, run_id, mode, provider, alias,
model_requested, model_returned, region, input_tokens, output_tokens, cache_read_tokens,
cache_write_tokens, price_list, cost_usd, costed, cached, latency_ms, http_status,
error_type, retries, request_sha256, response_sha256, trace_id, span_id, raw_path
call_uid, env                                                             -- added in 0.2
batch_id                                                                  -- added in 0.2
```

`call_uid` identifies the call across files and `env` says which environment made it;
together they are what lets `ledger merge` combine one file per environment and be safe to
run again. `batch_id` (v3) names the vendor batch a row belongs to, and is null for every
ordinary call. A file is upgraded in place on open, one version at a time and additively. A
reader written against v1 still works: nothing moved, and `SELECT` by name is unaffected.

One thing for a repository that pins `boundary>=0.1,<0.3`: the upgrade is one way. Once a
0.2 gateway has opened a ledger file, 0.1.0 refuses to write to it, by design, rather than
adding rows with no `call_uid` that a later merge would duplicate. It raises at
construction with the schema versions named. Either pin one version per ledger file, or
give each version its own file and merge them.

**Merging never writes to a source** (changed in 0.2, 2026-09-11). The rows are read from a
temporary copy and the copy is what gets upgraded, so a file stays at the version its owner
wrote it at and that owner can carry on appending to it. This is what lets a 0.2 gateway
merge the ledgers a 0.1.0 runner commits each month without disturbing the next run. Before
this change, merging a source upgraded it and the older library then refused to write to
it.

## 8. Configuration files

| File | Contents | Owner |
|---|---|---|
| `config/boundary.yaml` | `providers` (name, kind, base URL, key environment variable, pinned API version), `routes` (alias to provider, model, optional API version and region; may point at a separate `routes.yaml`), `defaults`, `retry`, `ledger` (`path`, and `env` since 0.2), `telemetry`, `cache`, and the paths to the two files below | This repository; a project may ship its own |
| `config/caps.yaml` | `portfolio_monthly_usd`, per-project `monthly_usd` and `per_run_usd`, a `default` | This repository |
| `config/prices/YYYY-MM-DD.yaml` | USD per million tokens per provider and model: `input`, `output`, optional `cache_read`, `cache_write`, `batch_multiplier`; `source` names where the numbers came from. The newest date is used. A new price is a new file | This repository |

Keys come only from the environment variables named in `api_key_env`. Nothing in any
configuration file is secret.

## 9. Command line

| Command | Since | What it does |
|---|---|---|
| `boundary smoke <provider>` | 0.1 | One short standard-mode call, costed, with the ledger row id. `local` defaults to `llama3.2:3b` on a local server at price zero |
| `boundary smoke <provider> --batch` | 0.2 | Two short requests as a real vendor batch, submitted and collected. `--wait` and `--poll` set how long it will sit there |
| `boundary routes show` | 0.1 | What every alias points at, and the provider entries |
| `boundary prices check` | 0.1 | Validates every price file, warns when the newest is not this month, lists routes with no price |
| `boundary ledger report` | 0.1 | Calls, tokens and cost by month, environment, project and model |
| `boundary bench` | 0.1 | The README's measured row, against an in-process mock |
| `boundary ledger merge --into <dest> <sources...>` | 0.2 | Combines per-environment ledgers. Idempotent; `--dry-run` reports without writing |
| `boundary experiment remote-ledger` | 0.2 | The Rule C measurement behind `docs/rejected.md` |

## 10. What is deliberately not here in 0.x

Streaming, typed tool calls, embeddings, any server, any content inspection. See PLAN.md
section 2.8. Tool-use fields pass through inside `messages` and `extra` untouched.

## 11. Choices the plan left open

Answered in the draft and accepted at the freeze on 2026-09-08.

1. `ChatResponse` has five fields beyond the plan's list (`provider`, `model_requested`,
   `mode`, `retries`, `cached`, `price_list`, `trace_id`). All are also ledger columns;
   returning them saves a caller a ledger lookup.
2. `max_tokens` is required in pass-through mode rather than defaulted. Defaulting it would
   be a rewrite the drift record could not see.
3. The raw store is a constructor argument, not a per-call one, so a run has one directory
   and a call cannot land in the wrong one.
4. Provider entries are named by the user (`anthropic`, `openai`, `google`, `openweights`,
   `local`), and the name is what the ledger records. Renaming a provider entry is
   therefore a change to the data; the 03 plan should fix the names before the first run.
5. Amounts in `caps.yaml` are USD, because that is what vendors bill in; the plan
   repository converts at reporting time.
