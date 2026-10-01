# The deployment

`gateway.peterparker.ca` is `boundary serve` behind Caddy, in Docker Compose, on one VPS.
The files are in `deploy/`. Stood up 2026-09-29.

## The host

| | |
|---|---|
| Provider | OVHcloud VPS-2 |
| Size | 4 vCPU, 8 GB RAM, 75 GB NVMe |
| Region | Beauharnois, Quebec (BHS) |
| OS | Ubuntu 24.04 LTS |
| DNS | an A record in Cloudflare, **DNS only**, so the TLS certificate is this host's own and nothing sits between a caller and the proxy |

The host is the machine the published load test names. It runs nothing else.

## What runs (since 0.28, 2026-09-30)

| Service | What | Reachable from |
|---|---|---|
| `caddy` | TLS from Let's Encrypt, HTTP to HTTPS, HSTS, a 2 MB body limit, server-sent events flushed as they arrive | ports 80 and 443 |
| `gateway` | `boundary serve --hosted --workers 4 --semantic-cache`, as a non-root user: four processes sharing everything below | Caddy only: compose publishes no port for it |
| `sealer` | `boundary audit follow`: the one process that appends completed ledger rows to the audit chain, every 50 ms | nothing |
| `postgres` | pgvector's Postgres 17: the ledger, the audit chain, the central ledger and the semantic cache (`boundary/pg.py`) | the compose network only |
| `redis` | the team request quotas, a sliding minute per team; nothing persisted | the compose network only |
| `init` | `boundary db init`, once per start: tables, triggers and grants, idempotently | exits |

**Two database roles.** `init` runs as the owner, `boundary_admin`. The proxy and the sealer
connect as `boundary_app`. That role can read and append the ledger and complete a row, but
cannot delete one. It can read and append the audit chain, but cannot change or remove a
record: the grant does not exist, and a trigger refuses UPDATE, DELETE and TRUNCATE on
`audit` for the owner too. A test on a real Postgres tries each one (`tests/test_pg.py`, the
`hosted` CI job). The owner's password is in `db-admin.env`, which only `postgres` and
`init` read. The proxy's own is in `db-app.env`. Both are mode 600, outside the checkout.

**The chain's head lags by at most one sealing interval.** The workers never seal: the
appender's record of what it has sealed is its own, so there is exactly one sealer.
`GET /audit/head` therefore reports the chain as the sealer last left it. A call completed
in the 50 ms before the daily anchor is read is in the next day's anchor, not that one.

**The move, 2026-09-30.** The proxy was stopped, and a copy of `/srv/boundary/data` was kept
as `data-before-postgres`. Then `boundary db import` copied the proxy's 3 ledger rows, its
3-record chain verbatim, and the central ledger's 140,573 rows and 95 sources into Postgres.
`audit verify --hosted` then checked the imported chain against the anchor already
published (seq 3): intact, 0 unanchored. The chain continued from that head: seq 4 and 5
were the first two calls on Postgres, a miss and a cache hit naming it.

## The demo (0.30)

https://gateway.peterparker.ca/demo publishes a key anybody may use, for the team `public`
in `/srv/boundary/teams.yaml`:

| Limit | Value | Why |
|---|---|---|
| `daily_usd` | US$0.25 | A key that is published will be found and scripted, so what it can spend in a day is the bill for any month: about US$7.50 at worst |
| `monthly_usd` | US$5 | The backstop under the daily budget |
| `requests_per_minute` | 10 | One visitor cannot spend the day in seconds |
| `models` | `anthropic/claude-haiku-4-5-20251001` only | About US$0.002 a call at 300 tokens, so about 125 calls a day |
| `max_tokens` | 300 | Bounds each call's worst case, which is what the budget check estimates |

