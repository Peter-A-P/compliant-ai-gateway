"""The portfolio observability dashboard (0.27, PLAN.md B1 and B4's `dashboard/`).

One read-only page over the central ledger and the proxy's own: calls, errors, cost and
latency by project, by provider and model, and by day, and the completeness panel. Server
rendered, no script, no key: a ledger row holds no content, and the page shows only counts,
sums and percentiles of rows. PLAN.md's risk table fences the scope: anything beyond calls,
cost, latency and errors by project and model is out.

Percentiles here are descriptive, over the calls there were, not estimates of anything, so
they carry no interval. Every other figure is a count or a sum.
"""

from __future__ import annotations

import html
from collections.abc import Mapping, Sequence
from typing import Any

from boundary import __version__
from boundary.central import Group, SourceState, days_ago, group_by

_CSS = """
:root { --bg:#fbfbf9; --fg:#1d1f21; --muted:#5f6368; --line:#e2e2dc; --accent:#1f5f8b;
  --warn:#9a3412; --ok:#166534; --card:#ffffff; }
@media (prefers-color-scheme: dark) { :root { --bg:#141517; --fg:#e8e8e3; --muted:#a0a3a8;
  --line:#2b2d31; --accent:#7fb3d9; --warn:#f0a070; --ok:#7cc79a; --card:#1b1c1f; } }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--fg);
  font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
main { max-width: 1080px; margin: 0 auto; padding: 24px 16px 64px; }
h1 { font-size: 22px; margin: 0 0 4px; }
h2 { font-size: 16px; margin: 32px 0 8px; }
p.lede, p.note { color: var(--muted); margin: 4px 0 0; }
.cards { display:grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap:12px;
  margin-top: 20px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:8px; padding:12px; }
.card b { display:block; font-size: 20px; font-variant-numeric: tabular-nums; }
.card span { color: var(--muted); font-size: 13px; }
.scroll { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; font-variant-numeric: tabular-nums; }
th, td { text-align: right; padding: 6px 10px; border-bottom: 1px solid var(--line);
  white-space: nowrap; }
th:first-child, td:first-child, th.l, td.l { text-align: left; }
th { color: var(--muted); font-weight: 600; font-size: 13px; }
.warn { color: var(--warn); } .ok { color: var(--ok); }
footer { color: var(--muted); font-size: 13px; margin-top: 40px; }
a { color: var(--accent); }
"""


def _e(v: Any) -> str:
    return html.escape(str(v))


def _usd(v: float) -> str:
    return f"{v:,.4f}" if v < 1 else f"{v:,.2f}"


def _ms(v: float | None) -> str:
    return "-" if v is None else f"{v:,.0f}"


def _group_table(groups: Sequence[Group], heads: Sequence[str]) -> str:
    cols = "".join(f'<th class="l">{_e(h)}</th>' for h in heads)
    head = (
        f"<tr>{cols}<th>Calls</th><th>Errors</th><th>Refused</th><th>Cached</th>"
        "<th>Uncosted</th><th>US$</th><th>p50 ms</th><th>p95 ms</th></tr>"
    )
    body = []
    for g in groups:
        keys = "".join(f'<td class="l">{_e(k)}</td>' for k in g.key)
        unc = f'<span class="warn">{g.uncosted}</span>' if g.uncosted else "0"
        body.append(
            f"<tr>{keys}<td>{g.calls:,}</td><td>{g.errors:,}</td><td>{g.refused:,}</td>"
            f"<td>{g.cached:,}</td><td>{unc}</td><td>{_usd(g.cost_usd)}</td>"
            f"<td>{_ms(g.p50_ms)}</td><td>{_ms(g.p95_ms)}</td></tr>"
        )
    return f'<div class="scroll"><table>{head}{"".join(body)}</table></div>'


def _sources_table(sources: Sequence[SourceState], proxy_rows: int) -> str:
    head = (
        '<tr><th class="l">Source</th><th class="l">Environment</th><th>Rows it holds</th>'
        '<th>Rows held here</th><th class="l">State</th><th class="l">Last push (UTC)</th></tr>'
    )
    body = [
        f'<tr><td class="l">the proxy\'s own ledger</td><td class="l">vps</td>'
        f'<td>{proxy_rows:,}</td><td>{proxy_rows:,}</td><td class="l ok">read live</td>'
        '<td class="l">-</td></tr>'
    ]
    for s in sources:
        state = (
            '<span class="ok">complete</span>'
            if s.complete
            else f'<span class="warn">{s.local_rows - s.held:,} missing</span>'
        )
        body.append(
            f'<tr><td class="l">{_e(s.source)}</td><td class="l">{_e(s.env or "-")}</td>'
            f'<td>{s.local_rows:,}</td><td>{s.held:,}</td><td class="l">{state}</td>'
            f'<td class="l">{_e(s.last_utc[:19].replace("T", " "))}</td></tr>'
        )
    return f'<div class="scroll"><table>{head}{"".join(body)}</table></div>'


def render(
    rows: Sequence[Mapping[str, Any]],
    sources: Sequence[SourceState],
    *,
    proxy_rows: int,
    generated_utc: str,
    demo: bool = False,
) -> str:
    total = group_by(rows)
    t = total[0] if total else Group(())
    first = min((str(r.get("ts_utc") or "") for r in rows), default="")[:10]
    held = sum(s.held for s in sources) + proxy_rows
    claimed = sum(s.local_rows for s in sources) + proxy_rows
    cards = [
        (f"{t.calls:,}", "calls on the record"),
        (f"US${_usd(t.cost_usd)}", "spent, from returned usage"),
        (f"{t.uncosted:,}", "uncosted calls"),
        (f"{t.errors:,}", "errors"),
        (f"{t.refused:,}", "refused by policy"),
        (f"{held:,} of {claimed:,}", "rows held of rows reported"),
    ]
    card_html = "".join(
        f'<div class="card"><b>{_e(a)}</b><span>{_e(b)}</span></div>' for a, b in cards
    )
    recent = days_ago(30)
    by_day = [g for g in group_by(rows, "day") if g.key[0] >= recent]
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Portfolio Calls Dashboard</title>
<style>{_CSS}</style></head>
<body><main>
<h1>Every model call in the portfolio, on the record</h1>
<p class="lede">Read from the ledger rows each project's environment pushed to this gateway,
and the gateway's own. A row holds hashes, counts and identifiers, never a prompt or an
answer. Since {_e(first or "-")}.</p>
<div class="cards">{card_html}</div>

<h2>By project</h2>
{_group_table(group_by(rows, "project"), ["Project"])}

<h2>By provider and model</h2>
{_group_table(group_by(rows, "provider", "model_requested"), ["Provider", "Model"])}

<h2>By day, last 30 days</h2>
{_group_table(by_day, ["Day (UTC)"])}

<h2>Completeness</h2>
<p class="note">Each source says how many rows its own ledger holds when it pushes, and
sends them all. A source short of its own count shows how many are missing.</p>
{_sources_table(sources, proxy_rows)}

<footer>boundary {_e(__version__)}, generated {_e(generated_utc[:19].replace("T", " "))} UTC.
Cost is computed from the usage each vendor returned and a dated price file; an uncosted row
is one whose price was unknown, never estimated. Latency is over answered, uncached calls.
<a href="https://github.com/Peter-A-P/compliant-ai-gateway">Source and method</a>.{
        ' <a href="/demo">Try it with the demo key</a>.' if demo else ""
    }</footer>
</main></body></html>
"""


__all__ = ["render"]
