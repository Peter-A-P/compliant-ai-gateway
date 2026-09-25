"""The mutation-rate harness: classification, the interval on the intervention's effect, and
a collection run against a fake gateway. No model and no network; the live run is a
command (`boundary redact mutation`) and its stored output is re-scored by the same code."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from boundary.redact.evaluate import Rate
from boundary.redact.mutation import (
    Call,
    MutationRun,
    classify,
    collect,
    corpus_policy,
    newcombe,
    read,
    score,
    write,
)
from boundary.types import ChatRequest


@pytest.fixture(scope="module")
def corpus() -> tuple[list[str], Any]:
    return corpus_policy(5)


def test_each_class(corpus: tuple[list[str], Any]) -> None:
    _, policy = corpus
    text = (
        "Dear <NAME_LIKE_1>, about <file_number_1>, NAME_LIKE_2, [PHONE_1] and **<EMAIL_1>**; "
        "also <NAME_LIKE_999>, Name Like 3, <EMAIL> and [PERSON]."
    )
    c = classify(text, policy)
    assert (c.exact, c.tolerated, c.unresolvable, c.degraded) == (2, 3, 1, 3)
    assert c.forms == {"lower case": 1, "brackets dropped": 1, "square brackets": 1}
    assert c.mutation.hits == 7 and c.mutation.total == 9
    assert c.unrecoverable.hits == 4


def test_a_title_the_page_did_not_have_is_counted(corpus: tuple[list[str], Any]) -> None:
    _, policy = corpus
    page = "The applicant, <NAME_LIKE_1>, wrote to Dr. <NAME_LIKE_2>."
    c = classify("Dear Mr. <NAME_LIKE_1>, Dr. <NAME_LIKE_2> has replied.", policy, page)
    assert c.titled == 1, "Dr. was on the page; Mr. was the model's guess"


def test_an_abbreviation_used_as_a_label_is_not_degraded(corpus: tuple[list[str], Any]) -> None:
    """The first live run's models wrote "SIN: <SIN_1>"; the label is not a mutation."""
    _, policy = corpus
    c = classify("MCP: <HEALTH_NUMBER_1>, SIN: <SIN_1>; RE: FILE NUMBER 7 and NAME_LIKE", policy)
    assert c.degraded == 2, "FILE NUMBER 7 and a bare NAME_LIKE, not the SIN label"


def test_loss_counts_only_what_the_prompt_asked_for(corpus: tuple[list[str], Any]) -> None:
    _, policy = corpus
    page = "<NAME_LIKE_1>, born <DATE_OF_BIRTH_1>, SIN <SIN_1>, file <FILE_NUMBER_1>."
    c = classify("<NAME_LIKE_1>\n<FILE_NUMBER_1>", policy, page)
    assert (c.expected, c.kept) == (2, 2)


def test_ordinary_prose_is_not_a_placeholder(corpus: tuple[list[str], Any]) -> None:
    _, policy = corpus
    c = classify("Please send your email and phone number to the person at the desk.", policy)
    assert c.tokens == 0


def test_loss_counts_the_pages_placeholders_that_did_not_come_back(
    corpus: tuple[list[str], Any],
) -> None:
    pages, policy = corpus
    page = pages[0]
    everything = classify(page, policy, page)
    assert everything.expected > 3 and everything.kept == everything.expected
    assert everything.loss.hits == 0
    nothing = classify("I could not find anyone.", policy, page)
    assert nothing.loss.hits == nothing.expected


def test_newcombe_matches_the_published_example() -> None:
    """Newcombe (1998), method 10, the worked example 56/70 against 48/80: 0.2000 with
    interval 0.0524 to 0.3339."""
    d, lo, hi = newcombe(Rate(56, 70), Rate(48, 80))
    assert d == pytest.approx(0.2)
    assert lo == pytest.approx(0.0524, abs=5e-4)
    assert hi == pytest.approx(0.3339, abs=5e-4)


@dataclass
class _Resp:
    status: int
    text: str
    cost_usd: float
    call_uid: str


class _EchoGateway:
    """Answers each request with its own last line of placeholders, lower-cased when the
    preserve line is absent, so the two arms differ in a known way."""

    def __init__(self, cost: float) -> None:
        self.cost = cost
        self.calls = 0

    async def achat(self, request: ChatRequest, **_kw: Any) -> _Resp:
        self.calls += 1
        body = str(request.messages[0]["content"]).split("---\n", 1)[1]
        preserve = "Copy every placeholder" in (request.system or "")
        return _Resp(200, body if preserve else body.lower(), self.cost, f"uid{self.calls}")


async def test_collect_and_score_see_the_arms_differ(tmp_path: Path) -> None:
    gw = _EchoGateway(cost=0.0001)
    run = await collect(gw, ["fake/model"], pages=3, run_id="t", max_usd=1.0)
    assert len(run.calls) == 2 * 2 * 3 == gw.calls
    write(tmp_path / "m.json", run)
    scored = score(read(tmp_path / "m.json"))
    plain = scored.by_arm[("fake/model", "plain")]
    keep = scored.by_arm[("fake/model", "preserve")]
    assert keep.mutation.hits == 0 and keep.tokens > 0
    assert plain.mutation.hits == plain.tokens > 0
    assert scored.by_task[("fake/model", "preserve", "extract")].loss.hits == 0
    assert "fake/model" in scored.table() and "fake/model" in scored.readme_rows()


async def test_collect_stops_sending_at_the_cap() -> None:
    gw = _EchoGateway(cost=0.01)
    run = await collect(gw, ["fake/model"], pages=10, run_id="t", max_usd=0.03, concurrency=1)
    assert gw.calls == 3, "calls stop once the returned cost reaches the cap"
    assert len(run.calls) == 3


def test_a_failed_call_is_counted_not_scored() -> None:
    run = MutationRun(
        "x",
        "t",
        2,
        20260920,
        "h",
        [Call("m", "plain", "reply", 0, "ProviderError", None, None, None)],
    )
    assert score(run).failed == 1
