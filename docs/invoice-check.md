# The ledger against the invoice

The ledger is only worth something if it agrees with what the vendors actually bill. This
document is the check, run monthly from October 2026 (PLAN.md section 5.1, item 7). It is a
Part A deliverable, not housekeeping: the claim this project makes is that every call is on
record and costed from returned usage, and the invoice is the only thing that can falsify it.

Each month this file gains a section: the merged total, the per-vendor split, the four
console figures, and the difference. A difference above 5 percent is investigated before the
next month's run.

## Method

Every environment keeps its own ledger. The check merges them into one file kept outside
git, reports on it, and compares per vendor with the vendor consoles. `merge` reads each
source through its own temporary copy, so the sources are never written to.

```
boundary ledger merge --into <central.sqlite> <every source ledger, by its own path>
boundary ledger report --ledger <central.sqlite> --month YYYY-MM
```

Three rules about the sources, each of which cost something to learn:

1. **Merge never writes to a source.** `ledger merge` reads each source through a temporary
   copy, so merging an environment's ledger cannot stop that environment appending to it
   afterwards. Verified again here against 72 real v1 files: every source was byte-identical
   after the merge.
2. **Point `merge` at the source paths themselves. Do not pre-copy them by hand.** `merge`
   already copies the `-wal` and `-shm` sidecars with the database, so it reads everything a
   live source has committed. A hand-made `cp` of the `.sqlite` alone does not. See "The WAL
   trap" below.
3. **Quiesce what you can, and timestamp what you cannot.** A ledger being written while it
   is read gives a total that is already stale. Say when the snapshot was taken.

## September 2026

**Snapshot taken 2026-09-14T12:34:59Z. This month is not closed.** It is published early
because the run it was scoped around, 03's first official drift run, moved from Sep 27 to
Sep 13, and there was no reason to leave the method untested until October. Project 02 was
mid-run when the snapshot was taken and September's total will grow. The four vendor console
figures are the point of the exercise and they are not here yet, because the invoices are
not issued yet.

### What the ledger holds

70,834 calls, first row 2026-09-10T12:29:35Z, last 2026-09-14T12:34:59Z, **US$61.3551**.
1,964 errors, 1 row in flight at the instant of the snapshot, 9 uncosted.

| Vendor | Calls | Errors | Uncosted | US$ |
|---|---:|---:|---:|---:|
| Anthropic | 22,447 | 20 | 0 | 32.3254 |
| Google | 16,867 | 1,931 | 0 | 14.6984 |
| OpenAI | 14,957 | 4 | 0 | 10.0011 |
| Together (openweights) | 10,491 | 4 | 9 | 4.3302 |
| Local (Ollama) | 6,072 | 5 | 0 | 0.0000 |
| **Total** | **70,834** | **1,964** | **9** | **61.3551** |

| Project | Calls | US$ |
|---|---:|---:|
| 03, ai-release-gate | 35,728 | 42.9851 |
| 02, model-selection-tenth-cost | 35,099 | 18.3699 |
| 04, compliant-ai-gateway | 7 | 0.0000 |

Sources: 72 per-arm ledgers from 03's nine September runs (seven dry runs, the full dry run
and the first official run), 02's own-run ledger, and this laptop's `boundary.sqlite` (four
zero-cost rows from the TLS-proxy attempt, which stay on the record, and three local calls).

### Against the vendor consoles

The table above is the 2026-09-14 snapshot. September closed on 2026-09-30, and the check
against the consoles is in "September, closed" below.

## September, closed

