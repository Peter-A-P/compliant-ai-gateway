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
from boundary.redact.names import COMMON_INITIALISMS as _COMMON_INITIALISMS
from boundary.redact.names import HONORIFICS as _HONORIFICS
from boundary.redact.names import PARTICLES as _PARTICLES
from boundary.redact.names import RSQUO as _RSQUO
from boundary.redact.names import is_name_shaped as _is_name_shaped
from boundary.redact.names import name_parts as _name_parts
from boundary.redact.types import EntityType, Span
from boundary.redact.vocabulary import DECISION_VOCABULARY

# <PERSON_1>, <PERSON_1.2>, <EMAIL_3>. Tolerant on the way back in: any case, spaces inside
# the brackets, or the brackets dropped altogether, which are the ways models mutate them.
PLACEHOLDER = re.compile(
    r"<\s*([A-Za-z][A-Za-z_\-\s]*?)[_\-\s]\s*(\d+(?:\.\d+)?)\s*>"
    r"|(?<![A-Za-z0-9_])([A-Z_]+?)_(\d+(?:\.\d+)?)(?![A-Za-z0-9_])"
)


def placeholder_kind(m: re.Match[str]) -> tuple[str, str]:
    """The entity name and number a placeholder match carries, in canonical form.

    Inside the brackets the separator may be an underscore, a hyphen or whitespace, and the
    name may carry either in place of its underscore: `<EMAIL-1>`, `<NAME LIKE 2>`, and a
    placeholder a model line-wrapped in the middle all resolve to what they plainly mean.
    The measurement in evaluate.py is what set this list (0.6.4); before it, four mutation
    forms models actually produce resolved to nothing and `unresolved` reported them, which
    is safe and is also a value the reader never gets back.

    The widening is deliberately confined to the bracketed form. Outside brackets the exact
    `TYPE_n` spelling is still required, because tolerance there is not a kindness: a
    document that really contains such text would have it replaced by somebody's name, which
    is the fabrication `minted_placeholders_in` exists to prevent.
    """
    name = (m.group(1) or m.group(3)).upper()
    # Any run of separators inside the brackets is one underscore, so a placeholder a model
    # wrapped across a line break in the middle of its name reads as the name it plainly is.
    name = re.sub(r"[\s_-]+", "_", name)
    number = m.group(2) or m.group(4)
    # A zero-padded index can only mean the index. `<EMAIL_01>` is `<EMAIL_1>`, and a part
    # index keeps its own padding rules for the same reason.
    number = ".".join(str(int(part)) for part in number.split("."))
    return name, number


