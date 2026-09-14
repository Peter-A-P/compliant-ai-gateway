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

| Vendor | Ledger US$ | Console US$ | Difference |
|---|---:|---:|---:|
| Anthropic | 32.3254 | pending | pending |
| Google | 14.6984 | pending | pending |
| OpenAI | 10.0011 | pending | pending |
| Together | 4.3302 | pending | pending |

To be filled between Oct 1 and Oct 3, against a re-taken snapshot once September has closed.

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

**September was priced from two repositories.** The rows cite price lists `2026-09-09`,
`2026-09-10` and `2026-09-12`. This repository holds `2026-09-07`, `2026-09-09` and
`2026-09-10`; the `2026-09-12` list lives in 02, at `mselect/config/prices/`. So September's
costing cannot be reproduced from this repository alone. That is a gap in the reproducibility
claim rather than in the arithmetic, and it needs a decision before October: either price
files live here and other projects read them, or this document records which repository
priced which rows. Nothing is wrong with the numbers; what is wrong is that checking them
needs two checkouts and the ledger does not say so.

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