**Snapshot taken 2026-10-02.** Sources: every ledger file on this laptop (03's 100 committed
ledgers, 02's own-run ledger, 06's, and this repository's two), merged into one file kept
outside git, 166,313 rows in all; and the hosted proxy's own rows, read from its Postgres.

### Anthropic

The console exports usage per day, model, key and kind (standard or batch) as tokens, and cost
per line rounded to the cent. Two keys carry the portfolio's calls: `POC`, which every project
uses through this library, and `vps`, the hosted proxy's. A third, `General-Key`, US$3.23 in
September, is not this library's and is outside the check.

| Route | US$ |
|---|---:|
| Console, `POC` and `vps` keys, sum of the daily lines | 87.02 |
| Console tokens priced at this library's rates | 87.0440 |
| Merged ledger | 87.7982 |
| **Ledger against console** | **+0.78, +0.9%** |

Inside the 5% line. **The rates are right**: the console's own token counts, priced from the
price files, give the console's bill to the cent. **And every token is accounted for**: the
ledger and the console agree exactly on 24 of 39 day, model and kind cells, and the other 15
are each one of three things.

| What | Cells | Effect on the ledger |
|---|---:|---:|
| Batches 02 submitted and never collected: 250 Haiku requests on 2026-09-14, and 100 of an Opus batch on 2026-09-18. Their rows are still in flight and carry the cap's pessimistic estimate, US$2.0511, where Anthropic billed US$0.2225 | 2 | +1.8286 |
| Calls the ledger never held: the gate's checks in `regulated-qa-demo`'s CI on 2026-09-27 and 09-28, 625,033 Haiku input and 89,576 output tokens. **They ran before the gate's action kept any artifact** (it began keeping them at 2026-09-28 20:46 UTC), so their ledgers went with their runners. A first reading here blamed the WAL trap, because the one artifact from those days holds a 0-row ledger; 03 checked, and that run was answered from its development cache and paid for nothing, so 0 rows is right | 2 | -1.0729 |
| Smoke calls from GitHub Actions, whose ledgers are not kept, and one call either side of midnight UTC | 11 | -0.0016 |

87.7982 - 1.8286 + 1.0729 + 0.0016 = 87.0441, against the console's 87.0440.

Two things follow, both in other repositories:

- **02 collects its two batches.** Done by 02 on 2026-10-02, with `batch_results` under the
  version that submitted them: all 350 requests succeeded, and the rows now cost US$0.222450
  from returned usage at the batch rate, against the US$0.2225 Anthropic billed and in place
  of the US$2.0511 estimate. Re-merged, the ledger is US$85.9807 against the console's
  87.0440 from its own tokens: the gate's unrecorded checks (1.0729) and the smoke calls
  (0.0016), less the estimates still on the nine rows below (0.0145), close it to US$0.003. Nine standard rows
  of 02's from 09-15 to 09-18 stay in flight, calls that never returned (US$0.0145 of
  estimate across three vendors), because there is nothing to collect them from.
- **The gate's CI keeps its ledger.** Done by 03 on 2026-10-02 (ai-release-gate 2de26f6):
  `gate check` checkpoints its ledger and writes its row count against its paid calls, the
  action fails a step when they differ, and the nightly harvest commits each run's ledger
  so it reaches the central ledger. The first run since holds 400 rows for 400 paid calls.

One more thing the export showed: 107 rows on 2026-09-15 are development-cache hits that
carry the token counts of the call they repeat, at zero cost. That is the cache's design, and
it is why the comparison above is of answered, uncached rows. A token total from `ledger
report` would count them twice.

### OpenAI

The console exports cost per day, and usage per day and model with input split into uncached,
cached and cache-write tokens. One project and one key carry every call.

| Route | US$ |
|---|---:|
| Console, cost export | 69.1211 |
| Merged ledger | 57.0241 |
| **Ledger against console** | **-12.10, -17.5%** |

**Above the 5% line, so investigated, and it found a defect in this library.** Two causes
carry all but two cents of it.

