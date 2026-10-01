"""The frame the proxy's own pages share with the project's website (0.32): its header,
its footer and its stylesheet, so `/demo` and `/dashboard` look like the page at `/`.

The stylesheet and the fonts are the website's (`web/`), linked from this host, where Caddy
serves them (docs/site.md). Nothing is inline, so the pages keep working under the site's
content security policy too. A proxy started from a checkout without Caddy in front serves
`web/` itself (`WEB`), so a local `boundary serve` looks the same.
"""

from __future__ import annotations

import html
from pathlib import Path

from boundary import __version__

REPOSITORY = "https://github.com/Peter-A-P/compliant-ai-gateway"
# The website's source in a checkout. Absent from the container image, where Caddy serves it.
WEB = Path(__file__).resolve().parents[2] / "web"


def e(v: object) -> str:
    return html.escape(str(v))


def page(*, title: str, description: str, body: str, note: str = "") -> str:
    """A whole page: the site's header, `body` inside `main`, and the site's footer, with
    `note` as the footer's first line."""
    first = f"<li>{note}</li>" if note else ""
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(title)}</title>
<meta name="description" content="{e(description)}">
<link rel="stylesheet" href="/style.css">
</head>
<body>
<header class="top">
  <div class="top-inner">
    <a class="brand" href="https://peterparker.ca"><span class="mono-mark" aria-hidden="true">P</span>Peter Parker</a>
    <p class="top-nav"><a href="/">Project 04: Inside the Boundary, On the Record</a></p>
  </div>
</header>
<main>
{body}
</main>
<footer class="foot">
  <div class="foot-inner">
    <p>
      <a href="/">What this gateway is and what it measured</a>, the
      <a href="/demo">demo key</a> and the <a href="/dashboard">dashboard</a>. Source, methods and
      every measurement: <a href="{REPOSITORY}">github.com/Peter-A-P/compliant-ai-gateway</a>.
    </p>
    <ul>
      {first}
      <li>Type: Newsreader and Inter, SIL Open Font License, served from this host</li>
      <li>boundary {e(__version__)}</li>
    </ul>
  </div>
</footer>
</body>
</html>
"""


__all__ = ["REPOSITORY", "WEB", "e", "page"]
