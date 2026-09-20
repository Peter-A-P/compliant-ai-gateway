"""boundary.redact: detection and the personal-class policy, with no gateway, no
configuration and no network.

The tests that matter are the three findings from project 07's corpus-wide test (PLAN.md
section B2.3): the policy is document-wide, a detected name licenses its parts, and a
second pass masks what no detector found. Each has a test that fails without the fix. The
round-trip property test is the honesty check on the whole thing: redact then rehydrate is
the identity, for documents the generator builds from names, identifiers and prose.
"""

from __future__ import annotations

import importlib.util
import random
from collections.abc import Sequence
from typing import Any

import pytest

from boundary.errors import BoundaryError
from boundary.redact import (
    DECISION_VOCABULARY,
    Analyzer,
    EntityType,
    Policy,
    RedactionRefused,
    RegexRecogniser,
    Span,
    cut_at_line_break,
    resolve_overlaps,
)
from boundary.redact.presidio import PresidioRecogniser
from boundary.redact.recognisers import (
    CA_POSTAL_CODE,
    DATE_OF_BIRTH,
    EMAIL,
    EMPLOYEE_ID,
    FILE_NUMBER,
    NL_MCP,
    PHONE,
    SIN,
    luhn_ok,
)


def _span(
    text: str, kind: EntityType = EntityType.PERSON, *, page: int = 1, start: int = 0
) -> Span:
    return Span(page, start, start + len(text), text, kind, 0.9, "test")


# -- Span -----------------------------------------------------------------------------------


def test_a_span_checks_its_own_consistency() -> None:
    with pytest.raises(ValueError, match="offsets"):
        Span(1, 5, 5, "", EntityType.PERSON, 0.9, "t")
    with pytest.raises(ValueError, match="disagree"):
        Span(1, 0, 3, "Marie", EntityType.PERSON, 0.9, "t")
    with pytest.raises(ValueError, match="score"):
        Span(1, 0, 5, "Marie", EntityType.PERSON, 1.5, "t")
    with pytest.raises(ValueError, match="recogniser"):
        Span(1, 0, 5, "Marie", EntityType.PERSON, 0.9, "")


# -- built-in recognisers -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("recogniser", "text", "expected"),
    [
        # The specific Presidio miss: a multi-label government domain is one address.
        (
            EMAIL,
            "write to aaronpenashue@gov.nl.ca.example today",
            ["aaronpenashue@gov.nl.ca.example"],
        ),
        (EMAIL, "two: a.b+c@x.ca, d@e.f.g.h", ["a.b+c@x.ca", "d@e.f.g.h"]),
        (CA_POSTAL_CODE, "St. John's NL A1B 2C3 and a1b2c3", ["A1B 2C3", "a1b2c3"]),
        (CA_POSTAL_CODE, "D1B 2C3 is not a postal code, nor is A1B 2C34", []),
        (
            SIN,
            "SIN 046 454 286 and 046-454-286 and 046454286",
            ["046 454 286", "046-454-286", "046454286"],
        ),
        (SIN, "123 456 789 fails the checksum", []),
        (NL_MCP, "MCP 123456789012 on file", ["123456789012"]),
        (
            PHONE,
            "call (709) 555-0199 or 709.555.0199 or +1 709-555-0199",
            ["(709) 555-0199", "709.555.0199", "+1 709-555-0199"],
        ),
        (
            DATE_OF_BIRTH,
            "Date of birth: April 2, 1971. DOB 1971-04-02; born on 2 April 1971",
            ["April 2, 1971", "1971-04-02", "2 April 1971"],
        ),
        (DATE_OF_BIRTH, "Date: 2024-03-15 is not a birth date", []),
        (
            FILE_NUMBER,
            "File No. ATIPP-2024-0153 and case #2023/118",
            ["ATIPP-2024-0153", "2023/118"],
        ),
        (FILE_NUMBER, "file number: the record", []),
        (EMPLOYEE_ID, "Employee ID: E-44821; payroll no 00912", ["E-44821", "00912"]),
        # A label word inside a longer word is not a label. Project 07 hit this in its own
        # recogniser, where "ref" matched inside "referred" and captured "erred", and probed
        # these for the same bug on 2026-09-19. They are clean because the identifier's digit
        # lookahead cannot reach past the space, but that is worth asserting rather than
        # relying on.
        (FILE_NUMBER, "referred 2024, referenced 4471, the claimant 90210", []),
        (EMPLOYEE_ID, "staff 12345 and personnel 4471", []),
    ],
)
def test_recogniser_goldens(recogniser: RegexRecogniser, text: str, expected: list[str]) -> None:
    spans = recogniser.analyze(text, 1)
    assert [s.text for s in spans] == expected
    for s in spans:
        assert text[s.start : s.end] == s.text, "half-open offsets select the text"
        assert s.recogniser == recogniser.id
        assert s.entity_type is recogniser.entity_type


