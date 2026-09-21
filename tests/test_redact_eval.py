"""The corpus and the evaluation harness: the first redaction numbers this repository owns.

These tests are about the measurement rather than the engine. A harness that reports a
comfortable number because it is built wrong is worse than no harness, so what is asserted
here is the things that would make the table a lie: labels that do not sit where they say
they do, a corpus that is not the same twice, an interval that is not an interval, and a
leak rate computed over the wrong denominator.
"""

from __future__ import annotations

import math

import pytest

from boundary.redact import EntityType, Policy
from boundary.redact.corpus import DISTRACTORS, Label, build
from boundary.redact.evaluate import Rate, run, to_json, wilson


def test_every_label_sits_exactly_where_it_says() -> None:
    corpus = build(pages=25)
    assert corpus.labels, "a corpus with no labels would pass every other test here"
    for lab in corpus.labels:
        page = corpus.pages[lab.page - 1]
        assert page[lab.start : lab.end] == lab.text, (
            "a label whose offsets do not select its own text makes every recall figure "
            "measured against it meaningless"
        )


def test_the_corpus_is_the_same_twice_and_different_on_another_seed() -> None:
    a = build(pages=10, seed=1)
    b = build(pages=10, seed=1)
    c = build(pages=10, seed=2)
    assert a == b, "a number nobody can reproduce is not a result"
    assert a.pages != c.pages


def test_no_slot_is_left_unfilled() -> None:
    for page in build(pages=40).pages:
        assert "{" not in page and "}" not in page


def test_every_planted_value_appears_in_its_page() -> None:
    corpus = build(pages=20)
    for lab in corpus.labels:
        assert lab.text in corpus.pages[lab.page - 1]


def test_a_page_never_gives_one_person_two_roles() -> None:
    # Three slots drawing the same name would make a page where the applicant, the analyst
    # and the third party are one person, and every metric over it would measure a document
    # nobody could write.
    corpus = build(pages=30)
    for page in range(1, 31):
        people = [lab.text for lab in corpus.labels if lab.page == page and lab.shape]
        assert len(set(people)) == 3


def test_distractors_are_present_and_unlabelled() -> None:
    # Without them a precision figure measures nothing, because everything shaped like a
    # value would be a value.
    corpus = build(pages=40)
    text = "\n".join(corpus.pages)
    assert sum(1 for d in DISTRACTORS if d in text) >= 8
    labelled = {lab.text for lab in corpus.labels}
    assert not (labelled & set(DISTRACTORS))


def test_towns_and_departments_are_labelled_but_not_personal() -> None:
    corpus = build(pages=20)
    impersonal = [lab for lab in corpus.labels if not lab.personal]
    assert impersonal, "a detector that finds a town is right, and the corpus should say so"
    assert {lab.entity_type for lab in impersonal} == {
        EntityType.LOCATION,
        EntityType.ORGANISATION,
    }
    assert all(lab.personal for lab in corpus.personal)
    assert len(corpus.personal) < len(corpus.labels)


def test_the_name_pool_carries_every_shape_that_has_leaked() -> None:
    # Four of these five were invisible to the second pass until 0.5.2, and a corpus without
    # them is how a 96.3% coexists with a leak.
    shapes = {lab.shape for lab in build(pages=60).labels if lab.shape}
    assert shapes == {"plain", "accented", "internal", "apostrophe", "hyphenated"}


@pytest.mark.parametrize(
    ("hits", "total"),
    [(0, 10), (10, 10), (1, 3), (97, 100), (0, 1), (1, 1)],
)
def test_wilson_stays_inside_zero_and_one_and_contains_the_estimate(hits: int, total: int) -> None:
    lo, hi = wilson(hits, total)
    assert 0.0 <= lo <= hi <= 1.0
    assert lo <= hits / total <= hi


def test_wilson_is_not_the_normal_approximation_at_the_ends() -> None:
    # The reason this file does not use the textbook interval: at 100 out of 100 the normal
    # one has zero width and claims certainty from a hundred observations.
    lo, hi = wilson(100, 100)
    assert hi == 1.0
    assert lo < 1.0
    assert math.isclose(lo, 0.9629, abs_tol=0.001)


def test_a_rate_always_prints_its_interval() -> None:
    # A bare number is a bug in this repository (CLAUDE.md).
    printed = str(Rate(3, 4))
    assert "%" in printed and "to" in printed and "(" in printed


def test_the_harness_reports_no_leak_and_a_round_trip_on_a_short_run() -> None:
    results = run(pages=20)
    assert results.leaks == 0, "a planted value left in clear with no refusal"
    assert results.round_trips == results.pages
    assert results.refusals == 0
    assert results.precision.value > 0.9
    # Rules only: there is no PERSON recogniser, so detection recall on people is zero and
    # the leak rate is zero anyway. That gap is the case for the second pass, and it is the
    # one number in the table that would vanish if the two were reported as one.
    person = next(r for r in results.entities if r.entity_type == "PERSON")
    assert person.found == 0
    assert results.recall.value < 0.6


def test_the_leak_rate_is_counted_over_personal_values_only() -> None:
    results = run(pages=10)
    corpus = build(pages=10)
    assert results.values == len(corpus.personal)
    assert results.recall.total == len(corpus.labels)


def test_results_serialise_with_their_intervals() -> None:
    payload = to_json(run(pages=5))
    assert '"summary"' in payload
    assert " to " in payload, "the summary carries intervals, not bare numbers"