| What | Effect on the ledger |
|---|---:|
| **GPT-5.6 cache writes priced as ordinary input.** From GPT-5.6, OpenAI bills a token written to the prompt cache at 1.25x input (US$5.00 a million for sol against 4.00, 0.25 for luna against 0.20). The usage reports them as `cache_write_tokens` inside `prompt_tokens`, and the adapter read only `cached_tokens`, so 7,045,179 sol and 11,044,654 luna writes were costed at the input rate. Nearly all of it is 06's runs of 2026-09-20 and 21, whose prompts were written to the cache almost whole | -7.5974 |
| **4,332 gpt-5.6-luna calls on 2026-09-30 that no ledger on this laptop holds**, 12.5 million input tokens. They are 06's distillation run, made from the other laptop, and they are on the ledger there (ids 6224 to 11334, US$3.86 as costed); 06's checkout here is a week behind. So the record exists and this merge did not have it, and the difference to the invoice, US$0.62, is the same cache-write premium as above | -4.4816 |
| One gpt-5.6-sol call on 2026-09-15 that the console counts and the ledger does not | -0.0149 |
| Five gpt-5.4-mini rows of 2026-09-27 written uncosted, before that dated id had a rate (the price file of that day says so) | -0.0030 |
| Twelve gpt-5-nano smoke calls from GitHub Actions, whose ledgers are not kept | -0.0001 |

57.0241 + 7.5974 + 4.4816 + 0.0149 + 0.0030 + 0.0001 = 69.1211, the console's figure. Every
other day and model agrees with the console to the token, once the ledger's input is read as
it is recorded: uncached only, with cache reads in their own column.

**The check's own lesson: it merges every machine, not every repository.** Two laptops run
model calls in this portfolio, and a ledger is a file on the machine that wrote it. October's
merge collects 06's from the other machine, or 06 pushes it to the central ledger once its
repository is public.

**Fixed in 0.35.0**: the adapter separates `cache_write_tokens`, and the 2026-10-01 price list
carries GPT-5.6's write rates. A write with no rate makes the row uncosted, never an estimate.
**The September rows stay as written.** They do not hold the write count, so there is nothing
to recompute them from, and changing a costed row is what this ledger does not do. They are
under-costed by the figure above, and this section is where that is said. A project gets the
fix by moving its `boundary` pin to 0.35.0 or later; 02 and 06, the two that call GPT-5.6, are
told so.

### Together (openweights)

The cost export lists each day's tokens per model and kind, in millions to six places, with
the unit price. One project carries every call.

| Route | US$ |
|---|---:|
| Console, quantity times unit price | 7.8925 |
| Merged ledger | 7.8910 |
| **Ledger against console** | **-0.0015, -0.02%** |

Eleven of 20 day and model cells agree to the token, and the other nine differ by under a thousand tokens each. Of the 0.0015: **US$0.0011 is the nine
gpt-oss-120b rows of 2026-09-12 written uncosted** (above), which is the whole of that model's
bill that day. The bound this document set for them on 2026-09-14, about US$0.0025 from the
same model's costed rows, was high by a little over half, which is what a bound should be. The
rest is smoke calls from GitHub Actions (44 or 88 tokens each), one Llama call either side of
midnight UTC on 09-16 and 09-17, and a 921-token difference in gpt-oss-120b on 09-15, where the
ledger's cost is US$0.0003 higher than the console's: the console splits that day's tokens
between input and output differently from the rows, by a few calls. Nothing here needs fixing.

### Azure (Foundry)

The Azure cost export lists every meter on the billing account for September, most of it
other projects' storage and functions at zero. Four lines are this library's: input and
output tokens of the `gpt-5.6-luna` deployment on the Canadian Foundry resource, on two days.
No Claude line appears, which is right: Claude on Foundry was refused by quota and never ran.

| Day | Tokens in / out, invoice | Tokens in / out, ledger | US$, invoice |
|---|---:|---:|---:|
| 2026-09-18 | 39 / 12 | none | 0.0000222 |
| 2026-09-25 | 113 / 263 | 113 / 263, uncosted | 0.0003382 |
| **Total** | | | **0.0003604** |