# The word shapes and the parts of a name live in boundary.redact.names, because the
# sweep needs the same answers and two copies of this judgement is a leak waiting for a
# disagreement.
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
# One piece of a code: letters, digits, slashes and hyphens, carrying at least one digit,
# optionally wrapped in brackets.
#
# The brackets arrived in 0.6.2, from measuring the second pass with every recogniser taken
# away. `(709) 555-0199` was the one North American phone form the backstop did not cover:
# the bracket ended the run, `555-0199` was masked on its own, and the area code was
# published beside the placeholder. A value half masked is the failure 0.5.2 fixed for
# `A1B 2C3`, and it was still here in another shape. Brackets cannot widen what is masked
# on their own, because a run still has to carry eight digits to be masked when it is all
# digits, which is what keeps `section 31(1)` readable.
_CODE_TOKEN = r"\(?[A-Za-z0-9/-]*\d[A-Za-z0-9/-]*\)?"
# Several of them joined by single spaces or dots, so that a value written in pieces is one
# candidate and not several: "123 456 789 012", "A1B 2C3", "709.555.0199". A piece without a
# digit ends the run, which is what keeps "12 of 40" and "pages 3, 4 and 5" readable.
_CODE_RUN = re.compile(
    r"(?<![A-Za-z0-9/-])" + _CODE_TOKEN + r"(?:[ .]" + _CODE_TOKEN + r")*(?![A-Za-z0-9/-])"
)
# Brackets are part of a piece, not a boundary, so they come off before the pieces are
# counted and judged.
_RUN_TRIM = "-/.()"
_RUN_SPLIT = re.compile(r"[ .]")
# How many digits an all-digit run needs before it is masked. Eight masks a health number,
# a SIN and a phone number written in pieces, and leaves a short list of small numbers
# readable. A run carrying letters as well is judged by shape instead, because "A1B 2C3" is
# only six characters and is somebody's postcode.
_GROUPED_DIGITS = 8
# Two tokens are one run when only spaces or tabs separate them. A line break ends a run,
# for the same reason the analyzer cuts spans at one.
_RUN_GAP = re.compile(r"[ \t]+")
# After an initial the full stop belongs to it, so `J.F.` and `M. Trznadel` are contiguous.
_INITIAL_GAP = re.compile(r"\.[ \t]*")
# Between a title and what follows it: spaces, after an optional full stop (`Dr. G`).
_TITLE_GAP = re.compile(r"\.?[ \t]+")
# Anything written as an address: a run of word characters, dots, and the punctuation a
# local part carries, around an @. Letters in any script.
#
# The second pass masks this even though a recogniser claims addresses, because the
# evaluation harness found both layers missing the same value on its first run (0.6.0): an
# address spelled with accents matched neither the ASCII pattern in recognisers.py nor any
# shape here, since an address carrying no digit is not identifier-shaped and a lowercase
# word is not name-shaped. The recogniser is fixed; this is the layer that is supposed to
# hold when a recogniser is wrong, and it was not holding.
_ADDRESSISH = re.compile(r"[^\W_][\w.%+-]*@[^\W_][\w-]*(?:\.[^\W_][\w-]*)+")
# The entity names a placeholder can carry, for reading one back out of a string.
_ENTITY_NAMES = frozenset(e.value for e in EntityType)


# The detector types a caller's allow list may release (0.11.0): a place or an organisation
# is a public fact often enough that a jurisdiction can vouch for its own. A person, an
# address and every identifier type are never released, whatever the list says.
RELEASABLE = frozenset({EntityType.LOCATION, EntityType.ORGANISATION})
_LEADING_ARTICLE = re.compile(r"^(?:the|a|an)\s+", re.I)


