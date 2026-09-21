# What did not work

Rule C of the portfolio plan says every project names an approach it tried and rejected,
with the evidence. This is that page for Part A. PLAN.md section 9 listed three candidates
before any of them was measured. Two are now measured and written up here:

1. **A central ledger written over the network**, the one that shaped the code.
2. **Costing a call from a local token estimate**, measured on 2026-09-14 once project
   03's first official run had produced 16,800 real calls to measure against.

A third is written up below and is not this project's own measurement:
**judging a detector by recall alone**, rejected on project 07's evidence and recorded here
because this library's published recall figure carries the same weakness.

---

# A central ledger written over the network

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

---

# Costing a call from a local token estimate

**The approach:** count the prompt's tokens locally before the call and cost it from that,
instead of waiting for the usage the vendor returns. Then `costed` would never have to be 0,
an unknown price would not have to write an uncosted row, and a caller could know what a call
cost before the response arrived.

**Why it was attractive:** it removes the library's dependence on what each vendor chooses to
return, and it makes a spend cap exact rather than pessimistic. Caps are checked *before* a
call, so today the check uses an estimate built from `max_tokens`, which is deliberately
generous. A real token count would make the cap tight. It would also close the uncosted-row
gap that the September invoice check had to bound
([docs/invoice-check.md](invoice-check.md)).

**Why it was rejected:** the estimate is not close enough to bill from, and the way it fails
is worse than being merely inaccurate. It is accurate in aggregate and wrong per call, which
is the combination most likely to be trusted and then to mislead.

## The measurement

`boundary experiment token-estimates <run-dir>` scores three estimators against what the
vendors actually returned, over **15,996 successful calls** from project 03's first official
drift run of 2026-09-13. Nothing is called and nothing is spent: the run already happened,
its raw store holds every request body, and its ledgers hold every returned count. The join
is per arm on the ledger id, and it was checked against the request hashes that the two sides
record independently.

    chars/4        the universal rule of thumb, no dependency, every vendor
    words x 1.3    the other common rule of thumb, same properties
    tiktoken       OpenAI's real BPE vocabulary (o200k_base), the strongest case available

The three are not equally available, and the asymmetry is part of the answer. tiktoken is
OpenAI's own tokenizer. Anthropic publishes no local tokenizer, and Google's counts come from
a network call to Google. So for two of the four vendors the best row below is not reachable
offline even in principle; running o200k_base against them is generous to the estimating
design, not unfair to it.

The comparison is against the whole prompt the vendor tokenised, `input_tokens +
cache_read_tokens`. That detail is not cosmetic, and getting it wrong the first time is
recorded here because it is the kind of error this table exists to catch. Compared against
`input_tokens` alone, tiktoken's worst OpenAI call looked like a 3,334 percent error and its
mean absolute error looked like 85.6 percent. Both were artefacts of prompt caching rather
than of any estimator: on this run OpenAI served 473,600 cached prompt tokens against 433,780
fresh ones, so the fresh column alone measures how much of the prompt OpenAI happened to have
cached, which is a property of the vendor's infrastructure. Corrected, the same cell is 14.9
percent. The two columns are separate because they bill at different rates, and reading the
wrong one is an easy mistake to make twice.

<!-- token-estimates:start -->
| Estimator | Vendor | Calls | Median error (95% CI) | Mean absolute error (95% CI) | Worst | Within 10% | Month's input count out by |
|---|---|---:|---:|---:|---:|---:|---:|
| chars/4 | anthropic | 6,000 | -16.2% (-16.8 to -15.6) | 18.5% (18.2 to 18.9) | 67% | 36.2% | -11.4% |
| chars/4 | google | 3,998 | +4.8% (+4.5 to +5.4) | 10.7% (10.4 to 11.0) | 48% | 58.3% | +7.0% |
| chars/4 | openai | 4,000 | -6.6% (-7.0 to -6.0) | 11.6% (11.3 to 11.9) | 45% | 59.5% | +6.7% |
| chars/4 | openweights | 1,998 | -31.2% (-32.0 to -30.7) | 31.7% (31.0 to 32.4) | 74% | 11.0% | +0.1% |
| words x 1.3 | anthropic | 6,000 | -20.9% (-21.3 to -20.1) | 22.8% (22.4 to 23.1) | 73% | 22.0% | -16.7% |
| words x 1.3 | google | 3,998 | -0.8% (-1.1 to -0.3) | 7.7% (7.5 to 8.0) | 58% | 74.2% | +0.6% |
| words x 1.3 | openai | 4,000 | -11.9% (-12.0 to -11.4) | 14.5% (14.2 to 14.9) | 55% | 42.2% | +0.2% |
| words x 1.3 | openweights | 1,998 | -35.0% (-35.7 to -34.3) | 35.7% (35.0 to 36.3) | 77% | 5.3% | -6.0% |
| tiktoken o200k | anthropic | 6,000 | -19.9% (-20.3 to -19.6) | 23.7% (23.4 to 24.0) | 75% | 6.8% | -18.8% |
| tiktoken o200k | google | 3,998 | -3.1% (-3.3 to -3.1) | 4.4% (4.3 to 4.5) | 22% | 94.0% | -1.9% |
| tiktoken o200k | openai | 4,000 | -12.8% (-13.0 to -12.3) | 14.9% (14.7 to 15.2) | 50% | 31.0% | -2.2% |
| tiktoken o200k | openweights | 1,998 | -34.6% (-34.9 to -33.6) | 36.1% (35.5 to 36.8) | 78% | 5.0% | -8.3% |
<!-- token-estimates:end -->