The 2026-09-25 call matches to the token. The 2026-09-18 call is in no ledger here. Its
size, 39 tokens in and 12 out, is that of a test call, and where it was made is not recorded.

**Two things the invoice settles.** First, the rate: the meters price luna at US$0.20 input and
1.20 output a million, OpenAI's own, so `foundry-canada` gets a price entry (2026-10-01 list,
0.35.1) and its next call is costed. The 09-25 row stays uncosted, as written. Second, the
residency: the meter is named "Std Gl", Global Standard, which is exactly the `residency:
global` the route declares beside `region: canadacentral`. The invoice is the first evidence
from outside the configuration that the declaration was the true one.

### Amazon Bedrock

September's bill for the account is USD 0.00 on both of its lines, AWS Canada and AWS
Marketplace. Claude on Bedrock is sold through the Marketplace as "Claude Haiku 4.5 (Amazon
Bedrock Edition)", and the ledger's one Bedrock row of the month, 117 tokens in and 69 out on
2026-09-25, comes to US$0.0005 at the rate below: the bill and the ledger agree, at the
resolution the bill has. Cost Explorer's export did not list the Marketplace line at all.

**The rate, at last, and not from the bill.** AWS's web pricing page does not show Claude's
rates for Canada, which is why Bedrock had stayed uncosted since its first call on
2026-09-16. AWS's public Price List API does, in the offer `AmazonBedrockFoundationModels`
for ca-central-1, published 2026-09-30: Haiku 4.5 is US$1.10 input and 5.50 output a million
through the `us.` (Geo) profile this route uses, and 1.00 and 5.00 through `global.`, the 10%
regional premium Anthropic's page describes. Both are in the 2026-10-01 price list (0.35.2),
keyed by profile, never by the bare model id the response returns, which is the same for both.
The September row stays uncosted, as written. The next Bedrock call was made on 2026-10-02 to
show it: ledger row 8411, 14 tokens in and 4 out, costed at US$0.0000374, which is 14 at 1.10
plus 4 at 5.50 a million.

### Google

The Cloud Billing report gives a cost per day and model, with a "($)" in every column
heading, and a total of 44.5649.

**The report is in Canadian dollars, whatever its headings say.** On nine days the report and
the ledger cover the same calls, and on all nine the report is the ledger times 1.386, to
three places (1.3823 to 1.3864, median 1.3862): the exchange rate, which Azure's export of
the same month states as 1.38605. The billing account is billed in CAD. Read as dollars, the
report would have put the ledger 33% short; read as what it is, US$32.1489 at the median rate.
**The report's days are Pacific time**, not UTC: grouped by UTC day, no day matches; grouped
by Pacific day, nine do, and 2026-09-17 is zero on both sides.

| Route | US$ |
|---|---:|
| Report, CA$44.5649 at 1.3862 | 32.1489 |
| Merged ledger | 29.6541 |
| **Ledger against report** | **-2.49, -7.8%** |

Above the 5% line, and the cause is not the rate or the arithmetic: it is calls no ledger here
holds, on four Pacific days.

| Pacific day | Report, US$ | Ledger, US$ | Short |
|---|---:|---:|---:|
| 2026-09-22 | 1.5605 | 0.0141 | 1.5464 |
| 2026-09-27 | 1.9940 | 1.3707 | 0.6233 |
| 2026-09-28 | 0.2085 | 0.0317 | 0.1768 |
| 2026-09-30 | 0.1587 | 0.0000 | 0.1587 |

