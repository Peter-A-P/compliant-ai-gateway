"""The project's website (0.31, docs/site.md): built from the README's tables, nothing
inline, nothing off this server, and the host config agreeing with the builder."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from boundary import site

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> site.Built:
    return site.build(ROOT, tmp_path_factory.mktemp("site") / "out")


def page(b: site.Built) -> str:
    return (b.out / "index.html").read_text(encoding="utf-8")


def test_every_token_is_filled_and_every_file_is_written(built: site.Built) -> None:
    text = page(built)
    assert "{{" not in text and "}}" not in text
    assert built.tokens > 50
    for name in ("index.html", "style.css", "app.js", site.DATA, "fonts/inter-latin.woff2"):
        assert name in built.files, name


def test_nothing_inline_and_nothing_off_this_server(built: site.Built) -> None:
    """The host's content security policy refuses inline styles and scripts and any
    off-origin load; a page that relied on one would look fine from a plain file server and
    break live."""
    text = page(built)
    assert not re.search(r"<style|style=|<script(?![^>]*\bsrc=)", text)
    assert not re.search(r"\son[a-z]+=", text)
    for src in re.findall(r'(?:src|href)="([^"]+)"', text):
        if src.startswith(("http://", "https://")):
            assert src.startswith(
                ("https://peterparker.ca", "https://github.com/Peter-A-P/compliant-ai-gateway")
            ), src
        elif src.startswith("#"):
            assert f'id="{src[1:]}"' in text, f"no section {src}"
        elif src.startswith("/"):
            assert src in ("/demo", "/dashboard"), src
        else:
            assert src in built.files, src
    js = (built.out / "app.js").read_text(encoding="utf-8")
    assert sorted(set(re.findall(r'getJson\("([^"]+)"\)', js))) == ["/audit/head", site.DATA]
    assert "innerHTML" not in js and not re.search(r"\.style\b", js)


def test_the_figures_are_the_readmes(built: site.Built) -> None:
    text = page(built)
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    tables = site.readme_tables(readme)
    f = site.figures(tables)
    assert f"{f['wire_leaked']} of {f['wire_values']}" in text
    assert f"{f['tamper_found']} of {f['tamper_tried']}" in text
    load = json.loads((ROOT / "bench" / "loadtest.json").read_text(encoding="utf-8"))
    assert f["load_requests"] == f"{sum(c['requests'] for c in load['cells']):,}"
    assert f["load_failed"] == f"{sum(c['errors'] for c in load['cells']):,}"
    data = json.loads((built.out / site.DATA).read_text(encoding="utf-8"))
    assert len(data["load"]) == len(tables["loadtest"])


def test_a_missing_table_fails_the_build() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    broken = re.sub(r"<!-- audit:start -->.*?<!-- audit:end -->", "", readme, flags=re.S)
    with pytest.raises(site.SiteError, match="audit"):
        site.figures(site.readme_tables(broken))


def test_the_example_sends_no_planted_value_and_gets_every_one_back() -> None:
    e = site.redaction_example()
    for value in ("Jane", "Roe", "046 454 286", "555-0142", "jane.roe@example.com", "1749"):
        assert value not in e.vendor, value
    assert "Jane Roe" in e.returned and "CLM-2024-1749" in e.returned
    assert "<" not in e.returned


def test_the_residency_grid_is_the_policys() -> None:
    grid = site.residency_grid(ROOT)
    by = {r["provider"]: [c["allowed"] for c in r["cells"]] for r in grid["rows"]}
    personal = grid["columns"].index("personal")
    redacted = grid["columns"].index("personal, redacted")
    assert [p for p, cells in by.items() if cells[personal]] == ["local"]
    assert by["bedrock"][redacted] and not by["anthropic"][redacted]
    assert all(cells[0] for cells in by.values()), "public may go anywhere"


def test_plain_punctuation_in_the_site() -> None:
    for name in ("index.html", "style.css", "app.js"):
        text = (ROOT / "web" / name).read_text(encoding="utf-8")
        assert not re.search("[\\u2013\\u2014\\u2018\\u2019\\u201c\\u201d]", text), name


def test_caddy_serves_exactly_the_site_with_its_headers() -> None:
    caddy = (ROOT / "deploy" / "Caddyfile").read_text(encoding="utf-8")
    m = re.search(r"@site path (.+)", caddy)
    assert m is not None
    assert m.group(1).split() == ["/", "/index.html", *(f"/{a}" for a in site.ASSETS),
                                  "/fonts/*", "/data/*"]  # fmt: skip
    for k, v in site.HEADERS.items():
        assert f'{k} "{v}"' in caddy, k
