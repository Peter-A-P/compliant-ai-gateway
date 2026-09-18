# The ledger, schema v4

One SQLite file per environment (`ledger.path` in `boundary.yaml`, or `ledger_path` on the
gateway), combined by `boundary ledger merge`. Writing locally rather than to one central
service is a decision with evidence behind it: [rejected.md](rejected.md). One row per
call. Rows are written in two phases:

1. **Before the request leaves the process**: the row is inserted with
   `error_type = 'in_flight'` and `cost_usd` set to the pessimistic pre-call estimate.
2. **After the response, or the failure**: the same row is completed with the outcome and
   the actual cost. A process killed between the two leaves an `in_flight` row, which still
   counts against the caps at its estimate. Nothing escapes the ledger.

Columns are additive only. A column is never renamed or removed. `schema_version` records
every version the file has been through.

**v2 (0.2)** adds `call_uid` and `env`, the two columns merging needs, and nothing else. A
v1 file is upgraded in place the first time this version opens it: the columns are added,
`call_uid` is backfilled for the rows already there, and the unique index is built. The
upgrade changes no value a call recorded. `env` stays null on those rows, because which
machine made a call written before the column existed is not recoverable, and a guess in
the ledger would be worse than a null.

**v3 (0.2)** adds `batch_id` and nothing else. A batch is submitted in one process and
collected in another, often hours later, so the rows written at submit have to be findable
again by something the vendor also knows.

The upgrade runs one step at a time, so what a step does depends on where the file started
rather than where it ended. That matters for the uid backfill: uids are invented only for a
file that predates the column. A null `call_uid` in a v2 or later file was put there by
hand, and inventing one would let the same call merge twice.

| Column | Type | Meaning |
|---|---|---|
| `id` | integer | Row id, returned to the caller as `ledger_id`. Local to one file: after a merge the destination assigns its own |
| `call_uid` | text | Uid for the call, minted in the process that made it (v2). What identifies a row across files, so merging the same file twice is one row per call |
| `env` | text or null | Which environment made the call: `ledger.env` in the configuration, or `BOUNDARY_ENV`. Null for rows written before v2 |
| `batch_id` | text or null | The vendor's batch identifier (v3). Null for every ordinary call. Set once the vendor has accepted the batch and named it, which is the one field that cannot be known before the submit leaves |
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
| `region` | text or null | From the route or provider entry. Where the request was **sent**, never where it was processed |
| `residency` | text or null | `single-region`, `geo` or `global`, as declared on the provider entry (v4). Null when none was declared, which is not the same as `global` |
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

## Residency queries

`boundary ledger residency` groups the same rows by provider, region and residency instead of
by project, model and month. It is the compliance question rather than the spend one, and it
is the only reader of the v4 column.

Smoke run #6 from GitHub Actions, 2026-09-18, copied from the run's own log rather than
written by hand:

```
reach          provider         region             calls  cached  err      in     out  models
undeclared     anthropic        -                      1       0    0      14       4  anthropic/claude-haiku-4-5-20251001
undeclared     google           -                      1       0    0       8       1  google/gemini-3.5-flash-lite
undeclared     openai           -                      1       0    0      13      10  openai/gpt-5-nano
undeclared     openweights      -                      1       0    0      42       2  openweights/meta-llama/Llama-3.3-70B-Instruct-Turbo
global         foundry-canada   canadacentral          1       0    0      13       4  foundry-canada/gpt-5.6-luna
global         vertex           global                 1       0    1       0       0  vertex/claude-haiku-4-5@20251001
geo            bedrock          ca-central-1           1       0    0      14       4  bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0
```

Seven calls, and the shape of the answer is the finding. **Four of the seven declared no
residency at all**, because Anthropic, OpenAI, Google and Together publish nothing
per-request to declare. **The three that could declare one all declared `geo` or `global`.**
Nothing in a live run reached `single-region`, and the only route in the configuration that
can is the local server, which is not in this run because nothing in Actions calls it.

The `err 1` on the Vertex row is a 401 from an expired pasted token, and the row is there
anyway: the request left the runner and reached `global`, so it belongs in the residency
answer whatever came back. That is deliberate. A residency report that counted only
successful calls would under-report exactly the requests somebody would most want to know
about.

**Widest reach first**, so the rows that matter are at the top rather than in alphabetical
order in the middle. The ordering is `single-region < geo < global < anything else`, where
"anything else" is both the undeclared row and a residency class added by a later version
than the one reading the file.

**`--require geo` turns the report into a gate** and exits 2 when any group went wider. That
is the part worth having: a drift run can fail its own CI for sending data further than it
said it would, instead of producing a table nobody reads.

Three rules, all in the same direction:

- **An undeclared row fails every limit, including `--require global`.** Null is not `global`.
  `global` is the weakest claim somebody made; null is no claim at all, and there is nothing
  to check. Folding one into the other would invent a claim on the operator's behalf, which
  is the failure this column exists to prevent.
- **A residency class this version does not recognise fails every limit too.** An old reader
  cannot know whether a new class is narrower or wider than `global`, so it refuses to let it
  satisfy anything. It still prints the value as stored rather than hiding it.
