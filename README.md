# Compliant AI Gateway

Lets a bank, insurer, hospital or government department use frontier AI models without
personal data ever leaving the boundary, with a tamper-evident record of every call and a
hard cap on every team's spend. The blocker most regulated organisations cite for AI
adoption, removed, with the latency overhead measured and published rather than promised.

**Status: Part A released as `v0.1.0` on 2026-09-10**, after one live call per provider. The interface is frozen
([docs/interface.md](docs/interface.md)); the library table below is measured. **`v0.2.0` followed on 2026-09-11**: ledger merge
across environments, Anthropic Message Batches collected at half price, and merging that no
longer writes to the ledgers it reads. Every provider and the batch path were exercised
live, and a local model server answers at price zero. **The three hyperscaler adapters are
written and one of them is exercised**: Claude on Amazon Bedrock answered from
`ca-central-1`, first by hand on 2026-09-15 and then through this library from GitHub Actions
on 2026-09-16, writing a ledger row that records the region it was sent to and the residency
its configuration declared. That row is deliberately **uncosted**: AWS publishes its own rates,
they were not readable from the published page, and an unknown price never becomes an estimate.

**Microsoft Foundry and Google Vertex have credentials and are refused by quota, not by
code.** Foundry allocates zero requests a minute for every Anthropic model in every region
that offers them, while 159 non-Anthropic quotas in the same region are normal. Vertex
returned 429 on the first request the account ever made, against a bucket that carries no
limit at all rather than a limit of zero, while the Google-model buckets beside it sit at
600. Two vendors, the same shape: the platform's own catalogue is provisioned for a new
customer and the partner's frontier models are not, and neither vendor's documentation
mentions it. What that cost to find out, and the three different ways these platforms avoid
saying where a request is processed, is in
[docs/hyperscaler-setup.md](docs/hyperscaler-setup.md).

**One Canadian deployment does answer, and it needed no adapter code**: a `gpt-5.6-luna`
deployment in `canadacentral`, reached through Foundry's OpenAI-compatible route, status 200
on 2026-09-17. Its ledger row reads `region = canadacentral` and `residency = global`:
deployed in Canada, processed anywhere, and it says both. That is the strongest honest
Canadian claim available on any of the three platforms today. **`v0.3.0` on 2026-09-19** adds
streaming for OpenAI-compatible hosts with time to first token in the ledger row, and
measured price overlays for self-hosted GPU servers, both at project 06's request for its
load tests. The first live streamed call, to a cold local model, waited 5,484 ms for its
first token out of 5,557 ms in all, which is the kind of number the column exists to show.
**`v0.4.0`, the same day**, adds a `data_class` a caller declares on every call, from the
plan's closed vocabulary, written to the ledger row and the span and readable back with
`boundary ledger residency --data-class personal`: which calls carried personal data, and
where did they go. Requested by project 07, which had been smuggling the class into a label.
Nothing enforces it yet; that is Part B, and the column is what lets Part B's policy be
checked against calls made before it existed. **`v0.5.0`, also the same day**, pulls
`boundary.redact` forward from Part B because project 07 is building on it now: detection
with per-page offsets and the recogniser named on every span, and a personal-class policy
that is built over the whole document, substitutes the parts of a detected name, masks
anything name- or identifier-shaped that no detector claimed, and refuses to send rather
than warn. The three rules come from 07's corpus-wide test, which found 74 real values
leaving a policy built the obvious way. Project 07 measured 0.5.0's detection recall the
same day, 5,355 labelled values over 210 synthetic pages, and found a live leak: a
space-separated health number left unredacted on 57 of 210 pages with no refusal. 0.5.1
fixes that in both the recogniser and the second pass, along with organisations never
being requested from Presidio and containment losing to score in overlap resolution. The
re-measurement on 0.5.1 puts overall detection recall at **96.3% (95.7 to 96.9)**, up from
90.0%, with health numbers and organisations at 100% and 96.2%. The table, both runs and
their caveats are in [docs/redact.md](docs/redact.md): recall only, on a synthetic corpus,
so every row is an upper bound. This repository's own precision and recall harness does not
exist yet, and nothing here claims a precision. **`v0.5.2` the next day** is the other half
of that caveat made concrete: probing the redaction pass with names the corpus does not
contain, accented, `Mac` and `Mc` surnames, a postcode written with a space, found four more
values leaving in clear with no refusal, none of which the measured 96.3% could have seen.
A number is evidence about the inputs somebody thought to measure.
Both parts are planned in
[PLAN.md](PLAN.md):
Part A, the `boundary` library that every project in this portfolio calls models through;
Part B, the full gateway with redaction, residency routing, audit log, cache, budgets and
the portfolio-wide observability dashboard.

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

**Gateway (Part B)**

| Layer | Load (rps) | Overhead p50 / p95 / p99 ms (95% CI) |
|---|---|---|
| _not yet_ | | |

| Redaction precision / recall by entity (95% CI) | Rehydration fidelity | Quality cost of redaction (non-inferiority delta) | Residency violations | Cache hit rate / false-hit rate / saved | Audit tamper detection |
|---|---|---|---|---|---|
| _not yet_ | | | | | |

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

## What this does not do

- It does not classify data. The caller declares a data class on every request, and the
  gateway enforces the policy for that class. A gateway that guessed classifications would
  be making a compliance decision nobody reviewed.
- It does not run in more than one region. Residency here means controlling where requests
  are allowed to go, not where the proxy runs.
- **It cannot verify residency, and no tool can.** It records what the operator declared and
  checks the request against that declaration. Not one of the eight providers reports where
  a request was actually processed. A product that presented a declaration as a measurement
  would be selling the assurance this one refuses to fake.
- It does not redact images or audio. Text only; non-text content is refused for anything
  but the public class.
- Redaction is not perfect, and the results table says by how much, per entity type.

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
every model call in the portfolio goes through it, the release-gate project measures the
quality cost of its redaction, and the access-to-information redaction project builds on
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
