# What did not work: a central ledger written over the network

Rule C of the portfolio plan says every project names an approach it tried and rejected,
with the evidence. This is that page for Part A. PLAN.md section 9 listed three candidates
before any of them was measured; this is the third, and it is the one that shaped the code,
so it is the one written up.

**The approach:** keep the cost ledger in one place from day one. Every call writes its row
to a central service over the network, so `boundary ledger report` and the portfolio spend
cap always see every environment at once, and there is nothing to merge.

**Why it was attractive:** the portfolio cap is a single number across every project and
machine, and a cap can only be enforced against a total it can see. A local file per
environment cannot see the others, which is a real weakness of what shipped: until a merge
runs, the portfolio cap is checked against one machine's view. A central ledger removes
that gap, and removes the merge, the `call_uid` column and the idempotency argument with
it.

**Why it was rejected:** it trades a gap that is knowable and bounded for one that is not.
A local ledger's view is incomplete in a way you can state exactly, and it costs nothing to
close by merging. A remote ledger's view is complete right up to the moment the network
drops, and then the run either stops or spends money it cannot account for. The plan needs
a record of every call more than it needs one file.

## The measurement

`boundary experiment remote-ledger` runs three designs over the same calls, the same
seeded request corpus and the same simulated outage, against an in-process mock upstream.
No network and no money are involved.

| Design | What happens to a ledger write that cannot reach the host |
|---|---|
| `remote-strict` | It raises, so the run stops. The honest version: it will not proceed without a record |
| `remote-best-effort` | It is logged and swallowed, so the run continues. What gets written on the second day, after the first outage stops a run |
| `local-first` | Nothing: the write is to a file on the machine making the call. `ledger merge` combines the files afterwards, and is run twice here to show a second merge is a no-op |

The outage is a window over ledger operations rather than over calls, because where it
lands inside a call is the whole question. One call is five operations: three spend queries
for the caps, the `begin` before the request goes out, and the `complete` after it comes
back. A window opening before the cap check costs a call that was never made. A window
opening after the request went out costs the record a cost it can never state, and the
vendor bills for it anyway.

100 repetitions of 40 calls per design, one outage of 4 to 30 operations per repetition,
seed 20260910. "Unrecorded" counts the calls that reached the vendor whose actual cost the
central record cannot state: rows missing outright, plus rows stuck at the pessimistic
pre-call estimate. The interval is a 95% bootstrap interval over the per-repetition rates.

| Design | Runs that finished | Calls billed | With no row at all | Stuck at the estimate | Unrecorded, % of billed (95% CI) | Calls made with no cap check | Rows added by a second merge |
|---|---|---|---|---|---|---|---|
<!-- experiment:remote-ledger:start -->
| `remote-strict` | 0/100 | 2,004 | 0 | 14 | 0.7% (0.4 to 1.9) | 0 | n/a |
| `remote-best-effort` | 100/100 | 4,000 | 339 | 14 | 8.8% (8.1 to 9.7) | 981 | n/a |
| `local-first` | 100/100 | 4,000 | 0 | 0 | 0.0% (0.0 to 0.0) | 0 | 0 |
<!-- experiment:remote-ledger:end -->

Filled by `boundary experiment remote-ledger --write-doc`; the raw results, including the
per-repetition rates behind the interval, are in `bench/remote-ledger.json`. Never edited
by hand.

## What the numbers say

1. **The strict remote ledger does not finish a run.** Every repetition contained an
   outage, and not one of the hundred runs reached its last call. Each stopped part way,
   having already paid for everything it had sent, and a handful of those in-flight rows
   were left at the estimate because the outage arrived between the request and its
   outcome. In a portfolio where a drift run is a scheduled job on a laptop and on a
   GitHub Actions runner, a run that aborts is a measurement lost, not an inconvenience.

2. **The best-effort remote ledger loses rows, which is worse.** The run finishes and the
   calls are billed, but a share of them leave no record at all. "Every call writes a
   ledger row before returning" is the one promise this library makes; a design that
   cannot keep it during an outage cannot keep it.

3. **The cap fails at exactly the wrong moment.** A remote ledger has to be queried before
   the call to know what has been spent. When the host is unreachable, the best-effort
   design cannot answer the question, and the calls it then makes are made with no cap
   check at all. The column counts them. A spend cap that stops working when the network
   does is not a second line of defence.

4. **Local-first loses nothing, and the merge is free of surprises.** No rows missing, none
   stuck at an estimate, and the second merge of the same file inserts nothing.

## What was kept from the idea

The criticism that motivated the central ledger was fair, so two pieces of it are in the
code:

- **`call_uid`** (schema v2). Every row carries a uid minted in the process that made the
  call, so merging the same file twice, or two copies of one file, yields one row per call.
  Merge being idempotent is what makes "run it again" a safe answer to a failed merge.
- **A merge that converges rather than freezes.** A row that was in flight when it was
  merged is completed in the destination the next time the source is merged, so a central
  file built by repeated merges ends up with actual costs and not estimates.

What remains true is that between merges the portfolio cap sees one environment. That is
recorded here rather than hidden: the merge before the monthly budget review is the step
that closes it, and the ledger-against-invoice check from October is what would catch it
if the step were skipped.

## The other two candidates

PLAN.md section 9 also listed vendor SDKs against raw HTTP, and estimating cost from a
tokenizer before the call against costing from returned usage. Both need something this
experiment did not: two SDK releases to diff, and a corpus of live calls whose returned
usage can be compared with a tokenizer's guess. They stay candidates for v0.2 in October,
when there are live calls to measure against. Rule C asks for one rejected approach with
evidence, and this is it.
