# The ledger, schema v1

One SQLite file per environment (`ledger.path` in `boundary.yaml`, or `ledger_path` on the
gateway). One row per call. Rows are written in two phases:

1. **Before the request leaves the process**: the row is inserted with
   `error_type = 'in_flight'` and `cost_usd` set to the pessimistic pre-call estimate.
2. **After the response, or the failure**: the same row is completed with the outcome and
   the actual cost. A process killed between the two leaves an `in_flight` row, which still
   counts against the caps at its estimate. Nothing escapes the ledger.

Columns are additive only. A column is never renamed or removed. `schema_version` records
the version that created the file.

| Column | Type | Meaning |
|---|---|---|
| `id` | integer | Row id, returned to the caller as `ledger_id` |
| `ts_utc` | text | Insert time, ISO 8601 UTC with milliseconds, `Z` suffix |
| `boundary_version` | text | Library version that wrote the row |
| `project` | text | The gateway's project; the key into `caps.yaml` |
| `purpose` | text | Caller's short label: `drift-run`, `grader-dev`, ... |
| `run_id` | text or null | Groups rows; the per-run cap is measured over it |
| `mode` | text | `standard` or `passthrough` |
| `provider` | text | The `providers` key the call went to |
| `alias` | text or null | The alias the caller used, if any |
| `model_requested` | text | Explicit `provider/model-id` after alias resolution |
| `model_returned` | text or null | The identifier the vendor reported |
| `region` | text or null | From the route or provider entry |
| `input_tokens` | integer | As returned. For OpenAI-compatible hosts this is `prompt_tokens` minus cached tokens |
| `output_tokens` | integer | As returned |
| `cache_read_tokens` | integer | As returned (Anthropic `cache_read_input_tokens`; OpenAI `cached_tokens`) |
| `cache_write_tokens` | integer | As returned (Anthropic `cache_creation_input_tokens`) |
| `price_list` | text or null | Date of the price file the row was costed with |
| `cost_usd` | real or null | Actual cost from usage and the price entry. Null when uncosted. The estimate while in flight |
| `costed` | 0 or 1 | 1 when `cost_usd` is an actual cost. An unknown price is 0, never a guess |
| `cached` | 0 or 1 | 1 when the development cache answered (standard mode only) |
| `latency_ms` | real or null | Upstream wall time for the final attempt |
| `http_status` | integer or null | Status of the final attempt; null when no response arrived |
| `error_type` | text or null | `in_flight`, `http_<status>`, `MalformedResponse`, or the transport exception class name (`ReadTimeout`, `ConnectError`); null on success |
| `retries` | integer | Retries made; always 0 in pass-through |
| `request_sha256` | text | Hash of the request body bytes as sent |
| `response_sha256` | text or null | Hash of the response body bytes |
| `trace_id`, `span_id` | text or null | OpenTelemetry ids, hex; null when telemetry is off |
| `raw_path` | text or null | Pass-through only: the JSONL file holding the raw record |

## Spend queries

`spend_usd(project, year_month, run_id)` sums `cost_usd` where it is not null, so in-flight
estimates count and uncosted rows do not. Uncosted successful calls are counted separately
by `uncosted_count()` and should be zero; the README reports the figure.

The portfolio cap is checked against the ledger the gateway can see, which is one
environment's file until `ledger merge` (v0.2) combines them.

## Costing rules

- Price lookup uses `model_returned` when present, otherwise `model_requested`'s model
  part, so an alias that resolves to a snapshot is costed at the snapshot's price.
- A provider entry with `price_zero: true` (a local server) is costed at zero and marked
  costed.
- A call that used cache or batch features is costed only if the price entry has those
  rates; otherwise it is uncosted, never approximated.
- A development-cache hit costs zero and is marked `cached`.