def _norm(value: str) -> str:
    return " ".join(value.split()).casefold()


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

    kind: Literal["value", "shape", "placeholder"]
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
        names, the legislation it cites. Since 0.11.0 they also release a detector's own
        LOCATION or ORGANISATION span when the span is one of these terms, or made only of
        them (`RELEASABLE`): the caller has said the term identifies nobody, and the
        second pass would have left it alone. Never a PERSON or an identifier, and never on
        the strength of the default vocabulary alone, so a caller who passes no `allow`
        gets exactly what they got before.
    allow_patterns: regular expressions whose whole match the second pass leaves alone, for
        shapes rather than words: an ISO date, a dollar amount, a section reference.
    vocabulary: the base vocabulary; the default is `DECISION_VOCABULARY`.
    vault: a vault from an earlier policy, to carry its placeholders into this one.

        A policy learns two kinds of value. The ones from `spans` it derives, so rebuilding
        it over the same spans mints the same placeholders; the ones the second pass finds
        it discovers only while redacting, so a policy rebuilt between redacting and
        rehydrating has never heard of them. It then leaves them in the text, and only
        `unresolved` says so. Pass the earlier policy's `vault` here and the round trip
        holds across processes, which is also what the Redis vault in Part B is for.
    """

    def __init__(
        self,
        spans: Iterable[Span],
        *,
        allow: Collection[str] = (),
        allow_patterns: Sequence[str | re.Pattern[str]] = (),
        vocabulary: Collection[str] = DECISION_VOCABULARY,
        vault: Mapping[str, str] | None = None,
    ) -> None:
        self._allow: frozenset[str] = frozenset(
            {w.casefold() for w in vocabulary} | {w.casefold() for w in allow}
        )
        # The caller's own terms, kept apart from the default vocabulary because only these
        # may release a detector's span (0.11.0).
        self._caller_words: frozenset[str] = frozenset(
            w.strip().casefold() for w in allow if len(w.split()) == 1
        )
        self._caller_phrases: frozenset[str] = frozenset(
            _norm(w) for w in allow if len(w.split()) > 1
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
        # The placeholders the second pass minted, as opposed to those derived from a span.
        #
        # Which pass produced a placeholder used to be readable from its type, because the
        # second pass only ever minted NAME_LIKE and ID_LIKE. In 0.6.0 it began minting
        # EMAIL as well, for an address no recogniser claimed, and that quietly broke the
        # separability types.py promises: a consumer counting `<EMAIL_n>` could no longer
        # tell a detection from a guess. Two modules each defensible on their own and in
        # contradiction with each other, which is the class of defect project 07 named on
        # 2026-09-21 after finding one of its own. The answer is neither to give the address
        # a worse type nor to drop the promise: the policy records which pass minted what,
        # and the type stays the most accurate one available.
        self._from_second_pass: set[str] = set()
        # normalised value -> placeholder; the substitution table, matched without regard
        # to case or spacing.
        self._table: dict[str, str] = {}
        # exact part of a name -> its placeholder (finding 2), matched case-sensitively:
        # "Grant" in "Will Grant" is substituted, "grant the request" is not. A name is
        # capitalised in prose, and an ordinary word that shares its spelling is not.
        self._parts: dict[str, str] = {}
        self._counters: dict[EntityType, int] = defaultdict(int)
        # Restored first, so that building from the spans reuses these placeholders rather
        # than minting a second one for the same value.
        if vault is not None:
            self._restore(vault)
        self._released: list[Span] = []
        kept: list[Span] = []
        for s in spans:
            (self._released if self._releases(s) else kept).append(s)
        self._build(kept)
        self._pattern = self._compile()
        self._parts_pattern = self._compile_parts()

    # -- building --------------------------------------------------------------------------

    def _releases(self, span: Span) -> bool:
        """Whether the caller's allow list releases this detector span (0.11.0).

        Only an impersonal type, and only when the whole span is one of the caller's
        phrases, or every word in it that could be a name is one of the caller's words
        (`Court of Appeal` when `Court` and `Appeal` are). Anything else is kept, because a
        span the list does not fully account for may carry something it does not know
        about: "Oslo" is on no list that mentions "University of Oslo Hospital".
        """
        if span.entity_type not in RELEASABLE:
            return False
        if not (self._caller_words or self._caller_phrases):
            return False
        # A detector's span often carries the article in front of a name ("the United
        # Kingdom"), and an article is not part of what the caller vouched for.
        text = _LEADING_ARTICLE.sub("", span.text)
        if _norm(text) in self._caller_phrases:
            return True
        words = [m.group(0) for m in _WORD.finditer(text)]
        named = [w for w in words if _is_name_shaped(w)]
        return bool(named) and all(w.casefold() in self._caller_words for w in named)

    @property
    def released(self) -> tuple[Span, ...]:
        """The detector spans the caller's allow list released rather than masked (0.11.0).

        Reported so that a consumer can count what its list let through, the same way
        `second_pass` reports what the fallback caught.
        """
        return tuple(self._released)

    def _restore(self, vault: Mapping[str, str]) -> None:
        """Take on an earlier policy's placeholders, so this one resolves what that one
        minted. The counters move past every index restored, so a value this policy meets
        for the first time cannot be given a placeholder that already means something else.
        """
        for placeholder, value in vault.items():
            m = PLACEHOLDER.fullmatch(placeholder)
            if m is None:
                raise ValueError(f"{placeholder!r} is not a placeholder this library writes")
            name = placeholder_kind(m)[0]
            if name not in _ENTITY_NAMES:
                raise ValueError(f"{placeholder!r} names no entity type this version knows")
            number = placeholder_kind(m)[1]
            # Stored under the canonical spelling whatever the caller wrote, because that is
            # the only form `rehydrate` looks up. A vault that came back from somewhere else
            # having lost its brackets would otherwise restore without complaint and then
            # resolve nothing.
            canonical = f"<{name}_{number}>"
            self._vault[canonical] = value
            if "." in number:
                # A part of a name, matched exactly rather than normalised.
                self._parts[value] = placeholder
            else:
                self._table[_norm(value)] = placeholder
            entity = EntityType(name)
            self._counters[entity] = max(self._counters[entity], int(number.split(".")[0]))

    def _mint(self, entity_type: EntityType, value: str, *, second_pass: bool = False) -> str:
        key = _norm(value)
        existing = self._table.get(key)
        if existing is not None:
            return existing
        if second_pass:
            self._from_second_pass.add(f"<{entity_type.value}_{self._counters[entity_type] + 1}>")
        self._counters[entity_type] += 1
        placeholder = f"<{entity_type.value}_{self._counters[entity_type]}>"
        self._table[key] = placeholder
        self._vault[placeholder] = value
        return placeholder

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
            for part in _name_parts(s.text)
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
        #
        # A part the vocabulary allows is not spread (0.5.6). Project 07 warned of this
        # from its own sweep and this policy had the same hole: a heading the detector
        # typed as a PERSON, "Decision Letter" at 0.6, licensed `Decision` and `Letter`
        # and then blacked out both words on every page of an access-to-information
        # record, which is the one word such a record is about. The detected span itself
        # is still masked, because the detector said so and this library fails closed.
        # What the policy declines to do is carry one detection across a document it was
        # never asked about. The cost is a surname that happens to be a vocabulary word:
        # `Will Grant` ships as `<PERSON_1>` as before, and a bare `Grant` three pages
        # later is released, where the second pass would have excused it in any case.
        owners: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for placeholder, name in persons:
            for part in _name_parts(name):
                if part.casefold() in self._allow or not _is_name_shaped(part):
                    continue
                # A part a restored vault already carries keeps the placeholder it had.
                if _norm(part) not in self._table and part not in self._parts:
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
    def second_pass(self) -> frozenset[str]:
        """The placeholders the second pass minted, rather than a recogniser's span.

        A consumer attributing a redaction to the detector or to the fallback reads this,
        not the entity type: since 0.6.0 the second pass mints `EMAIL` for an address no
        recogniser claimed, because that is the accurate type for it, and the type alone
        can no longer say which pass produced it.

        This policy's own work only. A policy rebuilt from an earlier one's `vault` resolves
        the placeholders that one found and cannot say which pass found them, for the same
        reason the rebuild was needed at all: a vault carries values, not provenance.
        """
        return frozenset(self._from_second_pass)

    @property
    def placeholders(self) -> Mapping[str, str]:
        """normalised value -> placeholder, and exact name part -> part placeholder."""
        return MappingProxyType({**self._table, **self._parts})

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
        # Regions between the placeholders THIS policy minted, so its own output is never
        # masked twice. Placeholder-shaped text it did not mint is not skipped: it is
        # ordinary text that happens to look like a placeholder, and a document can contain
        # such text for real (an office's own procedure manual, a record already redacted by
        # some other hand). Left alone it would be handed back to `rehydrate` as though this
        # policy had written it.
        cursor = 0
        regions: list[tuple[int, int]] = []
        foreign: list[tuple[int, int]] = []
        for m in PLACEHOLDER.finditer(text):
            if self._lookup(m) is None:
                foreign.append((m.start(), m.end()))
                continue
            regions.append((cursor, m.start()))
            cursor = m.end()
        regions.append((cursor, len(text)))

        for lo, hi in regions:
            # Placeholder-shaped text this policy did not write is masked whole, as one
            # opaque token. Masking only the word inside it would leave the brackets and the
            # number around a placeholder of ours, which reads as a nested placeholder and
            # is the sort of thing a model helpfully tidies up.
            claimed: list[tuple[int, int]] = [(s, e) for s, e in foreign if lo <= s and e <= hi]
            found.extend((s, e, EntityType.ID_LIKE) for s, e in claimed)
            # Addresses next, whole, before anything can claim a piece of one.
            addresses = [
                (m.start(), m.end())
                for m in _ADDRESSISH.finditer(text, lo, hi)
                if not self._allowed(m.group(0), m.start(), m.end(), excused)
            ]
            claimed += addresses
            found.extend((s, e, EntityType.EMAIL) for s, e in addresses)
            # Codes written in pieces next: a value split across single spaces or dots is
            # one candidate, because judging the pieces separately masks some of them and
            # leaves the rest, which is a leak wearing a redaction.
            runs = [
                r
                for r in self._code_runs(text, lo, hi, excused)
                if not any(cs <= r[0] and r[1] <= ce for cs, ce in claimed)
            ]
            claimed += runs
            found.extend((s, e, EntityType.ID_LIKE) for s, e in runs)
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
            run = m.group(0).rstrip(_RUN_TRIM)
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
        spaces only, with vocabulary words breaking a run rather than joining it.

        Three shapes join a run as well, all found on the train split of the Text
        Anonymization Benchmark (0.9.0), where real judgments name people in ways the
        generated corpora never did:

        - **Initials**: a capital letter followed by a full stop, or a capital on its own
          straight after a title. `Mr M. Trznadel`, `J.F. Muller`, `Mr G`, `Ms A.`. Before
          this, the surname was masked and the initial published beside it.
        - **Particles** such as `van der` and `de`, between two parts of a name and never
          at either end of it, so `Johan van der Merwe` is one run and a sentence with
          "de" in it is not masked on that account.
        - A run made only of initials is masked when a title precedes it or when it has two
          or more letters (`H.W.K.`, `A.B. v. Switzerland`), unless those letters are a
          common initialism (`U.K.`). A single initial with no title (`Annex A.`) is not.
        """
        runs: list[tuple[int, int, EntityType]] = []
        # (start, end, kind), kind one of "name", "initial", "particle"
        run: list[tuple[int, int, str]] = []
        run_after_title = False
        last_end = -1
        last_kind = ""
        prev_word = ""
        prev_end = -1

        def flush() -> None:
            nonlocal run
            while run and run[-1][2] == "particle":
                run.pop()
            if run:
                kinds = {k for _, _, k in run}
                initials = [s for s, _, k in run if k == "initial"]
                letters = "".join(text[s] for s in initials).casefold()
                keep = (
                    "name" in kinds
                    or run_after_title
                    or (
                        len(initials) >= 2
                        and letters not in _COMMON_INITIALISMS
                        and letters not in self._allow
                    )
                )
                if keep:
                    runs.append((run[0][0], run[-1][1], EntityType.NAME_LIKE))
            run = []

        for m in _WORD.finditer(text, lo, hi):
            s, e = m.start(), m.end()
            tok = m.group(0)
            # A word touching a digit is part of a code, not a name, and a word inside an
            # identifier this pass has already taken is already covered.
            glued = (s > 0 and text[s - 1].isdigit()) or (e < len(text) and text[e].isdigit())
            inside = any(cs <= s and e <= ce for cs, ce in claimed)
            gap = text[last_end:s] if last_end >= 0 else ""
            contiguous = last_end >= 0 and (
                _RUN_GAP.fullmatch(gap) is not None
                or (last_kind == "initial" and _INITIAL_GAP.fullmatch(gap) is not None)
            )
            after_title = (
                prev_word.casefold() in _HONORIFICS
                and _TITLE_GAP.fullmatch(text[prev_end:s]) is not None
            )
            kind = ""
            if not (glued or inside):
                if len(tok) == 1 and tok.isupper() and (text[e : e + 1] == "." or after_title):
                    kind = "initial"
                elif _is_name_shaped(tok) and not self._allowed(tok, s, e, excused):
                    kind = "name"
                elif tok in _PARTICLES and run and contiguous:
                    kind = "particle"
            if kind and run and contiguous:
                run.append((s, e, kind))
            else:
                flush()
                if kind in ("name", "initial"):
                    run = [(s, e, kind)]
                    run_after_title = after_title
            last_end, last_kind = e, kind
            prev_word, prev_end = tok, e
        flush()
        return runs

    def redact(self, text: str) -> str:
        """Placeholders for every known value, then the second pass over what is left.
        Deterministic for a given policy: the same value always gets the same placeholder,
        and a shape masked on one call is a known value on the next."""
        return self.redact_with_spans(text)[0]

    def redact_with_spans(self, text: str) -> tuple[str, list[tuple[int, int]]]:
        """`redact`, and the half-open character ranges of `text` it replaced.

        The ranges are what a benchmark scored on offsets needs (docs/redact.md, TAB): which
        characters of the source were masked, rather than which placeholders came out. They
        are computed by the same three steps `redact` runs, whole values, name parts and the
        second pass, so the two can never disagree; `redact` is this with the ranges dropped.
        Sorted, non-overlapping, and merged where two masks touch.
        """
        # origin[i] is the index in `text` of character i of the current text, or -1 for a
        # character a placeholder put there.
        origin = list(range(len(text)))
        masked: list[tuple[int, int]] = []
        current = text

        def apply(matches: list[tuple[int, int, str]]) -> None:
            nonlocal current, origin
            if not matches:
                return
            out: list[str] = []
            new_origin: list[int] = []
            cursor = 0
            for s, e, replacement in matches:
                out.append(current[cursor:s])
                new_origin.extend(origin[cursor:s])
                source = [o for o in origin[s:e] if o >= 0]
                if source:
                    masked.append((min(source), max(source) + 1))
                out.append(replacement)
                new_origin.extend([-1] * len(replacement))
                cursor = e
            out.append(current[cursor:])
            new_origin.extend(origin[cursor:])
            current, origin = "".join(out), new_origin

        if self._pattern is not None:
            apply(
                [
                    (m.start(), m.end(), self._table[_norm(m.group(0))])
                    for m in self._pattern.finditer(current)
                ]
            )
        if self._parts_pattern is not None:
            apply(
                [
                    (m.start(), m.end(), self._parts[m.group(0)])
                    for m in self._parts_pattern.finditer(current)
                ]
            )
        apply(
            [
                (s, e, self._mint(kind, current[s:e], second_pass=True))
                for s, e, kind in self._shapes(current)
            ]
        )
        merged: list[tuple[int, int]] = []
        for s, e in sorted(masked):
            if merged and s <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], e))
            else:
                merged.append((s, e))
        return current, merged

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

    def minted_placeholders_in(self, text: str) -> list[Leak]:
        """Placeholders in `text` that this policy minted.

        In source text, one of these is unresolvable. `<PERSON_1>` written in a document is
        indistinguishable from `<PERSON_1>` that this policy substituted, so rehydration
        would put a real person's name where the document had only the word, and the record
        released at the end would name somebody it never mentioned. `outbound` refuses on
        it. A placeholder this policy did not mint is not ambiguous and is masked like any
        other text, so it survives the round trip as itself.
        """
        out: list[Leak] = []
        for m in PLACEHOLDER.finditer(text):
            if self._lookup(m) is None:
                continue
            kind = placeholder_kind(m)[0]
            entity = EntityType(kind) if kind in _ENTITY_NAMES else EntityType.NAME_LIKE
            out.append(Leak("placeholder", entity, m.start(), m.end(), m.group(0)))
        return out

    def outbound(self, text: str) -> str:
        """Redact, then refuse unless the result is clean. This is the method to build a
        request body with: it never returns text the policy cannot vouch for.

        Source text carrying a placeholder this policy minted is refused before anything
        else happens; see `minted_placeholders_in`. That makes `outbound` a method for
        source text only. Handing it its own output raises rather than quietly redacting
        twice, which is the honest answer to a call that has already lost track of which
        side of the boundary its text is on. `redact` has no such guard and is idempotent.
        """
        clash = self.minted_placeholders_in(text)
        if clash:
            raise RedactionRefused(clash)
        redacted = self.redact(text)
        leaks = self.check(redacted)
        if leaks:
            raise RedactionRefused(leaks)
        return redacted

    # -- inbound ---------------------------------------------------------------------------

    def _lookup(self, m: re.Match[str]) -> str | None:
        kind, number = placeholder_kind(m)
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


__all__ = ["PLACEHOLDER", "Leak", "Policy", "RedactionRefused", "placeholder_kind"]
