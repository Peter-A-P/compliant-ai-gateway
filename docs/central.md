# The central ledger and the dashboard

`boundary serve --central PATH` (0.27) keeps a central ledger beside the proxy's own. Other
environments push copies of their rows to it, and `GET /dashboard` shows all of them: every
model call in the portfolio, by project, provider and model, and day. The hosted page is
https://gateway.peterparker.ca/dashboard.

## Why push, and not write centrally

Every environment keeps its own ledger and writes it before a call returns. That is the
Part A design, and Rule C's rejected alternative is the reason (docs/rejected.md). A remote
ledger as the only record lost calls in every outage simulated, and left caps with nothing
to check against. So the central ledger is a copy, made after the fact:

```
BOUNDARY_INGEST_KEY=bnd_... boundary ledger push --url https://gateway.peterparker.ca
```

`push` sends every row of the configured ledger (`--ledger` for another file) in batches of
500 to `POST /v1/ledger/ingest`. `--project NAME`, repeatable, sends only those projects' rows
and counts only those for completeness; the rest never leave the machine. A laptop ledger can
hold calls for a project whose repository is still private, and a public dashboard is not
where such a project should first appear. The source file is read from a temporary copy, as
`ledger merge` reads one, so a push never upgrades or otherwise writes to it.

The proxy merges the rows by `call_uid`, exactly as `ledger merge` does. A row it holds is left alone, and a row it holds in flight that has since
completed is completed. So a push is safe to repeat, and repeating it is how a push that
failed half way is finished. The key comes from the environment, never from a flag, so it
stays out of shell history. Since 0.36.1 a `.env` in the directory `push` is run from fills
it, so a key kept there as `BOUNDARY_INGEST_KEY=...` needs no export.

### From another project's own runs (0.34.1)

A project pushes its own ledgers after it runs, so the page is live rather than as of the
last push from this laptop. It need not upgrade the `boundary` it pins: `push` reads every
schema from v1 on, so the project runs a current library as a tool beside its pin, with no
configuration:

```
BOUNDARY_INGEST_KEY=bnd_... uvx --from "git+https://github.com/Peter-A-P/compliant-ai-gateway@v0.34.1" \
  boundary ledger push --url https://gateway.peterparker.ca \
  --ledger runs/2026-10/arm/ledger.sqlite --source my-repo:runs/2026-10/arm/ledger.sqlite \
  --project my-repo
```

Name the source `<repository>:<path from its root>`, which is how the first pushes were
named, so a later push of the same file adds to that source rather than starting another.
Pass `--project` with the project's own name, so only its rows leave even if a file holds
someone else's. Each repository gets its own ingest key, so one can be revoked alone.

## Completeness, measured

B1 asks for "rows in the central ledger against rows in every environment's local ledger".
Each push says how many rows its file holds, and the central file records which `call_uid`
each source has delivered. The dashboard's completeness panel shows both, per source, with
the time of the last push. A source that has delivered fewer rows than it holds shows how
many are missing. `push` exits 1 unless the central ledger holds every row it sent. The
proxy's own ledger is read live, so it is complete by construction, and the panel says so
rather than counting it as a push.

What this does not show: a call its own environment never recorded. That is what the
ledger's own completeness figure is for (600 of 600 under fault injection, in the README).

## What crosses the network

A ledger row, which holds no content by construction: hashes, counts, identifiers, costs,
latencies. One column is set to null on the way in: `raw_path`, a path on the machine that
made a pass-through call. It names that machine's directories and means nothing anywhere
else. A row with a column the central ledger does not know is refused rather than merged
short, so a newer library's rows wait for the proxy to be upgraded.

## Keys

An ingest key is not a team key. Pushing rows is not making calls, and a key that can do one
should not be able to do the other. `teams.yaml` lists ingest key hashes under
`ingest_key_sha256`, and a hash that is also a team's is refused at load time. Mint one with
`boundary teams key --team ingest` and paste the hash there.

## The page

Server-rendered, no script, no key. It shows only counts, sums and percentiles of rows:

- calls, errors, refusals, cache hits, uncosted calls and spend, by project, by provider and
  model, and by day for the last 30 days, newest first;
- latency p50 and p95 over answered, uncached calls.

The percentiles are descriptive, over the calls there were, not estimates, so they carry no
interval. The page is rebuilt at most once a minute, and after every push. PLAN.md's risk
table fences its scope: anything beyond calls, cost, latency and errors by project and model
is out.

## Not yet

- Hosted, the central ledger is Postgres's `central` table (0.28). A single-process proxy
  run with `--central PATH` keeps a SQLite file instead, and the rules are the same.
- Each environment pushes when it is told to. On 2026-09-30 the ledgers of the three public
  projects that call models were pushed from this laptop: 02's own-run ledger, 03's 92
  committed ledger files, each its own source, and 04's own rows. That is 140,573 rows from
  95 sources, every one complete. The rows of projects whose repositories are still private
  were held back with `--project`. A push from each project's own runs, so that the page is
  live rather than as of the last push, is each repository's change to make.
