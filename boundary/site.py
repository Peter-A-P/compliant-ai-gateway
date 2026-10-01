"""The project's website (0.31): what the gateway is and what it measured, in the style of
the other project pages on peterparker.ca.

Built by `boundary site --out DIR` from committed files only, and served by Caddy at the root
of gateway.peterparker.ca, beside the proxy's own `/demo` and `/dashboard` (docs/site.md):

- `web/`, the page itself: `index.html` with `{{tokens}}`, `style.css`, `app.js` and the
  fonts, shared with projects 01, 02, 08 and 12;
- the README's tables, which the measurement commands fill and nobody types. Every figure on
  the page is a cell of one of them, read here by its marker, so the page and the README
  cannot disagree. A cell that is missing or no longer parses fails the build;
- `bench/loadtest.json`'s README rows for the chart, `anchors/gateway.jsonl` for the daily
  anchors, and `config/policy.yaml` with `config/boundary.yaml` for the residency grid,
  decided by the same `boundary.enforce.decide` the proxy calls;
- one fictional request, redacted at build time by the same `boundary.redact.Policy` the
  proxy uses, so the example of what a vendor receives is the engine's output, not a
  drawing of it.

Nothing inline: Caddy sends a content security policy that allows scripts, styles and
fonts from this site only, and `boundary site --serve` sends the same one locally. The page
asks one thing of the proxy at run time, `GET /audit/head`, the chain's live head.
"""

from __future__ import annotations

import datetime as dt
import html
import http.server
import json
import re
import shutil
import socketserver
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any

from boundary import __version__

REPOSITORY = "https://github.com/Peter-A-P/compliant-ai-gateway"
PROJECT_PAGE = "https://peterparker.ca/projects/compliant-ai-gateway/"
DATA = "data/site.json"

# What Caddy sends with every file of the site (deploy/Caddyfile), and `--serve` locally.
HEADERS: dict[str, str] = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "font-src 'self'; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; "
        "base-uri 'none'; form-action 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "geolocation=(), camera=(), microphone=(), interest-cohort=()",
}

# The files the site is made of, which Caddy serves from the built folder (deploy/Caddyfile
# names the same paths). Everything else on the host is the proxy's.
ASSETS = ("style.css", "app.js")

# The fictional request the redaction example runs on. Nobody here exists: a name from the
# usual placeholder pair, the sample social insurance number the federal government
# publishes for testing, a reserved 555 telephone number and an example.com address.
EXAMPLE_REQUEST = (
    "Draft a short reply to Jane Roe about her access request, file ATIPP-2024-1749. "
    "Her SIN is 046 454 286 and she asked us to call 709-555-0142 or write to "
    "jane.roe@example.com. Confirm we received it and that we will answer within 30 days."
)


class SiteError(Exception):
    """A figure the page needs is missing from the README, or no longer reads as it did."""


# -- the README's tables -----------------------------------------------------------------------


def readme_tables(readme: str) -> dict[str, list[list[str]]]:
    """Every marked table in the README: `<!-- name:start -->` to `<!-- name:end -->`, as
    rows of stripped cells."""
    out: dict[str, list[list[str]]] = {}
    for m in re.finditer(r"<!-- ([a-z-]+):start -->\n(.*?)<!-- \1:end -->", readme, re.S):
        rows = []
        for line in m.group(2).splitlines():
            if line.startswith("|"):
                rows.append([c.strip() for c in line.strip().strip("|").split("|")])
        out[m.group(1)] = rows
    return out


def cell(tables: Mapping[str, Sequence[Sequence[str]]], marker: str, row: int, col: int) -> str:
    try:
        return tables[marker][row][col]
    except (KeyError, IndexError):
        raise SiteError(f"the README has no cell {marker}[{row}][{col}]") from None


def find(text: str, pattern: str, what: str) -> re.Match[str]:
    m = re.search(pattern, text)
    if m is None:
        raise SiteError(f"{what}: {text!r} does not match {pattern!r}")
    return m


def thousands(n: str | int) -> str:
    return f"{int(str(n).replace(',', '')):,}"


_INTERVAL = r"(-?[\d.]+%?) \((-?[\d.+]+%?) to (-?[\d.+]+%?)\)"