- **A group of pure cache hits is never a violation.** Nothing left the machine, so nothing
  travelled. The calls are still counted, because a residency report whose totals disagree
  with `ledger report` invites the reader to wonder which one is lying.

**What a clean report does not prove.** `residency` is configuration, not observation: no
vendor reports where a request was actually processed, and
[hyperscaler-setup.md](hyperscaler-setup.md) has the three separate ways they each avoid
saying. So a pass means every call went to an endpoint whose declared limit was within the
one required. It means nothing at all about the vendor's conduct. The command prints that
sentence itself, because the moment somebody is most likely to over-read a clean report is
when they are looking at one.

**Today, one route in the checked-in configuration can claim `single-region`, and it is the
local server**, where the request never reaches a network. Every hosted entry declares `geo`,
`global`, or nothing. That is the state of the market rather than a gap in the configuration,
and `tests/test_residency.py` asserts it so that the day it changes is a failing test.

## Spend queries

`spend_usd(project, year_month, run_id)` sums `cost_usd` where it is not null, so in-flight
estimates count and uncosted rows do not. Uncosted successful calls are counted separately
by `uncosted_count()` and should be zero; the README reports the figure.

The portfolio cap is checked against the ledger the gateway can see, which is one
environment's file until `ledger merge` combines them. That gap is real and is stated in
[rejected.md](rejected.md): the merge before the monthly budget review is what closes it,
and the ledger-against-invoice check is what would catch a review that skipped it.

## Merging

```
boundary ledger merge --into central.sqlite laptop.sqlite actions.sqlite
boundary ledger merge --into central.sqlite laptop.sqlite --dry-run
```

Rows are matched on `call_uid`, never on `id`:

- A call the destination does not hold is inserted, with a new `id`. Everything else, `env`
  and `raw_path` included, is copied verbatim, so a pass-through row still points into the
  raw store on the machine that made the call.
- A call it already holds is left alone, which is what makes a second merge of the same
  file a no-op. Merge is safe to re-run, and re-running is the answer to a merge that
  failed half way: one source is one transaction, so a failure leaves the destination as
  it was.
- The exception is a row the destination holds as `in_flight` and the source now has
  complete: that row is completed, so a central file built by repeated merges settles on
  actual costs instead of freezing the first estimate it saw. A completed row is never
  reverted by an older copy of the same call.
- **A source is never written to.** Whatever schema it is at, the rows are read from a
  temporary copy and the copy is what gets upgraded. This matters because opening a ledger
  upgrades it, and an older library then refuses to write to the upgraded file by design:
  a merge that upgraded its sources would stop the environment that owns one appending to
  it. Project 03 pins boundary 0.1.0 and commits one ledger per arm per month, and the
  monthly invoice check merges exactly those files. A v1 source still merges to the same
  uids, because the backfill derives them from the rows rather than inventing them.
- `--dry-run` suppresses the writes to the destination. There were never any to a source.
- Merging a file into itself, or a file whose schema is newer than the library, is refused
  rather than attempted.

## Costing rules

- Price lookup uses `model_returned` when present, otherwise `model_requested`'s model
  part, so an alias that resolves to a snapshot is costed at the snapshot's price.
- A provider entry with `price_zero: true` (a local server) is costed at zero and marked
  costed.
- A call that used cache or batch features is costed only if the price entry has those
  rates; otherwise it is uncosted, never approximated.
- A development-cache hit costs zero and is marked `cached`.
- A batched call is costed at the price entry's `batch_multiplier`, from the usage the
  vendor returns per request. An entry with no batch rate leaves the row uncosted rather
  than costing it at the full rate, which is the same rule as for cache rates.

## Batches

One row per request, written before the submit leaves the process, in flight and carrying
the estimate at the batch rate. The vendor bills for every request the moment it accepts
the batch, so the rows exist even if the process dies before the answer comes back.

- The `custom_id` sent to the vendor is the row's `call_uid`. A result maps back to exactly
  one row in any process, whatever order the results file is in.
- `batch_id` is set after the vendor names the batch. Until then the rows are in flight
  with no batch id, which is what a failed submit leaves behind before it completes them
  as failures.
- A submit the vendor refused completes every row as a failure with no cost: nothing was
  billed. A submit the vendor accepted whose body cannot be read leaves the rows in flight
  at their estimate, because the batch will be billed and recording it as failed would
  understate the month.
- At result time each row is completed with the returned usage, the batch-rate cost, and
  `http_status` 200 for a request the vendor answered, even though it had no response of
  its own. That is deliberate: it keeps an uncosted batch success inside `uncosted_count`,
  the figure that has to stay at zero.
- `latency_ms` stays null. A batched request has no wall time of its own; it waited in the
  vendor's queue for as long as the batch did, and a number here would be an invention.
- `trace_id` and `span_id` are null until the results are collected, because the span that
  describes a call is the one that knows how it turned out.
- A request the results file never mentions is completed as `batch_missing`. A batch that
  has ended will not mention it later either.
