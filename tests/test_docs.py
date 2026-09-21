"""The documentation checks that would have caught this week's drift.

Nine releases in two days left four pages behind the code, and nothing failed. Each test
here is one of the errors that pass found, turned into something that fails next time:

- `docs/interface.md` is the frozen public interface. Eight names were exported from
  `boundary.redact` and named nowhere in it.
- Every version in the changelog is a version the interface page accounts for. Its version
  history had stopped at 0.5.1 while the package was at 0.5.8.
- The repository writes plain punctuation only (CLAUDE.md). A curly quote or an em-dash
  reaches a file the moment somebody pastes from a vendor's page.
- A relative link that points at nothing is a broken promise in a repository whose whole
  claim is that its numbers can be followed to their source.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import boundary
import boundary.redact

ROOT = Path(__file__).resolve().parent.parent
DOCS = sorted(ROOT.glob("*.md")) + sorted((ROOT / "docs").glob("*.md"))
# Private working notes, gitignored, and not written to this standard.
DOCS = [p for p in DOCS if p.name != "CLAUDE.local.md"]
INTERFACE = ROOT / "docs" / "interface.md"

# Typographic punctuation, which this repository does not use. The names are here so a
# failure says which character rather than printing it.
# Built from code points rather than written out: the formatter rewrites an escape back to
# the character, and a file that lists the characters it bans cannot contain them.
FORBIDDEN = {
    chr(0x2014): "em dash",
    chr(0x2013): "en dash",
    chr(0x2012): "figure dash",
    chr(0x2018): "left single quote",
    chr(0x2019): "right single quote",
    chr(0x201C): "left double quote",
    chr(0x201D): "right double quote",
    chr(0x2026): "ellipsis",
}


def test_every_exported_name_is_in_the_interface_document() -> None:
    text = INTERFACE.read_text(encoding="utf-8")
    exported = [*boundary.__all__, *boundary.redact.__all__]
    missing = sorted({n for n in exported if n not in text})
    assert missing == [], (
        f"exported but undocumented in docs/interface.md: {missing}. The interface is the "
        "contract other repositories are written against; a name that is importable and "
        "undocumented is a promise nobody agreed to."
    )


def test_every_released_version_is_accounted_for_in_the_interface_document() -> None:
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    released = {m.group(1) for m in re.finditer(r"^## (\d+\.\d+\.\d+) \(", changelog, re.M)}
    text = INTERFACE.read_text(encoding="utf-8")
    # A patch release can be covered by a range row ("0.5.2 to 0.5.4"), so a version counts
    # as accounted for when its string appears anywhere on the page.
    missing = sorted(v for v in released if v not in text)
    assert missing == [], f"released but absent from docs/interface.md: {missing}"


def test_the_installed_version_is_the_top_of_the_changelog() -> None:
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    first = re.search(r"^## (\d+\.\d+\.\d+) \(", changelog, re.M)
    assert first is not None
    assert first.group(1) == boundary.__version__, (
        "the newest changelog entry and boundary.__version__ disagree; one of them was "
        "written and the other forgotten"
    )


@pytest.mark.parametrize("path", DOCS, ids=lambda p: p.name)
def test_plain_punctuation_only(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    found = sorted({name for ch, name in FORBIDDEN.items() if ch in text})
    assert found == [], f"{path.name} contains {', '.join(found)}; see CLAUDE.md"


@pytest.mark.parametrize("path", DOCS, ids=lambda p: p.name)
def test_relative_links_resolve(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    broken = []
    for m in re.finditer(r"\[[^\]]*\]\(([^)#\s]+)(?:#[^)]*)?\)", text):
        target = m.group(1)
        if target.startswith(("http://", "https://", "mailto:")):
            continue
        if not (path.parent / target).exists():
            broken.append(target)
    assert broken == [], f"{path.name} links to nothing: {broken}"