## What the numbers say

**No estimator is usable per call.** The best cell in the table is tiktoken against Google, at
4.4 percent mean absolute error with 94.0 percent of calls within 10 percent. It is also the
one cell that cannot be obtained offline in practice, and the least deserved: o200k_base is
not Google's tokenizer, and it happens to fit. Everywhere else the share of calls landing
within 10 percent of the truth runs from 59.5 percent down to **5.0 percent**. For an
open-weights Llama model, every estimator is out by about a third on a typical call.

**The monthly total hides all of it, which is the actual finding.** Compare the last column
with the one before it. chars/4 on the open-weights host gets the month's input count right to
**+0.1 percent** while getting the typical call wrong by **-31.2 percent**, landing within 10
percent on 11.0 percent of calls. words x 1.3 on OpenAI is +0.2 percent for the month and 42.2
percent within 10 percent per call. Over-estimates and under-estimates cancel, so a monthly
reconciliation against an invoice would look healthy while nearly every individual row was
wrong.

That is fatal for what this library uses the number for. A spend cap is checked before a
single call. A per-team budget in Part B refuses a single request. A per-call cost is what
goes in the ledger row, and the ledger is the product (PLAN.md section 2.4). An estimator that
is right on average and wrong in every row is useless for all three, and worse than useless
because the monthly check that is supposed to catch errors would not catch this one.

**Even the vendor's own tokenizer misses, and always in the same direction.** tiktoken against
OpenAI has a median error of -12.8 percent (-13.0 to -12.3): consistently under, never
centred. The estimators see the message text, and the vendor counts the chat template around
it too, which a caller cannot see before the call. A signed, consistent bias is the shape of a
missing constant rather than of noise, so it is tempting to correct it with a per-vendor fudge
factor. That was not done and should not be. The factor would be fitted on one run of one
suite, and the next model version would move it silently, which is the same class of mistake
as guessing a price.

## What was kept from the idea

Nothing about the costing path changed. The rule stands: cost comes from returned usage and a
dated price file, and an unknown price writes an uncosted row rather than an estimate.

What the measurement did change is the confidence in a number that was already there. The
pre-call cap check has to estimate, because before the request leaves there is nothing else to
estimate from. It estimates from `max_tokens`, which is a bound rather than a guess, and it is
deliberately pessimistic: it refuses early rather than late. This experiment says that making
it "smarter" with a tokenizer would trade a bound that is honest for an estimate wrong by 5 to
35 percent per call with no bound at all. The pessimistic check is the right one, and there is
now evidence for it rather than a preference.

The uncosted row stays too. Nine rows in September were uncosted because a price entry did not
exist yet, worth about US$0.0025 in total. The alternative on offer was to fill those nine
rows with a number wrong by about a third. Nine honest gaps are worth more than 70,834
plausible fictions.

## Reproducing this

    boundary experiment token-estimates path/to/03/drift/runs/2026-09

About three minutes, no network, no cost. `bench/token-estimates.json` holds the full result
including the per-vendor token totals. tiktoken is a development dependency and deliberately
not a runtime one: needing a tokenizer to state a cost is the design this experiment rejects.

---

# Judging a detector by recall alone

**Not measured here.** The evidence is project 07's, on 07's corpus, and it is recorded on
this page because it is about the number this repository publishes.

**The approach:** report detection recall per entity type and treat it as the measure of a
detector. It is the natural thing to do, it is what `docs/redact.md` publishes, and it is
what both projects were doing.

**What it misses:** recall asks whether a span was **found**. It does not ask what the span
was **called**, and in any tool that decides what to release, the label is the decision.
Presidio typed "Bernadette Tuglavina" a `PERSON` in an introducing sentence and the bare
"Tuglavina" in the list below a `LOCATION`. Recall counted that span, every table stayed
healthy, and then 07's release rule read `LOCATION` as not information about an identifiable
individual and printed that sentence in the schedule beside a third party's surname. A miss
would have been better, because a miss does not argue for itself.

**The evidence.** 07 split its leaks three ways and got 16 never found, 23 found and
mislabelled, and zero from the decision rules. The figure it had been publishing as 50
decision errors was not about decisions at all. Correcting the labels moved its leak rate
from 4.7% to 2.8% rules-only and 7.2% to 5.3% with the model, and moved **detector recall
not at all**.

**What was kept.** The sweep does the correction (0.5.7) and `swept(spans)` and
`retyped(spans)` make the two buckets countable (0.5.8), so a consumer can print the
mislabel count beside its recall. Part B's redaction table will carry precision as well as
recall for the same reason: a number that can only go up when more text is covered is not a
measure of a boundary. The 96.3% in `docs/redact.md` stands, with this next to it.

---

## The remaining candidate

PLAN.md section 9 also listed vendor SDKs against raw HTTP. That one needs two SDK releases to
diff, which this project does not have yet. It stays a candidate.
