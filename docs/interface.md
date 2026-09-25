# The `boundary` interface

**Status: frozen 2026-09-08** (draft written 2026-09-07, reviewed and frozen by Peter a day
early). From here, changes within a major version are additive only: new optional fields, new methods, new
error subclasses. Anything removed or renamed is a major version and a note in the plan of
every project that pins the old one.

This page is what the 02 and 03 repositories are written against. The 03 drift runner is
written from Sep 16 and needs `Gateway.chat` in pass-through mode, the raw store and the
ledger row. The 02 runner (November) needs standard mode, batches (v0.2) and the local
price-zero host.

Column "since" is the version each item first appeared in. Every release after the freeze is
listed here; nothing any earlier version offered has changed shape.

| Version | Date | What it added to this interface |
|---|---|---|
| 0.1.0 | 2026-09-10 | The frozen interface: `Gateway`, `ChatRequest`, `ChatResponse`, `Usage`, `Mode`, the error hierarchy, the ledger row and the raw store |
| 0.2.0 | 2026-09-11 | The `env` argument and configuration key, ledger schema v2's two columns and v3's one, Anthropic Message Batches with their command-line collection, `boundary ledger merge`, `boundary experiment` |
| 0.2.1 | 2026-09-18 | The three hyperscaler adapters, the `credentials` and `residency` provider fields, schema v4's `residency` column, `boundary ledger residency` |
| 0.2.2 | 2026-09-18 | Schema v5's `price_sha256` column and the matching field on `ChatResponse` |
| 0.3.0 | 2026-09-19 | Streaming for `openai_compat` hosts (`chat_stream`, `achat_stream`, `ttft_ms` on `ChatResponse`, schema v6's `ttft_ms` column), measured price overlays for self-hosted hosts (`self_hosted`, `self_hosted_prices`), `boundary smoke --stream` |
| 0.4.0 | 2026-09-19 | `data_class` on every call method, the `DataClass` vocabulary, schema v7's `data_class` column, `data_class` and `call_uid` on `ChatResponse`, `--data-class` on both ledger commands |
| 0.5.0 | 2026-09-19 | The `boundary.redact` package (section 12), pulled forward from Part B for project 07 |
| 0.5.1 | 2026-09-19 | `EntityType.ADDRESS`; three fixes from 07's first run, none of them interface changes |
| 0.5.2 to 0.5.4 | 2026-09-19, 2026-09-20 | Leak fixes in the policy's second pass, the placeholder clash guard (`minted_placeholders_in`, `Leak.kind == "placeholder"`), and a `--data-class` filter that accepts a class a later version wrote |
| 0.5.5 | 2026-09-20 | `Policy(..., vault=...)`, so a policy rebuilt between redacting and rehydrating resolves what the first one found |
| 0.5.6 | 2026-09-20 | `sweep`, `SWEEP_ID`, `is_name_shaped`, `name_parts`; a name part the vocabulary allows is no longer licensed |
| 0.5.7 | 2026-09-20 | `retype` on `sweep` and the `RETYPED` default: a span that is exactly a detected person's name parts and carries an impersonal type is re-typed `PERSON` |
| 0.5.8 | 2026-09-20 | `swept`, `retyped`, `original_recogniser`: the sweep's two buckets, countable |
| 0.6.0 | 2026-09-20 | `boundary redact eval` and the modules behind it, `boundary.redact.corpus` and `boundary.redact.evaluate`: a generated labelled corpus and the first redaction measurement this repository owns. The EMAIL recogniser reads letters in any script and the policy's second pass masks address-shaped text, both from a leak the harness found on its first run |
| 0.6.1 | 2026-09-20 | `boundary redact eval --identifiers` and `boundary.redact.identifiers`: the Canadian identifier set, every claimed shape in every written form, the shapes no recogniser claims, and the near-misses that must not fire |
| 0.6.2 | 2026-09-20 | The identifier set measures the second pass with every recogniser removed, a column that found a bracketed area code being published beside its own placeholder. `FamilyRow.backstop` |
| 0.6.3 | 2026-09-21 | `Policy.second_pass`: which placeholders the fallback minted, after 0.6.0 made the entity type stop answering that |
| 0.6.4 | 2026-09-21 | `boundary redact eval --rehydration` and `placeholder_kind`. Inside the brackets a placeholder now tolerates a hyphen, a space or a line break where its underscore was, and a zero-padded index; four mutation forms models produce used to resolve to nothing |
| 0.7.0 | 2026-09-22 | The `boundary.audit` package (section 13): the hash chain, the append-only SQLite log, anchors, and `boundary audit seal`, `anchor`, `verify` and `tamper-test`. Nothing any earlier version exposes changed, and the gateway does not call it |
| 0.8.0 | 2026-09-23 | `Policy.redact_with_spans`, which `redact` now calls, and `boundary.redact.tab` with `boundary redact eval --tab`: the Text Anonymization Benchmark, the first public corpus and the first real text. `redact` returns exactly what it did |
| 0.9.0 | 2026-09-23 | No new names. The policy's second pass masks initials and surname particles with the name they belong to (`names.PARTICLES`, `names.COMMON_INITIALISMS`), a change in what `redact` and `outbound` mask rather than in any signature; `tab.fetch` accepts `train` and `dev` |
| 0.10.0 | 2026-09-23 | `tab.derive_allow`, `tab.read_allow`, `allow=` on `tab.run`, and `boundary redact eval --tab-allow`: a jurisdiction's allow list derived from labelled data and measured. No change to the policy |
| 0.11.0 | 2026-09-23 | `Policy.released` and `RELEASABLE`: a caller's `allow` terms also release a detector's LOCATION or ORGANISATION span that is one of them. A caller who passes no `allow` sees no change |
| 0.12.0 | 2026-09-23 | The data policy (section 14): `Gateway(..., policy=)`, the `policy` configuration key, `PolicyRefused`, `boundary.enforce` and `boundary policy eval`. Opt-in: a gateway with no policy behaves as before |
| 0.13.0 | 2026-09-25 | The OpenAI-compatible proxy (section 15): `boundary.server`, the `server` extra, `boundary serve` and `boundary teams key`. `on_text` on `chat_stream` and `achat_stream`. Nothing a library caller already uses changed |
| 0.16.0 | 2026-09-25 | `Gateway.record_refusal` and `CALLER_REFUSALS` (a redaction refusal is on the ledger); `boundary.redact.request` (`redact_request` with `allow=`, `StreamRehydrator`, moved from `boundary.server.redaction`, which re-exports them) and `boundary.redact.preserve`; `boundary redact overmask`; `boundary redact mutation --arms` and repeated `--score` |
| 0.15.0 | 2026-09-25 | Redaction in the proxy (section 15). `redacted=` on `chat`, `achat`, `chat_stream` and `achat_stream`, `redacted` on `ChatResponse`, schema v8's `redacted` column; `ClassRule.redacted_as`, `decide(..., redacted=)` and `Decision.judged_as` (section 14). A caller that passes nothing new sees no change |
| 0.14.0 | 2026-09-25 | `boundary redact mutation` and `boundary.redact.mutation` (`collect`, `score`, `classify`, `newcombe`, `PRESERVE`, `ASKED`): placeholder mutation under a model, from a stored run. Not re-exported from `boundary.redact` |
| 0.13.1 | 2026-09-25 | `boundary policy eval --proxy` and `enforce_eval.run_proxy`, `door`, `PROXY_CLASSES`, `PROXY_ENTRY_POINTS`: the adversarial suite through the proxy over HTTP |

## 1. Importing

```python
from boundary import Gateway, ChatRequest, ChatResponse, Usage, Mode
from boundary import DataClass  # 0.4
from boundary import __version__  # the installed version, as a string
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
| Synchronous call | `gw.chat(request, *, purpose, run_id=None, mode=Mode.STANDARD, data_class=None, redacted=False) -> ChatResponse` | 0.1, `data_class` 0.4, `redacted` 0.15 | `purpose` is a short free-text label for the ledger ("drift-run", "grader-dev"). `run_id` groups rows and is what the per-run cap is measured against. `data_class` is what kind of data the request carries, from the closed vocabulary in section 5a; it is written to the row and the span, and in 0.x enforced by nothing. None means no claim was made; a word outside the vocabulary raises `ValueError` before the model is resolved, and no row is written. `redacted=True` says the caller redacted the payload before handing it over: it is written to the row, and where the policy's rule for the class names a `redacted_as` the call is judged by that class (section 14). The library cannot check it, exactly as it cannot check `data_class` |
| Asynchronous call | `await gw.achat(request, *, purpose, run_id=None, mode=Mode.STANDARD, data_class=None, redacted=False) -> ChatResponse` | 0.1, `data_class` 0.4, `redacted` 0.15 | Same adapter code path as `chat`; concurrency is the caller's business |
| Streamed call | `gw.chat_stream(request, *, purpose, run_id=None, mode=Mode.STANDARD, data_class=None, on_text=None, redacted=False) -> ChatResponse` | 0.3, `data_class` 0.4, `on_text` 0.13, `redacted` 0.15 | Sends `stream: true` with `stream_options: {include_usage: true}`, reads the events as they arrive and returns the whole answer: full text, the usage from the final event, and `ttft_ms`. One ledger row per call. **Standard mode only**: `Mode.PASSTHROUGH` is refused with `PassthroughViolation` before anything is built, and the development cache is never consulted. `openai_compat` only; every other kind raises `NotImplementedError` naming the kind. A stream that fails after it began is not retried, because the host may bill for tokens the library cannot count. `on_text(piece)`, when given, is handed the text as it arrives; on a successful call the pieces joined are exactly `text`, none repeated, because a stream is only retried before its first byte. If `on_text` raises it is not called again, the stream is read to its end and the row written, and then its exception is raised, so a callback cannot leave a row in flight |
| Streamed call, async | `await gw.achat_stream(request, *, purpose, run_id=None, mode=Mode.STANDARD, data_class=None, on_text=None, redacted=False) -> ChatResponse` | 0.3, `data_class` 0.4, `on_text` 0.13, `redacted` 0.15 | The async twin, with an awaited `on_text`. The transport's connection pool admits at least 64 streams at once against one host, and a test holds 64 open |
| Escape hatch | `gw.raw(provider, method, path, json, *, purpose, run_id=None, mode=Mode.STANDARD, data_class=None) -> RawResponse` | 0.1, `data_class` 0.4 | For a vendor feature the library does not model. Traced and ledgered; costed when the body carries usage in a shape the adapter knows, otherwise written uncosted |
| Resolve without calling | `gw.resolve(model, mode=Mode.STANDARD) -> ModelRef` | 0.1 | What an alias points at right now. Lets a runner print the identifiers it is about to use |
| Close | `gw.close()`, and `with Gateway.from_config(...) as gw:` | 0.1 | Flushes the ledger and telemetry, closes the HTTP client |
| Submit a batch | `gw.batch_submit(requests, *, purpose, run_id=None, data_class=None) -> BatchHandle` | 0.2, `data_class` 0.4 | Anthropic Message Batches. Standard mode by definition; the development cache is not consulted. One ledger row per request, written before the submit leaves and carrying the estimate at the batch rate. The caps are checked once, for the whole batch. One `data_class` covers the batch and is written to every row: the requests travel together, so they carry the class of the most sensitive one |
| Collect a batch | `gw.batch_results(handle, *, wait_s=0.0, poll_s=30.0) -> list[ChatResponse]` | 0.2 | Completes one row per request from the returned usage at the batch rate. Does not wait by default: `BatchNotReady` is the ordinary answer to "is it done". Responses come back in submission order |
| Ask after a batch | `gw.batch_status(handle) -> BatchProgress` | 0.2 | `processing_status`, `ended`, `results_url` and the vendor's counts. Writes nothing |
| Record a refusal | `gw.record_refusal(request, *, purpose, error_type, run_id=None, mode=Mode.STANDARD, data_class=None) -> int` | 0.16 | The row for a call the caller refused before handing it over: no body, no key, no hash, cost zero, `error_type` from `CALLER_REFUSALS` (`redaction_refused`). Returns the row id |
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
| `price_sha256` | `str \| None` | 0.2.2 | Fingerprint of the rates that file held, hashed from the parsed values. A date is not unique across repositories; this is, so a caller can record what it was charged at without trusting that two files sharing a date shared their contents |
| `trace_id` | `str \| None` | 0.1 | OpenTelemetry trace id, hex, or None when telemetry is off |
| `ttft_ms` | `float \| None` | 0.3 | Streamed calls only: wall time from sending the request to the first content delta arriving. None when the call was not streamed, or when no content arrived. `latency_ms` on a streamed call runs to the last byte. On a streamed call `raw` is the completion assembled from the events in the non-streaming shape, marked `assembled_from_stream_events`, and `response_sha256` on the row hashes the event bytes as received |
| `data_class` | `str \| None` | 0.4 | The class the caller declared on this call, as written to the row; None when none was declared |
| `call_uid` | `str \| None` | 0.4 | The row's `call_uid`. `ledger_id` is local to one file and reassigned by `ledger merge`; this is the identifier that survives it. A caller keeping its own records of a call (which document, which page, which decision) joins them to the ledger on this, which is the answer to "can I attach my own metadata to a call" (section 11, item 6) |
| `redacted` | `bool` | 0.15 | Whether the caller said the request was sent redacted, as written to the row. The library cannot check it |
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
`results_url` and `counts`. `BatchItemResult` (0.2, exported from `boundary`) is one
request's outcome inside a batch, matched back by `custom_id`: `custom_id`, `outcome` (the
vendor's word, `succeeded`, `errored`, `canceled` or `expired`), `parsed`, `error`, and the
`succeeded` property. Only a succeeded item has a parsed response, and only it is billed for
output. Each `ChatResponse` from `batch_results` has `latency_ms` of
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

## 5a. `DataClass` (0.4)

What kind of data a request carries, declared by the caller on every call method. The
vocabulary is PLAN.md section B2.2's, so that a row written now can be read by the Part B
policy without translation:

| Value | Meaning |
|---|---|
| `DataClass.PUBLIC` (`"public"`) | No confidentiality; may go anywhere |
| `DataClass.INTERNAL` (`"internal"`) | Not for publication, but identifies nobody |
| `DataClass.PERSONAL` (`"personal"`) | Identifies or could identify a person |
| `DataClass.SENSITIVE` (`"sensitive"`) | Personal data whose disclosure would cause serious harm (health, financial, legal), and anything under a legislative access restriction |

Three rules, the same shape as the residency column's:

- **It is a declaration, never an inference.** The library does not read content to guess a
  class, and it never will: a gateway that classified content would be making a compliance
  decision nobody reviewed. `data_class` on a call is the caller saying what it sent.
- **The vocabulary is closed.** A string outside it raises `ValueError` before the model is
  resolved and before any row exists, because a class the policy cannot place would be a row
  nobody can act on. The enum or its string value are both accepted.
- **Absent is not `public`.** None writes a null column, and the ledger commands report it
  as `undeclared`, apart from every declared class. "Nobody said" is the list an auditor
  asks for first, and folding it into the weakest class would hide it.

In 0.x the class is recorded and reported and nothing more: no route is refused, no layer is
required. Part B attaches the policy (section B2.2: `personal` requires redaction and forbids
the cache; the compliant provider set per class; fail closed when absent). The column is
what lets that policy be checked against calls made before it existed.

## 6. Errors

All subclass `BoundaryError`.

| Error | Raised when | Carries | Since |
|---|---|---|---|
| `ConfigError` | A configuration file is missing, malformed or inconsistent; an explicit provider is unknown | message | 0.1 |
| `UnknownAlias` | An alias is not in the routes file | `alias`, `known` | 0.1 |
| `UnknownPrice` | Strict costing asked for and the returned model has no price | `provider`, `model`, `price_list` | 0.1 |
| `SpendCapExceeded` | The pre-call estimate would pass a cap. **No request was made** | `scope`, `cap_usd`, `spent_usd`, `estimate_usd` | 0.1 |
| `PassthroughViolation` | Alias, cache, or missing `max_tokens` in pass-through mode; pass-through without a raw store; a streamed call asked for in pass-through mode (0.3) | message | 0.1 |
| `BatchNotReady` | Results were asked for before the vendor finished the batch. Not a failure: "not yet" is the ordinary answer | `batch_id`, `processing_status`, `counts` | 0.2 |
| `PolicyRefused` | The data policy forbids this class of data from reaching this provider (0.12). **No request was made**, and a ledger row with `error_type = 'policy_refused'` was written | `data_class`, `provider`, `reason`, `ledger_id` | 0.12 |
| `ProviderError` | Non-2xx after retries (standard) or transport failure. In pass-through the same information is returned as a `ChatResponse` instead | `provider`, `status`, `body`, `retries`, `headers` | 0.1 |

## 7. The ledger row (schema v8)

One row per call, written before the response is returned, including failures. Columns
are additive only; never renamed or removed. Field-by-field notes in `docs/ledger.md`
(Sep 8, v2 Sep 10, v4 Sep 15, v7 Sep 19, v8 Sep 25).

```
id, ts_utc, boundary_version, project, purpose, run_id, mode, provider, alias,
model_requested, model_returned, region, input_tokens, output_tokens, cache_read_tokens,
cache_write_tokens, price_list, cost_usd, costed, cached, latency_ms, http_status,
error_type, retries, request_sha256, response_sha256, trace_id, span_id, raw_path
call_uid, env                                                             -- added in 0.2
batch_id                                                                  -- added in 0.2
residency                                                                 -- added in 0.2
price_sha256                                                              -- added in 0.2.2
ttft_ms                                                                   -- added in 0.3.0
data_class                                                                -- added in 0.4.0
redacted                                                                  -- added in 0.15.0
```

`call_uid` identifies the call across files and `env` says which environment made it;
together they are what lets `ledger merge` combine one file per environment and be safe to
run again. `batch_id` (v3) names the vendor batch a row belongs to, and is null for every
ordinary call. `residency` (v4) is how far the request was allowed to travel from `region`,
copied from the provider entry's declaration, and is null when the entry declared nothing.
`ttft_ms` (v6) is the time to first content delta on a streamed call and null on every
other row; `latency_ms` on a streamed row runs to the last byte. `data_class` (v7) is the
class the caller declared on the call (section 5a), null when it declared none, and never
backfilled: nobody declared a class on a call made before there was a way to. `redacted`
(v8) is 1 when the caller said the request was sent redacted and null otherwise, never 0.

It is worth being exact about why `residency` is configuration rather than something parsed
from a response, because the distinction is the whole value of the column. No vendor reports
where a request was processed. Foundry does not say which hosting version served a
deployment, Vertex does not say where inference ran, and Bedrock strips the routing profile
out of the model identifier it echoes back. So the row records what the operator chose, at
the moment of the call, and never what the vendor did. Null and `"global"` are therefore
different statements and stay apart: null means no claim was made, `"global"` means the
weakest claim was made deliberately. A file is upgraded in place on open, one version at a time and additively. A
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
| `config/boundary.yaml` | `providers` (name, kind, base URL, key environment variable, pinned API version, `project` since 0.2.1 for `gcp_vertex` only, and `batches` since 0.2.1), `routes` (alias to provider, model, optional API version and region; may point at a separate `routes.yaml`), `defaults`, `retry`, `ledger` (`path`, and `env` since 0.2), `telemetry`, `cache`, and the paths to the two files below | This repository; a project may ship its own |
| `config/caps.yaml` | `portfolio_monthly_usd`, per-project `monthly_usd` and `per_run_usd`, a `default` | This repository |
| `<self_hosted_prices>/YYYY-MM-DD.yaml` | Since 0.3, optional. The same format as a price file, for providers flagged `self_hosted: true` only: rates a project **measured** for a host it runs itself, from a dated GPU-hour price and a measured throughput. `source` names the measurement run. Refused for any provider not so flagged, and the packaged vendor lists refuse to carry a flagged provider, so vendor prices keep one copy. A row costed from it cites it in `price_list` and `price_sha256`. See `docs/prices.md` | The project that measured them |
| `config/prices/YYYY-MM-DD.yaml` | USD per million tokens per provider and model: `input`, `output`, optional `cache_read`, `cache_write`, `batch_multiplier`; `source` names where the numbers came from. The newest date is used. A new price is a new file | This repository |

Keys come only from the environment variables named in `api_key_env`. Nothing in any
configuration file is secret.

**Provider kinds.** `anthropic`, `openai_compat` and `google` since 0.1. `azure_foundry` and
`gcp_vertex` since 0.2.1: both serve the Anthropic Messages API, so both reuse its body
builder and every one of its parsers, and both differ only in the envelope. Foundry takes the
Azure key in `api-key` and keeps `model` in the body, where it names the deployment. Vertex
takes a Google OAuth bearer token, moves `model` into the URL and `anthropic_version` into the
body, and needs `project` and a `region`. A Vertex `region` that disagrees with its `base_url`
host is refused rather than sent, because the host is what decides the geography.

**Batches.** Anthropic since 0.2; `openai_compat` and `google` since 0.2.1. A provider entry
must also opt in with `batches: true` for `openai_compat`, because most OpenAI-compatible
hosts serve chat completions and no batch endpoint; `anthropic` and `google` default on.
Foundry and Vertex have no batch API at all and are refused by name before anything is sent.
The OpenAI shape uploads its requests as a file before creating the batch, which is two round
trips rather than one; the ledger rows are written before the upload, because that is the
request the prompts leave in. Neither round trip is billed, so a failure in either completes
every row as a failure at no cost rather than leaving it in flight at an estimate.

## 9. Command line

| Command | Since | What it does |
|---|---|---|
| `boundary smoke <provider>` | 0.1 | One short standard-mode call, costed, with the ledger row id. `local` defaults to `llama3.2:3b` on a local server at price zero |
| `boundary smoke <provider> --batch` | 0.2 | Two short requests as a real vendor batch, submitted and collected. `--wait` and `--poll` set how long it will sit there |
| `boundary smoke <provider> --stream` | 0.3 | The same short call, streamed, printing time to first token beside the total. `openai_compat` hosts only |
| `boundary routes show` | 0.1 | What every alias points at, and the provider entries |
| `boundary prices check` | 0.1 | Validates every price file, warns when the newest is not this month, lists routes with no price |
| `boundary ledger report` | 0.1 | Calls, tokens and cost by month, environment, project, declared data class (0.4) and model. `--data-class personal` (0.4) keeps only the rows whose caller declared that class; `--data-class undeclared` keeps the rows that declared none. A word that matches neither the vocabulary nor anything in the ledger exits 2 rather than printing an empty table, and since 0.5.4 a class this version does not know but the file holds is a valid filter, because an old reader must still be able to query its own data |
| `boundary ledger residency` | 0.2.1 | Calls, tokens and models by provider, region and declared residency, widest reach first. `--require single-region|geo|global` exits 2 when any call went wider, and an undeclared row fails every limit. `--data-class personal` (0.4) is the audit question in one command: which calls carried personal data, and where did they go |
| `boundary bench` | 0.1 | The README's measured row, against an in-process mock |
| `boundary ledger merge --into <dest> <sources...>` | 0.2 | Combines per-environment ledgers. Idempotent; `--dry-run` reports without writing |
| `boundary batch status <id>` | 0.2 | Where the vendor has got to with a batch. Writes nothing; exits non-zero until it has ended, so a script can wait on it |
| `boundary batch collect <id>` | 0.2 | Completes the ledger rows of a batch submitted earlier, possibly by another process. `--ledger` points at the ledger that submitted it |
| `boundary audit seal` | 0.7 | Appends a record for every ledger row new or changed since the last seal. Idempotent. `--settle-hours` (default 24) holds a row in flight before sealing it as it stands |
| `boundary audit anchor --anchors FILE` | 0.7 | Appends the current head to an anchor file, one canonical JSON line; never rewrites the lines before it. Refuses an empty log |
| `boundary audit verify` | 0.7 | Recomputes the chain, checks every anchor in `--anchors`, and checks the ledger against the latest record for each call unless `--no-ledger`. Exits 1 on any break, and reports every break rather than the first |
| `boundary audit tamper-test` | 0.7 | The README's audit row: twelve kinds of corruption before and after the last anchor, with a control. In memory, from a seed |
| `boundary redact overmask --gold DIR` | 0.16 | How much of a question's source page the proxy's redaction masks, and how many of the phrases its answer must mention, on project 03's gold set. Offline |
| `boundary redact mutation` | 0.14 | Placeholder mutation under real models: `--models` (default Haiku 4.5, Llama 3.3 70B on Together, Gemini 3.5 Flash-Lite), `--pages` (default 20), `--max-usd` (default 0.35, after which no call is sent), `--out`. **Calls vendors and costs money.** `--score FILE` re-reads a stored run and makes no call; `--write-readme` fills the README rows from it |
| `boundary serve` | 0.13 | The OpenAI-compatible proxy (section 15). Needs the `server` extra. `--teams`, `--policy`, `--ledger`, `--host` (default `127.0.0.1`), `--port` (default 8080). Refuses to start without a data policy |
| `boundary teams key --team NAME` | 0.13 | Mints a proxy key, prints it once, and prints the SHA-256 line for `teams.yaml`. The key is stored nowhere |
| `boundary policy eval` | 0.12 | The adversarial suite for the data policy over this configuration's providers and aliases; exits 1 on any violation or false refusal. `--policy` names a file other than `policy.yaml` beside the configuration. `--proxy` (0.13.1) runs it through the proxy over HTTP instead, with the header's rules at the door; needs the `server` extra |
| `boundary experiment remote-ledger` | 0.2 | The Rule C measurement behind `docs/rejected.md` |
| `boundary experiment token-estimates <run-dir>` | 0.2.1 | The second Rule C measurement: local token estimates against returned usage, over a drift run's raw store and ledgers. Reads only; no network |

## 10. What is deliberately not here in 0.x

Streaming for any kind but `openai_compat`, typed tool calls, embeddings, any
content inspection **inside a call**. A server arrived in 0.13 (section 15), as Part B's
first stage pulled forward; it is a separate package behind an optional extra, and nothing
in `boundary` imports it. See PLAN.md section 2.8, which 0.3 amends: streaming
was out of scope until project 06 needed time to first token against self-hosted hosts,
and it arrived for the one kind those hosts speak. Tool-use fields pass through inside
`messages` and `extra` untouched. `boundary.redact` (0.5) inspects content, but only when
a caller hands it text; the gateway itself still reads nothing it sends.

## 12. `boundary.redact` (0.5)

Detection and the personal-class policy as library objects, with no gateway, configuration
or network. The full page is `docs/redact.md`; the shapes are here because 07 imports them.

```python
from boundary.redact import Analyzer, Policy, Span, EntityType, RedactionRefused
from boundary.redact.presidio import PresidioRecogniser   # optional `redact` extra
```

| Item | Signature | Since | Notes |
|---|---|---|---|
| Detect | `Analyzer(recognisers=DEFAULT_RECOGNISERS, *, extra=()).analyze(pages, *, first_page=1) -> list[Span]` | 0.5 | Every recogniser over every page. No span crosses a line break; no two overlap. `analyze_page(text, page)` for one page |
| A span | `Span(page, start, end, text, entity_type, score, recogniser)` | 0.5 | Frozen. `[start, end)` into that page's text, checked against `text` at construction. `entity_type` is `EntityType`, closed. `recogniser` names what fired |
| A recogniser | protocol: `id: str`, `analyze(text, page) -> Sequence[Span]` | 0.5 | Built-ins in `boundary.redact.recognisers`; `RegexRecogniser(id, entity_type, pattern, score, group=0, validate=None)` builds one |
| Presidio | `PresidioRecogniser(engine=None, *, language="en", entities=...)` | 0.5 | `ConfigError` without the extra. Accepts any object with Presidio's `analyze(text=, language=, entities=)` shape |
| Sweep | `sweep(pages, spans, *, first_page=1, vocabulary=DECISION_VOCABULARY, allow=(), retype=RETYPED) -> list[Span]` | 0.5.6, `retype` 0.5.7 | A person found anywhere licenses the other whole-word occurrences of their name parts everywhere. Returns the given spans plus the found ones, overlaps resolved; the added ones carry `recogniser == SWEEP_ID` (`boundary:sweep`). Case-sensitive, whole word, token runs not single words, vocabulary words refused. 07 measured +4.6 points of person recall on its hard name pool. Since 0.5.7 a span that is exactly those name parts and carries a type a consumer reads as impersonal (`LOCATION`, `ORGANISATION`, `NAME_LIKE`) is re-typed `PERSON` and carries `recogniser == f"{SWEEP_ID}:{original}"`; `retype=()` switches that off |
| Policy | `Policy(spans, *, allow=(), allow_patterns=(), vocabulary=DECISION_VOCABULARY, vault=None)` | 0.5, `vault` 0.5.5 | Document-wide. Placeholders `<TYPE_n>` and, for name parts, `<PERSON_n.m>`. Since 0.5.6 a name part the vocabulary allows is not licensed, so a heading typed as a PERSON no longer blacks out an ordinary word on every page. `vault` carries an earlier policy's placeholders in, which is what a pipeline that rebuilds its policy between redacting and rehydrating needs: values from `spans` are derived and survive a rebuild, values the second pass found are not |
| Outbound | `policy.outbound(text) -> str` | 0.5 | Substitute, second pass, then refuse with `RedactionRefused` unless clean. **For source text only** since 0.5.3: text carrying a placeholder this policy minted is refused first, because rehydration could not tell it from the policy's own work. `redact(text)` is the same without either refusal and is idempotent; `check(text) -> list[Leak]` is the second refusal's reason |
| Placeholder clash | `policy.minted_placeholders_in(text) -> list[Leak]` | 0.5.3 | What `outbound` refuses source text for. A placeholder the policy did **not** mint is not ambiguous: it is masked whole, as an opaque token, and survives the round trip as itself |
| Released spans | `policy.released -> tuple[Span, ...]`, `RELEASABLE` | 0.11 | The detector spans the caller's `allow` list released instead of masking: LOCATION or ORGANISATION only, and only when the span less a leading article is one of the caller's phrases or made only of the caller's words. Never a PERSON or an identifier, and never on the default vocabulary alone |
| Masked ranges | `policy.redact_with_spans(text) -> tuple[str, list[tuple[int, int]]]` | 0.8 | `redact`, and the half-open character ranges of the source it replaced: sorted, non-overlapping, merged where two touch. Computed by the same three steps, and `redact` is this with the ranges dropped, so the two cannot disagree. What an offset-scored benchmark needs |
| Inbound | `policy.rehydrate(text) -> str`, `policy.unresolved(text) -> list[str]` | 0.5 | Tolerant of case, inner spaces, dropped brackets and, since 0.6.4, a hyphen, space or line break in place of the underscore and a zero-padded index, all inside the brackets only. `PLACEHOLDER_EMAIL_1` does not resolve and will not: that is text around a placeholder, not a mutation of one |
| Which pass | `policy.second_pass -> frozenset[str]` | 0.6.3 | The placeholders the fallback minted rather than a recogniser's span. Read this, not the entity type: since 0.6.0 the second pass mints `EMAIL` for an address no recogniser claimed. This policy's own work only; one rebuilt from a `vault` reports an empty set |
| Vault | `policy.vault -> Mapping[str, str]` | 0.5 | placeholder to value, in memory. Grows as the second pass masks. Never written by the library |
| Sweep buckets | `swept(spans) -> list[Span]`, `retyped(spans) -> list[Span]`, `original_recogniser(span) -> str` | 0.5.8 | What the sweep added, against what it re-typed: an occurrence nothing found, against one that was found and called something impersonal. A recall figure sees only the first. `original_recogniser` gives back the detector that fired whatever the sweep did to the span |
| Analyzer parts | `resolve_overlaps(spans, *, priority=()) -> list[Span]`, `cut_at_line_break(span) -> Span | None` | 0.5 | The analyzer's two guarantees, for a consumer assembling its own pipeline from other recognisers |
| Name shapes | `is_name_shaped(token) -> bool`, `name_parts(name) -> list[str]` | 0.5.6 | The judgements the policy's second pass and the sweep share. A caseless script is never name-shaped; see docs/redact.md |
| Placeholder pattern | `PLACEHOLDER: re.Pattern[str]` | 0.5 | What `rehydrate` reads. Tolerant of case, inner spaces and dropped brackets, which are the ways models mutate a placeholder |
| Corpus | `corpus.build(*, pages=200, seed=20260920) -> Corpus` with `Corpus.pages`, `.labels`, `.personal`, and `Label(page, start, end, text, entity_type, shape, personal)` | 0.6 | A labelled corpus generated from a seed. `Label` validates its own offsets against the page, for the same reason `Span` does and with more at stake |
| Identifier set | `identifiers.build_cases(*, per_family=50, seed=20260920) -> list[Case]`, `evaluate.identifiers(...) -> IdentifierResults` | 0.6.1 | Every Canadian identifier shape in every written form, the shapes no recogniser claims, and the near-misses that must not fire. `Case.expect` is `detect`, `mask only` or `ignore`. `boundary redact eval --identifiers` |
| Evaluate | `evaluate.run(*, pages=200, seed=20260920, extra=(), detector=...) -> EvalResults`, `evaluate.wilson(hits, total) -> tuple[float, float]` | 0.6 | Detection recall, type accuracy, precision, leak rate after `outbound`, over-redaction, round trip and latency, each with a Wilson 95% interval. `boundary redact eval` is this with a table around it |
| TAB | `tab.fetch(split="test", *, directory=...) -> Path`, `tab.load(path) -> list[Document]`, `tab.run(docs, *, extra=(), detector=...) -> TabResults`, `tab.mention_masked(text, masked, start, end) -> bool` | 0.8 | The Text Anonymization Benchmark, scored as its own `evaluation.py` scores it. `fetch` downloads at a pinned commit and raises `ValueError` on a checksum mismatch. `TabResults` carries `direct`, `quasi`, `precision`, `safe_touched`, `chars_masked` and `by_type`, each a `Counts` with `.value` and `.interval()`, a bootstrap over documents. `boundary redact eval --tab`. Since 0.10, `tab.derive_allow(path, *, min_docs=2, max_masked_share=0.1, words=True) -> list[str]` reads a jurisdiction's allow list off a labelled split, `tab.read_allow(path)` reads one from a file, and `tab.run(..., allow=...)` passes it to the policy |
| Errors | `RedactionRefused(BoundaryError)` with `.leaks: tuple[Leak, ...]` | 0.5 | Message carries counts only. `Leak.kind` is `value`, `shape` or, since 0.5.3, `placeholder` |

## 13. `boundary.audit` (0.7)

A hash chain over ledger rows, the append-only log that holds it, and the verifier. The full
page is `docs/audit.md`. Part B's proxy will append to the same chain format; the record
body carries `"schema": 1` so that a later format is a new number rather than a silent change.

```python
from boundary.audit import AuditLog, Anchor, verify, read_anchors, append_anchor
```

| Item | Signature | Since | Notes |
|---|---|---|---|
| The log | `AuditLog(path)`, a context manager; `.seal(ledger_rows, *, settle_s=DEFAULT_SETTLE_S, now=None) -> SealStats`, `.records() -> list[Record]`, `.head() -> tuple[int, str]`, `.anchor(*, ts_utc=None) -> Anchor`, `.append_bodies(bodies) -> list[Record]` | 0.7 | SQLite, with triggers refusing `UPDATE` and `DELETE`. `seal` takes rows as `LedgerStore.rows()` returns them |
| Seal counts | `SealStats(sealed, resealed, unchanged, held_in_flight, no_call_uid, head_seq, head)` | 0.7 | Frozen |
| A record | `Record(seq, prev_hash, record_hash, body)` | 0.7 | Frozen. `body` is canonical JSON; `SEALED` lists the ledger columns it carries |
| An anchor | `Anchor(seq, head, ts_utc)` with `.to_line()` and `Anchor.from_line(line)`; `read_anchors(path)`, `append_anchor(path, anchor)` | 0.7 | One canonical JSON line per anchor. A missing file is no anchors |
| Verify | `verify(records, anchors=(), *, ledger_rows=None) -> Verification` | 0.7 | `Verification.ok`, `.breaks: tuple[Break, ...]`, `.unanchored`, `.unsealed`, `.resealable`, `.summary()`. `Break(kind, seq, detail)`; the kinds are `BREAK_KINDS` |
| Constants | `GENESIS`, `SEALED`, `BREAK_KINDS` | 0.7 | `GENESIS` is 64 zeros, the hash the first record links to |

## 14. The data policy (0.12)

Opt-in enforcement of which provider a call may reach, by its declared class. The full page
is `docs/policy.md`.

```python
from boundary import PolicyRefused
from boundary.enforce import DataPolicy, ClassRule, load_policy, decide
```

| Item | Signature | Since | Notes |
|---|---|---|---|
| Turn it on | `Gateway(..., policy=load_policy(path))`, or `policy: policy.yaml` in `boundary.yaml` | 0.12 | Absent, nothing is enforced |
| The policy | `DataPolicy(version=1, undeclared=DataClass.PERSONAL, classes={DataClass: ClassRule})`, `ClassRule(max_residency=None, regions=None, providers=None, cache=False, redacted_as=None)` | 0.12, `redacted_as` 0.15 | Frozen. `load_policy(path)` raises `ConfigError` on a malformed file |
| Decide | `decide(policy, data_class, *, provider, provider_config, region, redacted=False) -> Decision` | 0.12, `redacted` 0.15 | `Decision(allowed, data_class, reason, cache, judged_as=None)`. Pure; the gateway calls it straight after resolving the model. With `redacted=True` and a rule naming `redacted_as`, the call is judged by that class's rule, `judged_as` names it, and the cache needs both rules to allow it. A `redacted_as` must name a listed class that has none of its own, checked at load |
| Refusal | `PolicyRefused` | 0.12 | Section 6. The row's `data_class` is what the caller declared; the error's is what the policy judged it as |

## 15. The proxy (0.13)

An OpenAI-compatible HTTP proxy on the library, stage 1 of Part B. The full page is
`docs/server.md`. Needs the `server` extra (`uv sync --extra server`, or
`pip install 'boundary[server]'`); `import boundary` never imports it.

```python
from boundary.server import create_app, load_teams, TeamsConfig, hash_key, new_key
```

| Item | Signature | Since | Notes |
|---|---|---|---|
| The app | `create_app(config, teams, *, ledger_path=None, env=None, policy=None, transport=None) -> FastAPI` | 0.13 | One `Gateway` per team, sharing one transport and one ledger file. Raises `ConfigError` when neither `policy` nor the configuration names a data policy. Test seams `clock`, `wall`, `sleep` and `asleep` are keyword arguments too |
| Teams | `load_teams(path) -> TeamsConfig`; `TeamsConfig(version=1, gateway_monthly_usd, teams={name: Team(key_sha256=[...], monthly_usd, per_run_usd=None, requests_per_minute)})` | 0.13 | Frozen. `.caps()` is the teams as a `CapsConfig`, one project per team and no default. `load_teams` raises `ConfigError` on a malformed file |
| Keys | `new_key() -> str`, `hash_key(key) -> str` | 0.13 | A key is `bnd_` and 256 random bits; the file holds its SHA-256 in hex |

The HTTP surface, which is the interface a proxy client is written against:

| Item | Since | Notes |
|---|---|---|
| `POST /v1/chat/completions` | 0.13 | OpenAI's request and response shapes, streamed or not. Accepts `model`, `messages`, `max_tokens` or `max_completion_tokens`, `temperature`, `stop`, `stream`, `stream_options`, `n` of 1; any other field is a 400 naming it |
| `GET /v1/models` | 0.13 | The configuration's routes |
| `GET /healthz` | 0.13 | `{"status": "ok", "version": ...}`, without a key |
| Request headers | 0.13 | `Authorization: Bearer <key>` (required), `X-Data-Class` (absent is `personal`), `X-Boundary-Purpose`, `X-Boundary-Run-Id` |
| Response headers | 0.13 | `x-boundary-data-class`, `x-boundary-data-class-source` (`header` or `absent`), `x-boundary-version`, `x-boundary-call-uid` (not on a native stream, whose uid rides on its last choice event), `x-boundary-stream` (`native` or `whole`) on a stream |
| Redaction | 0.15 | A request whose class's policy rule names a `redacted_as` is redacted as one document (`boundary.server.redaction.redact_request`) behind the refusing guard, sent with `PRESERVE_LINE` added to its system prompt and `redacted=True`, and its answer rehydrated, streamed or not (`StreamRehydrator`). Response headers `x-boundary-redacted` (`true` or `false`), `x-boundary-placeholders` (how many values were replaced) and, on a non-streamed answer, `x-boundary-unresolved` (placeholders in the answer the vault does not hold) |
| Errors | 0.13 | OpenAI's `{"error": {"message", "type", "code", "param"}}`, with extra keys inside `error`. 401 `missing_api_key` or `invalid_api_key`; 400 `invalid_request`, `unsupported_parameter`, `unsupported_content` or `invalid_data_class`; 403 `policy_refused` with `ledger_id`; 422 `redaction_refused` (0.15) with `findings`, counts by kind and type and never a value; 404 `model_not_found`; 429 `team_monthly_budget`, `team_run_budget`, `gateway_monthly_budget` (with `resets_at`) or `team_requests_per_minute` (with `retry_after_s`), each with `Retry-After` where there is a reset; 4xx, 429 or 502 `upstream_error` |

## 11. Choices the plan left open

Answered in the draft and accepted at the freeze on 2026-09-08.

1. `ChatResponse` has five fields beyond the plan's list (`provider`, `model_requested`,
   `mode`, `retries`, `cached`, `price_list`, `price_sha256`, `trace_id`). All are also ledger columns;
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
6. **`Gateway.chat` will not take a free-form per-call metadata mapping** (asked by project
   07 on 2026-09-19, answered the same day). Every value that reaches a ledger row or a span
   has to be one the library can show cannot carry content, and the span attribute allow list
   is how "no content in telemetry" is enforced rather than promised. A mapping the caller
   fills in is a channel the library cannot check, and the day somebody puts a page of text
   in it, the ledger and the traces hold content. So a fact worth recording gets a typed,
   validated column, which is what `data_class` is; and anything else a caller wants to
   attach to a call (a document identifier, a page number, which rule fired) lives in the
   caller's own records, joined to the ledger on `call_uid`, which is now on `ChatResponse`
   for exactly that. `purpose` stays a label. If a second typed fact turns out to be needed
   by more than one project, it gets a column of its own, the same way.