def test_luhn() -> None:
    assert luhn_ok("046454286") and not luhn_ok("123456789")


def test_a_labelled_health_number_scores_higher_than_a_bare_one() -> None:
    labelled = NL_MCP.analyze("MCP: 123456789012", 1)[0].score
    bare = NL_MCP.analyze("ref 123456789012", 1)[0].score
    assert labelled == 0.95 and bare == 0.6


# -- the analyzer's guarantees -----------------------------------------------------------------------


class _Fixed:
    """A recogniser that returns exactly the spans it was given: a stand-in for any
    detector, including one with Presidio's defects."""

    def __init__(self, rid: str, spans: Sequence[Span]) -> None:
        self.id = rid
        self._spans = list(spans)

    def analyze(self, text: str, page: int) -> Sequence[Span]:
        return [s for s in self._spans if s.page == page]


def test_no_span_crosses_a_line_break_whatever_produced_it() -> None:
    text = "Applicant: Wallace Penashue \nDate: 2024-03-15"
    crossing = Span(1, 11, 33, "Wallace Penashue \nDate", EntityType.PERSON, 0.85, "presidio")
    spans = Analyzer([_Fixed("presidio", [crossing])]).analyze_page(text, 1)
    assert [s.text for s in spans] == ["Wallace Penashue"]
    assert spans[0].end == 27, "trailing space before the break is dropped too"
    assert cut_at_line_break(Span(1, 0, 2, "\nX", EntityType.PERSON, 0.5, "t")) is None


def test_overlaps_resolve_to_the_higher_score_then_the_longer_span() -> None:
    text = "aaronpenashue@gov.nl.ca.example"
    presidio_url = Span(1, 14, 23, "gov.nl.ca", EntityType.URL, 0.5, "presidio")
    spans = Analyzer(extra=[_Fixed("presidio", [presidio_url])]).analyze_page(text, 1)
    assert [(s.entity_type, s.text) for s in spans] == [(EntityType.EMAIL, text)]

    a = Span(1, 0, 5, "Marie", EntityType.PERSON, 0.9, "x")
    b = Span(1, 0, 12, "Marie Chaulk", EntityType.PERSON, 0.9, "y")
    assert resolve_overlaps([a, b]) == [b]
    assert resolve_overlaps([a, b], priority=["x", "y"]) == [b], "length beats priority"
    c = Span(1, 0, 5, "Marie", EntityType.PERSON, 0.9, "z")
    assert resolve_overlaps([a, c], priority=["z", "x"]) == [c], "priority breaks a full tie"


def test_offsets_are_per_page_never_document_global() -> None:
    pages = ["no entities here", "reach me at x@y.ca"]
    spans = Analyzer().analyze(pages, first_page=7)
    assert len(spans) == 1
    s = spans[0]
    assert s.page == 8 and pages[1][s.start : s.end] == "x@y.ca"


def test_a_recogniser_that_lies_about_its_offsets_is_caught() -> None:
    bad = Span(1, 0, 3, "xyz", EntityType.PERSON, 0.9, "liar")
    with pytest.raises(ValueError, match="do not select its text"):
        Analyzer([_Fixed("liar", [bad])]).analyze_page("abc", 1)
    with pytest.raises(ValueError, match="unique"):
        Analyzer([_Fixed("dup", []), _Fixed("dup", [])])


