"""The analyzer: every recogniser over every page, with two guarantees no recogniser has to
know about.

1. **No span crosses a line break.** Presidio returned "Wallace Penashue\\nDate" as one
   person. That draws a box over a word that is not personal information, and it poisons
   any placeholder policy downstream, because "Date" becomes an alias for a person. The
   analyzer cuts every span at the first line break, whatever produced it, so the fix is
   not something each adapter has to remember.
2. **No two spans overlap.** Where recognisers disagree the higher score wins, then the
   longer span, then the recogniser listed first. A consumer that draws boxes or builds
   placeholders never has to arbitrate.

Offsets are per page. The analyzer never concatenates pages, so there is no global offset
for a consumer to convert away from.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable, Sequence

from boundary.redact.recognisers import DEFAULT_RECOGNISERS
from boundary.redact.types import Recogniser, Span


def cut_at_line_break(span: Span) -> Span | None:
    """The span up to its first line break, or None when nothing precedes it. Trailing
    whitespace before the break is dropped too, so "Wallace Penashue \\n" ends at the e."""
    if "\n" not in span.text and "\r" not in span.text:
        return span
    cut = min(i for i, ch in enumerate(span.text) if ch in "\r\n")
    kept = span.text[:cut].rstrip()
    if not kept:
        return None
    return dataclasses.replace(span, end=span.start + len(kept), text=kept)


def _contains(outer: Span, inner: Span) -> bool:
    return (
        outer.page == inner.page
        and outer.start <= inner.start
        and inner.end <= outer.end
        and outer.length > inner.length
    )


def resolve_overlaps(spans: Iterable[Span], *, priority: Sequence[str] = ()) -> list[Span]:
    """One span per stretch of text, in page and offset order.

    Containment is resolved first and regardless of score: a span that strictly contains
    another is the more complete redaction, and the smaller one loses. Project 07 found the
    other rule releasing house numbers: Presidio's LOCATION "Bannerman Street" at 0.85 beat
    an ADDRESS "14 Bannerman Street" at 0.75, and a partially covered value is a leak
    wearing a redaction. Partial overlaps, where neither span holds the other, go to the
    higher score, then the longer span, then the recogniser earliest in `priority`.
    """
    all_spans = list(spans)
    uncontained = [s for s in all_spans if not any(_contains(o, s) for o in all_spans)]
    rank = {rid: i for i, rid in enumerate(priority)}
    ordered = sorted(
        uncontained,
        key=lambda s: (-s.score, -s.length, rank.get(s.recogniser, len(rank)), s.page, s.start),
    )
    kept: list[Span] = []
    for s in ordered:
        if not any(s.overlaps(k) for k in kept):
            kept.append(s)
    kept.sort()
    return kept


class Analyzer:
    """Run recognisers over pages. The default set is the built-in regular expressions;
    `extra` adds a project's own, or the Presidio adapter, without losing the defaults."""

    def __init__(
        self,
        recognisers: Sequence[Recogniser] = DEFAULT_RECOGNISERS,
        *,
        extra: Sequence[Recogniser] = (),
    ) -> None:
        self.recognisers: tuple[Recogniser, ...] = (*recognisers, *extra)
        ids = [r.id for r in self.recognisers]
        if len(set(ids)) != len(ids):
            raise ValueError(f"recogniser ids must be unique; got {ids}")

    def analyze_page(self, text: str, page: int) -> list[Span]:
        found: list[Span] = []
        for r in self.recognisers:
            for s in r.analyze(text, page):
                if s.page != page:
                    raise ValueError(
                        f"recogniser {r.id!r} returned a span for page {s.page} while "
                        f"analysing page {page}"
                    )
                if text[s.start : s.end] != s.text:
                    raise ValueError(
                        f"recogniser {r.id!r} returned a span whose offsets do not select its "
                        f"text on page {page}"
                    )
                cut = cut_at_line_break(s)
                if cut is not None:
                    found.append(cut)
        return resolve_overlaps(found, priority=[r.id for r in self.recognisers])

    def analyze(self, pages: Sequence[str], *, first_page: int = 1) -> list[Span]:
        """Every page in turn, numbered from `first_page`. The result is one flat list in
        page and offset order, which is the shape a document-wide policy is built from."""
        out: list[Span] = []
        for i, text in enumerate(pages):
            out.extend(self.analyze_page(text, first_page + i))
        return out


__all__ = ["Analyzer", "cut_at_line_break", "resolve_overlaps"]
