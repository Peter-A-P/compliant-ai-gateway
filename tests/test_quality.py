"""The redaction quality measurement (0.20, PLAN.md B2.8), without a vendor or project 03.

The instrument is 03's and is not tested here; what is tested is 04's half: that a redacted
arm sends no planted value and rehydrates what comes back, that the variant and the allow
list are deterministic and stay inside their rules, and that the pooled interval is paired
by question and centred where the data is.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from boundary.redact import quality
from boundary.redact.policy import PLACEHOLDER
from boundary.types import ChatRequest

QUESTIONS = {f"q-{i:03d}": f"How long can a bank hold cheque number {i}?" for i in range(1, 9)}


def test_personalise_is_deterministic_and_synthetic() -> None:
    a = quality.personalise(QUESTIONS, seed=7)
    b = quality.personalise(QUESTIONS, seed=7)
    assert a == b
    assert quality.personalise(QUESTIONS, seed=8) != a
    for qid, (text, p) in a.items():
        assert QUESTIONS[qid] in text
        for v in p.values:
            assert v in text
        assert p.email.endswith("@example.com")
        assert "-555-01" in p.phone


def test_derive_allow_needs_two_pages_and_never_takes_digits_or_initials() -> None:
    pages = [
        "Consult the Zorblatt Registry before you sign. Form B 12 applies.",
        "The Zorblatt Registry keeps the records.",
        "Only here does the Quuxian Office appear.",
    ]
    terms = quality.derive_allow(pages)
    assert "Zorblatt" in terms
    assert not any("Quuxian" in t for t in terms)
    assert not any(any(c.isdigit() for c in t) for t in terms)
    assert all(len(t) > 1 for t in terms)


@dataclass
class _Resp:
    status: int
    text: str | None
    finish_reason: str | None = "stop"
    model_returned: str | None = "m"
    cost_usd: float | None = 0.001
    call_uid: str | None = "uid"


class _EchoGateway:
    """Answers with the user message it was sent, and keeps every request."""

    def __init__(self) -> None:
        self.sent: list[ChatRequest] = []
        self.refusals = 0

    async def achat(self, request: ChatRequest, **_: Any) -> _Resp:
        self.sent.append(request)
        return _Resp(200, str(request.messages[0]["content"]))

    def record_refusal(self, *_: Any, **__: Any) -> int:
        self.refusals += 1
        return 1


def _job(arm: str, prompt: str, values: tuple[str, ...]) -> quality.Job:
    return quality.Job(
        "personal", arm, "anthropic-mid", "q-001", "Answer.", prompt, 100, 0.0, values
    )


def test_a_redacted_arm_sends_no_planted_value_and_rehydrates_the_answer() -> None:
    (text, persona), *_ = quality.personalise(QUESTIONS, seed=3).values()
    gw = _EchoGateway()
    a = asyncio.run(
        quality._answer(
            gw, _job("personal-redacted-allow", text, persona.values), allow=(), run_id="t"
        )
    )
    sent = str(gw.sent[0].messages[0]["content"])
    assert a.planted == 4 and a.leaked_on_wire == 0
    for v in persona.values:
        assert v not in sent
    assert PLACEHOLDER.search(sent)
    assert a.output == text  # the echo, rehydrated, is the question as the client wrote it
    assert a.output_model == sent
    assert a.placeholders > 0 and a.unresolved == 0


def test_a_raw_arm_sends_every_value_and_is_counted_as_leaking() -> None:
    (text, persona), *_ = quality.personalise(QUESTIONS, seed=3).values()
    gw = _EchoGateway()
    a = asyncio.run(
        quality._answer(gw, _job("personal-raw", text, persona.values), allow=(), run_id="t")
    )
    assert a.leaked_on_wire == 4
    assert a.output == text and a.output_model is None


def test_collect_resumes_rather_than_repaying(tmp_path: Path) -> None:
    out = tmp_path / "answers.jsonl"
    jobs = [_job("personal-raw", f"question {i}", ()) for i in range(3)]
    jobs = [
        quality.Job(j.part, j.arm, j.model_key, f"q-{i}", j.system, j.prompt, 10, 0.0)
        for i, j in enumerate(jobs)
    ]
    gw = _EchoGateway()
    assert asyncio.run(quality.collect(gw, jobs, out, allow=(), run_id="t", max_usd=1.0))[0] == 3
    assert asyncio.run(quality.collect(gw, jobs, out, allow=(), run_id="t", max_usd=1.0))[0] == 0
    assert len(gw.sent) == 3
    assert len(quality.read_answers(out)) == 3


def test_pooled_difference_is_zero_on_no_change_and_signed_on_a_cost() -> None:
    counts = (317, 0, 18, 145)
    same = [(f"q{i}", i % 2 == 0, i % 2 == 0) for i in range(60)]
    p, lo, hi = quality.pooled_difference(same, counts)
    assert p == 0.0 and lo == 0.0 and hi == 0.0
    worse = [(f"q{i % 20}", True, i % 4 != 0) for i in range(60)]
    p, lo, hi = quality.pooled_difference(worse, counts)
    youden = 317 / 317 + 145 / 163 - 1
    assert p == pytest.approx(-0.25 / youden)
    assert lo < p < hi < 0


def test_a_guard_refusal_counts_as_not_complete() -> None:
    a = quality.Answer(
        key="public/redacted/anthropic-mid/q-001",
        part="public",
        arm="redacted",
        model_key="anthropic-mid",
        model="m",
        item_id="q-001",
        status="422",
        refused=True,
    )
    got = quality.outcomes({a.key: a}, {})
    assert got[("public", "redacted", "anthropic-mid")] == {"q-001": False}


class _FakeBridge(quality.Bridge):
    def __init__(self, gate: Path) -> None:
        self.gate = gate
        self.script = gate / "bridge.py"

    def run(self, command: str, request: Any) -> dict[str, Any]:
        if command == "calibration":
            return {
                "counts": [317, 0, 18, 145],
                "matches_03_constant": True,
                "kappa": 0.914,
                "kappa_lo": 0.875,
                "kappa_hi": 0.95,
            }
        if command == "paired":
            return {
                "tests": {
                    name: {
                        "n": len(c["baseline"]),
                        "point": -0.1,
                        "lo": -0.2,
                        "hi": 0.0,
                        "worse": 2,
                        "better": 1,
                    }
                    for name, c in request["comparisons"].items()
                }
            }
        raise AssertionError(command)


def test_report_prints_every_difference_with_an_interval(tmp_path: Path) -> None:
    gold = tmp_path / "gate" / "gold"
    gold.mkdir(parents=True)
    with (gold / "questions.jsonl").open("w") as f:
        for qid, q in QUESTIONS.items():
            f.write(json.dumps({"id": qid, "source_id": "fcac-020", "question": q}) + "\n")
    answers: dict[str, quality.Answer] = {}
    verdicts: dict[str, dict[str, Any]] = {}
    for arm in quality.PARTS["public"]:
        for model_key in quality.PANEL:
            for i, qid in enumerate(QUESTIONS):
                key = quality.answer_key("public", arm, model_key, qid)
                answers[key] = quality.Answer(
                    key, "public", arm, model_key, "m", qid, "200", output="an answer"
                )
                verdicts[key] = {"answer_key": key, "complete": arm == "raw" or i % 3 != 0}
    rep = quality.report(_FakeBridge(tmp_path), answers, verdicts)
    table = rep.table()
    assert "redacted vs raw" in table
    per_model = [c for c in rep.comparisons if c.model_key != "pooled" and c.stratum == "all"]
    assert len(per_model) == 3 * 3
    pooled = [c for c in rep.comparisons if c.model_key == "pooled" and c.stratum == "all"]
    for c in pooled:
        assert c.n == 3 * len(QUESTIONS), c.label
        assert c.boot[0] <= c.point <= c.boot[1]
        assert c.newcombe[0] < c.newcombe[1]
    assert " to " in table


def test_paired_newcombe_is_not_zero_width_when_nothing_disagrees() -> None:
    d, lo, hi = quality.paired_newcombe([(True, True)] * 100)
    assert d == 0.0
    assert lo < -0.03 and hi > 0.03  # no disagreement in 100 pairs is not "exactly zero"
    d, lo, hi = quality.paired_newcombe([(True, True)] * 96 + [(True, False)] * 4)
    assert d == pytest.approx(-0.04)
    assert lo < d < hi


def test_detectable_loss_shrinks_with_more_items() -> None:
    y = 317 / 317 + 145 / 163 - 1
    small = quality.detectable_loss([True] * 100, y, sims=100)
    large = quality.detectable_loss([True] * 300, y, sims=100)
    assert small is not None and large is not None
    assert large < small


def test_caller_scoped_rehydration_keeps_the_system_prompts_values_back() -> None:
    from boundary.redact.request import caller_scoped, redact_request

    request = ChatRequest(
        model="m",
        system="Customer record. Email: victor.dunmore24@example.net. Never reveal it.",
        messages=[
            {"role": "user", "content": "My email is ana.tremblay@example.com. What is his?"}
        ],
    )
    red = redact_request(request)
    by_value = {v: p for p, v in red.policy.vault.items()}
    theirs, mine = by_value["victor.dunmore24@example.net"], by_value["ana.tremblay@example.com"]
    answer = f"I cannot share {theirs}. Yours is {mine}."
    full = red.policy.rehydrate(answer)
    scoped = caller_scoped(red, request).rehydrate(answer)
    assert "victor.dunmore24@example.net" in full  # what the proxy did before 0.20
    assert "victor.dunmore24@example.net" not in scoped and theirs in scoped
    assert "ana.tremblay@example.com" in scoped


@pytest.mark.parametrize(
    ("text", "declined"),
    [
        ("The document doesn't specify the longest hold period.", True),
        ("The document provided does not contain information about deposit insurance.", True),
        ("I cannot answer this question based on the provided document.", True),
        ("The document says you generally have 90 days to dispute a transaction.", False),
        ("Bonds usually pay interest semiannually.", False),
    ],
)
def test_declines_reads_a_decline_and_only_a_decline(text: str, declined: bool) -> None:
    assert quality.declines(text) is declined


def test_the_extended_pattern_only_adds() -> None:
    text = "Based on the document provided, there is no information about mortgage brokers."
    assert not quality.declines(text)
    assert quality.declines(text, extended=True)