# -- the Presidio adapter ----------------------------------------------------------------------


class _Result:
    def __init__(
        self, entity_type: str, start: int, end: int, score: float, name: str | None
    ) -> None:
        self.entity_type = entity_type
        self.start, self.end, self.score = start, end, score
        self.recognition_metadata: dict[str, Any] | None = (
            {"recognizer_name": name} if name else None
        )


class _FakeEngine:
    def __init__(self, results: Sequence[_Result]) -> None:
        self.results = list(results)
        self.calls: list[dict[str, Any]] = []

    def analyze(self, *, text: str, language: str, entities: list[str]) -> Sequence[_Result]:
        self.calls.append({"text": text, "language": language, "entities": entities})
        return self.results


def test_presidio_adapter_maps_types_and_names_the_recogniser_that_fired() -> None:
    text = "Marie Chaulk, 4111 1111 1111 1111, Corner Brook"
    engine = _FakeEngine(
        [
            _Result("PERSON", 0, 12, 0.85, "SpacyRecognizer"),
            _Result("CREDIT_CARD", 14, 33, 1.0, "CreditCardRecognizer"),
            _Result("LOCATION", 35, 47, 0.85, None),
            _Result("PERSON", 5, 5, 0.85, "SpacyRecognizer"),
        ]
    )
    spans = PresidioRecogniser(engine).analyze(text, 3)
    assert [(s.entity_type, s.text, s.recogniser, s.page) for s in spans] == [
        (EntityType.PERSON, "Marie Chaulk", "presidio:SpacyRecognizer", 3),
        (EntityType.LOCATION, "Corner Brook", "presidio", 3),
    ], "an entity outside the closed vocabulary is dropped, an empty span too"
    assert engine.calls[0]["language"] == "en"
    assert "CREDIT_CARD" not in engine.calls[0]["entities"]


_HAVE_PRESIDIO = (
    importlib.util.find_spec("presidio_analyzer") is not None
    and importlib.util.find_spec("en_core_web_lg") is not None
)


@pytest.mark.skipif(
    not _HAVE_PRESIDIO, reason="the redact extra and the spaCy model are not installed"
)
def test_presidio_live_both_defects_from_07s_brief_are_handled() -> None:
    """Runs only where Presidio and its model are installed. On 2026-09-19 Presidio 2.x
    classified only the gov.nl.ca inside the address, as a URL, exactly as 07 reported."""
    pages = [
        "Applicant: Wallace Penashue\nDate: 2024-03-15\nContact aaronpenashue@gov.nl.ca.example"
    ]
    spans = Analyzer(extra=[PresidioRecogniser()]).analyze(pages)
    by_type = {s.entity_type: s.text for s in spans}
    assert by_type[EntityType.EMAIL] == "aaronpenashue@gov.nl.ca.example"
    assert by_type[EntityType.PERSON] == "Wallace Penashue"
    assert all("\n" not in s.text for s in spans)


@pytest.mark.skipif(
    not _HAVE_PRESIDIO, reason="the redact extra and the spaCy model are not installed"
)
def test_presidio_live_organisations_are_actually_requested() -> None:
    """07's second finding: a bare AnalyzerEngine() does not declare ORGANIZATION, so
    asking for it returned nothing. The default engine is built explicitly."""
    text = "The Department of Justice and Public Safety in Newfoundland wrote to Marie Chaulk."
    spans = PresidioRecogniser().analyze(text, 1)
    found = {(s.entity_type, s.text) for s in spans}
    assert (EntityType.ORGANISATION, "The Department of Justice and Public Safety") in found
    assert (EntityType.LOCATION, "Newfoundland") in found
    assert (EntityType.PERSON, "Marie Chaulk") in found


# -- 07's first run against 0.5.0: the three findings -----------------------------------------------


