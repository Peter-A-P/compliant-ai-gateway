# The proxy

`boundary serve` (0.13) is an OpenAI-compatible HTTP proxy on the library: stage 1 of Part B
(PLAN.md B2.1, B2.2's header, B2.7). A client written for OpenAI's API works by changing
two things, its base URL and its key, and every call it makes goes through the same
`Gateway` a library caller uses. So a call through the proxy writes exactly the ledger row a
library call would: the same columns, costed the same way, capped the same way, judged by
the same data policy.

What the proxy adds is the boundary's half of three things the library records but cannot
decide on its own: who is calling, what the data is, and whether it may go. Since 0.15 it
adds a fourth, which the library cannot do because it never reads content: personal data
is redacted before it leaves and restored in the answer.

## Running it

```
uv sync --extra server
boundary teams key --team analytics          # prints a key once, and the hash for teams.yaml
cp config/teams.example.yaml config/teams.yaml   # then paste the hash in
boundary serve                               # http://127.0.0.1:8080/v1
```

`serve` binds to loopback unless `--host` says otherwise, because a process holding vendor
keys is not put on a network by default. It reads the vendor keys from the environment or
the `.env` file, as the library does. Its options:

| Option | Default |
|---|---|
| `--teams` | `teams.yaml` beside `boundary.yaml` |
| `--policy` | the configuration's `policy:`, else `policy.yaml` beside it. **The proxy will not start without one** |
| `--ledger` | `boundary.proxy.sqlite` beside the configured ledger, so the teams' spend and the library's own calls are counted apart |
| `--host`, `--port` | `127.0.0.1`, `8080` |

From a client:

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8080/v1", api_key="bnd_...",
                default_headers={"X-Data-Class": "public"})
