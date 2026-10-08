"""The hosted demo's page (0.30): a key anybody may use, its limits, and how to call it.

The key is published, so what it can spend is the whole of its protection. The proxy only
serves this page for a team with a daily budget, a model list and an answer length, and
only when the key it is given is one of that team's (`check`): a key that does not work, or
another team's key, is never printed. The vendor console limits on the server's own keys
stay behind it as the last line.
"""

from __future__ import annotations

import html
from collections.abc import Mapping
from typing import Any

from boundary.errors import ConfigError
from boundary.server.chrome import page
from boundary.server.teams import Team, TeamsConfig, hash_key


def check(teams: TeamsConfig, team: str, key: str) -> Team:
    """The team whose key the page may publish, or ConfigError. Every limit a public key
    needs must be set, so that the page cannot go up in front of an unbounded team."""
    t = teams.teams.get(team)
    if t is None:
        raise ConfigError(f"the demo team {team!r} is not in the teams file")
    if hash_key(key) not in t.key_sha256:
        raise ConfigError(f"the demo key is not one of team {team!r}'s keys")
    missing = [
        name
        for name, value in (
            ("daily_usd", t.daily_usd),
            ("models", t.models),
            ("max_tokens", t.max_tokens),
        )
        if value is None
    ]
    if missing:
        raise ConfigError(
            f"team {team!r} has no {', '.join(missing)}; a key anybody may use needs every one"
        )
    return t


def _e(v: Any) -> str:
    return html.escape(str(v))


def render(
    *,
    base_url: str,
    key: str,
    team: str,
    t: Team,
    spent_today: float,
    targets: Mapping[str, str] | None = None,
) -> str:
    """The page. `targets` names what each of the team's aliases resolves to (1.2.0), so a
    visitor sees the model behind `demo` and not only the alias."""
    assert t.daily_usd is not None and t.models is not None and t.max_tokens is not None
    model = t.models[0]
    left = max(0.0, t.daily_usd - spent_today)
    url = base_url.rstrip("/")
    curl = f"""curl {url}/v1/chat/completions \\
  -H "Authorization: Bearer {key}" \\
  -H "Content-Type: application/json" \\
  -H "X-Data-Class: public" \\
  -i -d '{{"model": "{model}",
       "messages": [{{"role": "user", "content": "In one sentence, what is a hash chain?"}}]}}'"""
    refused = f"""curl {url}/v1/chat/completions \\
  -H "Authorization: Bearer {key}" \\
  -H "Content-Type: application/json" \\
  -d '{{"model": "{model}",
       "messages": [{{"role": "user", "content": "Summarise: Jane Roe, SIN 046 454 286"}}]}}'"""
    python = f"""from openai import OpenAI

client = OpenAI(base_url="{url}/v1", api_key="{key}")
r = client.chat.completions.with_raw_response.create(
    model="{model}",
    messages=[{{"role": "user", "content": "What is data residency?"}}],
    extra_headers={{"X-Data-Class": "public", "X-Boundary-Cache": "question"}},
)
print(r.headers["x-boundary-cache"], r.headers["x-boundary-call-uid"])
print(r.parse().choices[0].message.content)"""
    targets = targets or {}
    models = ", ".join(f"{m} ({targets[m]})" if m in targets else m for m in t.models)
    body = f"""
  <header class="hero">
    <p class="eyebrow">The live gateway, with a public key</p>
    <h1>Call a model through the gateway</h1>
    <p class="lead">
      The gateway speaks the same protocol as OpenAI's API, so any client works by changing
      its base address and key. Every call below is checked against the data policy, written
      to the ledger before it returns, sealed into the audit chain, and counted on the
      <a href="/dashboard">dashboard</a>.
    </p>
    <div class="headline">
      <div class="stat up"><span class="figure">US${left:.2f}</span><span class="caption">left today of US${t.daily_usd:g}, resetting at midnight UTC</span></div>
      <div class="stat"><span class="figure">{t.requests_per_minute}</span><span class="caption">requests a minute</span></div>
      <div class="stat"><span class="figure">{t.max_tokens}</span><span class="caption">tokens an answer, at most</span></div>
      <div class="stat"><span class="figure">{len(t.models)}</span><span class="caption">model{"s" if len(t.models) != 1 else ""}: {_e(models)}</span></div>
    </div>
  </header>

  <section class="section" id="key">
    <h2>The key</h2>
    <pre class="code">{_e(key)}</pre>
    <p class="note">Public on purpose, so do not send anything private with it. A prompt goes to
    the model's vendor; the gateway keeps hashes, counts and identifiers of it, never the text.
    When today's budget is spent, calls are refused with a 429 that says when it resets.</p>
  </section>

  <section class="section" id="call">
    <h2>A call</h2>
    <pre class="code">{_e(curl)}</pre>
    <p class="note">The response headers say what the gateway did: <code>x-boundary-data-class</code>,
    <code>x-boundary-redacted</code> and <code>x-boundary-call-uid</code>, the row's id in the
    ledger.</p>
  </section>

  <section class="section" id="refusal">
    <h2>A refusal</h2>
    <pre class="code">{_e(refused)}</pre>
    <p class="note">No <code>X-Data-Class</code> header means <code>personal</code>, and the
    policy lets personal data go only where processing in Canada can be declared. No hosted
    vendor here can declare it, so the call is refused with a 403 before anything is sent, and
    the refusal is on the ledger too.</p>
  </section>

  <section class="section" id="cache">
    <h2>From Python, with the cache</h2>
    <pre class="code">{_e(python)}</pre>
    <p class="note"><code>X-Boundary-Cache: question</code> marks a bare question the semantic
    cache may answer. Ask the same thing twice and the second answer says <code>hit</code>,
    costs nothing, and names the call it came from.</p>
  </section>
"""
    return page(
        title="Gateway Demo Key",
        description="A public key for the gateway at gateway.peterparker.ca, its limits, and a call, a refusal and a cached repeat to copy.",
        body=body,
        note=f"Team <code>{_e(team)}</code>, US${t.monthly_usd:g} a month",
    )


__all__ = ["check", "render"]