@pytest.mark.parametrize("written", ["123456789012", "123-456-789-012", "123 456 789 012"])
def test_finding_a_health_number_leaves_in_none_of_its_written_forms(written: str) -> None:
    """The live leak. 0.5.0 caught the first two forms and let the third, the one a form
    uses, leave unredacted with no refusal: 57 of 210 pages on 07's corpus."""
    text = f"MCP {written}."
    spans = Analyzer().analyze([text])
    assert [(s.entity_type, s.text) for s in spans] == [(EntityType.HEALTH_NUMBER, written)]
    assert Policy(spans).outbound(text) == "MCP <HEALTH_NUMBER_1>."
    # And with no detection at all, the second pass still refuses to let it through.
    out = Policy([]).outbound(text)
    assert written not in out and out == "MCP <ID_LIKE_1>."


def test_grouped_digits_are_one_identifier_and_small_lists_are_not() -> None:
    policy = Policy([])
    assert policy.outbound("SIN 123 456 789 fails the checksum but still goes") == (
        "SIN <ID_LIKE_1> fails the checksum but still goes"
    )
    assert policy.outbound("file ATIPP-2019-0042 and 046-454-286") == (
        "file <ID_LIKE_2> and <ID_LIKE_3>"
    ), "a grouped run glued to letters is part of that token, not a second shape"
    assert policy.outbound("pages 3, 4 and 5; 12 of 40; in 2024 and 2025") == (
        "pages 3, 4 and 5; 12 of 40; in 2024 and 2025"
    )


def test_finding_a_span_that_contains_another_wins_regardless_of_score() -> None:
    text = "lives at 14 Bannerman Street, St. John's"
    street = Span(1, 12, 28, "Bannerman Street", EntityType.LOCATION, 0.85, "presidio")
    address = Span(1, 9, 28, "14 Bannerman Street", EntityType.ADDRESS, 0.75, "07:address")
    kept = resolve_overlaps([street, address])
    assert kept == [address], "the house number is not released"
    assert Analyzer([_Fixed("a", [street]), _Fixed("b", [address])]).analyze_page(text, 1) == [
        address
    ]
    # Partial overlap, neither holding the other, still goes to the score.
    left = Span(1, 0, 8, "lives at", EntityType.LOCATION, 0.9, "x")
    right = Span(1, 6, 14, "at 14 Ba", EntityType.ADDRESS, 0.7, "y")
    assert resolve_overlaps([left, right]) == [left]


# -- the policy: the three findings ----------------------------------------------------------------


def test_finding_1_the_policy_is_built_over_the_whole_document() -> None:
    # The entity was decided on page 3. The text being sent is from page 1 and mentions it.
    spans = [_span("Marie Chaulk", page=3, start=40)]
    policy = Policy(spans)
    assert (
        policy.outbound("Page 1 says Marie Chaulk complained.")
        == "Page 1 says <PERSON_1> complained."
    )


def test_finding_2_a_detected_full_name_licenses_its_parts() -> None:
    policy = Policy([_span("Marie Chaulk")])
    out = policy.outbound("Marie Chaulk wrote. Two sentences later, Marie called. Chaulk's file.")
    assert out == "<PERSON_1> wrote. Two sentences later, <PERSON_1.1> called. <PERSON_1.2>'s file."
    assert (
        policy.rehydrate(out)
        == "Marie Chaulk wrote. Two sentences later, Marie called. Chaulk's file."
    )


def test_a_first_name_the_detector_also_found_alone_is_the_same_person() -> None:
    spans = [_span("Marie Chaulk", page=1), _span("Marie", page=2)]
    policy = Policy(spans)
    assert policy.outbound("Marie Chaulk; Marie") == "<PERSON_1>; <PERSON_1.1>"


def test_a_part_two_people_share_gets_its_own_placeholder() -> None:
    policy = Policy([_span("Marie Chaulk"), _span("Marie Penashue", start=20)])
    out = policy.outbound("Marie Chaulk, Marie Penashue, and Marie alone; Chaulk too")
    assert out == "<PERSON_1>, <PERSON_2>, and <PERSON_3> alone; <PERSON_1.1> too"
    assert policy.rehydrate(out) == "Marie Chaulk, Marie Penashue, and Marie alone; Chaulk too"


