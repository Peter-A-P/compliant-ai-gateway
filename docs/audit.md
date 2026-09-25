# The audit chain

`boundary.audit` (0.7) is a hash-chained, append-only record of every call in a ledger, and
the tool that checks it. It is the library half of PLAN.md B2.4, pulled forward from Part B
because none of it needs the proxy: every call already writes a ledger row, so the chain
seals those rows. What Part B adds is the Postgres store, the proxy appending as it
answers, and an Action that commits an anchor to this public repository every day.

The ledger answers "what did this call cost, and where did it go". The chain answers the
question a regulator asks next: **"how do I know nobody changed that answer afterwards,
including you?"**

## What a record is

One record per ledger row, appended when the row is sealed, and a second one for the same
call if the row changes later (a call sealed in flight that then completes). A record is:

| Field | What it is |
|---|---|
| `seq` | Its position, from 1, with no gaps |
| `prev_hash` | The previous record's hash; 64 zeros for the first |
| `record_hash` | `sha256(prev_hash as 32 raw bytes, then body as ASCII)`, in hex |
| `body` | Canonical JSON: sorted keys, no whitespace, ASCII only, no NaN |

The body is `{"kind": "ledger_row", "schema": 1, "sealed_utc": ..., "row": {...}}`, and
`row` holds exactly the ledger columns listed in `boundary.audit.SEALED`: identifiers,
counts, hashes, the declared region, residency and data class, the price fingerprint and the
cost. **Never content**, because the ledger holds none. Not `id`, which is per file and
changes in a merge; not `raw_path`, which is a path on somebody's disk; not the trace ids.
The list is fixed rather than "whatever the row holds", so a column a later ledger schema
adds does not quietly change what an older verifier has to reproduce.

**Record schema 2 (0.18)** seals one more column, `redacted` (`SEALED_V2`), so that a
personal call the proxy sent as placeholders is in the chain as redacted and not only in the
ledger. Nothing already in a chain is rewritten: a schema 1 record keeps verifying against
the fields it sealed, read from the record itself. A row sealed under schema 1 is sealed
again only if it now carries a `redacted` value schema 1 had no place for, and `verify`
counts such a row as `resealable`, not as a break. A verifier older than 0.18 still checks
every link and hash of a schema 2 record, but when it compares a schema 2 record with the
ledger it will report `redacted` as a changed column, because it does not know the column.
The real audit log on the development laptop, 2,811 schema 1 records, verified intact
against its ledger on the day schema 2 shipped.

A body that parses but is not in canonical form is a break of its own. The hash would still
check, but this library could not have written it.

## Commands

```
boundary audit seal    [--ledger L] [--audit A] [--settle-hours 24]
boundary audit anchor  [--ledger L] [--audit A] --anchors FILE
boundary audit verify  [--ledger L] [--audit A] [--anchors FILE] [--no-ledger]
boundary audit tamper-test [--records 500] [--anchor-interval 50] [--trials 200] [--write-readme] [--write-doc]
```

The audit log defaults to `<ledger>.audit.sqlite` beside the ledger, so one ledger has one
chain and a merge destination gets its own.

- **`seal`** appends a record for every row that is new or has changed since it was last
  sealed. It is idempotent: sealing the same ledger twice appends nothing. A row still
  **in flight** is held back for `--settle-hours`, because a slow call completes well
  inside that; one whose process was killed never completes and is sealed as it stands, and
  if it does complete later the next seal appends its outcome. A row with no `call_uid` is
  counted and not sealed, because a null uid in a v2 or later ledger was put there by hand
  and a record of it could never be checked against anything.
- **`anchor`** appends the current head, `{"head", "seq", "ts_utc"}` as one canonical JSON
  line, to an anchor file. It never rewrites the lines before it. Publishing the file is the
  point, and is the caller's job (below).
- **`verify`** recomputes the whole chain from the first record, checks it against every
  anchor, and checks the ledger against the latest record for each call. It reports every
  break rather than the first, and exits 1 on any. It also prints what an intact chain does
  not prove, for the same reason `ledger residency` does.

## What verification finds

| Break | Meaning |
|---|---|
| `sequence` | A record is not at the position after the one before it |
| `link` | A record does not follow the hash of the record before it |
| `hash` | A record's hash is not the hash of its own body and link |
| `body` | A record's body is not canonical JSON, so it was not written by this library |
| `anchor` | The record at an anchored position is not the one that was anchored |
| `truncated` | An anchor names a position the log no longer reaches |
| `ledger_changed` | A sealed ledger row no longer says what its latest record sealed; the detail names the columns |
| `ledger_missing` | A sealed call is no longer in the ledger |

Two counts are reported and are **not** breaks: `unsealed`, ledger rows written since the
last seal, and `resealable`, calls sealed in flight that have completed since. Both clear on
the next seal.

## Append-only, three ways, and which one holds

1. **Triggers** on the SQLite table refuse `UPDATE` and `DELETE`. That stops an accident and
   a careless script. It does not stop the operator, who owns the file and can drop a
   trigger, and a test does exactly that to prove the next layer catches the edit.
2. **The chain** makes any edit to a record visible, and a recomputed hash visible at the
   next record. It does not survive a forger who rewrites every hash from the change onwards:
   the result is a perfectly consistent chain.
3. **The anchors** are what defeat that forger. A head published somewhere the operator
   cannot rewrite pins every record up to it. In Part B that somewhere is this public
   repository, committed daily by an Action, so tampering by the operator, and not only by
   an outsider, is detectable by anyone who can read the repository.

