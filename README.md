# Inside the Boundary, On the Record

An AI gateway that lets a bank, insurer, hospital or government department use frontier AI
models without personal data ever leaving the boundary, with a tamper-evident record of
every call and a hard cap on every team's spend. The blocker most regulated organisations
cite for AI adoption, removed, with the latency overhead measured and published rather than
promised.

**Status: Part A is released, and its interface is frozen**
([docs/interface.md](docs/interface.md)). `v0.1.0` on 2026-09-10, after one live call per
provider; the library table below is measured. Everything since is additive: ledger merge
across environments and Anthropic Message Batches at half price (`v0.2.0`), the three
hyperscaler adapters (`v0.2.1`), streaming with time to first token and measured price
overlays for self-hosted GPU servers (`v0.3.0`), a data class declared on every call
(`v0.4.0`), and the `boundary.redact` package pulled forward from Part B for project 07
(`v0.5.0` to `v0.5.8`). Release by release, with the evidence for each: [CHANGELOG.md](CHANGELOG.md).
Part B, the full gateway, is planned for May 2027 in [PLAN.md](PLAN.md).

Four things worth knowing before the tables.

**Two of the three hyperscalers have credentials and are refused by quota, not by code.**
Microsoft Foundry allocates zero requests a minute for every Anthropic model in every region
that offers them, while 159 non-Anthropic quotas in the same region are normal. Vertex
returned 429 on the first request the account ever made, against a bucket that carries no
limit at all rather than a limit of zero, while the Google-model buckets beside it sit at
600. Two vendors, the same shape: the platform's own catalogue is provisioned for a new
customer and the partner's frontier models are not, and neither vendor's documentation
mentions it. Amazon Bedrock does answer: Claude from `ca-central-1`, by hand on 2026-09-15
and through this library from GitHub Actions on 2026-09-16. That row is deliberately
**uncosted**, because AWS's rates were not readable from the published page and an unknown
price never becomes an estimate. What this cost to find out, and the three different ways
these platforms avoid saying where a request is processed, is in
[docs/hyperscaler-setup.md](docs/hyperscaler-setup.md).

**One Canadian deployment answers, and it needed no adapter code**: a `gpt-5.6-luna`
deployment in `canadacentral`, reached through Foundry's OpenAI-compatible route, status 200
on 2026-09-17. Its ledger row reads `region = canadacentral` and `residency = global`:
deployed in Canada, processed anywhere, and it says both. That is the strongest honest
Canadian claim available on any of the three platforms today.

**The redaction engine has been measured, but not by this repository.** Project 07 ran its
own corpus against it and found a live leak on the first pass: a space-separated health
number left in clear on 57 of 210 pages with no refusal. Its re-measurement puts detection
recall at **96.3% (95.7 to 96.9)**, up from 90.0%. That figure is recall only, on a
synthetic corpus, so every row of it is an upper bound, and this repository has no precision
and recall harness of its own yet. The table, both runs and their caveats are in
[docs/redact.md](docs/redact.md).

**A measured number is evidence about the inputs somebody thought to measure.** Probing the
redaction pass with names that corpus does not contain, accented, `Mac` and `Mc` surnames, a
postcode written with a space, found four more values leaving in clear with no refusal, none
of which the 96.3% could have seen. Six of the seven defects found that way were in the
layer whose whole job is to catch what the detector missed.

## Result

Part A's table is measured against an in-process mock; the live-call column fills from October.
Part B fills the second table.

**Library (Part A)**

| Overhead p50 / p95 (95% CI) | Ledger completeness under fault injection | Ledger vs invoice, monthly | Calls past a spend cap | Pass-through byte fidelity |
|---|---|---|---|---|
<!-- bench:start -->
| 2.61 ms (2.58 to 2.64) / 4.62 ms (4.42 to 4.97), n = 1,000 | 600/600 = 100.0% across ok, 500, 400, timeout, malformed and a kill mid-call, both modes (100 rows left in flight by the kills, as designed) | _from October_ | 17 attempted, 0 reached the upstream | 500/500 = 100% |
<!-- bench:end -->

Filled by `boundary bench --write-readme` against an in-process mock upstream; the raw
results are in `bench/results.json`. Overhead is the library's own cost per call on the
machine that ran it: resolving, building, cap checks, two ledger writes, telemetry and
parsing, with the upstream answering instantly.

