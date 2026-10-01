# The website

https://gateway.peterparker.ca/ is this project's page: what the gateway is, what it
measured, and the limit of each number, written for a reader who does not build these
systems and, folded away under "For the technical reader", for one who does. It is in the
style of the other project pages on peterparker.ca (projects 01, 02, 08 and 12), whose
stylesheet and fonts it copies.

## How it is built

```
uv run boundary site --out site               # build it
uv run boundary site --out site --serve 8080  # and look at it, under the live headers
```

`boundary/site.py` fills `web/index.html`'s `{{tokens}}` and writes `data/site.json` beside it,
from committed files only:

| What the page shows | Where it comes from |
|---|---|
| Every figure | A cell of one of the README's marked tables, which the measurement commands write and nobody types. The builder reads each by its marker, row and column, and fails if one is missing or no longer reads as it did, so the page cannot disagree with the README |
| The load test chart | The README's load test rows, which `boundary loadtest --write-readme` wrote from `bench/loadtest.json` |
| The residency grid | `config/boundary.yaml` and `config/policy.yaml`, decided by `boundary.enforce.decide`, the function the proxy calls |
| The redaction example | A fictional request, redacted at build time by `boundary.redact.Policy`, the engine the proxy uses. The vendor's reply in it is written by the builder, since no model is called; every placeholder in it is the engine's, and so is the text the caller gets back |
| The audit chain drawing | `anchors/gateway.jsonl`, and at run time `GET /audit/head`, the live chain's head, the one request the page makes to the proxy |
| Tests, lines of code, releases | Counted from `tests/`, `boundary/` and the changelog |

## How it is served

Caddy serves it at the root of gateway.peterparker.ca, beside the proxy's own `/demo` and
`/dashboard` (deploy/Caddyfile). The site's paths are its files and nothing else, and they
carry a content security policy that allows nothing inline and nothing from another host;
every other path goes to the proxy as before. The one-shot `site` service in
`deploy/compose.yaml` builds the page from the checkout into `/srv/boundary/site/current`
on every start. A build that fails leaves the previous page up and the proxy unaffected.

Served from the VPS rather than from an Azure Static Web App like the other project pages,
by decision on 2026-10-01: the page and the gateway it describes are one host, the buttons
on it are the live gateway, and there is nothing else to set up. The cost is that the page
is down when the VPS is.

## The proxy's pages in the same frame (0.32)

`/demo` and `/dashboard` are rendered by the proxy, because they show live state: the demo
key's spend today, and the ledger. They use `boundary.server.chrome`, the website's header
and footer, and link the website's `/style.css`, so the three pages are one site. A proxy
started from a checkout without Caddy serves that stylesheet and the fonts from `web/`.

## Rules it keeps

`tests/test_site.py` checks each one:

- every token filled and every file written;
- no inline style or script, no event handler attribute, no request off this server, and
  no link out except to peterparker.ca and this repository;
- the figures are the README's, and the request counts equal `bench/loadtest.json`'s;
- a missing README table fails the build;
- the redaction example sends none of its planted values and gets every one back;
- plain punctuation in `web/`;
- the Caddyfile serves exactly the builder's files, with the builder's headers.
