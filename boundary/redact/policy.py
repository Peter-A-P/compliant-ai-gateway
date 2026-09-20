"""The personal-class policy: stable typed placeholders outbound, rehydration inbound, and
a guard that refuses to send rather than warns.

A policy is a library object. It is built from a document's spans and used on any text,
with no gateway, no configuration and no network, so a project can build the outbound
request in a test and assert on it. The gateway applies the same object; it has nothing
the caller does not.

Three findings from project 07's corpus-wide test shaped this file, each of which put real
values past a policy that looked correct (PLAN.md section B2.3, amended 2026-09-19):

1. **The policy is built over every entity in the document**, not the spans being sent.
   The sentences around a span are full of entities the rules had already decided, and
   those sentences travel in the payload.
2. **A detected full name licenses its parts.** The detector finds "Marie Chaulk"; the prose
   two sentences later says "Marie". Each part of a person's name is substituted with a
   placeholder tied to that person, so the second mention does not ship.
3. **Detector recall is not 100 percent, and a boundary built on detections inherits every
   miss.** After the first two fixes, seven names still left because Presidio had not
   found them. So a second pass, independent of any detector, masks anything shaped like
   a name or an identifier unless it is on a vocabulary of terms that carry decision-relevant
   meaning and identify nobody. It is always on. A policy without it is not a boundary, and
   this module does not offer one.

The guard (`outbound`) redacts and then checks its own output for any vault value and any
remaining shape, and raises `RedactionRefused` rather than returning text it cannot vouch
for. By construction it should never fire; it exists because "should never" is not a
privacy guarantee.

Nothing here logs, prints or raises a personal value. `RedactionRefused` carries counts in
its message and the details on an attribute, so a traceback is safe to paste.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

from boundary.errors import BoundaryError
from boundary.redact.types import EntityType, Span
from boundary.redact.vocabulary import DECISION_VOCABULARY

# <PERSON_1>, <PERSON_1.2>, <EMAIL_3>. Tolerant on the way back in: any case, spaces inside
# the brackets, or the brackets dropped altogether, which are the ways models mutate them.
PLACEHOLDER = re.compile(
    r"<\s*([A-Za-z_]+?)_(\d+(?:\.\d+)?)\s*>|(?<![A-Za-z0-9_])([A-Z_]+?)_(\d+(?:\.\d+)?)(?![A-Za-z0-9_])"
)

# Titles that precede a name and are not part of it.
_HONORIFICS = frozenset(
    {"mr", "mrs", "ms", "mx", "miss", "dr", "prof", "hon", "sir", "madam", "rev", "sgt", "cst"}
)

# The right single quotation mark, built from its code point because the formatter would
# otherwise write the character itself into this file, and this repository keeps to plain
# punctuation.
_RSQUO = chr(0x2019)
# One word: a run of letters in any script, joined by hyphens or apostrophes. `[^\W\d_]` is
# "a word character that is neither a digit nor an underscore", which on a str pattern means
# any Unicode letter.
#
# This is deliberately not a pattern for capitalised words. Writing the shape as
# `[A-Z][a-z]+` looks right and leaks in two directions, both found by probing this file on
# 2026-09-19 and both fixed here. It cannot see a letter outside ASCII, so `Emile Berube`
# was masked and the same name spelled properly was not, which in a province with French,
# Innu and Mi'kmaq names is not an edge case. And it splits a word at an internal capital,
# so `MacDonald` became `Mac` + `Donald`, each glued to a letter and therefore each
# discarded: the commonest surname shape in Newfoundland was invisible to the pass whose
# whole job is to catch what the detector missed. The word is matched first and judged
# afterwards, in `_is_name_shaped`, where the judgement can be stated in one place.
_WORD = re.compile(r"[^\W\d_]+(?:[-'" + _RSQUO + r"][^\W\d_]+)*")
# Something an identifier is made of: letters, digits, slashes, hyphens, no spaces.
_ID_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9/-]*")
# One piece of a code: letters, digits, slashes and hyphens, carrying at least one digit.
_CODE_TOKEN = r"[A-Za-z0-9/-]*\d[A-Za-z0-9/-]*"
# Several of them joined by single spaces or dots, so that a value written in pieces is one
# candidate and not several: "123 456 789 012", "A1B 2C3", "709.555.0199". A piece without a
# digit ends the run, which is what keeps "12 of 40" and "pages 3, 4 and 5" readable.
_CODE_RUN = re.compile(
    r"(?<![A-Za-z0-9/-])" + _CODE_TOKEN + r"(?:[ .]" + _CODE_TOKEN + r")*(?![A-Za-z0-9/-])"
)
_RUN_SPLIT = re.compile(r"[ .]")
# How many digits an all-digit run needs before it is masked. Eight masks a health number,
# a SIN and a phone number written in pieces, and leaves a short list of small numbers
# readable. A run carrying letters as well is judged by shape instead, because "A1B 2C3" is
# only six characters and is somebody's postcode.
_GROUPED_DIGITS = 8
# Two tokens are one run when only spaces or tabs separate them. A line break ends a run,
# for the same reason the analyzer cuts spans at one.
_RUN_GAP = re.compile(r"[ \t]+")


def _norm(value: str) -> str:
    return " ".join(value.split()).casefold()


def _is_name_shaped(token: str) -> bool:
    """Whether a word could be part of somebody's name, judged on case alone.

    Two or more letters, starting with a capital. An internal capital is fine and is the
    point: `MacDonald`, `McCarthy`, `LeBlanc`, `O'Brien` and `Jean-Pierre` are all one word.
    Accented and non-Latin letters count, because `isupper` knows about them and a character
    class written in ASCII does not. An all-capitals word needs three letters, so `OK` and
    `NL` stay readable while `GAGNE` and `PENASHUE` do not.
    """
    letters = [c for c in token if c.isalpha()]
    if len(letters) < 2 or not letters[0].isupper():
        return False
    return not (len(letters) < 3 and all(c.isupper() for c in letters))


def _is_identifier_shaped(token: str) -> bool:
    digits = sum(ch.isdigit() for ch in token)
    if digits == 0:
        return False
    letters = sum(ch.isalpha() for ch in token)
    # Five or more digits (a phone fragment, a file number, an account), or letters mixed
    # with two or more digits (ATIPP-2024-0153, F12, SVC-07). A year, a percentage, a small
    # count and an ordinal ("3rd") all stay readable.
    return digits >= 5 or (letters > 0 and digits >= 2)


@dataclass(frozen=True, slots=True)
class Leak:
    """One thing the guard found in text it was asked to vouch for. `value` means a vault
    value is present in clear; `shape` means something name- or identifier-shaped that
    nothing masked. Held in memory for the caller's test; never written anywhere by the
    library."""

    kind: Literal["value", "shape"]
    entity_type: EntityType
    start: int
    end: int
    text: str


class RedactionRefused(BoundaryError):
    """The guard would not vouch for the text. The message carries counts only."""

    def __init__(self, leaks: Sequence[Leak]) -> None:
        self.leaks: tuple[Leak, ...] = tuple(leaks)
        by_type: dict[str, int] = defaultdict(int)
        for leak in leaks:
            by_type[f"{leak.kind}:{leak.entity_type.value}"] += 1
        summary = ", ".join(f"{k} x{n}" for k, n in sorted(by_type.items()))
        super().__init__(
            f"refusing to send: {len(leaks)} leak(s) after redaction ({summary}); "
            "details on .leaks, deliberately not in this message"
        )


class Policy:
    """Placeholders for every entity in a document, applied to any text.

    spans: every span of the document, from `Analyzer.analyze`, not the spans of the text
        about to be sent (finding 1).
    allow: terms the second pass leaves alone, on top of the default vocabulary. A project
        adds its own decision-relevant terms here: the department names, the programme
        names, the legislation it cites.
    allow_patterns: regular expressions whose whole match the second pass leaves alone, for
        shapes rather than words: an ISO date, a dollar amount, a section reference.
    vocabulary: the base vocabulary; the default is `DECISION_VOCABULARY`.
    """

    def __init__(
        self,
        spans: Iterable[Span],
        *,
        allow: Collection[str] = (),
        allow_patterns: Sequence[str | re.Pattern[str]] = (),
        vocabulary: Collection[str] = DECISION_VOCABULARY,
    ) -> None:
        self._allow: frozenset[str] = frozenset(
            {w.casefold() for w in vocabulary} | {w.casefold() for w in allow}
        )
        # A multi-word allow term ("Corner Brook") is a phrase to excuse wherever it
        # appears, so it joins the patterns rather than the word set.
        phrases = [
            re.compile(
                r"(?<![A-Za-z0-9])"
                + re.escape(w.strip()).replace(r"\ ", r"\s+")
                + r"(?![A-Za-z0-9])",
                re.I,
            )
            for w in allow
            if len(w.split()) > 1
        ]
        self._allow_patterns: tuple[re.Pattern[str], ...] = (
            *(re.compile(p) if isinstance(p, str) else p for p in allow_patterns),
            *phrases,
        )
        # placeholder -> the value it stands for; the vault.
        self._vault: dict[str, str] = {}
        # normalised value -> placeholder; the substitution table, matched without regard
        # to case or spacing.
        self._table: dict[str, str] = {}
        # exact part of a name -> its placeholder (finding 2), matched case-sensitively:
        # "Grant" in "Will Grant" is substituted, "grant the request" is not. A name is
        # capitalised in prose, and an ordinary word that shares its spelling is not.
        self._parts: dict[str, str] = {}
        self._counters: dict[EntityType, int] = defaultdict(int)
        self._build(list(spans))
        self._pattern = self._compile()
        self._parts_pattern = self._compile_parts()

    # -- building --------------------------------------------------------------------------

    def _mint(self, entity_type: EntityType, value: str) -> str:
        key = _norm(value)
        existing = self._table.get(key)
        if existing is not None:
            return existing
        self._counters[entity_type] += 1
        placeholder = f"<{entity_type.value}_{self._counters[entity_type]}>"
        self._table[key] = placeholder
        self._vault[placeholder] = value
        return placeholder

    @staticmethod
    def _name_parts(name: str) -> list[str]:
        parts = []
        for part in name.replace("-", " ").split():
            cleaned = part.strip(".,;:'" + _RSQUO)
            if len(cleaned) >= 2 and cleaned.casefold() not in _HONORIFICS:
                parts.append(cleaned)
        return parts

    def _build(self, spans: list[Span]) -> None:
        # Document order, so <PERSON_1> is the first person the document mentions.
        persons: list[tuple[str, str]] = []
        # A person the detector also found on their own as a first name ("Marie" on page
        # 2, "Marie Chaulk" on page 1) is one person, not two. Single-token PERSON spans
        # that are a part of a longer detected name are left to the parts step below.
        parts_of_longer = {
            _norm(part)
            for s in spans
            if s.entity_type is EntityType.PERSON and len(s.text.split()) > 1
            for part in self._name_parts(s.text)
        }
        for s in sorted(spans, key=lambda s: (s.page, s.start, -s.length)):
            if s.entity_type in (EntityType.NAME_LIKE, EntityType.ID_LIKE):
                # Second-pass output from an earlier policy; treat it as what it is.
                self._mint(s.entity_type, s.text)
                continue
            if (
                s.entity_type is EntityType.PERSON
                and len(s.text.split()) == 1
                and _norm(s.text) in parts_of_longer
            ):
                continue
            placeholder = self._mint(s.entity_type, s.text)
            if s.entity_type is EntityType.PERSON:
                persons.append((placeholder, s.text))

        # Finding 2: the parts of a name. A part that belongs to exactly one person gets a
        # placeholder tied to that person (<PERSON_1.2>), so the two mentions are visibly
        # the same person and each rehydrates to what it was. A part shared by two people
        # gets its own top-level placeholder, since the text alone cannot say which one.
        owners: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for placeholder, name in persons:
            for part in self._name_parts(name):
                if _norm(part) not in self._table:
                    owners[_norm(part)].append((placeholder, part))
        for key, claims in owners.items():
            if key in self._table:
                continue
            if len({p for p, _ in claims}) == 1:
                placeholder, part = claims[0]
                stem = placeholder[:-1]
                n = 1 + sum(1 for p in self._vault if p.startswith(stem + "."))
                sub = f"{stem}.{n}>"
                self._parts[part] = sub
                self._vault[sub] = part
            else:
                self._mint(EntityType.PERSON, claims[0][1])

    @staticmethod
    def _alternation(values: Iterable[str], flags: int) -> re.Pattern[str] | None:
        # Longest first, whitespace flexible, bounded by non-alphanumerics on both sides.
        alternatives = sorted(
            (re.escape(v).replace(r"\ ", r"\s+") for v in values), key=len, reverse=True
        )
        if not alternatives:
            return None
        return re.compile(
            r"(?<![A-Za-z0-9])(?:" + "|".join(alternatives) + r")(?![A-Za-z0-9])", flags
        )

    def _compile(self) -> re.Pattern[str] | None:
        # Whole values, matched without regard to case: an email or a SIN is the same value
        # however it is cased, and so is a full name.
        whole = [v for p, v in self._vault.items() if "." not in p]
        return self._alternation(whole, re.I)

    def _compile_parts(self) -> re.Pattern[str] | None:
        return self._alternation(self._parts, 0)

    # -- outbound --------------------------------------------------------------------------

    @property
    def vault(self) -> Mapping[str, str]:
        """placeholder -> value. Grows as the second pass masks things. Never log it."""
        return MappingProxyType(self._vault)

    @property
    def placeholders(self) -> Mapping[str, str]:
        """normalised value -> placeholder, and exact name part -> part placeholder."""
        return MappingProxyType({**self._table, **self._parts})

    def _substitute(self, text: str) -> str:
        if self._pattern is not None:
            text = self._pattern.sub(lambda m: self._table[_norm(m.group(0))], text)
        if self._parts_pattern is not None:
            text = self._parts_pattern.sub(lambda m: self._parts[m.group(0)], text)
        return text

    def _excused(self, text: str) -> list[tuple[int, int]]:
        """Regions of `text` the allow patterns and phrases cover."""
        return [(m.start(), m.end()) for p in self._allow_patterns for m in p.finditer(text)]

    def _allowed(
        self, token: str, start: int, end: int, excused: Sequence[tuple[int, int]]
    ) -> bool:
        if token.casefold() in self._allow:
            return True
        return any(s <= start and e >= end for s, e in excused)

    def _shapes(self, text: str) -> list[tuple[int, int, EntityType]]:
        """Name-shaped runs and identifier-shaped tokens, outside placeholders, that the
        vocabulary does not excuse. Offsets into `text`."""
        found: list[tuple[int, int, EntityType]] = []
        excused = self._excused(text)
        # Regions between placeholders, so a placeholder is never re-masked.
        cursor = 0
        regions: list[tuple[int, int]] = []
        for m in PLACEHOLDER.finditer(text):
            regions.append((cursor, m.start()))
            cursor = m.end()
        regions.append((cursor, len(text)))

        for lo, hi in regions:
            # Codes written in pieces first: a value split across single spaces or dots is
            # one candidate, because judging the pieces separately masks some of them and
            # leaves the rest, which is a leak wearing a redaction.
            claimed: list[tuple[int, int]] = self._code_runs(text, lo, hi, excused)
            found.extend((s, e, EntityType.ID_LIKE) for s, e in claimed)
            # Then identifier-shaped tokens, outside anything already taken.
            for m in _ID_TOKEN.finditer(text, lo, hi):
                tok = m.group(0).rstrip("-/")
                if not _is_identifier_shaped(tok):
                    continue
                s, e = m.start(), m.start() + len(tok)
                if any(cs <= s and e <= ce for cs, ce in claimed):
                    continue
                if self._allowed(tok, s, e, excused):
                    continue
                claimed.append((s, e))
                found.append((s, e, EntityType.ID_LIKE))
            # Names last, and never inside an identifier already claimed. The letters inside
            # a code are a word like any other: `HCS` in `HCS-2024-0881` is name-shaped, and
            # masking it as well as the code it sits in produced two placeholders over one
            # value, which rehydrated to the value twice.
            found.extend(self._name_runs(text, lo, hi, excused, claimed))
        found.sort()
        return found

    def _code_runs(
        self, text: str, lo: int, hi: int, excused: Sequence[tuple[int, int]]
    ) -> list[tuple[int, int]]:
        """Code-shaped pieces joined by single spaces or dots, taken as one candidate.

        A postcode written `A1B 2C3` is the case that forced this: judged piece by piece,
        `2C3` is identifier-shaped and `A1B` is not, so half of it was masked and half was
        released, which reads as a redaction and is not one. A piece carrying no digit ends
        the run, and a piece the vocabulary allows breaks it, the same way a vocabulary word
        breaks a name run: `Q1 2024` stays readable because `q1` is a word this corpus uses.

        Single pieces are left to the per-token pass, which already judges them.
        """
        runs: list[tuple[int, int]] = []
        for m in _CODE_RUN.finditer(text, lo, hi):
            run = m.group(0).rstrip("-/.")
            pieces = _RUN_SPLIT.split(run)
            if len(pieces) < 2:
                continue
            if any(piece.casefold() in self._allow for piece in pieces):
                continue
            if not _is_identifier_shaped(run):
                continue
            # An all-digit run needs enough digits to be somebody's number rather than a
            # list of small ones; a run carrying letters is judged by shape alone.
            all_digits = not any(ch.isalpha() for ch in run)
            if all_digits and sum(ch.isdigit() for ch in run) < _GROUPED_DIGITS:
                continue
            s, e = m.start(), m.start() + len(run)
            if self._allowed(run, s, e, excused):
                continue
            runs.append((s, e))
        return runs

    def _name_runs(
        self,
        text: str,
        lo: int,
        hi: int,
        excused: Sequence[tuple[int, int]],
        claimed: Sequence[tuple[int, int]] = (),
    ) -> list[tuple[int, int, EntityType]]:
        """Name-shaped runs inside text[lo:hi]: adjacent name-shaped words separated by
        spaces only, with vocabulary words breaking a run rather than joining it."""
        runs: list[tuple[int, int, EntityType]] = []
        run: list[tuple[int, int]] = []
        last_end = -1
        for m in _WORD.finditer(text, lo, hi):
            s, e = m.start(), m.end()
            tok = m.group(0)
            # A word touching a digit is part of a code, not a name, and a word inside an
            # identifier this pass has already taken is already covered.
            glued = (s > 0 and text[s - 1].isdigit()) or (e < len(text) and text[e].isdigit())
            inside = any(cs <= s and e <= ce for cs, ce in claimed)
            contiguous = last_end >= 0 and _RUN_GAP.fullmatch(text[last_end:s]) is not None
            allowed = (
                glued or inside or not _is_name_shaped(tok) or self._allowed(tok, s, e, excused)
            )
            if (allowed or not contiguous) and run:
                runs.append((run[0][0], run[-1][1], EntityType.NAME_LIKE))
                run = []
            if not allowed:
                run.append((s, e))
            last_end = e
        if run:
            runs.append((run[0][0], run[-1][1], EntityType.NAME_LIKE))
        return runs

    def _second_pass(self, text: str) -> str:
        shapes = self._shapes(text)
        if not shapes:
            return text
        out: list[str] = []
        cursor = 0
        for s, e, kind in shapes:
            out.append(text[cursor:s])
            out.append(self._mint(kind, text[s:e]))
            cursor = e
        out.append(text[cursor:])
        return "".join(out)

    def redact(self, text: str) -> str:
        """Placeholders for every known value, then the second pass over what is left.
        Deterministic for a given policy: the same value always gets the same placeholder,
        and a shape masked on one call is a known value on the next."""
        return self._second_pass(self._substitute(text))

    def check(self, text: str) -> list[Leak]:
        """What the guard would refuse `text` for: any vault value in clear, and any shape
        the second pass would have masked. Empty means the policy vouches for it."""
        leaks: list[Leak] = []
        if self._pattern is not None:
            for m in self._pattern.finditer(text):
                placeholder = self._table[_norm(m.group(0))]
                kind = placeholder[1:].rsplit("_", 1)[0]
                leaks.append(Leak("value", EntityType(kind), m.start(), m.end(), m.group(0)))
        if self._parts_pattern is not None:
            # A part inside a whole name already reported is the same leak, not another.
            wholes = [(leak.start, leak.end) for leak in leaks]
            for m in self._parts_pattern.finditer(text):
                if any(s <= m.start() and m.end() <= e for s, e in wholes):
                    continue
                leaks.append(Leak("value", EntityType.PERSON, m.start(), m.end(), m.group(0)))
        for s, e, kind in self._shapes(text):
            leaks.append(Leak("shape", kind, s, e, text[s:e]))
        leaks.sort(key=lambda leak: (leak.start, -leak.end))
        return leaks

    def outbound(self, text: str) -> str:
        """Redact, then refuse unless the result is clean. This is the method to build a
        request body with: it never returns text the policy cannot vouch for."""
        redacted = self.redact(text)
        leaks = self.check(redacted)
        if leaks:
            raise RedactionRefused(leaks)
        return redacted

    # -- inbound ---------------------------------------------------------------------------

    def _lookup(self, m: re.Match[str]) -> str | None:
        kind = (m.group(1) or m.group(3)).upper()
        number = m.group(2) or m.group(4)
        return self._vault.get(f"<{kind}_{number}>")

    def rehydrate(self, text: str) -> str:
        """Put the values back. Tolerant of the ways models mutate a placeholder: case,
        spaces inside the brackets, brackets dropped. A possessive or punctuation after
        the placeholder is untouched. A placeholder the vault does not hold is left as it
        is, and `unresolved` lists them."""

        def sub(m: re.Match[str]) -> str:
            value = self._lookup(m)
            return m.group(0) if value is None else value

        return PLACEHOLDER.sub(sub, text)

    def unresolved(self, text: str) -> list[str]:
        """Placeholders in `text` that this policy did not mint: a model's invention, or a
        placeholder from another document's policy."""
        return [m.group(0) for m in PLACEHOLDER.finditer(text) if self._lookup(m) is None]


__all__ = ["PLACEHOLDER", "Leak", "Policy", "RedactionRefused"]
