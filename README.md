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
(`v0.4.0`), the `boundary.redact` package pulled forward from Part B for project 07
(`v0.5.0` to `v0.5.8`), this repository's own redaction measurements: the labelled corpus,
the Canadian identifier set and rehydration fidelity (`v0.6.0` to `v0.6.4`), a hash-chained
audit log over the ledger (`v0.7.0`), redaction measured on real court judgments and the
fixes that measurement led to (`v0.8.0` to `v0.11.0`), opt-in enforcement of the data
policy (`v0.12.0`), and the first stage of the OpenAI-compatible proxy, with team keys and
budgets and the data policy always on (`v0.13.0`, [docs/server.md](docs/server.md)). The
last five are pieces of Part B pulled forward because none of them needed the VPS or any
spend. Release by release, with the evidence for each: [CHANGELOG.md](CHANGELOG.md).
Part B, the full gateway, is planned for May 2027 in [PLAN.md](PLAN.md).

Four things worth knowing before the tables.

**Foundry and Bedrock answer this library live; Vertex refuses it by quota, not by code.**
Claude in particular is refused on two of the three. Microsoft Foundry allocates zero requests
a minute for every Anthropic model in every region that offers them, while 159 non-Anthropic
quotas in the same region are normal, so the live Foundry calls here go to a GPT deployment
on the same subscription (below) and the Claude-on-Foundry adapter is covered by goldens
only. Vertex
returned 429 on the first request the account ever made, against a bucket that carries no
limit at all rather than a limit of zero, while the Google-model buckets beside it sit at
600, and three self-service requests to raise it were denied within the minute they were
filed. Vertex therefore counts as exercised as far as the vendor allows: the adapter reached
the platform and the ledger recorded the refusal, and a 200 now waits on a support
conversation rather than on code. Two vendors, the same shape: the platform's own catalogue is provisioned for a new
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

**The redaction engine has been measured three times, on three corpora.** Project
07 ran its own corpus against it first and found a live leak on the first pass: a
space-separated health number left in clear on 57 of 210 pages with no refusal. Its
re-measurement puts detection recall at **96.3% (95.7 to 96.9)**, up from 90.0%. Since
`v0.6.0` this repository has its own harness as well, with precision beside recall and an
interval on every row (the redaction table below). Both corpora are synthetic, so every
recall figure is an upper bound. Since `v0.8.0` it has also been run on a public corpus of
real text, the Text Anonymization Benchmark, and that run is where its limitation shows (the
redaction section below). All three runs and their caveats are in
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

**On real text: the Text Anonymization Benchmark (TAB)**, 127 European Court of Human Rights
judgments annotated by hand, the benchmark's test split, scored the way its own evaluation
script scores it:

| Detector | Direct identifiers masked | Quasi identifiers masked | Precision | Safe spans touched |
|---|---|---|---|---|
<!-- tab:start -->
| built-in recognisers only | 97.1% (94.7% to 98.9%) | 32.0% (27.6% to 36.9%) | 32.2% (29.9% to 34.9%) | 78.4% (75.3% to 81.2%) |
| built-in recognisers only, allow list of 1,013 | 97.1% (94.7% to 98.8%) | 29.4% (25.0% to 34.5%) | 45.4% (42.6% to 48.4%) | 41.7% (37.8% to 45.5%) |
| built-in recognisers and Presidio | 98.7% (97.4% to 99.6%) | 33.7% (29.3% to 38.6%) | 27.9% (25.8% to 30.3%) | 80.7% (78.2% to 83.1%) |
| built-in recognisers and Presidio, allow list of 1,013 | 98.7% (97.4% to 99.6%) | 32.2% (27.8% to 37.2%) | 37.6% (34.9% to 40.5%) | 51.4% (47.2% to 55.5%) |
<!-- tab:end -->

Filled by `boundary redact eval --tab --presidio --tab-allow train --write-readme`, which
downloads the splits once at a pinned commit and refuses a file whose checksum differs.
**This is the honest limitation of the whole redaction engine, measured.** On the synthetic
corpus above about one word in forty is over-masked; on real judgments, with the default
vocabulary, **precision is under a third and about four in five of the spans the annotators
marked safe to leave are touched**, because the second pass masks every capitalised run it
cannot vouch for and its vocabulary knows nothing of British courts and ministries: the
most-touched safe spans are "United Kingdom", "Court of Appeal" and "Secretary of State".
**A jurisdiction's own allow list halves it** (`v0.10.0`): 1,013 terms derived mechanically
from TAB's train split, with no person and no cited case among its sources, take safe spans
touched from 78.4% to 41.7% and precision from 32.2% to 45.4%, with direct identifiers
unmoved and quasi identifiers down 2.6 points, which is the price of releasing places the
annotators occasionally masked. The list's two settings were chosen on the dev split by a
rule written before this split was run. With Presidio the list at first barely helped,
because it reached only the second pass; since `v0.11.0` it also releases a detector's own
place or organisation span that is one of its terms (never a person, never on the default
vocabulary alone), which takes Presidio's safe spans touched from 78.9% to 51.4% and
precision from 29.5% to 37.6%, direct identifiers unmoved. Direct identifiers, the ones that name somebody on their
own, are the part the engine exists for. **The first run on this split (`v0.8.0`) found
initials and surname particles published beside masked names** (`Mr M. Trznadel`, `Mr G`,
`van der`); `v0.9.0` masks them, a fix developed on TAB's train split and confirmed on its
dev split before this split was run a second time. It took direct identifiers from 93.3% to
97.1% and people found without a model from 37.5% to 92.7%, close to Presidio's 95.5% at a
twentieth of the latency, and it cost half a point of safe spans. Quasi identifiers are low
by design: dates, amounts and nationalities are left readable. Per-type figures, both runs,
and the cross-check against TAB's own script are in [docs/redact.md](docs/redact.md).