def test_parts_match_case_sensitively_and_honorifics_are_not_parts() -> None:
    policy = Policy([_span("Dr. Will Grant")])
    out = policy.outbound("Dr. Will Grant will grant the request. Dr. Grant agreed.")
    # "will grant" in lower case is prose and stays; "Grant" capitalised is the person. The
    # honorific is not a part, so a bare "Dr." is never substituted.
    assert out == "<PERSON_1> will grant the request. Dr. <PERSON_1.2> agreed."
    assert policy.rehydrate(out) == "Dr. Will Grant will grant the request. Dr. Grant agreed."


def test_finding_3_the_second_pass_masks_what_no_detector_found() -> None:
    policy = Policy([_span("Marie Chaulk")])
    out = policy.outbound(
        "Marie Chaulk met Wallace Penashue in Gander on Monday. Ref ATIPP-2024-0153, "
        "account 5551234567, section 31, 3rd request, 2024 budget. WALLACE again."
    )
    assert out == (
        "<PERSON_1> met <NAME_LIKE_1> in <NAME_LIKE_2> on Monday. Ref <ID_LIKE_1>, "
        "account <ID_LIKE_2>, section 31, 3rd request, 2024 budget. <NAME_LIKE_3> again."
    )
    assert policy.vault["<NAME_LIKE_1>"] == "Wallace Penashue"
    assert policy.vault["<NAME_LIKE_3>"] == "WALLACE"
    assert policy.rehydrate(out).startswith("Marie Chaulk met Wallace Penashue in Gander")


# -- the second pass: what a word is ---------------------------------------------------------


# Every one of these left 0.5.1 in clear, with no refusal, because the second pass looked
# for `[A-Z][a-z]+`: it cannot see a letter outside ASCII, and it splits a word at an
# internal capital and then discards both halves for being glued to a letter. Found by
# probing rather than by a report, and the names are the ordinary ones here.
@pytest.mark.parametrize(
    "name",
    [
        "MacDonald",
        "McCarthy",
        "LeBlanc",
        "DeSouza",
        "O'Brien",
        "Jean-Pierre",
        "Côté",
        "Bérubé",
        "Émile",
        "GAGNÉ",
        "PENASHUE",
        "Seán",
    ],
)
def test_a_name_no_detector_found_is_masked_whatever_its_letters(name: str) -> None:
    policy = Policy([])
    text = f"spoke to {name} about it"
    out = policy.outbound(text)
    assert name not in out
    assert out == "spoke to <NAME_LIKE_1> about it"
    assert policy.rehydrate(out) == text
    # And the guard sees it in text it is asked to vouch for, rather than passing it.
    assert [leak.text for leak in Policy([]).check(text)] == [name]


@pytest.mark.parametrize("word", ["OK", "NL", "I", "A", "the", "department"])
def test_short_or_lowercase_words_stay_readable(word: str) -> None:
    text = f"it was {word} in the end"
    assert Policy([]).outbound(text) == text


def test_a_caseless_script_is_not_name_shaped_and_the_limit_is_known() -> None:
    """A stated limitation rather than an accident. The second pass judges a word by its
    capital, so a script without case (Chinese, Inuktitut syllabics) carries no signal it
    can read. Masking every such word would make a Labrador document unreadable, so the
    pass leaves them and a caller working with those documents brings a detector. Cyrillic
    and Greek have case and are covered, which is the half of this the ASCII pattern lost.
    """
    policy = Policy([])
    assert policy.outbound("中村 wrote") == "中村 wrote"
    # With a detector, the same name is substituted like any other.
    spans = [_span("中村")]
    assert Policy(spans).outbound("中村 wrote") == "<PERSON_1> wrote"
    # Cased scripts beyond ASCII need no detector.
    assert policy.outbound("Петров called") == "<NAME_LIKE_1> called"