def test_a_label_checks_its_own_offsets() -> None:
    # A span that lies about its offsets is one bad detection. A label that lies about its
    # offsets is every number measured against it, so the ground truth validates itself.
    Label(1, 0, 5, "Marie", EntityType.PERSON)
    with pytest.raises(ValueError, match="characters but"):
        Label(1, 0, 4, "Marie", EntityType.PERSON)
    with pytest.raises(ValueError, match="0 <= start < end"):
        Label(1, 4, 4, "", EntityType.PERSON)


# -- the Canadian identifier set ------------------------------------------------------------


def test_every_case_contains_its_own_value() -> None:
    from boundary.redact.identifiers import build_cases

    cases = build_cases(per_family=10)
    assert cases
    for case in cases:
        assert case.value in case.text


def test_a_case_that_does_not_contain_its_value_is_refused() -> None:
    from boundary.redact.identifiers import Case

    with pytest.raises(ValueError, match="is not in its own sentence"):
        Case("sin", "spaced", "nothing here", "046 454 286", EntityType.SIN)


def test_the_set_carries_all_three_kinds_of_case() -> None:
    from boundary.redact.identifiers import build_cases

    kinds = {case.expect for case in build_cases(per_family=5)}
    assert kinds == {"detect", "mask only", "ignore"}


def test_the_negatives_really_are_negatives() -> None:
    # A suite whose near-misses are near-misses only in the author's head measures nothing.
    # These two are checkable: a SIN negative must fail the Luhn check, and a postcode
    # negative must use a letter Canada Post does not.
    from boundary.redact.identifiers import _luhn_ok, build_cases

    cases = build_cases(per_family=20)
    sins = [c for c in cases if c.family == "sin" and c.expect == "ignore"]
    assert sins
    assert all(not _luhn_ok(c.value.replace(" ", "")) for c in sins)
    posts = [c for c in cases if c.family == "postal_code" and c.expect == "ignore"]
    assert posts
    assert all(
        any(ch in "DFIOQUWZ" for ch in c.value.upper().replace(" ", "")[:1])
        or any(ch in "DFIOQU" for ch in c.value.upper().replace(" ", ""))
        for c in posts
    )


def test_nothing_in_the_identifier_set_leaves_in_clear() -> None:
    from boundary.redact.evaluate import identifiers

    results = identifiers(per_family=12)
    leaked = [r for r in results.families if r.expect != "ignore" and r.masked < r.cases]
    assert leaked == [], f"a claimed or unclaimed identifier left in clear: {leaked}"


def test_no_recogniser_fires_on_a_near_miss() -> None:
    from boundary.redact.evaluate import identifiers

    results = identifiers(per_family=12)
    fired = [r for r in results.families if r.expect == "ignore" and r.detected > 0]
    assert fired == [], f"a recogniser claimed something that is not one: {fired}"


def test_the_unclaimed_families_are_masked_without_being_detected() -> None:
    # The second pass carrying values nothing in this repository was written to find. If a
    # recogniser is ever added for one of these, this test says so rather than going quiet.
    from boundary.redact.evaluate import identifiers

    results = identifiers(per_family=12)
    unclaimed = [r for r in results.families if r.expect == "mask only"]
    assert {r.family for r in unclaimed} == {"business number", "driver licence", "passport"}
    for row in unclaimed:
        assert row.masked == row.cases
        assert row.detection.value < 0.2


def test_the_second_pass_alone_covers_every_identifier_family() -> None:
    # Project 07's point, 2026-09-20: its EMAIL recogniser found every address, so its
    # corpus never asked the second pass whether it could, and the backstop was untested
    # rather than working. This asserts the layer that exists for when a recogniser is
    # wrong, with every recogniser taken away.
    from boundary.redact.evaluate import identifiers

    results = identifiers(per_family=12)
    weak = [
        r
        for r in results.families
        if r.expect != "ignore" and r.backstop.value < 1.0 and r.family != "date_of_birth"
    ]
    assert weak == [], f"the second pass alone does not cover: {[r.family for r in weak]}"


def test_a_bracketed_area_code_is_masked_with_the_rest_of_the_number() -> None:
    # Found by measuring the backstop on its own: (709) ended the run, 555-0199 was masked
    # alone, and the area code was published beside the placeholder. Half a value masked is
    # the 0.5.2 failure in another shape.
    policy = Policy([])
    out = policy.redact("Call (709) 555-0199 today.")
    assert "709" not in out
    # And the thing that keeps the brackets from widening it: a section reference is not a
    # code because it does not carry eight digits.
    assert policy.redact("See section 31(1) and page 12 of 40.") == (
        "See section 31(1) and page 12 of 40."
    )


def test_a_date_written_in_words_has_no_backstop_and_the_number_says_so() -> None:
    # 66.7%, not 100%, and on purpose. "14 March 1978" is masked only because the
    # DATE_OF_BIRTH recogniser sees the label in front of it; the second pass leaves it,
    # because masking every written date would black out the dates a decision turns on.
    # If that judgement is ever revisited, this test is where the revisit shows up.
    policy = Policy([])
    assert policy.redact("The meeting of 14 March 1978 was minuted.") == (
        "The meeting of 14 March 1978 was minuted."
    )
    from boundary.redact.evaluate import identifiers

    row = next(
        r
        for r in identifiers(per_family=12).families
        if r.family == "date_of_birth" and r.expect == "detect"
    )
    assert 0.6 < row.backstop.value < 0.7
    assert row.masking.value == 1.0, "with the recogniser present, every form is masked"
