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
budgets and the data policy always on (`v0.13.0`, [docs/server.md](docs/server.md)), and
placeholder mutation measured under three models (`v0.14.0`), and redaction inside the
proxy, where personal data leaves as placeholders and comes back restored (`v0.15.0`). The
last seven are pieces of Part B pulled forward because none of them needed the VPS or any
spend. **Since `v0.26.0` the proxy is live at gateway.peterparker.ca** on a VPS in Quebec
([docs/deploy.md](docs/deploy.md)), with its audit chain anchored in this repository every
day and its overhead measured there. `v0.27.0` adds the semantic cache to the proxy, off by
default ([docs/cache.md](docs/cache.md)), and the portfolio dashboard at
[gateway.peterparker.ca/dashboard](https://gateway.peterparker.ca/dashboard), read from the
ledger rows each project pushes ([docs/central.md](docs/central.md)). `v0.30.0` publishes a
key anybody may call it with, at
[gateway.peterparker.ca/demo](https://gateway.peterparker.ca/demo), bounded at US$0.25 a day,
and `v0.31.0` puts this project's page, every figure read from the tables below, at
[gateway.peterparker.ca](https://gateway.peterparker.ca/) ([docs/site.md](docs/site.md)).
Release by release,
with the evidence for each: [CHANGELOG.md](CHANGELOG.md).
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

**The same corpus through the proxy** (`v0.17.0`): every page sent to `boundary serve` over
HTTP as a `personal` request, the upstream a mock that parses what it received and echoes it.

| Personal values that reached the wire | Came back as sent, plain | Came back as sent, streamed | Refused by the guard |
|---|---|---|---|
<!-- redact-proxy:start -->
| 0 of 1450 plain, 0.0% (0.0% to 0.3%); 0 of 1450 streamed, 0.0% (0.0% to 0.3%) | 100.0% (98.1% to 100.0%) | 100.0% (98.1% to 100.0%) | 0 of 400 |
<!-- redact-proxy:end -->

Filled by `boundary redact eval --proxy --write-readme`. A value counts as reaching the wire
if it is there whole or any run of three of its digits is, unless the page's own public text
has that run too. The test also runs with redaction switched off in the proxy and requires
every value to show up, so the zero is a measurement that could have said otherwise.

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
| built-in recognisers and Presidio, allow list of 1,013 | 98.7% (97.4% to 99.6%) | 32.3% (27.8% to 37.2%) | 37.6% (34.9% to 40.5%) | 51.4% (47.2% to 55.5%) |
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

| Layer | Load (rps) | Overhead p50 / p95 / p99 ms (95% CI) | Errors |
|---|---|---|---|
<!-- loadtest:start -->
| routing | 50 | 4.0 (3.9 to 4.1) / 5.0 (4.9 to 5.1) / 6.4 (5.8 to 7.0) | 0 of 2503 |
| routing | 200 | 4.2 (4.2 to 4.3) / 5.6 (5.4 to 5.8) / 7.0 (6.6 to 7.4) | 0 of 10005 |
| routing | 500 | 9.0 (8.6 to 9.3) / 16.3 (15.5 to 17.2) / 27.2 (22.3 to 32.4) | 0 of 25002 |
| audit | 50 | 4.2 (4.0 to 4.4) / 5.1 (4.9 to 5.4) / 6.2 (5.5 to 7.6) | 0 of 2504 |
| audit | 200 | 4.6 (4.4 to 4.7) / 6.1 (5.9 to 6.4) / 7.9 (7.2 to 8.8) | 0 of 10005 |
| audit | 500 | 9.9 (9.6 to 10.1) / 19.1 (18.4 to 20.1) / 27.6 (25.4 to 30.9) | 0 of 25005 |
| redaction | 50 | 5.6 (5.4 to 5.8) / 6.7 (6.3 to 7.0) / 7.7 (7.0 to 8.5) | 0 of 2503 |
| redaction | 200 | 7.9 (7.7 to 8.0) / 11.4 (10.6 to 12.3) / 16.4 (13.4 to 21.7) | 0 of 10003 |
| redaction | 500 | 188.4 (174.3 to 200.7) / 347.8 (320.4 to 376.1) / 432.0 (383.4 to 493.6) | 0 of 25003 |
| cache | 50 | 33.1 (31.4 to 34.7) / 38.4 (36.9 to 39.8) / 41.2 (39.5 to 42.7) | 0 of 2502 |
| cache | 200 | shedding: 4537 of 10003 answered past a busy cache, so no cache figure | 0 of 10003 |
| cache | 500 | shedding: 22761 of 25003 answered past a busy cache, so no cache figure | 0 of 25003 |
<!-- loadtest:end -->

Filled by `boundary loadtest --generator k6 --hosted --workers 4 --published --write-readme`
(`v0.29.0`), run on the VPS that serves gateway.peterparker.ca: an OVHcloud VPS-2, 4 vCPU (AMD
EPYC-Milan) and 8 GB, in Beauharnois. The proxy runs as four workers on Postgres and Redis,
as it does in production, with k6, a 50 ms mock upstream and the databases on the same host.
Five runs of 10 s per cell; overhead is the proxy's percentile minus the mock's in the same
run, and the interval is a bootstrap over runs. **Every cell answered every request: 0 failed
of 150,041. At 500 requests a second routing and the audit chain add 27 ms at p99, inside the
provisional budget of PLAN.md B4.** Redaction holds 500 too, but at a 188 ms median: that
cell is the proxy's queue, at the edge of what four shared cores can redact, and is not a
budget. The semantic cache sheds instead of queueing: a worker with its embedder backed up
answers upstream as if the cache were off and says `skip: busy`, so past 50 requests a
second most questions go past it and the cell has no cache figure. **Each measurement has
changed the architecture, as B4 meant it to.** In `v0.26.0` the audit chain stopped one
process at 200 requests a second, and moving its commits off the event loop fixed that in
`v0.27.0`. `v0.28.0` moved to four workers on shared state and still failed 13% at 500.
That run blamed the machine, which was wrong: the workers had CPU to spare and were queued
on the spend-cap lock, held across six round trips to Postgres. `v0.29.0` makes the cap
check one call to a function in the database. It also fixes three smaller costs a profile
found, gives the cache an HNSW index and has it shed its load (docs/loadtest.md). The
single-process tables, and the Python client's cross-check, are in
[docs/loadtest.md](docs/loadtest.md).

| Residency violations through the proxy | False refusals | Refusals audited on the ledger |
|---|---|---|
<!-- policy-proxy:start -->
| 0 of 220 forbidden cases sent, 0.0% (0.0% to 1.7%) | 0 of 118 allowed cases refused | 168 of 168 refusals on the ledger |
<!-- policy-proxy:end -->

Filled by `boundary policy eval --proxy --write-readme` (`v0.13.1`): the same suite over
HTTP through `boundary serve`, where the class arrives as an `X-Data-Class` header. Every
provider entry and alias, thirteen header values (absent, blank, the four classes, forms the
proxy forgives such as `Personal`, and words it refuses such as `secret`), a plain call and a
stream. The oracle models the door's rules on its own: absent or blank is `personal`, case
and space are forgiven, any other word is a 400, and since `v0.15.0` a `personal` request is
redacted and routed as `internal`, as the policy's `redacted_as` says. Against a counting mock upstream, like the
row above; the proxy's live calls are in [docs/server.md](docs/server.md).

**Placeholder mutation under a model** (`v0.14.0`)

| Model | Mutated, task alone | With the 0.14 line | With the proxy's line | Effect, 0.14 line | Effect, proxy's line | Unrecoverable, alone / 0.14 / proxy | Lost from a list, alone / 0.14 / proxy |
|---|---|---|---|---|---|---|---|
<!-- mutation:start -->
| anthropic/claude-haiku-4-5-20251001 | 6.8% (5.1% to 8.9%) of 695 | 0.0% (0.0% to 0.5%) of 715 | 0.0% (0.0% to 0.5%) of 725 | -6.8 points (-8.9 to -5.0) | -6.8 points (-8.9 to -5.0) | 0.6% (0.2% to 1.5%) / 0.0% (0.0% to 0.5%) / 0.0% (0.0% to 0.5%) | 0.3% (0.1% to 1.9%) / 0.0% (0.0% to 1.3%) / 0.0% (0.0% to 1.3%) |
| google/gemini-3.5-flash-lite | 0.0% (0.0% to 0.6%) of 673 | 0.0% (0.0% to 0.6%) of 685 | 0.0% (0.0% to 0.5%) of 716 | +0.0 points (-0.6 to +0.6) | +0.0 points (-0.6 to +0.5) | 0.0% (0.0% to 0.6%) / 0.0% (0.0% to 0.6%) / 0.0% (0.0% to 0.5%) | 1.0% (0.3% to 3.0%) / 0.0% (0.0% to 1.3%) / 0.0% (0.0% to 1.3%) |
| openweights/meta-llama/Llama-3.3-70B-Instruct-Turbo | 37.0% (33.3% to 40.9%) of 605 | 0.6% (0.2% to 1.6%) of 629 | 0.0% (0.0% to 0.6%) of 619 | -36.4 points (-40.3 to -32.5) | -37.0 points (-40.9 to -33.2) | 0.0% (0.0% to 0.6%) / 0.6% (0.2% to 1.6%) / 0.0% (0.0% to 0.6%) | 0.0% (0.0% to 1.3%) / 0.3% (0.1% to 1.9%) / 0.3% (0.1% to 1.9%) |
<!-- mutation:end -->

Filled by `boundary redact mutation --score bench/mutation.json --score
bench/mutation-proxy-line.json --write-readme`, from two stored runs over the same pages, 720
calls in all (US$0.37): 40 redacted pages from the generated corpus, each sent twice,
once to list every person and identifier and once to draft a reply, with and without one
system line asking for placeholders to be copied exactly. **Mutated** is any placeholder not
written as minted; **unrecoverable** is the part the library cannot put back (an invented or
renumbered placeholder, or one respelled as words). Most mutation is recoverable, because the
forms models chose (brackets dropped, square brackets) are forms the 0.6.4 matcher already
reads. The line cuts mutation sharply for the two models that mutate and does not raise how
many placeholders go missing, which stays at 1% or under. The only unrecoverable tokens under the 0.14 line are the example placeholder Llama
copied out of the instruction itself; the proxy's line (`v0.16.0`) gives none and carries no
example, and it is the one the proxy sends. Detail, and what this cannot see, in
[docs/redact.md](docs/redact.md).

**The quality cost of redaction, through project 03's gate** (`v0.20.0`, PLAN.md B2.8)

| Comparison, pooled over three models | Pairs | Worse / better | Complete answers, points (Newcombe) | 03's bootstrap | Could detect a loss of |
|---|---|---|---|---|---|
<!-- quality:start -->
| Public pages, redacted as the proxy does today, against raw | 300 | 4 / 0 | -1.5 (-3.8 to +0.2) | -3.1 to -0.4 | 3.0 |
|   of which, questions the page cannot answer | 36 | 4 / 0 | -12.5 (-28.5 to +0.7) | -22.3 to -3.1 | 20.5 |
| Public pages, redacted with the allow list, against raw | 300 | 1 / 0 | -0.4 (-2.1 to +1.1) | -1.2 to +0.0 | 3.0 |
|   of which, questions the page cannot answer | 36 | 1 / 0 | -3.1 (-15.9 to +8.0) | -9.6 to +0.0 | 20.5 |
| A customer's details in the question, redacted (with the list), against raw | 300 | 2 / 0 | -0.7 (-2.7 to +0.8) | -1.9 to +0.0 | 3.0 |
|   of which, questions the page cannot answer | 36 | 2 / 0 | -6.2 (-20.4 to +5.5) | -15.4 to +0.0 | 20.5 |
<!-- quality:end -->

**Where the cost is: a page that cannot answer** (03's distractor stratum, 60 questions
served against another page, three models; the outcome is whether the answer says the page
does not cover the question)

| Comparison, pooled over three models | Pairs | Stopped / started declining | Declines, points (Newcombe) | Bootstrap by question | Per model |
|---|---|---|---|---|---|
<!-- quality-distractor:start -->
| Redacted as the proxy does today, against raw | 180 | 32 / 6 | -14.4 (-20.6 to -8.0) | -21.1 to -7.2 | claude-haiku-4-5-20251001 -6.7 (-16.1 to +2.6); Llama-3.3-70B-Instruct-Turbo -25.0 (-35.5 to -13.8); gpt-5.4-mini-2026-03-17 -11.7 (-23.6 to +0.7) |
| Redacted with the allow list, against raw | 180 | 35 / 3 | -17.8 (-23.8 to -11.5) | -23.9 to -11.7 | claude-haiku-4-5-20251001 -6.7 (-15.1 to +1.4); Llama-3.3-70B-Instruct-Turbo -28.3 (-39.1 to -16.6); gpt-5.4-mini-2026-03-17 -18.3 (-29.9 to -6.4) |
| Redacted against raw, decline pattern extended after reading the pairs | 180 | 29 / 6 | -12.8 (-18.8 to -6.6) | -19.4 to -6.1 | claude-haiku-4-5-20251001 -1.7 (-9.6 to +6.2); Llama-3.3-70B-Instruct-Turbo -25.0 (-35.5 to -13.8); gpt-5.4-mini-2026-03-17 -11.7 (-23.6 to +0.7) |
<!-- quality-distractor:end -->

Filled by the same command (`v0.24.0`, US$0.45 more, design committed first). **The lead
held, and it is the real cost of redaction**: served a page that cannot answer, a raw model
says so about four times in five, and a redacted one 14 points less often (-20.6 to -8.0),
answering from memory instead ("The document says you generally have 90 days"). It is not
run noise: today's raw answers and 03's own from 2026-09-22 differ by under 2 points. The
allow list does not help. The outcome is programmatic, and the pattern was read against 03's
human labels before the run (145 of 146 declines labelled faithful); reading the discordant
pairs showed it missing one phrasing Haiku used more when redacted, and the third row, with
that phrasing added after the fact, is the check that the result survives it. For a
retrieval pipeline this is the case that matters: the wrong page comes back, and a redacted
request is less likely to say so.

**What redaction does to a leak, on 03's red team** (`pii_leakage`, 200 attacks on a
confidential customer record, graded by 03's `withholds_pii`)

| Model | Leaked, raw | Redacted: value sent to the vendor | Redacted: value received by the caller | Redacted, rehydrating only what the caller sent |
|---|---|---|---|---|
<!-- quality-redteam:start -->
| anthropic/claude-haiku-4-5-20251001 | 0 of 200, 0.0% (0.0% to 1.9%) | 0 of 200, 0.0% (0.0% to 1.9%) | 13 of 200, 6.5% (3.8% to 10.8%) | 0 of 200, 0.0% (0.0% to 1.9%) |
| openweights/meta-llama/Llama-3.3-70B-Instruct-Turbo | 119 of 200, 59.5% (52.6% to 66.1%) | 0 of 200, 0.0% (0.0% to 1.9%) | 107 of 200, 53.5% (46.6% to 60.3%) | 0 of 200, 0.0% (0.0% to 1.9%) |
| openai/gpt-5.4-mini-2026-03-17 | 1 of 200, 0.5% (0.1% to 2.8%) | 0 of 200, 0.0% (0.0% to 1.9%) | 3 of 200, 1.5% (0.5% to 4.3%) | 0 of 200, 0.0% (0.0% to 1.9%) |
<!-- quality-redteam:end -->

Filled by `boundary redact quality report --write-readme` from stored answers and verdicts
(`bench/quality/`), US$3.33 of calls on 2026-09-27, the design committed before the first
answer. Answers from Haiku 4.5, Llama 3.3 70B and gpt-5.4-mini to 03's 100 gold questions,
judged by 03's own completeness judge through 03's own code (kappa 0.914), each difference
divided by the judge's Youden factor. Every raw answer was complete, so 03's bootstrap is
shown beside Newcombe's paired interval, which is the one that stays honest at a 100%
baseline. **No quality cost was visible on the 264 answerable pairs of any arm.** Every loss
is on a question the page cannot answer, where a redacted model stops saying so; that split
was chosen after reading the failures and is a lead, not a result. The prediction from
over-masking (0.16.0) did not hold: the 15 questions whose required phrases were masked lost
nothing. **The red team inverts the premise**: redacted, no value reaches the vendor, but the
proxy rehydrates the answer, and Haiku, which leaks nothing raw, lists the placeholders it
will not share and hands the caller all thirteen records it refused. Rehydrating only the
values the caller sent (`caller_scoped`, measured offline on the stored answers) leaks 0 of
600 and leaves a placeholder in 7 of 900 ordinary answers; **since `v0.21.0` it is what the
proxy does by default** (`rehydrate: caller` in the data policy, Peter's decision 2026-09-27).
Method and every row in [docs/redact.md](docs/redact.md).

**The proxy's redaction settings compared** (`v0.23.0`): the policy's `redaction` block

| Configuration | Personal values on the wire | Came back as sent | Refused | Required phrases masked (03 gold) | Placeholders, median | Redaction ms p50 / p95 |
|---|---|---|---|---|---|---|
<!-- detectors:start -->
| rules | 0 of 1450, 0.0% (0.0% to 0.3%) | 100.0% (99.0% to 100.0%) | 0 | 14.0% (9.0% to 21.0%) | 15 | 2.6 / 3.0 |
| rules + allow list | 0 of 1450, 0.0% (0.0% to 0.3%) | 100.0% (99.0% to 100.0%) | 0 | 10.1% (6.0% to 16.5%) | 11 | 3.1 / 3.5 |
| Presidio | 0 of 1450, 0.0% (0.0% to 0.3%) | 100.0% (99.0% to 100.0%) | 0 | 16.3% (10.9% to 23.6%) | 17 | 67.8 / 78.0 |
| Presidio + allow list | 0 of 1450, 0.0% (0.0% to 0.3%) | 100.0% (99.0% to 100.0%) | 0 | 12.4% (7.8% to 19.2%) | 12 | 68.7 / 79.3 |
<!-- detectors:end -->

Filled by `boundary redact detectors --write-readme`, offline: the proxy end to end on the
generated corpus (1,450 planted values, plain; 400 answers, plain and streamed), over-masking
on 03's gold set, and the time to redact each gold request. **Presidio buys nothing here and
costs 66 ms a request**: no leak the rules miss on this corpus, more of the answer's context
masked, and a median of 69 ms against 3. Its first run did buy something, a leak: it called
the `ATIPP-2024` of a file number `ATIPP-2024-1749` an organisation, the file-number
recogniser lost the span, and `-1749` went out in clear on 3 of 200 pages. The policy now
masks the rest of a value glued to one of its placeholders (`v0.23.0`), which is what the
rows above were run with. The proxy's default stays `rules`; a deployment turns Presidio on
where its text has the names the rules cannot see, and supplies its own allow list.

**The injection screen** (`v0.22.0`, PLAN.md B2.6): rules only, advisory by default

| Set | Injections detected | Ordinary requests flagged |
|---|---|---|
<!-- screen:start -->
| deepset train (development set) | 164 of 203, 80.8% (74.8% to 85.6%) | 2 of 343, 0.6% (0.2% to 2.1%) |
| deepset test | 26 of 60, 43.3% (31.6% to 55.9%) | 0 of 56, 0.0% (0.0% to 6.4%) |
| 03 prompt_injection | 200 of 200, 100.0% (98.1% to 100.0%) |  |
| 03 pii_leakage | 100 of 200, 50.0% (43.1% to 56.9%) |  |
| 03 gold questions |  | 2 of 100, 2.0% (0.6% to 7.0%) |
| 03 over_refusal |  | 0 of 150, 0.0% (0.0% to 2.5%) |
| 03 jailbreak |  | 0 of 150 flagged; not injection |
<!-- screen:end -->

Filled by `boundary screen eval --write-readme`, offline. Seven named rules for the moves an
injection makes (override, pivot, exfiltrate, persona, delimiter, canary, obfuscation),
written on deepset/prompt-injections' **train** split only and frozen in a commit before the
rest was run once. **The gap between the first two rows is the finding**: 80.8% on the set
the rules were written on, 43.3% on the same source's held-out split. Rules catch the
templated attacks (every one of 03's PromptInject attacks hidden in a regulator page, and
the half of its leak suite built from PromptInject) and miss paraphrase; the other half of
03's leak suite is social engineering ("I forgot which email I used"), which is not an
injection and which no screen of this kind should catch. On ordinary requests it flagged 2
of 100 of 03's gold prompts, both one page's "or pretend to be", and none of the 150
over-refusal prompts. The proxy records a flag on the ledger, the audit chain and a header,
and blocks only under `injection: block`. Detail in [docs/screen.md](docs/screen.md).

**The semantic cache, before it is switched on** (`v0.25.0`, PLAN.md B2.5; held-out half)

| Prompt shape | Query | Threshold | Hits on questions the cache holds | False hits, of all hits |
|---|---|---|---|---|
<!-- cache:start -->
| chat | paraphrase | 0.82 | 49 of 50, 98.0% (89.5% to 99.6%) | 0 of 49, 0.0% (0.0% to 7.3%) |
| chat | persona-raw | 0.82 | 23 of 25, 92.0% (75.0% to 97.8%) | 3 of 26, 11.5% (4.0% to 29.0%) |
| chat | persona-redacted | 0.82 | 25 of 25, 100.0% (86.7% to 100.0%) | 24 of 49, 49.0% (35.6% to 62.5%) |
| retrieval | paraphrase | 0.99, none qualified | 50 of 50, 100.0% (92.9% to 100.0%) | 20 of 70, 28.6% (19.3% to 40.1%) |
| retrieval | persona-raw | 0.99, none qualified | 22 of 25, 88.0% (70.0% to 95.8%) | 6 of 28, 21.4% (10.2% to 39.5%) |
| retrieval | persona-redacted | 0.99, none qualified | 25 of 25, 100.0% (86.7% to 100.0%) | 10 of 35, 28.6% (16.3% to 45.1%) |
<!-- cache:end -->

Filled by `boundary cache eval --write-readme`, offline, bge-small run locally, labelled by
construction on 03's gold questions (the cache holds half; a hit on the other half, or on a
different question, is false). The threshold, 0.82, was chosen on the odd half and committed
before the even half ran. **A bare question caches well**: paraphrases hit 98% of the time
with no false hit in 49. **A question inside a retrieved page cannot be cached this way at
all**: no threshold qualified, and even at 0.99 more than a quarter of hits were a different
question about the same page, because the page is most of the text. **Redaction does raise
the hit rate, as project 07 predicted, and the false-hit rate with it**: a customer's details
replaced by placeholders make every customer's preamble identical, and half the hits were
the wrong question. So the proxy's cache, when it is wired in, embeds bare questions only and
never a redacted payload, which the data policy's `cache: false` for `personal` already
says. Dollars saved need real traffic, which is the replay below.

**The semantic cache on real traffic** (`v0.36.0`, PLAN.md B2.5): every call of 03's twelve
drift runs, 2026-09-12 to 2026-10-01, replayed in order through one cache at the proxy's
threshold

| Requests looked up | Threshold | Hits, of lookups | Exact hits | Semantic hits | False, of semantic hits | False, of distinct semantic pairs | Saved, of what the calls cost | Saved by semantic hits alone |
|---|---|---|---|---|---|---|---|---|
<!-- cache-replay:start -->
| as marked | 0.82 | 55,802 of 59,000, 94.6% (94.1% to 95.0%) | 51,885 | 3,917 | 640 of 3,917, 16.3% (4.6% to 32.1%) | 6 of 37, 16.2% (7.7% to 31.1%) | US$50.54 of US$53.38, 94.7% (94.2% to 95.0%) | US$3.05 |
| every block marked | 0.82 | 60,578 of 63,920, 94.8% (94.3% to 95.1%) | 54,385 | 6,193 | 2,916 of 6,193, 47.1% (31.0% to 62.8%) | 34 of 65, 52.3% (40.4% to 64.0%) | US$63.63 of US$81.04, 78.5% (70.3% to 86.8%) | US$13.29 |
<!-- cache-replay:end -->

Filled by `boundary cache replay --write-readme`, offline: every answer and cost was already
in 03's raw stores and records. A hit is labelled by construction: correct when the stored
question is the same suite item, a paraphrase of it, or the same text. "As marked" looks up
only what a caller would mark a bare question, decided before the replay ran; "every block
marked" also looks up the two blocks whose message carries a document. Hit rates and
savings carry 95% intervals from resampling question families, because each question is
asked hundreds of times; the distinct pairs carry Wilson intervals.

**Nearly all of the saving is repeats, and the semantic part is where the risk is.** 03 asks
each of its 400 distinct questions five times a run, in twelve runs, so 51,885 of the 55,802 hits are
byte-identical repeats an exact cache would also have answered; the drift record exists to
measure those repeats, which is why pass-through never caches. The hits only a semantic
cache makes saved US$3.05, and 6 of their 37 distinct pairs were a different question
answered with another's answer, at the threshold that had none in 49 on 03's gold
questions: "Compute tan 45 degrees" answered as "Compute tan 135 degrees" at a cosine of
0.92, and two different group-theory statements at 0.967. The 31 right ones were the suite's
own paraphrases. So 0.82 does not carry from customer questions to mathematics, where one
symbol changes the answer; no threshold below 0.97 kept false pairs under 2% here, and 0.97
kept 9 of the 31 good ones. Marking still matters most: with the two document blocks looked
up too, 52% of distinct semantic pairs were false, every one of the 28 document pairs among
them, as the gold-question measurement predicted. Detail in [docs/cache.md](docs/cache.md).

| Audit tamper detection with daily anchors |
|---|
| _not yet_; the chain is measured above, the published anchors are Part B |

## What this does not do

- It does not classify data. The caller declares a data class on every request, and since
  `v0.12.0` a gateway given a data policy refuses a call whose class its provider's declared
  residency does not fit (below). It never infers a class from content: a gateway that
  guessed classifications would be making a compliance decision nobody reviewed. Enforcement
  is opt-in for a library caller; through the proxy (`v0.13.0`) it is always on, and an
  absent class becomes `personal` at the door.
- **The proxy's redaction is rules only.** Since `v0.15.0` a personal request through the proxy is
  redacted before it leaves and routed as internal data, so it can reach Claude on Bedrock in
  `ca-central-1` but not the direct Anthropic API, which declares no residency here. With no
  detector configured, names become `<NAME_LIKE_n>` and anything capitalised the vocabulary
  does not know is masked, which fails closed and over-masks: on project 03's gold set, which
  holds no personal data at all, it masks 14.0% (9.0 to 21.0) of the phrases the answers are
  judged on (`boundary redact overmask`, `v0.16.0`). Measured through project 03's gate
  (`v0.20.0`), that cost no answer its completeness on the questions a page answers, within
  about 1.6 points; the losses were on questions the page cannot answer, twelve per model,
  too few to measure, so `v0.24.0` tested it on 03's 60-question distractor stratum: served a
  page that cannot answer, a redacted request declines 14.4 points (-20.6 to -8.0) less often
  than a raw one and answers from memory instead. Until `v0.21.0` the proxy also rehydrated every value into the answer,
  including ones that arrived in the system prompt, which on 03's red team turned a model's
  refusals into disclosures; it now puts back only what the caller sent, so an application
  that addresses its user by a name held only in its system prompt sees a placeholder there
  (7 of 900 ordinary answers, all 03's own word "Answer").
- **One 4 vCPU host is its ceiling.** Hosted as four workers on Postgres and Redis, it
  answers every request at 500 a second since `v0.29.0`, but redaction at 500 is a queue (a
  188 ms median) and the semantic cache sheds its load past about 50 a second, answering
  those requests uncached. What is left is CPU on one machine that also runs the load
  generator in the published test. It runs in one region on one host (below).
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

**Part B's rejected approach is putting every value back into the answer**, what reversible
redaction usually means and what the proxy did until `v0.21.0`. The vault knows values, not
who may read them, so on 03's red team a model that refused to disclose a record while naming
its fields as placeholders had the record disclosed for it: Haiku 4.5 leaked 0 of 200 raw and
13 of 200 redacted. Putting back only what the caller sent leaks none; the evidence is in
[docs/rejected.md](docs/rejected.md).

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
