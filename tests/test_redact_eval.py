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

from boundary.redact import EntityType
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