The invoice column stays empty until the vendors issue September's invoices in October.
The rest of that check is already done and written up in
[docs/invoice-check.md](docs/invoice-check.md): 70,834 real calls across three projects
merged into one ledger, US$61.3551 to 2026-09-14, reconciling with project 03's independent
accounting to within US$0.000001 over 35,728 of those calls. Nine rows are uncosted, which is
the no-guessed-prices rule doing its job, and the document bounds what they hide.

**Redaction (`boundary.redact`, pulled forward from Part B)**

| Detection recall | Typed correctly | Detection precision | Leak rate after `outbound` | Over-redaction | Round trip | Latency p50 / p95 |
|---|---|---|---|---|---|---|
<!-- redact:start -->
| 35.3% (33.1% to 37.6%) | 100.0% (99.4% to 100.0%) | 100.0% (99.4% to 100.0%) | 0.0% (0.0% to 0.3%) | 2.3% (2.1% to 2.6%) | 100.0% (98.1% to 100.0%) | 0.8 / 0.9 ms |
<!-- redact:end -->

Filled by `boundary redact eval --write-readme` over 200 generated pages holding 1,700
labelled entities, 1,450 of them personal, with the built-in recognisers and **no model**,
so anyone can reproduce it with a checkout and no key, no account and no network. Adding
Presidio (`--presidio`) takes detection recall to **94.6% (93.5% to 95.6%)** and precision
to 93.2% (91.9% to 94.3%) for 18.4 ms a page instead of 0.2.

Read the first column against the fourth. Detection recall is 35.3% because there is no
`PERSON` recogniser without a model, and **nothing leaks anyway**, because the policy's
second pass masks what no detector claimed and refuses to send what it cannot vouch for.
That gap is the whole argument for having a second pass, and a table reporting only the
first column would have hidden it. The cost is the fifth: about one word in forty is masked
that did not need to be. Intervals are Wilson score intervals at 95%, and the corpus is
synthetic, so every recall figure is an upper bound on the same figure over real records.
Method, limits and the leak this harness found on its first run:
[docs/redact.md](docs/redact.md).

`boundary redact eval --identifiers` runs the Canadian identifier set beside it: 1,150 cases
covering every shape these recognisers claim, in every form a clerk writes it in, the shapes
no recogniser claims at all, and the near-misses. **No recogniser fired on any of the 700
near-misses**, and nothing in the set left in clear, including business numbers, driver's
licences and passport numbers that this library has no recogniser for and masks anyway.

**Gateway (Part B)**

| Layer | Load (rps) | Overhead p50 / p95 / p99 ms (95% CI) |
|---|---|---|
| _not yet_ | | |

| Rehydration mutation rate under a model | Quality effect of redaction (two-sided delta) | Residency violations | Cache hit rate / false-hit rate / saved, redacted and raw | Audit tamper detection |
|---|---|---|---|---|
| _not yet_ | | | | |

## What this does not do

- It does not classify data. The caller declares a data class on every request; Part A
  records that declaration on the row and the span, and Part B will enforce the policy for
  it. Nothing enforces it today. A gateway that guessed classifications would be making a
  compliance decision nobody reviewed.
- It does not run in more than one region. Residency here means controlling where requests
  are allowed to go, not where the proxy runs.
- **It cannot verify residency, and no tool can.** It records what the operator declared and
  checks the request against that declaration. Not one of the eight providers reports where
  a request was actually processed. A product that presented a declaration as a measurement
  would be selling the assurance this one refuses to fake.
- It does not redact images or audio. Text only; non-text content is refused for anything
  but the public class.
- Redaction is not perfect. Part B's table will say by how much per entity type, measured
  here; until then the only numbers are project 07's, on its own corpus, and they are
  recall without precision. See [docs/redact.md](docs/redact.md).

## Where the data went

Part B routes by residency. Part A already records it and can be asked about it, which is the
half that turned out to be worth having early. Every ledger row carries the region a request
was **sent** to and the residency its provider entry **declared**, and `boundary ledger
residency` reads them back. From smoke run #6, 2026-09-18, copied out of the run's own log:

```
reach          provider         region             calls  cached  err      in     out
undeclared     anthropic        -                      1       0    0      14       4
undeclared     google           -                      1       0    0       8       1
undeclared     openai           -                      1       0    0      13      10
undeclared     openweights      -                      1       0    0      42       2
global         foundry-canada   canadacentral          1       0    0      13       4
global         vertex           global                 1       0    1       0       0
geo            bedrock          ca-central-1           1       0    0      14       4
```