def test_a_value_written_in_pieces_is_masked_whole_or_not_at_all() -> None:
    """A postcode judged piece by piece masked `2C3` and released `A1B`: half a redaction
    reads as a whole one. Pieces joined by single spaces or dots are one candidate."""
    policy = Policy([])
    for text, expected in [
        ("postal A1B 2C3", "postal <ID_LIKE_1>"),
        ("MCP 123 456 789 012", "MCP <ID_LIKE_2>"),
        ("phone 709.555.0199", "phone <ID_LIKE_3>"),
    ]:
        out = policy.outbound(text)
        assert out == expected
        assert policy.rehydrate(out) == text


@pytest.mark.parametrize(
    "text",
    [
        "pages 3, 4 and 5; 12 of 40",
        "in 2024 and 2025",
        # A piece the vocabulary allows breaks the run, the way a vocabulary word breaks a
        # name run, so a fiscal quarter stays readable.
        "Q1 2024 figures",
        "apt 4B",
        "section 31(1)(a)",
        "3rd request",
        "10 20 30",
    ],
)
def test_numbers_that_carry_no_identity_stay_readable(text: str) -> None:
    assert Policy([]).outbound(text) == text


def test_an_acronym_inside_an_identifier_is_not_masked_twice() -> None:
    """`HCS` in `HCS-2024-0881` is name-shaped, and masking it as well as the code it sits
    in put two placeholders over one value, which rehydrated to the value twice."""
    policy = Policy([])
    text = "ref HCS-2024-0881 here"
    out = policy.outbound(text)
    assert out == "ref <ID_LIKE_1> here"
    assert policy.rehydrate(out) == text


def test_the_vocabulary_breaks_a_run_and_a_placeholder_is_never_remasked() -> None:
    policy = Policy([_span("Marie Chaulk")])
    out = policy.outbound("Marie Chaulk Department Gander")
    assert out == "<PERSON_1> Department <NAME_LIKE_1>"
    assert policy.redact(out) == out, "redacting redacted text changes nothing"


def test_placeholder_shaped_text_in_a_document_never_becomes_somebody_s_name() -> None:
    """A document can contain `<PERSON_7>` for real: an office's own procedure manual, or a
    record already redacted by another hand. Left alone it comes back from `rehydrate` as
    though this policy had written it, and the released record names somebody it never
    mentioned."""
    policy = Policy([_span("Marie Chaulk")])

    # One this policy did not mint is masked whole, as an opaque token, and survives the
    # round trip as itself. Masking only the word inside it would leave a nested placeholder.
    text = "The form said <PERSON_7> lives at Gander."
    out = policy.redact(text)
    assert out == "The form said <ID_LIKE_1> lives at <NAME_LIKE_1>."
    assert policy.rehydrate(out) == text

    # One it did mint is unresolvable in source text: it cannot be told from this policy's
    # own substitution, so `outbound` refuses rather than fabricating a name on the way back.
    with pytest.raises(RedactionRefused) as ei:
        policy.outbound("The form said <PERSON_1> lives at Gander.")
    assert [leak.kind for leak in ei.value.leaks] == ["placeholder"]
    assert policy.minted_placeholders_in("nothing here") == []


def test_a_name_is_matched_as_a_whole_word_and_never_inside_another() -> None:
    """Project 07 hit this in its own guard on 2026-09-20: the surname "Le Drew" made it
    refuse any text containing "withdrew". Both patterns here are bounded by non-alphanumeric
    on each side, for the whole value and for the parts of a name alike, so the same false
    positive is not available. Asserted rather than assumed, since 07 asked."""
    policy = Policy([_span("Le Drew")])
    for text in ["the applicant withdrew the request", "andrews filed it", "sundrew"]:
        assert policy.outbound(text) == text
        assert policy.check(text) == []
    # The name itself, and its parts, still go.
    assert policy.outbound("Le Drew withdrew it") == "<PERSON_1> withdrew it"
    assert policy.outbound("Drew signed") == "<PERSON_1.2> signed"
    assert policy.outbound("Drew's file") == "<PERSON_1.2>'s file"


