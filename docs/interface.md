# The `boundary` interface

**Status: draft for review, written 2026-09-07. Freezes 2026-09-09.** After the freeze,
changes within a major version are additive only: new optional fields, new methods, new
error subclasses. Anything removed or renamed is a major version and a note in the plan of
every project that pins the old one.

This page is what the 02 and 03 repositories are written against. The 03 drift runner is
written from Sep 16 and needs `Gateway.chat` in pass-through mode, the raw store and the
ledger row. The 02 runner (November) needs standard mode, batches (v0.2) and the local
price-zero host.

Column "since" is the version each item first appeared in.

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
| Construct from a file | `Gateway.from_config(path, *, project, ledger_path=None, raw_store=None, strict_cost=False)` | 0.1 | `project` is the ledger's project column and the key into `caps.yaml`. `ledger_path` overrides the config's ledger path (the Actions runner passes a path inside the checkout). `raw_store` is a directory the caller owns; required for any pass-through call. `strict_cost=True` raises `UnknownPrice` instead of writing an uncosted row |
| Synchronous call | `gw.chat(request, *, purpose, run_id=None, mode=Mode.STANDARD) -> ChatResponse` | 0.1 | `purpose` is a short free-text label for the ledger ("drift-run", "grader-dev"). `run_id` groups rows and is what the per-run cap is measured against |
| Asynchronous call | `await gw.achat(request, *, purpose, run_id=None, mode=Mode.STANDARD) -> ChatResponse` | 0.1 | Same adapter code path as `chat`; concurrency is the caller's business |
| Escape hatch | `gw.raw(provider, method, path, json, *, purpose, run_id=None, mode=Mode.STANDARD) -> RawResponse` | 0.1 | For a vendor feature the library does not model. Traced and ledgered; costed when the body carries usage in a shape the adapter knows, otherwise written uncosted |
| Resolve without calling | `gw.resolve(model, mode=Mode.STANDARD) -> ModelRef` | 0.1 | What an alias points at right now. Lets a runner print the identifiers it is about to use |
| Close | `gw.close()`, and `with Gateway.from_config(...) as gw:` | 0.1 | Flushes the ledger and telemetry, closes the HTTP client |
| Batches | `gw.batch_submit(requests, *, purpose, run_id) -> BatchHandle`, `gw.batch_results(handle) -> list[ChatResponse]` | 0.2 | Anthropic Message Batches; ledger rows written at result time at the batch rate |

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
| `ProviderError` | Non-2xx after retries (standard) or transport failure. In pass-through the same information is returned as a `ChatResponse` instead | `provider`, `status`, `body`, `retries`, `headers` | 0.1 |

## 7. The ledger row (schema v1)

One row per call, written before the response is returned, including failures. Columns
are additive only; never renamed or removed. Field-by-field notes in `docs/ledger.md`
(Sep 8).

```
id, ts_utc, boundary_version, project, purpose, run_id, mode, provider, alias,
model_requested, model_returned, region, input_tokens, output_tokens, cache_read_tokens,
cache_write_tokens, price_list, cost_usd, costed, cached, latency_ms, http_status,
error_type, retries, request_sha256, response_sha256, trace_id, span_id, raw_path
```

## 8. Configuration files

| File | Contents | Owner |
|---|---|---|
| `config/boundary.yaml` | `providers` (name, kind, base URL, key environment variable, pinned API version), `routes` (alias to provider, model, optional API version and region; may point at a separate `routes.yaml`), `defaults`, `retry`, `ledger`, `telemetry`, `cache`, and the paths to the two files below | This repository; a project may ship its own |
| `config/caps.yaml` | `portfolio_monthly_usd`, per-project `monthly_usd` and `per_run_usd`, a `default` | This repository |
| `config/prices/YYYY-MM-DD.yaml` | USD per million tokens per provider and model: `input`, `output`, optional `cache_read`, `cache_write`, `batch_multiplier`; `source` names where the numbers came from. The newest date is used. A new price is a new file | This repository |

Keys come only from the environment variables named in `api_key_env`. Nothing in any
configuration file is secret.

## 9. Command line

`boundary smoke <provider>`, `boundary routes show`, `boundary prices check`,
`boundary ledger report`, `boundary ledger merge` (merge and report in 0.2). Sep 10 to 12.

## 10. What is deliberately not here in 0.x

Streaming, typed tool calls, embeddings, any server, any content inspection. See PLAN.md
section 2.8. Tool-use fields pass through inside `messages` and `extra` untouched.

## 11. Open for review until Sep 9

Questions the plan left open that this draft answers; say if you would answer differently.

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