`boundary redact eval --identifiers` runs the Canadian identifier set beside it: 1,150 cases
covering every shape these recognisers claim, in every form a clerk writes it in, the shapes
no recogniser claims at all, and the near-misses. **No recogniser fired on any of the 700
near-misses**, and nothing in the set left in clear, including business numbers, driver's
licences and passport numbers that this library has no recogniser for and masks anyway.

`boundary redact eval --rehydration` measures what survives the trip back: of fifteen ways a
model rewrites the markup around a placeholder, **fourteen restore 100% of the values (99.8
to 100)** over 2,007 placeholders, and no placeholder this policy never minted resolves to
anything. The fifteenth is zero deliberately, and [docs/redact.md](docs/redact.md) says why.

**Audit chain (`boundary.audit`, pulled forward from Part B)**

| Tampering detected before the last anchor | False alarms on an untouched log | Detected after the last anchor | Verify time |
|---|---|---|---|
<!-- audit:start -->
| 2,400/2,400 = 100.0% (99.8% to 100.0%), 12 kinds | 0/400 = 0.0% (0.0% to 1.0%) | 1,193/2,400 = 49.7% (47.7% to 51.7%); 6 of 12 kinds never, by design | 8.3 ms for 500 records |
<!-- audit:end -->

Filled by `boundary audit tamper-test --write-readme`: a 500-record chain over a generated
ledger, anchored every 50 records except the last 50, corrupted 200 times per kind in each
region, from a seed and with no network. The kinds run from a careless edit to a forger who
owns every file, recomputes every hash and edits the ledger to agree. **Read the third column
as the design, not a shortfall.** After the last anchor a competent rewrite leaves nothing to
disagree with it, and no hash chain can do better. That window is one anchor interval, which
is why Part B anchors daily in this repository. The per-kind table, and the one deletion the
chain cannot tell from a row not yet sealed, are in [docs/audit.md](docs/audit.md).

**Data policy (`boundary.enforce`, pulled forward from Part B)**

| Residency violations on the adversarial suite | False refusals | Refusals audited on the ledger |
|---|---|---|
<!-- policy:start -->
| 0 of 550 forbidden cases sent, 0.0% (0.0% to 0.7%) | 0 of 100 allowed cases refused | 209 of 209 refusals on the ledger |
<!-- policy:end -->

Filled by `boundary policy eval --write-readme`: every provider entry and alias in this
configuration, the four declared classes, no declaration at all and five malformed ones,
through all five entry points (`chat` in both modes, `chat_stream`, `batch_submit`, `raw`),
driven through a real gateway against an in-process upstream that counts what reaches it.
The expected answer comes from an oracle that reads [config/policy.yaml](config/policy.yaml)
as plain data and shares no code with the engine. **The policy is the Canadian worked
example, and it is written to show the refusal**: personal data may go only where single-
region processing in Canada can be declared, no hosted vendor here can declare it, and so the
only provider personal data may reach is the local model. What it enforces is the operator's
declaration, not a vendor's conduct, and the command says so. Detail in
[docs/policy.md](docs/policy.md).

**Gateway (Part B)**

| Layer | Load (rps) | Overhead p50 / p95 / p99 ms (95% CI) |
|---|---|---|
| _not yet_ | | |

| Rehydration mutation rate under a model | Quality effect of redaction (two-sided delta) | Residency violations | Cache hit rate / false-hit rate / saved, redacted and raw | Audit tamper detection with daily anchors |
|---|---|---|---|---|
| _not yet_ | _not yet_ | _not yet_ | _not yet_ | _not yet_; the chain is measured above, the published anchors are Part B |

## What this does not do

- It does not classify data. The caller declares a data class on every request, and since
  `v0.12.0` a gateway given a data policy refuses a call whose class its provider's declared
  residency does not fit (below). It never infers a class from content: a gateway that
  guessed classifications would be making a compliance decision nobody reviewed. Enforcement
  is opt-in for a library caller; through the proxy (`v0.13.0`) it is always on, and an
  absent class becomes `personal` at the door.
- **The proxy does not redact yet, and its overhead is not measured yet.** A `personal`
  request through it goes only where the policy lets personal data go, which on the
  checked-in policy is the local model; redacting it so that it may go further is Part B's
  next stage. No latency figure for the proxy is claimed until the load test on the VPS.
- It does not run in more than one region. Residency here means controlling where requests
  are allowed to go, not where the proxy runs.
- **It cannot verify residency, and no tool can.** It records what the operator declared and
  checks the request against that declaration. Not one of the eight providers reports where
  a request was actually processed. A product that presented a declaration as a measurement
  would be selling the assurance this one refuses to fake.
- It does not redact images or audio. Text only; non-text content is refused for anything
  but the public class.
- Redaction is not perfect, and on real text it errs heavily towards masking. On the Text
  Anonymization Benchmark it masks most direct identifiers but touches about four in five of
  the spans annotators marked safe, because its default vocabulary is Canadian and it masks
  what it cannot vouch for. A deployment in another jurisdiction has to supply its own
  `allow` list; one derived for these judgments halves the over-masking and still leaves
  two safe spans in five touched without a model, and one in two with Presidio.

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