def _load_rows(tables: Mapping[str, Sequence[Sequence[str]]]) -> list[dict[str, Any]]:
    """The load test's README rows as numbers, for the chart: each layer and rate, the three
    percentiles with their intervals, or what stopped the cell from having them."""
    out = []
    for row in tables.get("loadtest", []):
        layer, rps, figures, errors = row
        entry: dict[str, Any] = {"layer": layer, "rps": int(rps), "errors": errors}
        parts = figures.split(" / ")
        if len(parts) == 3 and all(re.fullmatch(_INTERVAL, p) for p in parts):
            for name, p in zip(("p50", "p95", "p99"), parts, strict=True):
                m = find(p, _INTERVAL, f"loadtest {layer} {rps}")
                entry[name] = [float(m.group(1)), float(m.group(2)), float(m.group(3))]
        else:
            entry["note"] = figures
        out.append(entry)
    if not out:
        raise SiteError("the README has no load test rows")
    return out


# -- the figures ---------------------------------------------------------------------------


def figures(tables: Mapping[str, Sequence[Sequence[str]]]) -> dict[str, str]:
    """Every number the page prints, as text, each from one README cell."""
    f: dict[str, str] = {}

    wire = cell(tables, "redact-proxy", 0, 0)
    m = find(wire, r"(\d+) of (\d+) plain, " + _INTERVAL, "values on the wire")
    f["wire_leaked"], f["wire_values"] = m.group(1), thousands(m.group(2))
    f["wire_ci"] = f"{m.group(4)} to {m.group(5)}"
    f["wire_round_trip"] = cell(tables, "redact-proxy", 0, 1)

    tamper = cell(tables, "audit", 0, 0)
    m = find(tamper, r"([\d,]+)/([\d,]+) = " + _INTERVAL + r", (\d+) kinds", "tamper test")
    f["tamper_found"], f["tamper_tried"], f["tamper_kinds"] = m.group(1), m.group(2), m.group(6)
    f["tamper_ci"] = f"{m.group(4)} to {m.group(5)}"
    f["false_alarms"] = cell(tables, "audit", 0, 1)
    f["after_anchor"] = cell(tables, "audit", 0, 2)
    f["verify_time"] = cell(tables, "audit", 0, 3)

    forbidden = cell(tables, "policy", 0, 0)
    m = find(forbidden, r"(\d+) of (\d+) forbidden cases sent, " + _INTERVAL, "policy")
    f["policy_sent"], f["policy_forbidden"] = m.group(1), m.group(2)
    f["policy_ci"] = f"{m.group(4)} to {m.group(5)}"
    f["policy_false"] = cell(tables, "policy", 0, 1)
    f["policy_audited"] = cell(tables, "policy", 0, 2)
    f["policy_proxy"] = cell(tables, "policy-proxy", 0, 0)

    f["lib_overhead"] = cell(tables, "bench", 0, 0)
    f["completeness"] = cell(tables, "bench", 0, 1)
    f["caps"] = cell(tables, "bench", 0, 3)
    f["fidelity"] = cell(tables, "bench", 0, 4)

    load = _load_rows(tables)
    total = sum(int(find(r["errors"], r"of (\d+)", "requests").group(1)) for r in load)
    failed = sum(int(find(r["errors"], r"^(\d+) of", "errors").group(1)) for r in load)
    f["load_requests"], f["load_failed"] = thousands(total), thousands(failed)
    audit500 = next(r for r in load if r["layer"] == "audit" and r["rps"] == 500)
    f["audit500_p99"] = f"{audit500['p99'][0]:.1f}"
    f["audit500_ci"] = f"{audit500['p99'][1]:.1f} to {audit500['p99'][2]:.1f}"
    red200 = next(r for r in load if r["layer"] == "redaction" and r["rps"] == 200)
    f["red200_p99"] = f"{red200['p99'][0]:.1f}"
    f["red200_ci"] = f"{red200['p99'][1]:.1f} to {red200['p99'][2]:.1f}"
    red500 = next(r for r in load if r["layer"] == "redaction" and r["rps"] == 500)
    f["red500_p50"] = f"{red500['p50'][0]:.0f}"

    f["recall_rules"] = find(cell(tables, "redact", 0, 0), r"^([\d.]+%)", "recall").group(1)
    f["leak_rate"] = cell(tables, "redact", 0, 3)
    f["over_redaction"] = find(cell(tables, "redact", 0, 4), r"^([\d.]+%)", "over").group(1)
    f["tab_direct"] = find(cell(tables, "tab", 1, 1), r"^([\d.]+%)", "tab direct").group(1)
    f["tab_precision"] = find(cell(tables, "tab", 0, 3), r"^([\d.]+%)", "tab precision").group(1)
    f["tab_safe"] = find(cell(tables, "tab", 0, 4), r"^([\d.]+%)", "tab safe").group(1)
    f["tab_safe_allow"] = find(cell(tables, "tab", 1, 4), r"^([\d.]+%)", "tab allow").group(1)

    f["quality_pairs"] = cell(tables, "quality", 0, 1)
    f["quality_effect"] = cell(tables, "quality", 0, 3)
    f["distractor_effect"] = cell(tables, "quality-distractor", 0, 3)
    haiku = cell(tables, "quality-redteam", 0, 3)
    f["redteam_haiku_full"] = find(haiku, r"^(\d+) of (\d+)", "red team").group(0)
    f["redteam_haiku_scoped"] = find(
        cell(tables, "quality-redteam", 0, 4), r"^(\d+) of (\d+)", "scoped"
    ).group(0)
    f["redteam_vendor"] = find(
        cell(tables, "quality-redteam", 1, 2), r"^(\d+) of (\d+)", "vendor"
    ).group(0)
    f["redteam_llama_raw"] = find(
        cell(tables, "quality-redteam", 1, 1), r"^(\d+) of (\d+)", "llama"
    ).group(0)

    f["screen_heldout"] = find(cell(tables, "screen", 1, 1), r"([\d.]+%)", "screen").group(1)
    f["screen_train"] = find(cell(tables, "screen", 0, 1), r"([\d.]+%)", "screen").group(1)
    f["screen_templated"] = cell(tables, "screen", 2, 1)
    f["screen_overrefusal"] = cell(tables, "screen", 5, 2)

    f["cache_hits"] = cell(tables, "cache", 0, 3)
    f["cache_false"] = cell(tables, "cache", 0, 4)
    f["cache_redacted_false"] = find(
        cell(tables, "cache", 2, 4), r"([\d.]+%)", "cache redacted"
    ).group(1)
    f["cache_retrieval_false"] = find(
        cell(tables, "cache", 3, 4), r"([\d.]+%)", "cache retrieval"
    ).group(1)
    # Short forms for the big figures, the rest of the cell going into the caption.
    f["round_trip_pct"] = find(f["wire_round_trip"], r"^([\d.]+%)", "round trip").group(1)
    f["round_trip_ci"] = find(f["wire_round_trip"], r"\(([^)]*)\)", "round trip").group(1)
    f["false_alarms_n"] = find(f["false_alarms"], r"^([\d,]+)/([\d,]+)", "alarms").expand(
        r"\1 of \2"
    )
    f["false_alarms_ci"] = find(f["false_alarms"], r"\(([^()]*)\)\)?$", "alarms").group(1)
    f["verify_ms"] = find(f["verify_time"], r"^([\d.]+ ms)", "verify").group(1)
    f["verify_records"] = find(f["verify_time"], r"for ([\d,]+) records", "verify").group(1)
    f["policy_refused_n"] = find(f["policy_false"], r"^(\d+ of \d+)", "false").group(1)
    f["policy_audited_n"] = find(f["policy_audited"], r"^(\d+ of \d+)", "audited").group(1)
    m = find(f["caps"], r"^(\d+) attempted, (\d+) reached", "caps")
    f["caps_reached"], f["caps_attempted"] = m.group(2), m.group(1)
    f["fidelity_n"] = find(f["fidelity"], r"^(\d+/\d+)", "fidelity").group(1).replace("/", " of ")
    f["lib_p50"] = find(f["lib_overhead"], r"^([\d.]+ ms)", "overhead").group(1)
    f["lib_p50_ci"] = find(f["lib_overhead"], r"^[\d.]+ ms \(([^)]*)\)", "overhead").group(1)
    m = find(f["completeness"], r"^(\d+)/(\d+)", "completeness")
    f["complete_n"] = f"{m.group(1)} of {m.group(2)}"
    f["complete_across"] = find(f["completeness"], r"(across .*)$", "completeness").group(1)
    for key in ("cache_hits", "cache_false"):
        m = find(f[key], r"^(\d+ of \d+), (.*)$", key)
        f[f"{key}_n"], f[f"{key}_rest"] = m.group(1), m.group(2)
    return f