client.chat.completions.create(model="fast", messages=[{"role": "user", "content": "Hi"}])
```

`model` is a route from `boundary.yaml` (`fast`, `balanced`, ...) or the explicit
`provider/model-id` form. `GET /v1/models` lists the routes.

## Live, 2026-09-25

The first calls through the proxy to real vendors, from a laptop on a network that does not
inspect TLS: `boundary serve` on loopback, OpenAI's own Python client (3.x) as the caller,
one team capped at US$0.10 for the run, `max_tokens` 16, `X-Data-Class: public`.

| Call | Route | Stream | Result | Tokens in/out | Cost (US$) |
|---|---|---|---|---|---|
| 1 | `fast`, Claude Haiku 4.5 | no | 200, `claude-haiku-4-5-20251001` | 22 / 10 | 0.000072 |
| 2 | `fast` | `whole`, one content event | 200, `ttft_ms` null | 22 / 10 | 0.000072 |
| 3 | Together, `Llama-3.3-70B-Instruct-Turbo` | no | 200 | 47 / 4 | 0.000053 |
| 4 | Together, the same model | `native`, two content events | 200, `ttft_ms` 835 | 47 / 4 | 0.000053 |
| 5 | `fast`, no `X-Data-Class` header | no | 403 `policy_refused`, judged `personal`, nothing sent | 0 / 0 | 0 |

Five rows in the proxy's ledger, all under the team's project, 0 uncosted, US$0.00025 in
all. Each streamed call's `call_uid` as the client read it matched its row. The system
message reached both vendors as a system prompt. One call per route is a smoke test, not a
measurement: the latencies above are single observations and are not reported as overhead.

## Who is calling: team keys

A bearer key maps to a team, and **a team is a ledger `project`**. That is the whole
mapping. It means the proxy adds no second place where spend is counted: `ledger report`
shows a team's spend on its project line, and a portfolio project that later calls through
the proxy under its own name keeps one line rather than two.

`teams.yaml` stores the SHA-256 of each key, never the key. The file is therefore safe to
review and even to publish, although this repository ignores `config/teams.yaml` because it
belongs to a deployment. A raw key pasted into it is refused at load time, one hash listed
for two teams is refused, and team budgets that add up to more than the gateway's ceiling
are refused. Each team's `key_sha256` is a list, so a key can be rotated with no outage: add
the new hash, move the callers, remove the old one.

A request with no key or an unknown key is a 401 before anything else is read, and writes
no row.

## What the data is: `X-Data-Class`

Each request may carry `X-Data-Class: public | internal | personal | sensitive`. **Absent
means `personal`.** The library records a missing declaration as null, because it records
what was said; the proxy is the compliance boundary, so it substitutes the class that fails
closed and passes it to the library as a declaration. The row then says what the boundary
decided, and two response headers say how: `x-boundary-data-class` (the class used) and
`x-boundary-data-class-source` (`header` or `absent`).

Case and surrounding space are forgiven; the vocabulary is not. Any other word is a 400.
`boundary policy eval --proxy` drives every provider and alias through these rules and the
policy over HTTP, redaction's routing included: 338 cases, none of the 220 forbidden sent
(docs/policy.md).

## Whether it may go: the data policy, always on

The library's data policy (docs/policy.md) is opt-in, because turning it on for the pinned
02 and 03 runs would change what they measure. Through the proxy it is not optional: the
proxy refuses to start without a policy, so every request is judged. A refusal is a **403**
with `type: policy_refused`, the reason, and the ledger id of the `policy_refused` row that
records it. Nothing was sent. On the checked-in Canadian policy, a request with no header is
`personal`, so it is redacted (below) and then routed as `internal`: it may reach the local
model, the Canada Central Foundry deployment, Bedrock in `ca-central-1` and Vertex, the
entries that declare a residency, and is refused by the direct Anthropic, OpenAI, Google and
Together entries, which declare none. `sensitive` is never redacted and reaches only the
local model.

## Redaction (0.15)

A request whose class's policy rule names a `redacted_as` is redacted before it leaves.
On the checked-in policy that is `personal`, and so every request that arrives without a
header. What happens, in order:

1. **One policy per request, over all of it.** The system prompt and every message are
   analysed together and one `boundary.redact.Policy` is built from every span, so a person
   named in the first turn and again in the fourth is one placeholder. This is 07's first
   finding (PLAN.md B2.3), and the reason the proxy does not redact message by message.
2. **The guard refuses rather than warns.** Each text goes through `Policy.outbound`, which
   masks what the recognisers found, masks anything name- or identifier-shaped they did not
   (the second pass), and then refuses if it still cannot vouch for the result. A refusal
   is a **422** with `type: redaction_refused` and `findings`, counts by kind and type
   (`{"placeholder:EMAIL": 1}`), never a value. Nothing is sent. Since 0.16 the refusal is on
   the ledger as a `redaction_refused` row, written through `Gateway.record_refusal` with no
   request hash and no content, and the 422 carries its `ledger_id`.
3. **The preserve line.** The system prompt gains `PRESERVE_LINE`, asking the model to copy
   placeholders exactly. 0.14.0 measured a line like it cutting placeholder mutation from
   37.0% to 0.6% for Llama 3.3 70B and from 6.8% to 0.0% for Haiku, and found Llama copying
   that line's example placeholder into its answers, so this one carries no example.
   **Measured in 0.16**: 0.0% mutation for Haiku, Llama and Gemini, nothing unrecoverable and
   no copied example, against 6.8%, 37.0% and 0.0% for the task alone (docs/redact.md).
4. **Routed as the `redacted_as` class**, with `redacted=True` on the gateway call. The row
   keeps the declared class and records the redaction: `data_class = personal`,
   `redacted = 1`.
5. **The answer is rehydrated.** Placeholders in the model's text are replaced with the
   values they stand for, tolerating the forms models write (docs/redact.md). A streamed
   answer goes through `StreamRehydrator`, which never releases half a placeholder: a test
   over random texts and random cuts checks that what it releases, joined, is exactly the
   whole answer rehydrated.

Response headers say what happened: `x-boundary-redacted` (`true` or `false`),
`x-boundary-placeholders` (how many values the request's vault holds) and, on a non-streamed
answer, `x-boundary-unresolved` (placeholders in the answer that the vault does not hold,
such as one a model invented, which are left as written).

**Live, 2026-09-25.** Two requests with no header, so judged `personal`, each naming an
invented person with an email address, a phone number and a file number: one to Claude Haiku
4.5 on Bedrock (`bedrock`, plain) and one to the Canada Central Foundry deployment
(`foundry-canada`, streamed natively). Both answered 200, each answer named the person and
quoted her details, restored by the proxy, and both rows read `data_class = personal`,
`redacted = 1`. The same request to the direct Anthropic entry was refused with a 403, judged
as internal and refused for declaring no residency. **What reached Bedrock was checked, not
assumed**: the request was rebuilt offline and its body captured without sending, and its
SHA-256 matched the live row's `request_sha256`, so the captured body is the body that left.
None of the four values was in it; the user message read "Draft a two-sentence
acknowledgement to <NAME_LIKE_1> (<EMAIL_1>, <PHONE_1>) for access request <FILE_NUMBER_1>,
addressed to her by name". Both rows are uncosted, because AWS's and Azure's rates are not
in the price files and an unknown price is never estimated (117 in and 69 out on Bedrock,
113 in and 263 out on Foundry).

Note the word "her" in that message. Redaction removes identifiers, not the pronouns and
roles around them, so a gender, a job title or a relationship still travels. On its own it
identifies nobody; with enough of it, a person may be recognisable, which is the
quasi-identifier problem docs/redact.md measures on real text.

**The vault is the request's policy, in memory for the life of the request**, and is written
nowhere: not the ledger, not a span, not a log, not the response headers. The Redis vault
under a per-team key that B2.3 describes is for a proxy with more than one process.

**Measured end to end, 0.17.** `boundary redact eval --proxy` sends all 200 pages of the
generated corpus through the proxy over HTTP as personal requests, plain and streamed, to an
upstream that parses what it received and echoes it: **0 of 1,450 planted personal values
reached the wire, 0.0% (0.0 to 0.3), and 200 of 200 pages came back as sent** in each mode.
Building it found two faults in the measurement itself and none in the proxy: the labels
number pages from 1 and the first version counted from 0, so every value was looked for on
the wrong page (it now fails if a label does not select its own text); and the identifier
set's survival test, applied to whole pages, called the "2024" of a masked file number a
leak because the page also said "the 2024 budget".

**What it costs in accuracy.** Detection here is the built-in recognisers and the second
pass, with no model, so a name becomes `<NAME_LIKE_n>` rather than `<PERSON_n>`, and
anything capitalised that the vocabulary does not know is masked. That fails closed and
over-masks: on real court judgments it touched about four in five of the spans annotators
marked safe (docs/redact.md). Whether that over-masking makes answers worse is B2.8's
quality measurement through the 03 gate, which has not been run.

## Limits: 429 names the limit and when it resets

| `code` | What bound | `resets_at` and `Retry-After` |
|---|---|---|
| `team_monthly_budget` | The team's `monthly_usd` | The first of next month, 00:00 UTC |
| `team_run_budget` | The team's `per_run_usd`, for calls carrying `X-Boundary-Run-Id` | None: a new run id starts a new count |
| `gateway_monthly_budget` | `gateway_monthly_usd`, over every team | The first of next month |
| `team_requests_per_minute` | The team's `requests_per_minute`, a sliding sixty-second window | `retry_after_s`, the seconds until one more fits |

The budgets are the library's spend caps (docs/interface.md section 8), built from
`teams.yaml` and checked over the proxy's ledger before the call leaves, with the call's
worst-case cost estimated from its size and `max_tokens`. A budget refusal body carries
`limit_usd`, `spent_usd` and `estimate_usd`.

Two properties of the limits, stated rather than left to be found:

- **A refused request writes no row**, whether refused for a key, a quota or a budget. This
  is how the library's caps have always behaved: the ledger records calls, and a refused
  request never became one. A policy refusal is the exception, deliberately, because it is
  a compliance decision and has to be on the record.
- **The request quota is in memory and per process.** It resets when the proxy restarts. The
  budget is the limit that protects money and it lives in the ledger; the quota protects the
  upstream from a runaway loop, and a restart is not a loop.

## Nothing a client sent is dropped silently

A request field this gateway does not carry to every vendor is a **400 naming the field**:
`tools`, `response_format`, `seed`, `logprobs`, `n` above one, and anything else not in
`boundary.server.wire.ACCEPTED`. A proxy that accepted `seed` and ignored it would let a
caller believe the seed was honoured. The accepted fields are `model`, `messages`,
`max_tokens` or `max_completion_tokens`, `temperature`, `stop`, `stream`, `stream_options`
and `n` (when it is 1).

Messages carry `role` and `content` only. System and developer messages must come first and
become the system prompt, because Anthropic and Google take one system prompt ahead of the
conversation and a system message part way through has no faithful place to go. Content may
be a string or a list of text parts; an image part is refused, because nothing here can
inspect or redact an image (PLAN.md B2.9).

## Streaming

`stream: true` works for every route, in one of two ways, and the `x-boundary-stream` header
says which:

- **`native`** for providers whose adapter streams (`openai_compat` in 0.13). Text is passed
  on as the host sends it. The proxy holds the response until the first piece of text
  arrives or the call ends, so a refusal (policy, budget, an upstream error before the
  stream began) is still an HTTP status rather than an event inside a 200. After that, a
  failure is an `error` event, which OpenAI's client raises. A test over a real socket
  checks that the first piece reaches the client while the upstream is still paused, which
  a proxy that buffered the answer would fail.
- **`whole`** for providers whose adapter does not stream yet (Anthropic, Google and the
  hyperscalers): the call is made whole and sent as a stream with one content event. The
  client still gets a stream; the row's `ttft_ms` is null, which is true, because no first
  token was timed.

`stream_options: {"include_usage": true}` adds OpenAI's final usage event. Because a row's
`call_uid` is only known when the row completes, which is after a stream's headers are
sent, a stream's completion id is its own and the `call_uid` rides as an extra field on the
last event with a choice. A non-streamed response carries it in the `x-boundary-call-uid`
header and in its id, `chatcmpl-<call_uid>`.

A client that disconnects part way through does not cancel the call. It runs on to write its
row with the usage the host reported, because the host bills for those tokens whether or not
anybody reads them.

## Headers a caller may send

| Header | Effect |
|---|---|
| `Authorization: Bearer <key>` | Required. Which team |
| `X-Data-Class` | The class of the data. Absent is `personal` |
| `X-Boundary-Purpose` | The row's `purpose`. Default `proxy`. At most 200 characters |
| `X-Boundary-Run-Id` | The row's `run_id`, which the team's per-run budget is measured against |

## Upstream failures

The library retries as in standard mode. What is left after that is passed on: an upstream
429 is a 429, another upstream 4xx keeps its status, and a 5xx or a transport failure is a
502. The body has `type: upstream_error`, the upstream status and the retry count, and the
row records the failure as every failed call's row does.

## What this does not do yet

Each of these is in PLAN.md Part B and lands in the stage it names:

- **Redaction is rules-only and unmeasured for quality.** No detector or allow list is
  configured for the proxy, and the effect of redaction on answer quality (B2.8) has not
  been measured; PLAN.md B2.8 has the plan for it.
- **The audit chain does not seal `redacted`.** Its sealed column list is fixed so that an
  older verifier still reproduces a record (docs/audit.md); adding the column is a new
  record schema, not an edit.
- **It has not been load-tested.** The overhead budget in PLAN.md B4 is published from the
  first measurement, on the VPS, and nothing about the proxy's overhead is claimed until
  then. The ledger writes are synchronous SQLite inside the event loop, which is the first
  thing that measurement will look at.
- **The audit chain is still sealed after the fact** (docs/audit.md). The proxy writing to
  it as it answers, and the daily anchor that needs an always-on log, are B2.4.
- No semantic cache (B2.5), no injection screen (B2.6), no dashboard, SQLite rather than
  Postgres, and one process.
- Typed tool calls are refused rather than carried (PLAN.md B11).
