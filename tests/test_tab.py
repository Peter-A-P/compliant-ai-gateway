"""TAB scoring, on a document built here in TAB's format: no download, no network.

The real measurement was cross-checked once against the benchmark's own evaluation.py
(docs/redact.md): the same direct recall and precision to four decimals, quasi recall within
0.01 points, 3 of about 30,500 mentions judged differently. These tests pin the rules that
agreement rests on, so a change to them fails here before it moves a published number.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import re
from pathlib import Path

import httpx
import pytest
import respx

from boundary.redact import tab
from boundary.redact.analyzer import Analyzer
from boundary.redact.corpus import build
from boundary.redact.policy import Policy
from boundary.redact.sweep import sweep

TEXT = "The applicant, Mr Oskar Brandt, was born in 1961. Mr O. Brandt lives in Oslo."
#       0         1         2         3         4         5         6         7
#       0123456789012345678901234567890123456789012345678901234567890123456789012345678


def _span(text: str, start: int = 0) -> tuple[int, int]:
    i = TEXT.index(text, start)
    return i, i + len(text)


def _mention(eid: str, text: str, kind: str, etype: str, start: int = 0) -> dict[str, object]:
    s, e = _span(text, start)
    return {
        "entity_id": eid,
        "entity_type": etype,
        "identifier_type": kind,
        "start_offset": s,
        "end_offset": e,
        "span_text": text,
        "entity_mention_id": f"{eid}_m{s}",
        "edit_type": "check",
        "confidential_status": "NOT_CONFIDENTIAL",
    }


def _corpus(tmp_path: Path) -> Path:
    doc = {
        "doc_id": "001-TEST",
        "text": TEXT,
        "dataset_type": "test",
        "annotations": {
            "annotator1": {
                "entity_mentions": [
                    _mention("a1_e1", "Mr Oskar Brandt", "DIRECT", "PERSON"),
                    _mention("a1_e1", "Mr O. Brandt", "DIRECT", "PERSON"),
                    _mention("a1_e2", "1961", "QUASI", "DATETIME"),
                    _mention("a1_e3", "Oslo", "NO_MASK", "LOC"),
                ]
            },
            "annotator2": {
                "entity_mentions": [
                    _mention("a2_e1", "Oskar Brandt", "DIRECT", "PERSON"),
                    _mention("a2_e2", "Oslo", "QUASI", "LOC"),
                ]
            },
        },
    }
    path = tmp_path / "echr_test.json"
    path.write_text(json.dumps([doc]), encoding="utf-8")
    return path


def _masked(*texts: str) -> bytearray:
    return tab._mask_array(len(TEXT), [_span(t) for t in texts])


# -- the mention rule ----------------------------------------------------------------------


def test_a_title_left_in_clear_does_not_count_against_the_mask() -> None:
    s, e = _span("Mr Oskar Brandt")
    assert tab.mention_masked(TEXT, _masked("Oskar Brandt"), s, e)


def test_an_initial_left_in_clear_does_count() -> None:
    """A bare capital is an initial, not the article or a possessive: it identifies."""
    s, e = _span("Mr O. Brandt")
    only_surname = tab._mask_array(len(TEXT), [(e - len("Brandt"), e)])
    assert not tab.mention_masked(TEXT, only_surname, s, e)


def test_the_possessive_s_is_ignored_and_punctuation_too() -> None:
    text = "Brandt's (farm)"
    masked = bytearray(len(text))
    masked[0:6] = b"\x01" * 6  # Brandt
    masked[10:14] = b"\x01" * 4  # farm
    assert tab.mention_masked(text, masked, 0, len(text))


# -- entities, annotators, precision -------------------------------------------------------


def test_an_entity_counts_only_when_every_mention_is_masked(tmp_path: Path) -> None:
    [doc] = tab.load(_corpus(tmp_path))
    person = next(e for e in doc.entities if e.annotator == "annotator1" and e.is_direct)
    assert len(person.mentions) == 2
    # First mention masked, second not: the entity is not masked.
    masked = tab._mask_array(len(TEXT), [_span("Oskar Brandt")])
    ok = all(tab.mention_masked(TEXT, masked, s, e) or not n for s, e, n in person.mentions)
    assert not ok


def test_counts_are_micro_averaged_over_annotators(tmp_path: Path) -> None:
    [doc] = tab.load(_corpus(tmp_path))
    direct = [e for e in doc.entities if e.need_masking and e.is_direct]
    quasi = [e for e in doc.entities if e.need_masking and not e.is_direct]
    # One person for each annotator; the date for one, the place for the other.
    assert len(direct) == 2 and len(quasi) == 2
    assert doc.safe == [("annotator1", *_span("Oslo"))]


def test_the_run_scores_a_real_policy_and_every_figure_has_an_interval(tmp_path: Path) -> None:
    docs = tab.load(_corpus(tmp_path))
    r = tab.run(docs)
    # The second pass masks both surnames, "Oskar" and "Oslo", and leaves the year by
    # design. It also leaves the initial in "Mr O. Brandt", so annotator 1's person, whose
    # mentions include that one, is not masked and annotator 2's is: the finding TAB made.
    assert r.direct.value == 0.5
    assert r.quasi.value == 0.5
    assert r.safe_touched.value == 1.0
    lo, hi = r.precision.interval()
    assert 0.0 <= lo <= r.precision.value <= hi <= 1.0
    assert "bootstrap over judgments" in r.table()


def test_precision_scores_each_masked_token_by_the_annotators_who_masked_it(
    tmp_path: Path,
) -> None:
    docs = tab.load(_corpus(tmp_path))
    r = tab.run(docs)
    # Four masked tokens, two annotators: Oskar (both), Brandt (both), the second Brandt
    # (annotator 1 only) and Oslo (annotator 2 only, who called it QUASI) = 6 of 8.
    assert (sum(r.precision.hits), sum(r.precision.totals)) == (6, 8)


# -- the download --------------------------------------------------------------------------


def test_a_file_with_the_wrong_checksum_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = b"[]"
    monkeypatch.setitem(tab.SPLITS, "test", ("echr_test.json", "0" * 64))
    with respx.mock() as mock:
        mock.get(url__regex=r".*/echr_test\.json").mock(
            return_value=httpx.Response(200, content=body)
        )
        with pytest.raises(ValueError, match="SHA-256"):
            tab.fetch(directory=tmp_path)
    assert not (tmp_path / "echr_test.json").exists()


def test_a_file_already_present_with_the_right_checksum_is_not_downloaded_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = b"[]"
    (tmp_path / "echr_test.json").write_bytes(body)
    digest = hashlib.sha256(body).hexdigest()
    monkeypatch.setitem(tab.SPLITS, "test", ("echr_test.json", digest))
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(url__regex=r".*")
        assert tab.fetch(directory=tmp_path) == tmp_path / "echr_test.json"
        assert not route.called


def test_the_download_is_pinned_to_a_commit_not_a_branch() -> None:
    assert len(tab.TAB_COMMIT) == 40 and "master" not in tab.TAB_URL


# -- redact_with_spans ---------------------------------------------------------------------


def test_redact_with_spans_returns_what_redact_returns_and_the_ranges_it_replaced() -> None:
    corpus = build(pages=40, seed=5)
    spans = sweep(corpus.pages, Analyzer().analyze(corpus.pages))
    a, b = Policy(spans), Policy(spans)
    placeholders = r"(?:<[A-Z_]+_[0-9]+(?:[.][0-9]+)?>)+"
    for page in corpus.pages:
        text, ranges = a.redact_with_spans(page)
        assert text == b.redact(page)
        assert all(s < e for s, e in ranges)
        assert all(r1[1] < r2[0] for r1, r2 in itertools.pairwise(ranges))
        # The redacted text is exactly the source outside the ranges, with one or more
        # placeholders standing in for each range: nothing else was changed.
        kept, cursor = [], 0
        for s, e in ranges:
            kept.append(page[cursor:s])
            cursor = e
        kept.append(page[cursor:])
        assert re.fullmatch(placeholders.join(re.escape(k) for k in kept), text), page[:60]