# -- what else the page shows ------------------------------------------------------------------


def residency_grid(root: Path) -> dict[str, Any]:
    """Every provider entry against every class, decided by `boundary.enforce.decide`, the
    function the proxy calls. `personal, redacted` is a personal request after the proxy
    has redacted it, judged as the policy's `redacted_as` says."""
    from boundary.config import load_config
    from boundary.enforce import decide, load_policy

    cfg = load_config(root / "config" / "boundary.yaml")
    policy = load_policy(root / "config" / "policy.yaml")
    columns = [
        ("public", "public", False),
        ("internal", "internal", False),
        ("personal", "personal", False),
        ("personal, redacted", "personal", True),
        ("sensitive", "sensitive", False),
    ]
    rows = []
    for name, pc in cfg.providers.items():
        cells = []
        for _label, cls, redacted in columns:
            d = decide(policy, cls, provider=name, provider_config=pc, region=pc.region,
                       redacted=redacted)  # fmt: skip
            cells.append({"allowed": d.allowed, "reason": d.reason})
        rows.append(
            {
                "provider": name,
                "region": pc.region or "not declared",
                "residency": pc.residency.value if pc.residency else "not declared",
                "cells": cells,
            }
        )
    return {"columns": [c[0] for c in columns], "rows": rows}


