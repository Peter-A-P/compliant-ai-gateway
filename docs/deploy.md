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

## What runs

| Service | What | Reachable from |
|---|---|---|
| `caddy` | TLS from Let's Encrypt, HTTP to HTTPS, HSTS, a 2 MB body limit, server-sent events flushed as they arrive | ports 80 and 443 |
| `gateway` | `boundary serve` on the tagged checkout, as a non-root user; the ledger and the audit chain are SQLite files on `/srv/boundary/data` | Caddy only: compose publishes no port for it |

**Postgres, pgvector and Redis are not deployed yet.** PLAN.md puts them on this host, and
they will be, but today the proxy's ledger and audit chain are SQLite and its vault is in
memory. A database container that nothing reads would be a claim the code does not make.
Each joins `compose.yaml` in the commit that makes the code use it.

## Security

- **SSH:** key only, for `ubuntu` only. Password and root login are off, with at most three attempts per connection. fail2ban bans an address for an hour after five failures.
- **Firewall:** ufw denies all inbound traffic except 22 (rate limited), 80 and 443. Docker's published ports bypass ufw, which is why the gateway publishes none.
- **Updates:** unattended security upgrades are on.
- **Vendor keys:** server-only Anthropic and OpenAI keys with their own vendor-side monthly limits, in a mode-600 file outside the checkout, passed in as environment variables.
- **Team keys:** `teams.yaml` holds hashes only. The gateway ceiling (US$5 a month) sits under the vendor limits, so the proxy's own caps are the ones that bind.

## Running it

On the host, from a clone of this repository at a tag, in `/srv/boundary/repo`:

```
cd /srv/boundary/repo && git fetch --tags && git checkout v0.26.0
cd deploy
sudo docker compose build gateway
sudo docker compose up -d
sudo docker compose exec gateway boundary --config /app/config/boundary.yaml \
  ledger report --ledger /data/boundary.proxy.sqlite
sudo docker compose exec gateway boundary --config /app/config/boundary.yaml \
  audit verify --ledger /data/boundary.proxy.sqlite
```

The host provides three files outside the checkout: `/home/ubuntu/boundary/.env` (the
vendor keys), `/srv/boundary/teams.yaml`, and the data directory, owned by uid 10001.

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
