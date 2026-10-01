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

from boundary.central import Group, SourceState, days_ago, group_by
from boundary.server.chrome import page


def _e(v: Any) -> str:
    return html.escape(str(v))


def _usd(v: float) -> str:
    return f"{v:,.4f}" if v < 1 else f"{v:,.2f}"


def _ms(v: float | None) -> str:
    return "-" if v is None else f"{v:,.0f}"


def _group_table(groups: Sequence[Group], heads: Sequence[str]) -> str:
    cols = "".join(f'<th scope="col">{_e(h)}</th>' for h in heads)
    head = (
        f'<thead><tr>{cols}<th class="num">Calls</th><th class="num">Errors</th>'
        '<th class="num">Refused</th><th class="num">Cached</th><th class="num">Uncosted</th>'
        '<th class="num">US$</th><th class="num">p50 ms</th><th class="num">p95 ms</th>'
        "</tr></thead>"
    )
    body = []
    for g in groups:
        keys = "".join(f"<td>{_e(k)}</td>" for k in g.key)
        unc = f'<span class="worse">{g.uncosted}</span>' if g.uncosted else "0"
        body.append(
            f'<tr>{keys}<td class="num">{g.calls:,}</td><td class="num">{g.errors:,}</td>'
            f'<td class="num">{g.refused:,}</td><td class="num">{g.cached:,}</td>'
            f'<td class="num">{unc}</td><td class="num">{_usd(g.cost_usd)}</td>'
            f'<td class="num">{_ms(g.p50_ms)}</td><td class="num">{_ms(g.p95_ms)}</td></tr>'
        )
    return f'<div class="table-wrap"><table>{head}<tbody>{"".join(body)}</tbody></table></div>'


def _sources_table(sources: Sequence[SourceState], proxy_rows: int) -> str:
    head = (
        '<thead><tr><th scope="col">Source</th><th scope="col">Environment</th>'
        '<th class="num">Rows it holds</th><th class="num">Rows held here</th>'
        '<th scope="col">State</th><th scope="col">Last push (UTC)</th></tr></thead>'
    )
    body = [
        "<tr><td>the proxy's own ledger</td><td>vps</td>"
        f'<td class="num">{proxy_rows:,}</td><td class="num">{proxy_rows:,}</td>'
        '<td><span class="better">read live</span></td><td>-</td></tr>'
    ]
    for s in sources:
        state = (
            '<span class="better">complete</span>'
            if s.complete
            else f'<span class="worse">{s.local_rows - s.held:,} missing</span>'
        )
        body.append(
            f"<tr><td>{_e(s.source)}</td><td>{_e(s.env or '-')}</td>"
            f'<td class="num">{s.local_rows:,}</td><td class="num">{s.held:,}</td><td>{state}</td>'
            f"<td>{_e(s.last_utc[:19].replace('T', ' '))}</td></tr>"
        )
    return f'<div class="table-wrap"><table>{head}<tbody>{"".join(body)}</tbody></table></div>'


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
        f'<div class="stat"><span class="figure">{_e(a)}</span><span class="caption">{_e(b)}</span></div>'
        for a, b in cards
    )
    recent = days_ago(30)
    by_day = [g for g in group_by(rows, "day") if g.key[0] >= recent]
    body = f"""
  <header class="hero">
    <p class="eyebrow">The portfolio's model calls, read from the record</p>
    <h1>Every model call in the portfolio, on the record</h1>
    <p class="lead">
      Every project in this portfolio that calls an AI model does it through this gateway's
      library, and pushes its ledger here. A row holds hashes, counts and identifiers, never a
      prompt or an answer, so this page can be public. Since {_e(first or "-")}.
    </p>
    <div class="headline six">{card_html}</div>
    <p class="hero-note">
      Cost is computed from the usage each vendor returned and a dated price file. An
      uncosted row is one whose price was unknown, never estimated. Latency is over answered,
      uncached calls; its percentiles describe the calls there were, so they carry no interval.
    </p>
  </header>

  <section class="section" id="projects">
    <h2>By project</h2>
    {_group_table(group_by(rows, "project"), ["Project"])}
  </section>

  <section class="section" id="models">
    <h2>By provider and model</h2>
    {_group_table(group_by(rows, "provider", "model_requested"), ["Provider", "Model"])}
  </section>

  <section class="section" id="days">
    <h2>By day, the last 30 days</h2>
    {_group_table(by_day, ["Day (UTC)"])}
  </section>

  <section class="section" id="completeness">
    <h2>Is the record complete?</h2>
    <p class="section-lead">
      A dashboard is usually shown to be full; this one is shown to be complete. Each source
      says how many rows its own ledger holds when it pushes, and sends them all, so a source
      short of its own count shows here how many are missing.
    </p>
    {_sources_table(sources, proxy_rows)}
  </section>
"""
    note = f"Generated {_e(generated_utc[:19].replace('T', ' '))} UTC" + (
        '. <a href="/demo">Try the gateway with the demo key</a>' if demo else ""
    )
    return page(
        title="Portfolio Calls Dashboard",
        description="Every AI model call in Peter Parker's portfolio: calls, cost, errors and latency by project, model and day, and whether the record is complete.",
        body=body,
        note=note,
    )


__all__ = ["render"]