def anchors(root: Path) -> list[dict[str, Any]]:
    path = root / "anchors" / "gateway.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


@dataclass(frozen=True)
class Example:
    sent: str
    vendor: str
    answer: str
    returned: str
    placeholders: int
    # Words masked that are none of the planted values: the second pass's over-masking.
    overmasked: list[str]


def redaction_example() -> Example:
    """The fictional request through the engine the proxy uses, and a reply as a model
    writes one: in placeholders, put back on the way out. The reply is written here, since
    no model is called at build time; what is computed is every placeholder in it and every
    value restored."""
    from boundary.redact import PLACEHOLDER, Analyzer, Policy

    spans = Analyzer().analyze([EXAMPLE_REQUEST])
    policy = Policy(spans)
    vendor = policy.outbound(EXAMPLE_REQUEST)
    minted = [m.group(0) for m in PLACEHOLDER.finditer(vendor)]

    def first(*kinds: str) -> str:
        for p in minted:
            if any(p.startswith(f"<{k}") for k in kinds):
                return p
        raise SiteError(f"the example minted no {kinds} placeholder: {vendor!r}")

    person = first("PERSON", "NAME_LIKE")
    file_no = first("FILE_NUMBER", "ID_LIKE")
    answer = (
        f"Dear {person}, thank you for your access request, file {file_no}. We have "
        "received it and will answer within 30 days."
    )
    returned = policy.rehydrate(answer)
    if "Jane" not in returned or "1749" not in returned:
        raise SiteError(f"the example's reply did not come back whole: {returned!r}")
    planted = ("Jane Roe", "046 454 286", "709-555-0142", "jane.roe@example.com", "ATIPP-2024-1749")
    for value in planted:
        if any(part in vendor for part in re.split(r"[ @.-]", value) if len(part) > 2):
            raise SiteError(f"the example sent part of {value!r} to the vendor")
    over = sorted({policy.rehydrate(p) for p in set(minted)} - set(planted))
    return Example(EXAMPLE_REQUEST, vendor, answer, returned, len(set(minted)), over)


def engineering(root: Path) -> dict[str, str]:
    tests = sum(
        len(re.findall(r"^(?:async )?def test_", p.read_text(encoding="utf-8"), re.M))
        for p in (root / "tests").glob("test_*.py")
    )
    lines = sum(
        len(p.read_text(encoding="utf-8").splitlines()) for p in (root / "boundary").rglob("*.py")
    )
    releases = len(
        re.findall(r"^## \d+\.\d+\.\d+", (root / "CHANGELOG.md").read_text("utf-8"), re.M)
    )
    return {"tests": thousands(tests), "code_lines": thousands(lines), "releases": str(releases)}


# -- the build -------------------------------------------------------------------------------


def _mark(text: str) -> str:
    """Escaped text with every placeholder wrapped, so the page can show them."""
    from boundary.redact import PLACEHOLDER

    out, last = [], 0
    for m in PLACEHOLDER.finditer(text):
        out.append(html.escape(text[last : m.start()]))
        out.append(f'<span class="ph">{html.escape(m.group(0))}</span>')
        last = m.end()
    out.append(html.escape(text[last:]))
    return "".join(out)