The private test team, `demo`, went down to US$4 a month, and the gateway's ceiling up to
US$10, so the two fit under it with US$1 to spare. Behind that ceiling, the vendor
projects the server's keys belong to are capped at CA$20 a month each, about US$15. The
caps refuse requests, not only alert, checked in the consoles on 2026-10-01. So the
proxy's own ceiling binds first, and the vendors' would stop a fault in it. The key is in `/home/ubuntu/boundary/demo.env`
(`BOUNDARY_DEMO_KEY`, mode 600), and only its hash is in `teams.yaml`. Rotating it is a new
key and hash in place of the old, and a restart of `gateway`. The proxy refuses to start
with a demo key that is not the team's, or for a team without all three of `daily_usd`,
`models` and `max_tokens` (docs/server.md).

## Security

- **SSH:** key only, for `ubuntu` only. Password and root login are off, with at most three attempts per connection. fail2ban bans an address for an hour after five failures.
- **Firewall:** ufw denies all inbound traffic except 22 (rate limited), 80 and 443. Docker's published ports bypass ufw, which is why the gateway publishes none.
- **Updates:** unattended security upgrades are on.
- **Vendor keys:** server-only Anthropic and OpenAI keys with their own vendor-side monthly limits, in a mode-600 file outside the checkout, passed in as environment variables.
- **Team keys:** `teams.yaml` holds hashes only. The gateway ceiling (US$10 a month since 0.30) sits under the vendor projects' blocking caps of CA$20 a month each, so the proxy's own caps are the ones that bind.

## Running it

On the host, from a clone of this repository at a tag, in `/srv/boundary/repo`:

```
cd /srv/boundary/repo && git fetch --tags && git checkout v0.28.0
cd deploy
sudo docker compose build init
sudo docker compose up -d
sudo docker compose run --rm --no-deps gateway boundary --config /app/config/boundary.yaml \
  audit verify --hosted --anchors /data/anchors.jsonl
```

**A release that changes `deploy/Caddyfile` also needs `sudo docker compose restart caddy`.**
The Caddyfile is mounted as a single file, and `git checkout` replaces the file rather than
writing into it, so the running container keeps reading the old one; `caddy reload` reloads
that old copy too. Found on 0.34.0, whose new content security policy was not served until
Caddy was restarted.

The host provides these outside the checkout: `/home/ubuntu/boundary/.env` (the vendor
keys), `db-admin.env` and `db-app.env` beside it, `/srv/boundary/teams.yaml`, and
`/srv/boundary/postgres` for the database's files. The daily VPS backup covers the disk.

## First calls, 2026-09-29

These were run from the host, through the public URL, with `max_tokens` 16.

| Call | Result |
|---|---|
| `GET /healthz` | 200, `0.25.0` |
| `GET /v1/models` with no key | 401 |
| `http://` | 308 to `https://` |
| port 8080 from outside | closed |
| `fast`, `X-Data-Class: public` | 200, `claude-haiku-4-5-20251001`, 19 in and 13 out, US$0.0001, `x-boundary-injection: clean` |
| `fast`, no data class, with a name and an email in the message | 403 `policy_refused`: redacted personal data is judged as `internal`, which needs a declared residency, and the `anthropic` entry declares none |

The last row is the policy working as written, on the public host. The development laptop
refuses the same request the same way. Personal data through the hosted proxy needs a
provider entry that declares a residency the policy accepts. Declaring one that has not been
checked would be the guess the policy exists to refuse.

Afterwards, `audit verify` reported the chain intact over 3 records, with no anchor yet.

## The anchor and the load test (0.26)

- **The daily anchor**: `GET /audit/head` on this host, read by `.github/workflows/anchor.yml`
  and committed to `anchors/gateway.jsonl`. The host holds no GitHub credential
  (docs/audit.md).
- **The load test**: `boundary loadtest --generator k6` runs in a throwaway container from
  the same image, with the host's k6 (from Grafana's apt repository) mounted in. It starts
  its own mock and proxies on the container's loopback, and never touches the live proxy or
  its ledger. Results and method are in docs/loadtest.md.

```
sudo docker run --rm -v /usr/bin/k6:/usr/local/bin/k6:ro -v /srv/boundary/loadtest:/out \
  boundary-gateway:local boundary --config /app/config/boundary.yaml loadtest \
  --generator k6 --levels 50,200,500 --published --machine "..." --out /out/loadtest.json
```