**Four of the seven declared nothing**, because Anthropic, OpenAI, Google and Together
publish nothing per-request to declare. **The three that could declare one all declared `geo`
or `global`.** Nothing in a live run reached `single-region`, and the only route in the
checked-in configuration that can is the local model server, where the request never reaches
a network. That is the state of the market, not a gap in the configuration, and a test fails
on the day it changes.

`--require single-region|geo|global` turns the report into a gate and exits 2 when any group
went wider, so a run can fail its own CI for sending data further than it said it would.
Three rules, all failing closed: an **undeclared** row fails every limit including `--require
global`, because null is no claim rather than the weakest claim; a residency class a **later
version** added fails every limit, because an old reader cannot tell whether it is narrower or
wider; and a group of **pure cache hits** is never a violation, because nothing left the
machine.

**What a clean report does not prove.** Residency here is configuration, not observation. No
vendor reports where a request was actually processed, so a pass means every call went to an
endpoint whose declared limit was within the one required, and it means nothing about the
vendor's conduct. The command prints that sentence itself, because the moment somebody is
most likely to over-read a clean report is while they are looking at one. The detail is in
[docs/ledger.md](docs/ledger.md).

## What did not work

A central ledger written over the network, instead of one file per environment combined by
`boundary ledger merge`. It would have closed a real gap: until a merge runs, the portfolio
spend cap is checked against one machine's view. Measured over 100 simulated runs with a
network outage in each (`bench/remote-ledger.json`), the central design either stopped
every one of them part way, or finished and left 8.8% of the calls it had already paid for
with no record at all, and made 981 calls with no cap check at all, because the cap cannot
be checked when the host holding the totals is the thing that is unreachable. Local-first
lost nothing. The evidence, the method and what was kept from the idea are in
[docs/rejected.md](docs/rejected.md), reproducible with
`boundary experiment remote-ledger`.

Two more are written up on the same page. **Costing a call from a local token estimate**,
measured against 16,800 real calls from project 03, was rejected because needing a
tokenizer to state a cost is the design the no-guessed-prices rule exists to avoid. And
**judging a detector by recall alone**, which is what the redaction figure above does: that
one is not this repository's measurement but project 07's, recorded because it is about a
number published here. Recall asks whether a span was found, not what it was called, and
the label is what decides whether text is released.

## How it works

In plain language: [docs/explained.md](docs/explained.md). In full: [PLAN.md](PLAN.md). Part A: a Python library with raw-HTTP adapters for Anthropic,
OpenAI, Google, OpenAI-compatible hosts and the three hyperscaler model platforms
(Microsoft Foundry, Amazon Bedrock, Vertex AI); routing by a configuration file; one
OpenTelemetry span and one cost-ledger row per call, computed from returned usage and a
dated price list; spend caps enforced before the call; a pass-through mode with no
retries, no cache and no rewriting, verified by byte-equality tests, for measurements
that must not be confounded.

**No vendor SDKs**, and one exception with a boundary around it. Every request body is built
here and sent with a pinned `httpx` client, because an SDK release that changes a default,
a retry or a header would change a measurement without changing this repository. The single
exception is `google-auth`, used to mint the Google access token that Vertex needs and for
nothing else: it is an optional dependency (`boundary[vertex]`), it lives in one module,
and it never builds a request or parses a response. Bedrock was expected to need a second
exception, `botocore`'s SigV4 signer, and turned out not to: AWS serves the Anthropic
Messages API against an API key, so that adapter carries no vendor dependency at all.

Part B: an OpenAI-compatible proxy on the same library with
reversible PII redaction, residency routing by declared data class, a semantic cache with
its false-hit rate measured, per-team budgets, a hash-chained audit log anchored daily in
this repository, a layered load test, and a read-only dashboard of every project's calls
and costs.

## Part of a portfolio

One of fifteen projects. This one is the plumbing the others share:
every model call in the portfolio goes through it, the release-gate project measures what
its redaction does to answer quality, and the access-to-information redaction project builds on
its redaction engine.

## How this was built

Design, methodology, evaluation choices and judgement are Peter Parker's. AI coding
assistants (Claude Code) were used for implementation and drafting, the way a senior
engineer uses them in 2026. Every number in the results tables is reproducible from this
repository with one command, and that reproducibility is the evidence that matters.

## Licence

All rights reserved ([LICENSE](LICENSE)). Published to be read and to have its numbers
checked, which is what it is for. Reproducing the measurements against your own vendor
accounts is welcome; reusing the code needs a word first, and the answer is likely to be yes.