def _grid_html(grid: Mapping[str, Any]) -> str:
    head = "".join(f'<th scope="col">{html.escape(c)}</th>' for c in grid["columns"])
    body = []
    for r in grid["rows"]:
        cells = "".join(
            f'<td class="{"go" if c["allowed"] else "stop"}" title="{html.escape(c["reason"])}">'
            f"{'may go' if c['allowed'] else 'refused'}</td>"
            for c in r["cells"]
        )
        body.append(
            f'<tr><th scope="row">{html.escape(r["provider"])}'
            f'<span class="where">{html.escape(r["region"])}, {html.escape(r["residency"])}'
            f"</span></th>{cells}</tr>"
        )
    return (
        '<div class="table-wrap"><table class="grid"><thead><tr><th scope="col">Provider entry'
        f"</th>{head}</tr></thead><tbody>{''.join(body)}</tbody></table></div>"
    )


@dataclass(frozen=True)
class Built:
    out: Path
    tokens: int
    files: list[str]


def build(root: Path, out: Path, *, today: dt.date | None = None) -> Built:
    """`web/` filled from the repository's results into `out`, and the JSON beside it."""
    web = root / "web"
    tables = readme_tables((root / "README.md").read_text(encoding="utf-8"))
    values: dict[str, str] = {k: html.escape(v) for k, v in figures(tables).items()}
    values.update({k: html.escape(v) for k, v in engineering(root).items()})
    example = redaction_example()
    grid = residency_grid(root)
    published = anchors(root)
    values.update(
        {
            "repository": REPOSITORY,
            "project_page": PROJECT_PAGE,
            "version": html.escape(__version__),
            "built": (today or dt.datetime.now(dt.UTC).date()).isoformat(),
            "example_sent": html.escape(example.sent),
            "example_vendor": _mark(example.vendor),
            "example_answer": _mark(example.answer),
            "example_returned": html.escape(example.returned),
            "example_placeholders": str(example.placeholders),
            "example_overmasked": html.escape(
                (
                    "It also masked "
                    + " and ".join(f'"{w}"' for w in example.overmasked)
                    + ", which identifies nobody. The second pass masks any capitalised word "
                    "it cannot vouch for, and that over-masking is measured below."
                )
                if example.overmasked
                else ""
            ),
            "residency_grid": _grid_html(grid),
            "providers": str(len(grid["rows"])),
            "anchors": str(len(published)),
            "first_anchor": published[0]["ts_utc"][:10] if published else "none yet",
        }
    )
    page = (web / "index.html").read_text(encoding="utf-8")
    used = set(re.findall(r"\{\{([a-z0-9_]+)\}\}", page))
    missing = sorted(used - values.keys())
    if missing:
        raise SiteError(f"web/index.html asks for tokens nothing fills: {missing}")
    page = re.sub(r"\{\{([a-z0-9_]+)\}\}", lambda m: values[m.group(1)], page)

    if out.exists():
        shutil.rmtree(out)
    (out / "data").mkdir(parents=True)
    (out / "index.html").write_text(page, encoding="utf-8")
    for name in ASSETS:
        shutil.copyfile(web / name, out / name)
    shutil.copytree(web / "fonts", out / "fonts")
    payload = {
        "boundary_version": __version__,
        "load": _load_rows(tables),
        "anchors": published,
        "residency": grid,
    }
    (out / DATA).write_text(json.dumps(payload, indent=1) + "\n", encoding="utf-8")
    files = sorted(str(p.relative_to(out)) for p in out.rglob("*") if p.is_file())
    return Built(out, len(used), files)


def serve(directory: Path, port: int) -> None:
    """The built site on localhost with the headers Caddy sends, so a page that breaks
    under the content security policy breaks here too. Only the site's files: `/audit/head`
    answers 404 locally and the page says the live head is unavailable."""

    class Handler(http.server.SimpleHTTPRequestHandler):
        def end_headers(self) -> None:
            for k, v in HEADERS.items():
                self.send_header(k, v)
            super().end_headers()

    handler = partial(Handler, directory=str(directory))
    with socketserver.TCPServer(("127.0.0.1", port), handler) as httpd:
        print(f"serving {directory} at http://127.0.0.1:{port}/ (Ctrl-C to stop)")
        httpd.serve_forever()


__all__ = [
    "ASSETS",
    "DATA",
    "EXAMPLE_REQUEST",
    "HEADERS",
    "Built",
    "SiteError",
    "build",
    "figures",
    "readme_tables",
    "redaction_example",
    "residency_grid",
    "serve",
]