That is US$2.51 of the 2.49, the rest being the ledger slightly above the report on the
matched days, which is the rate's spread. 09-27 and 09-28 are the gate's checks in
`regulated-qa-demo`'s CI, the same calls that are missing from Anthropic's side: every check
calls Haiku 4.5 for answers and Gemini 3.8 Flash as judge (03, 2026-10-02). **09-30 is now on
the record**: three gate checks cancelled a minute or two in, 178 Gemini judge calls for
US$0.167, whose rows were only in each artifact's `-wal` file, which 03's harvest did not
unpack. 03 fixed the harvest and committed the three ledgers the same day; the WAL trap, a
third time. 09-22 is open: Google's 09-23 already matches 03's committed calibration run of
that morning, so 09-22's US$1.55 is a second one, and the gold runs inside that Pacific day,
two of which failed, are being checked.

### The other vendors

| Vendor | Ledger US$ | Console US$ | Difference |
|---|---:|---:|---:|
| Anthropic | 87.7982 | 87.02 | +0.78, +0.9% |
| OpenAI | 57.0241 | 69.1211 | -12.10, -17.5%, explained above |
| Together | 7.8910 | 7.8925 | -0.0015, -0.02% |
| Foundry (Azure) | 0.0000, one row uncosted | 0.0003604 | the one row matches to the token; rate now known |
| Google | 29.6541 | 32.1489 (CA$44.5649) | -2.49, -7.8%, four days of calls no ledger here holds |
| Bedrock | one row, uncosted (US$0.0005 at the rate now known) | 0.00 | agrees at the bill's resolution |

### Against 03's own accounting

The consoles are not the only check available. Project 03 records what it believes it spent,
in each run's `RUN.json`, computed by the runner rather than by this library's report path.
Merging 72 files and summing them is an independent route to the same number.

| Route | US$ |
|---|---:|
| Sum of `spent_usd` over 03's nine `RUN.json` files | 42.985145 |
| Merged central ledger, project `ai-release-gate` | 42.985144 |
| Difference | 0.000001 |

A difference of one ten-thousandth of a cent over 35,728 calls, which is float summation
order and nothing else. This does not prove the ledger matches the invoice, since both routes
read the same returned usage and the same price files. It does prove that merging 72 separate
files loses and duplicates nothing, which is the step most likely to go wrong quietly.

The row count is the same evidence from another angle: 35,728 plus 35,099 plus 7 is 70,834,
and that is what the merge holds. The 72 files from 03 were written by version 0.1.0, which
predates the `call_uid` column, so their uids were backfilled from row contents. Had that
backfill collided even once across nine runs of the same 420-item suite, the merged file
would hold fewer rows. It holds exactly all of them.

### The nine uncosted rows

All nine are `openweights/openai/gpt-oss-120b`, all HTTP 200 successes, all on 2026-09-12,
priced against the `2026-09-10` and `2026-09-12` price lists. The same model has 3,003 costed
rows. The entry was added to the price file part way through, and the nine calls that came
before it stay uncosted, because the rule is that an unknown price writes an uncosted row and
never an estimate. Back-filling them now would be inventing a price for a call whose price was
not known when it was made.

For the reconciliation, the magnitude is worth bounding, so that a later comparison against
Together's invoice is not chasing this. The nine calls carry 5,620 tokens; the 3,003 costed
calls to the same model carry 910,292 tokens and came to US$0.4072. At the same tokens-to-cost
ratio the nine are about **US$0.0025**, which is 0.004 percent of the September total. That
figure is a bound for this document, derived from the same model's costed rows. It is not
written to any ledger row and no row's `costed` flag changes.

### Two things this check found about itself

**The WAL trap, which the library avoids and the operator can walk into anyway.** 02's
ledger was being written while this was assembled. Its main `.sqlite` file had an mtime
eleven hours old while its `-wal` file was seconds old, because SQLite in WAL mode defers the
checkpoint. Gathering the sources with a plain `cp` of the `.sqlite` alone, which is the
obvious way to take a copy, silently dropped **20 committed rows**.

`ledger merge` is not the thing that gets this wrong. It copies the `-wal` and `-shm`
sidecars with the database precisely so that a live source reads whole, and pointing it
straight at 02's open ledger returned 35,147 rows where the hand-made copy held 35,076. The
hazard is entirely in the step before it: an operator tidying ledgers into one directory
before merging, which is exactly what a monthly checklist invites.