## Measured: the tamper test

`boundary audit tamper-test` builds a 500-record chain over a generated ledger, anchors it
every 50 records except the last 50 (a real log always has a tail written since its last
anchor), and then corrupts it 200 times per kind in each region, from a seed and in memory.
Twelve kinds, ordered from a careless edit to a forger who owns every file and edits the
ledger to match the forged log. A control row with no corruption counts false alarms.

<!-- audit-doc:start -->
```
boundary 0.7.0, seed 20260922: 500 records, anchored every 50 (9 anchors, the last 50 records unanchored), 200 trials per kind per region

corruption                            before the last anchor    after it                  caught by
none (control)                        0.0% (0.0% to 1.9%)       0.0% (0.0% to 1.9%)       -
edit a ledger row                     100.0% (98.1% to 100.0%)  100.0% (98.1% to 100.0%)  ledger_changed 400
delete a ledger row                   100.0% (98.1% to 100.0%)  100.0% (98.1% to 100.0%)  ledger_missing 400
edit a record                         100.0% (98.1% to 100.0%)  100.0% (98.1% to 100.0%)  hash 400
edit a record, rehash it              100.0% (98.1% to 100.0%)  100.0% (98.1% to 100.0%)  link 391, ledger_changed 9
delete a record                       100.0% (98.1% to 100.0%)  96.5% (93.0% to 98.3%)    sequence 393
edit a record, rewrite after it       100.0% (98.1% to 100.0%)  100.0% (98.1% to 100.0%)  anchor 200, ledger_changed 200
edit row and record, rewrite after    100.0% (98.1% to 100.0%)  0.0% (0.0% to 1.9%)       anchor 200
delete call from both, rewrite after  100.0% (98.1% to 100.0%)  0.0% (0.0% to 1.9%)       anchor 200
insert a forged call, rewrite after   100.0% (98.1% to 100.0%)  0.0% (0.0% to 1.9%)       anchor 200
swap two records, rewrite after       100.0% (98.1% to 100.0%)  0.0% (0.0% to 1.9%)       anchor 200
truncate the log and the ledger       100.0% (98.1% to 100.0%)  0.0% (0.0% to 1.9%)       truncated 200
replace the log and the ledger        100.0% (98.1% to 100.0%)  0.0% (0.0% to 1.9%)       anchor 200
```
<!-- audit-doc:end -->

**Before the last anchor, all 2,400 corruptions were detected, 100.0% (99.8 to 100.0), and
the untouched log raised no false alarm in 400 checks.** Intervals are Wilson at 95%. The raw
results are in `bench/audit.json`.

**After it, six of the twelve kinds are never detected, and that is the design rather than
a defect.** Every one of the six is a forger who recomputes the chain from the change onwards
and edits the ledger to agree. Inside the window since the last anchor there is nothing left
to disagree with them, and no hash chain can do better. The width of that window is the
anchor interval, which is the whole reason Part B anchors daily and not weekly: the claim a
chain supports is "nothing before yesterday's anchor has changed", and it is worth exactly
as much as the anchor is hard to rewrite.

Two rows are worth reading closely:

- **`edit a record, rewrite after it` is still 100% after the anchor**, because a forger who
  rewrites the log but forgets the ledger leaves the ledger disagreeing with the chain. The
  cross-check between the two files is a second witness, not a formality.
- **`delete a record` is 96.5% after the anchor, not 100%.** The misses are the trials that
  deleted the newest record. With nothing after it, nothing links to it, and its ledger row
  reads as written since the last seal. The deletion is indistinguishable from not having
  sealed yet, and a test asserts that rather than hiding it. The next anchor closes it.

Verifying 500 records with the ledger cross-check took 8.3 ms on the laptop (the README row
is the current figure), so checking a year of the portfolio's calls is seconds.

## What this does not do, yet

- **Nothing is anchored in this repository yet.** The daily Action that commits an anchor is
  Part B, because there is no always-on log to anchor until the proxy runs somewhere that is
  always on. The proxy exists since 0.13 (docs/server.md) but runs on a laptop until it is
  deployed, and today each environment's ledger is sealed after the fact on the machine
  that holds it. An anchor file
  in the repository pointing at a laptop's log would be a claim nobody else could check.
- **For a library caller, the seal runs after the call, not during it.** The gateway is
  untouched, so the pass-through path and the overhead figure are exactly what they were, and
  a call is on the record from its ledger row onwards. The price is a window between a call
  and its seal in which only the ledger holds it. **The proxy closes that window (0.18)**:
  `boundary serve` appends each call to the chain as soon as its row completes
  (`boundary.audit.appender`), without rereading the log, holding a row that is still in
  flight until it finishes. A policy refusal and a redaction refusal are sealed the same way.
- **It proves the record was not changed, not that it was complete.** A call the ledger never
  recorded is not in the chain either. That is what the ledger's own completeness figure
  (600/600 under fault injection, in the README) is for.
- **SQLite, not Postgres.** B2.4 specifies Postgres with `UPDATE` and `DELETE` revoked from
  the application role. The chain and the verifier are storage-independent pure functions
  (`boundary/audit/chain.py`), so the Part B store is a different place to keep the same
  records rather than a different design.
- **Nothing is anchored yet, still.** The proxy appends as it answers, but it runs on a
  laptop, so its chain is not yet the always-on log a daily anchor needs.
