"""The document-wide sweep: a person the detector found anywhere licenses the parts of
their name everywhere else.

Project 07 built this on its own detector and measured it (2026-09-20). Person recall on
its hard pool went from 93.4 to 98.0 percent, and the two shapes that cost the most gained
the most: a two-token surname (`Le Drew`) by 6.8 points and a bare accented forename by 8.0.
The loss it recovers is lexical rather than semantic. A model reads `Marie Chaulk applied`
and answers confidently; three pages later the prose says only `Marie`, in a position that
carries no signal, and the model declines. The document holds the evidence the page does
not, and nothing was using it.

The mechanism is the one already in this library's placeholder policy, moved down a layer
to detection. That matters for consumers the policy is not the whole answer for: a redaction
workflow drawing boxes on a page, or an error decomposition that has to attribute a miss to
the detector rather than to the decision. `Policy` masks the bare `Marie` with or without
this; the span list did not show it until now.

Four guards, each of which 07 hit before it had them:

1. **Case-sensitive.** `Drew` is a person and `withdrew` is a verb.
2. **Whole word.** Same reason, from the other side.
3. **Token runs, not single words.** Claim `Le Drew` whole, or you claim `Drew`, release
   `Le`, and call the result a redaction.
4. **Vocabulary words refused.** A heading the detector typed as a PERSON would otherwise
   black out an ordinary word on every page of the record. The detected span is still
   masked, because the detector said so and this library fails closed; what the sweep
   declines to do is spread that one word across a document it was never asked about.

The sweep adds no confidence of its own. A swept span carries the score of the detection
that licensed it, and a run carries the lowest score among its parts, because a run is
evidenced no better than its weakest token.

**A bare surname is more often mistyped than missed** (07, 2026-09-21). Presidio typed
"Bernadette Tuglavina" a PERSON in the introducing sentence and the bare "Tuglavina" in the
list below a LOCATION. Recall counted that span as found and every table stayed healthy,
and then 07's release rule read LOCATION as "not information about an identifiable
individual" and printed exactly that in the schedule beside a third party's surname. A miss
would have been better, because a miss does not argue for itself. So the sweep does not
merely skip a span that already exists: a span that is exactly the name parts of a person
the document attributes, and whose type is one a consumer reads as impersonal, is re-typed
PERSON. On 07's corpus this moved the leak rate 4.7 to 2.8 percent rules-only and moved
detector recall not at all, which is the finding: recall asks whether a span was found, not
what it was called, and the label is what decides whether the text is released.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Collection, Iterable, Sequence

from boundary.redact.analyzer import resolve_overlaps
from boundary.redact.names import is_name_shaped, name_parts
from boundary.redact.types import EntityType, Span
from boundary.redact.vocabulary import DECISION_VOCABULARY

SWEEP_ID = "boundary:sweep"

# The types a bare surname is plausibly mistyped as, and which a consumer deciding what to
# release is liable to read as "not a person". A span that is exactly the name parts of a
# person the document already attributes gets re-typed out of these; everything else keeps
# the type its recogniser gave it.
RETYPED = (EntityType.LOCATION, EntityType.ORGANISATION, EntityType.NAME_LIKE)

# A word boundary that knows about accents. `[^\W_]` is a word character that is not an
# underscore, which on a str pattern is any letter or digit in any script. The ASCII
# `(?<![A-Za-z0-9])` used elsewhere in this package would let `Berube` match inside
# `Berube` written with its accent on the preceding letter, and this pass is about
# exactly those names.
_LEFT = r"(?<![^\W_])"
_RIGHT = r"(?![^\W_])"
# Two claimed tokens are one run when only spaces, tabs or a single hyphen separate them,
# so `Le Drew` and `Jean-Pierre` each come out whole. A line break ends a run, for the
# same reason the analyzer cuts a span at one.
_RUN_GAP = re.compile(r"(?:[ \t]+|-)")


def _candidates(spans: Iterable[Span], allow: Collection[str]) -> dict[str, float]:
    """Name part -> the score of the detection that licenses it, for every part worth
    sweeping. Case-sensitive keys: the part as the detector saw it spelled."""
    out: dict[str, float] = {}
    for s in spans:
        if s.entity_type is not EntityType.PERSON:
            continue
        for part in name_parts(s.text):
            if part.casefold() in allow or not is_name_shaped(part):
                continue
            out[part] = max(out.get(part, 0.0), s.score)
    return out


def sweep(
    pages: Sequence[str],
    spans: Iterable[Span],
    *,
    first_page: int = 1,
    vocabulary: Collection[str] = DECISION_VOCABULARY,
    allow: Collection[str] = (),
    retype: Collection[EntityType] = RETYPED,
) -> list[Span]:
    """`spans` plus a PERSON span for every other whole-word occurrence of a detected
    person's name parts, overlaps resolved, in page and offset order. A span that is
    exactly those name parts and carries one of the `retype` types is re-typed PERSON
    rather than left alone.

    Added spans carry `recogniser == SWEEP_ID` and re-typed ones `f"{SWEEP_ID}:{original}"`,
    so a consumer measuring its detector can separate what the recognisers found, what the
    document licensed and what it renamed, and can still see which recogniser fired.

    pages, first_page: the same pages and numbering `Analyzer.analyze` was given. The
        sweep reads the text again because a span carries only what it matched.
    vocabulary, allow: words the sweep will not spread, as for `Policy`.
    retype: the types a bare name is plausibly mistyped as. Empty leaves every existing
        span with the type its recogniser gave it.
    """
    known = list(spans)
    excluded = {w.casefold() for w in vocabulary} | {w.casefold() for w in allow}
    candidates = _candidates(known, excluded)
    if not candidates:
        return resolve_overlaps(known)
    pattern = re.compile(
        _LEFT
        + r"(?:"
        + "|".join(sorted((re.escape(p) for p in candidates), key=len, reverse=True))
        + r")"
        + _RIGHT
    )

    # Re-typing first, because a span the sweep would otherwise skip as already taken is
    # exactly the one that has to be corrected rather than skipped.
    kinds = frozenset(retype)
    known = [
        dataclasses.replace(
            s, entity_type=EntityType.PERSON, recogniser=f"{SWEEP_ID}:{s.recogniser}"
        )
        if s.entity_type in kinds and _is_whole_name(s.text, pattern)
        else s
        for s in known
    ]

    by_page: dict[int, list[Span]] = {}
    for s in known:
        by_page.setdefault(s.page, []).append(s)

    added: list[Span] = []
    for i, text in enumerate(pages):
        page = first_page + i
        taken = by_page.get(page, ())
        for start, end in _runs(text, pattern):
            found = Span(
                page=page,
                start=start,
                end=end,
                text=text[start:end],
                entity_type=EntityType.PERSON,
                score=min(candidates[m.group(0)] for m in pattern.finditer(text, start, end)),
                recogniser=SWEEP_ID,
            )
            if any(found.overlaps(k) for k in taken):
                continue
            added.append(found)
    return resolve_overlaps([*known, *added], priority=[SWEEP_ID])


def _is_whole_name(text: str, pattern: re.Pattern[str]) -> bool:
    """Whether `text` is exactly the name parts of a person this document attributes, with
    nothing else in it.

    Exactly, and nothing looser. 07 makes the point with the example: "Hearn" inside
    "Hearn Building" is a place doing honest work, and re-typing the building would be a
    worse error than the one this corrects.
    """
    return _runs(text, pattern) == [(0, len(text))]


def _runs(text: str, pattern: re.Pattern[str]) -> list[tuple[int, int]]:
    """Offsets of each run of claimed tokens: adjacent matches with nothing but spaces,
    tabs or one hyphen between them are one run."""
    runs: list[tuple[int, int]] = []
    start = end = -1
    for m in pattern.finditer(text):
        if end >= 0 and _RUN_GAP.fullmatch(text[end : m.start()]) is not None:
            end = m.end()
            continue
        if end >= 0:
            runs.append((start, end))
        start, end = m.start(), m.end()
    if end >= 0:
        runs.append((start, end))
    return runs


def swept(spans: Iterable[Span]) -> list[Span]:
    """The spans the sweep added: occurrences no recogniser found at all."""
    return [s for s in spans if s.recogniser == SWEEP_ID]


def retyped(spans: Iterable[Span]) -> list[Span]:
    """The spans the sweep re-typed: found, and called something impersonal.

    This is the bucket a recall number cannot give you. 07 split its leaks three ways on
    2026-09-21 and the split is the argument for keeping this countable: 16 never found,
    23 found and mislabelled, and zero from the decision rules. The figure it had been
    publishing as 50 decision errors was never about decisions. A consumer that reports
    `len(retyped(spans))` next to its recall is reporting the half of detection that recall
    is blind to, which neither project had until the sweep made it visible.
    """
    return [s for s in spans if s.recogniser.startswith(SWEEP_ID + ":")]


def original_recogniser(span: Span) -> str:
    """Which recogniser actually fired, whatever the sweep did to the span afterwards."""
    return span.recogniser.removeprefix(SWEEP_ID + ":")


__all__ = ["RETYPED", "SWEEP_ID", "original_recogniser", "retyped", "sweep", "swept"]