The twenty lost rows were in-flight rows costing US$0.0000, so the money was right by luck
rather than by construction; the same copy taken twenty minutes later would have dropped
costed rows. This matters for October, when several environments will have ledgers open, and
it would present as a vendor overcharge rather than as a tooling mistake. So the method above
says to pass source paths to `merge` and let it do the copying.

**September was priced from two repositories, and that is now settled.** The rows cite price
lists `2026-09-09`, `2026-09-10` and `2026-09-12`, and when this was written the `2026-09-12`
list lived only in 02, at `mselect/config/prices/`. The open question was whether price files
should live here and be read by other projects, or whether this document should record which
repository priced which rows.

**The first, and it had already happened without this page noticing.** Price files ship inside
the package at `boundary/prices/`, this repository's configuration says `prices: builtin`, and
all five dated lists including `2026-09-12` are there. September's costing is reproducible from
this checkout alone.

**Checked rather than assumed, on 2026-09-18, and the result is worth the detail:**

| File | This library | Project 02 | Bytes | Rates |
|---|---|---|---|---|
| `2026-09-10.yaml` | `boundary/prices/` | `mselect/config/prices/` | **identical** | identical |
| `2026-09-12.yaml` | `boundary/prices/` | `mselect/config/prices/` | **differ**, 6,766 against 7,607 | **identical** |

The `2026-09-12` pair differs by 841 bytes, all of it comments. Four providers, the same four
models, the same rates, the same `source` line. So the numbers in this document are right, and
they were right by diligence rather than by construction: **every ledger row from both projects
said only `price_list: 2026-09-12`, and nothing in either ledger could have shown a disagreement
if there had been one.**

### What was built so it cannot happen quietly

**A ledger row now records the rates it used, not only the date it read them from**
(schema v5, `price_sha256`). It is a sha256 of the *parsed* rates, canonically ordered, so two
files with the same rates fingerprint the same however their comments, key order or line
endings differ, and two files with different rates never do. A date is not unique across
repositories. A fingerprint is.

That turns "we checked and they agreed" into something a stranger can check from the rows
alone, which is the standard the rest of this document is held to.

**And the near-miss is held open as a test.** `tests/test_price_identity.py` loads 02's copies
from the sibling checkout and asserts the fingerprints match, skipping when 02 is not beside
this repository. A second test asserts the two files really are still byte-different, so that
if somebody makes them identical the parsed comparison is retired deliberately rather than
left passing for a reason nobody intended.

### The one thing still open, and it is in 02 rather than here

02's configuration says `prices: prices`, so it reads its own copies rather than the library's.
The copies agree today and the test above will say so, but the durable fix is one line in 02:
`prices: builtin`. That is a change to another repository and is not made from here.

Until it is made, the honest statement is the one this section now supports: the two sets of
rates were compared, they matched, and every row written from 2026-09-18 onward carries the
fingerprint that makes the comparison unnecessary.

### A column that reads empty, and why

`env` is null for 35,732 of the 70,834 rows. Those are the rows written by version 0.1.0,
which is schema v1 and has no `env` column; the merge backfills what it can from row contents
and cannot invent an environment that was never recorded. 03 pins 0.1.0 deliberately and will
keep doing so through its next runs, so September and October will both look like this. It is
the additive-schema rule behaving as intended, and it means per-environment attribution starts
from whenever 03 moves off 0.1.0, not from now.

## Reproducing this

Every number above comes from the two commands at the top of this document, run against
copies of the ledgers named in "Sources". The per-arm ledgers and each run's `RUN.json` are
committed in the 03 repository, which is public. The merged central file is deliberately not
committed anywhere: it is derived, it is rebuilt in a minute, and a copy of it going stale in
git is a better way to mislead someone than to inform them.