def test_a_rebuilt_policy_resolves_what_the_first_one_found_when_given_its_vault() -> None:
    """The values from spans are derived, so a policy rebuilt over the same spans mints the
    same placeholders and the guard still fires. The values the second pass finds are
    discovered while redacting, so a rebuilt policy has never heard of them: without the
    vault it leaves them in the text and only `unresolved` says so."""
    spans = [_span("Marie Chaulk")]
    first = Policy(spans)
    out = first.outbound("Marie Chaulk met Wallace Penashue in Gander; Marie again")
    assert out == "<PERSON_1> met <NAME_LIKE_1> in <NAME_LIKE_2>; <PERSON_1.1> again"

    bare = Policy(spans)
    assert bare.unresolved(out) == ["<NAME_LIKE_1>", "<NAME_LIKE_2>"]
    assert "Wallace Penashue" not in bare.rehydrate(out), "a second-pass value is not derivable"
    # The guard does carry across instances for the placeholders the spans derive.
    with pytest.raises(RedactionRefused):
        bare.outbound(out)

    restored = Policy(spans, vault=first.vault)
    assert restored.rehydrate(out) == first.rehydrate(out)
    assert restored.unresolved(out) == []


def test_a_restored_vault_keeps_its_placeholders_and_new_values_get_fresh_ones() -> None:
    first = Policy([_span("Marie Chaulk")])
    second = Policy([_span("Marie Chaulk"), _span("Aaron Rideout", start=20)], vault=first.vault)
    assert second.vault["<PERSON_1>"] == "Marie Chaulk"
    assert second.vault["<PERSON_2>"] == "Aaron Rideout", "the counter moved past what it restored"
    assert second.vault["<PERSON_1.2>"] == "Chaulk", "a restored name part keeps its placeholder"
    # Written any way round, a vault entry is stored under the one spelling rehydrate reads.
    loose = Policy([], vault={"PERSON_1": "Marie Chaulk", "<person_2>": "Aaron Rideout"})
    assert dict(loose.vault) == {"<PERSON_1>": "Marie Chaulk", "<PERSON_2>": "Aaron Rideout"}
    assert loose.rehydrate("<PERSON_1> and <PERSON_2>") == "Marie Chaulk and Aaron Rideout"
    with pytest.raises(ValueError, match="no entity type"):
        Policy([], vault={"<NOPE_1>": "x"})


def test_outbound_is_for_source_text_and_says_so_when_handed_its_own_output() -> None:
    policy = Policy([_span("Marie Chaulk")])
    out = policy.outbound("Marie Chaulk called")
    with pytest.raises(RedactionRefused):
        policy.outbound(out)
    assert policy.redact(out) == out, "redact has no such guard and stays idempotent"


def test_allow_terms_and_patterns_extend_the_vocabulary() -> None:
    iso_date = r"\d{4}-\d{2}-\d{2}"
    policy = Policy([], allow={"Corner Brook"}, allow_patterns=[iso_date])
    text = "Corner Brook office, 2024-03-15, file 2024-0153"
    assert policy.outbound(text) == "Corner Brook office, 2024-03-15, file <ID_LIKE_1>"
    assert "corner brook" not in DECISION_VOCABULARY


def test_placeholders_are_stable_and_typed_per_distinct_value() -> None:
    spans = [
        _span("x@y.ca", EntityType.EMAIL),
        _span("X@Y.CA", EntityType.EMAIL, start=10),
        _span("046 454 286", EntityType.SIN, start=20),
    ]
    policy = Policy(spans)
    assert policy.outbound("x@y.ca X@Y.CA 046 454 286 046  454 286") == (
        "<EMAIL_1> <EMAIL_1> <SIN_1> <SIN_1>"
    )


# -- the guard ---------------------------------------------------------------------------------


