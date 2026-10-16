# Architecture

How the gateway is put together, in four pictures: the gateway as a whole, one request's path
through the proxy, the audit chain with its daily anchor, and how each published number is
produced. For the reasoning behind the design, read [PLAN.md](../PLAN.md). For the same story in
plain language, read [explained.md](explained.md). The same four pictures are on the live site
at [gateway.peterparker.ca/architecture.html](https://gateway.peterparker.ca/architecture.html).

## The system

![Portfolio projects call the library directly and OpenAI-compatible clients call the proxy; both reach the same Gateway, which routes by configuration, checks the data policy and the caps, calls the vendor over raw HTTP, costs the call from a dated price file and writes one ledger row that the sealer chains and GitHub anchors daily.](architecture/system.svg)

Two kinds of caller reach the same gateway. The portfolio's own projects import it as a library
and call it directly. Everyone else, including any OpenAI-compatible client and anyone using the
published demo key, calls the proxy over HTTPS with a team key and a header declaring what kind
of data the request holds. The proxy hands the request to the same gateway, so a call made
either way leaves exactly the same record.

The proxy adds what the library cannot decide on its own: who is calling and what they are
allowed, what the data is, the swapping of personal details for placeholders and back, and a
cache of earlier answers.

The gateway itself asks the data policy whether that kind of data may go to the chosen vendor,
and refuses when it may not. It checks the spending caps and sends a plain HTTP request to the
vendor, with no vendor software in between. It works out the cost from the usage the vendor
reports and a dated price list, never a guess, and writes one row to the ledger. A separate
process adds each finished row to a tamper-evident chain, and a GitHub workflow copies the
chain's latest fingerprint into this repository every day. Every project's ledger is merged into
the central one behind the dashboard.

Where it lives: `boundary.gateway`, `boundary.server`, `boundary.enforce`,
`boundary.providers`, `boundary/prices/`, `boundary.ledger`, `boundary.audit` and
`boundary.central`.

## One request through the proxy

![A request is identified by its team key, classified by its declared data class, judged by the data policy, redacted if personal, checked against the team's limits, served from the semantic cache or sent through the library to the vendor, costed into the ledger, restored with only the values the caller sent, and answered; the sealer chains the ledger row behind it.](architecture/request-path.svg)

This is what happens when you press the demo button, or when any client calls the proxy.

The web server in front handles the secure connection and limits the request's size. The
request is identified by its team key. Its declared kind of data is read, and a request that
declares nothing is treated as personal, the safest assumption. The data policy then decides
whether it may go to the chosen vendor. Every rule fails closed: if the answer is no, the
request is refused and the refusal is recorded.

Personal details are swapped for placeholders, and the real values are held for this request
only. The team's limits are checked before the call, using the worst case the call could cost:
a per-minute rate, and daily and monthly budgets. A request over a limit is refused with a
message naming the limit and when it resets.

Only requests with public data may be answered from the cache of earlier answers. On a miss, the
gateway calls the vendor and writes the costed ledger row, which joins the audit chain within
50 milliseconds. The answer comes back with only the values this caller sent put back in place.
An earlier design put every value back, and a test showed that leaked, so it was rejected. The
answer is returned whole or streamed as it arrives.

Where it lives: `deploy/Caddyfile`, `boundary.server.teams`, `boundary.enforce`,
`boundary.server.redaction`, `boundary.redact` and `boundary.semcache`.

## The audit chain and the daily anchor

![One sealer appends every completed ledger row to a hash chain in an append-only Postgres table; the proxy serves the chain's head without a key, a GitHub Action pulls it every day into this repository, and the verifier checks the chain against those anchors.](architecture/audit-chain.svg)

Every call writes a ledger row. It holds identifiers, counts, fingerprints, the declared kind of
data and where it was processed, the price list used, and the cost. It never holds the content
of the request or the answer.

A single process turns each finished row into a link of a chain. Each link carries a sequence
number with no gaps, the previous link's fingerprint, and its own fingerprint computed over both,
so changing any earlier link breaks every link after it. The chain lives in a database table the
proxy cannot delete from, and a database rule refuses any edit or deletion, even by the table's
owner.

The chain's latest link is public, with no key needed. Every morning at 06:17 UTC a GitHub
workflow reads it and commits one line to this repository, so GitHub's history becomes a
timestamp nobody on the server controls. The server pushes nothing and holds no credential that
could write here. The workflow refuses to commit if the chain moves in a way an honest chain
never does, such as the sequence going backwards or the same number reappearing with a different
fingerprint.

A verifier checks the chain against the ledger and those daily anchors. A tamper test corrupts a
500-link chain 2,400 times, in twelve different ways, and reports what the verifier finds. The
six kinds of tampering no chain can detect after its last anchor are reported beside the ones it
catches.

Where it lives: `boundary.audit.chain`, `.github/workflows/anchor.yml` and
`anchors/gateway.jsonl`.

## How the published numbers are produced

![Each table in the README is filled by one command over a stated input, against a mock upstream or the hosted gateway, into a stored run file, and written between markers in the README so a reader can rerun it.](architecture/measurement.svg)

Every table in the README has one command, one stated input and one stored result. This is how
each one is made.

The gateway's own overhead, whether the ledger misses calls when things fail, whether calls get
past a spending cap, and whether requests pass through unaltered are all measured against a
stand-in vendor running on the same machine. Redaction is scored on 200 generated pages holding
1,700 labelled personal details, with no AI model involved, so anyone can rerun it offline. The
same pages are then sent through the proxy, and the personal details that reached the vendor
are counted. Redaction is also scored on a public benchmark of 127 court judgments, the way that
benchmark's own script scores it.

The tamper test corrupts a 500-link chain 2,400 times. The policy test sends every combination of
vendor and kind of data, including none and malformed ones, through all five ways in, and counts
what reaches a stand-in vendor. The load test runs against the live gateway on its server, one
layer at a time. The cache is scored offline, on held-out paraphrases and on replayed real
traffic. Once a month the merged ledgers are reconciled against the vendors' own bills.

Each command writes its own table into the README, so a reader can rerun any of them.

Where it lives: `boundary bench`, `boundary redact eval`, `boundary audit tamper-test`,
`boundary policy eval`, `boundary loadtest`, `boundary cache eval` and the results in `bench/`.