def test_check_reports_values_and_shapes_and_outbound_refuses_on_them() -> None:
    policy = Policy([_span("Marie Chaulk")])
    leaks = policy.check("Marie Chaulk and 046454286 walked to Gander")
    kinds = {(leak.kind, leak.entity_type) for leak in leaks}
    assert ("value", EntityType.PERSON) in kinds
    assert ("shape", EntityType.ID_LIKE) in kinds and ("shape", EntityType.NAME_LIKE) in kinds
    assert policy.check(policy.outbound("Marie Chaulk walked to Gander")) == []

    class Broken(Policy):
        def redact(self, text: str) -> str:
            return text  # a policy that forgot to redact

    with pytest.raises(RedactionRefused) as ei:
        Broken([_span("Marie Chaulk")]).outbound("Marie Chaulk called")
    assert isinstance(ei.value, BoundaryError)
    assert "Marie" not in str(ei.value) and "Chaulk" not in str(ei.value), "no value in the message"
    assert "value:PERSON" in str(ei.value) and ei.value.leaks[0].text == "Marie Chaulk"


# -- rehydration ---------------------------------------------------------------------------------


def test_rehydration_is_tolerant_of_the_ways_models_mutate_placeholders() -> None:
    policy = Policy([_span("Marie Chaulk"), _span("x@y.ca", EntityType.EMAIL, start=20)])
    text = "<PERSON_1>'s address is <email_1>; < PERSON_1 > said PERSON_1.1 would, <PERSON_7> not."
    assert policy.rehydrate(text) == (
        "Marie Chaulk's address is x@y.ca; Marie Chaulk said Marie would, <PERSON_7> not."
    )
    assert policy.unresolved(text) == ["<PERSON_7>"]


_FIRST = ["Marie", "Wallace", "Aaron", "Jean-Pierre", "Siobhan", "Kwame", "Émile"]
_LAST = [
    "Chaulk",
    "Penashue",
    "O'Brien",
    "Rideout",
    "Tobin",
    "Nkemelu",
    # The shapes the ASCII capitalised-word pattern could not see. They are in the generator
    # rather than only in a case of their own so that every property run exercises them.
    "MacDonald",
    "McCarthy",
    "LeBlanc",
    "Côté",
    "Bérubé",
]
_PLACES = [
    "Gander",
    "Corner Brook",
    "Happy Valley-Goose Bay",
    "Wabush",
    "Sheshatshiu",
    "Saint-Pierre",
]
_PROSE = "the applicant asked the department to review the record and the decision was".split()  # noqa: SIM905


def _document(rng: random.Random) -> tuple[list[str], list[Span]]:
    """A two-page document with detected names and identifiers, plus names and places no
    detector saw, in prose made of vocabulary words."""
    people = [f"{rng.choice(_FIRST)} {rng.choice(_LAST)}" for _ in range(3)]
    pages: list[str] = []
    spans: list[Span] = []
    for page in (1, 2):
        words: list[str] = []
        for _ in range(rng.randint(8, 20)):
            r = rng.random()
            if r < 0.12:
                words.append(rng.choice(people))
            elif r < 0.2:
                words.append(rng.choice(people).split()[rng.randint(0, 1)])
            elif r < 0.28:
                words.append(rng.choice(_PLACES))
            elif r < 0.36:
                words.append(f"ATIPP-{rng.randint(2019, 2026)}-{rng.randint(0, 9999):04d}")
            elif r < 0.4:
                words.append(f"a{rng.randint(0, 9)}b {rng.randint(0, 9)}c{rng.randint(0, 9)}")
            else:
                words.append(rng.choice(_PROSE))
        text = " ".join(words) + ".\nSecond line " + rng.choice(people) + " here."
        pages.append(text)
        for person in people:
            i = text.find(person)
            if i >= 0:
                spans.append(Span(page, i, i + len(person), person, EntityType.PERSON, 0.85, "t"))
    return pages, spans


@pytest.mark.parametrize("seed", range(60))
def test_round_trip_property_redact_then_rehydrate_is_the_identity(seed: int) -> None:
    rng = random.Random(seed)
    pages, spans = _document(rng)
    spans = spans + Analyzer().analyze(pages)
    policy = Policy(spans)
    for page in pages:
        out = policy.outbound(page)
        assert policy.check(out) == []
        for person in {s.text for s in spans if s.entity_type is EntityType.PERSON}:
            assert person not in out
        for place in _PLACES:
            assert place not in out, "undetected place names are masked by the second pass"
        assert policy.rehydrate(out) == page
