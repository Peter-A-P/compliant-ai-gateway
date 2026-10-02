"""The semantic cache replayed on recorded traffic (0.36): request rebuilding, the replay's
rules and labels, and reading a drift tree, with no embedder downloaded."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from boundary.semcache import scope_of
from boundary.semcache_replay import Call, families, load, rebuild, replay, summarise


def _call(
    i: int,
    question: str,
    *,
    family: str | None = None,
    block: str = "closed_form_reasoning",
    scope: str = "s",
    storable: bool = True,
    flagged: bool = False,
    cost: float | None = 0.01,
) -> Call:
    return Call(
        ts_utc=f"2026-09-12T00:00:{i:02d}Z",
        run="r",
        arm="a",
        block=block,
        item=f"item-{question}",
        family=family or f"item-{question}",
        scope=scope,
        question=question,
        flagged=flagged,
        storable=storable,
        cost_usd=cost,
    )


# Similarities between named questions; a question is identical to itself.
_SIMS = {frozenset({"p1", "p2"}): 0.95, frozenset({"tan45", "tan135"}): 0.92}


def _sims(a: str, b: str) -> float:
    return 1.0 if a == b else _SIMS.get(frozenset({a, b}), 0.1)


def _all(_c: Call) -> bool:
    return True


def test_exact_semantic_and_false_hits_are_told_apart() -> None:
    calls = [
        _call(0, "p1", family="reason-1"),
        _call(1, "p1", family="reason-1"),
        _call(2, "p2", family="reason-1"),
        _call(3, "tan135"),
        _call(4, "tan45"),
    ]
    out = replay(calls, _sims, threshold=0.9, marked=_all)
    assert [o.hit for o in out] == [False, True, True, False, True]
    assert out[1].exact and out[1].correct
    assert not out[2].exact and out[2].correct  # a paraphrase of the same item
    assert not out[4].exact and not out[4].correct  # a different question
    assert out[4].source == 3


def test_a_hit_is_never_stored_so_later_repeats_hit_the_same_source() -> None:
    calls = [_call(0, "p1", family="f"), _call(1, "p2", family="f"), _call(2, "p2", family="f")]
    out = replay(calls, _sims, threshold=0.9, marked=_all)
    assert out[1].source == 0 and out[2].source == 0 and not out[2].exact


def test_an_answer_that_is_not_whole_is_not_stored() -> None:
    calls = [_call(0, "q", storable=False), _call(1, "q")]
    out = replay(calls, _sims, threshold=0.9, marked=_all)
    assert not out[1].hit


def test_unmarked_and_flagged_requests_are_not_looked_up_or_stored() -> None:
    calls = [
        _call(0, "q", block="long_context_recall"),
        _call(1, "q", flagged=True),
        _call(2, "q"),
    ]
    out = replay(calls, _sims, threshold=0.9, marked=lambda c: c.block != "long_context_recall")
    assert [o.looked_up for o in out] == [False, False, True]
    assert not out[2].hit


def test_scopes_are_kept_apart() -> None:
    calls = [_call(0, "q", scope="a"), _call(1, "q", scope="b")]
    assert not replay(calls, _sims, threshold=0.9, marked=_all)[1].hit


def test_the_threshold_moves_the_false_hit() -> None:
    calls = [_call(0, "tan135"), _call(1, "tan45")]
    assert replay(calls, _sims, threshold=0.9, marked=_all)[1].hit
    assert not replay(calls, _sims, threshold=0.95, marked=_all)[1].hit


def test_the_summary_counts_pairs_once_and_sums_the_saving() -> None:
    pytest.importorskip("numpy")
    calls = [
        _call(0, "tan135", cost=0.5),
        *(_call(i, "tan45", cost=0.25) for i in range(1, 4)),
        _call(4, "p1", family="f", cost=1.0),
        _call(5, "p2", family="f", cost=1.0),
    ]
    out = replay(calls, _sims, threshold=0.9, marked=_all)
    s = summarise("x", calls, out, 0.9)
    assert (s.hits, s.exact_hits, s.semantic_hits, s.semantic_false) == (4, 0, 4, 3)
    assert (s.pairs, s.pairs_false) == (2, 1)
    assert s.spend_usd == pytest.approx(3.25)
    assert s.saved_usd == pytest.approx(1.75)
    assert s.saved_semantic_usd == pytest.approx(1.75)
    assert s.false_of_semantic.startswith("75.0%")


def test_rebuilt_requests_share_a_scope_only_with_the_same_settings() -> None:
    anthropic = rebuild(
        "anthropic/claude-haiku-4-5",
        {
            "model": "claude-haiku-4-5",
            "system": "Solve it.",
            "max_tokens": 64,
            "messages": [{"role": "user", "content": "What is 2+2?"}],
        },
    )
    assert anthropic.system == "Solve it."
    assert anthropic.messages[-1]["content"] == "What is 2+2?"
    other = rebuild(
        "anthropic/claude-haiku-4-5",
        {
            "model": "claude-haiku-4-5",
            "system": "Solve it.",
            "max_tokens": 64,
            "messages": [{"role": "user", "content": [{"type": "text", "text": "And 3+3?"}]}],
        },
    )
    assert scope_of(anthropic) == scope_of(other)
    longer = rebuild(
        "anthropic/claude-haiku-4-5",
        {
            "system": "Solve it.",
            "max_tokens": 128,
            "messages": [{"role": "user", "content": "What is 2+2?"}],
        },
    )
    assert scope_of(anthropic) != scope_of(longer)
    openai = rebuild(
        "openai/gpt-5.4-mini",
        {
            "model": "gpt-5.4-mini",
            "messages": [
                {"role": "system", "content": "Solve it."},
                {"role": "user", "content": "What is 2+2?"},
            ],
        },
    )
    assert openai.system == "Solve it." and len(openai.messages) == 1
    google = rebuild(
        "google/gemini-flash-latest",
        {
            "contents": [{"role": "user", "parts": [{"text": "What is 2+2?"}]}],
            "systemInstruction": {"parts": [{"text": "Solve it."}]},
            "generationConfig": {"maxOutputTokens": 64},
        },
    )
    assert google.system == "Solve it."
    assert google.messages == [{"role": "user", "content": "What is 2+2?"}]


def _drift(tmp: Path) -> Path:
    drift = tmp / "drift"
    suite = drift / "suite" / "v1"
    suite.mkdir(parents=True)
    (suite / "items.jsonl").write_text(
        json.dumps({"id": "reason-1", "parent_id": None})
        + "\n"
        + json.dumps({"id": "para-1-p1", "parent_id": "reason-1"})
        + "\n",
        encoding="utf-8",
    )
    arm = drift / "runs" / "2026-09" / "anthropic-alias"
    (arm / "raw" / "drift-2026-09").mkdir(parents=True)
    records, raws = [], []
    rows = [
        (1, "reason-1", "What is 2+2?", "end_turn", 200),
        (2, "para-1-p1", "What do 2 and 2 make?", "max_tokens", 200),
        (3, "reason-1", "What is 2+2?", None, 503),
    ]
    for lid, item, q, finish, status in rows:
        records.append(
            {
                "ledger_id": lid,
                "item_id": item,
                "block": "closed_form_reasoning",
                "model_requested": "anthropic/claude-haiku-4-5",
                "status": status,
                "finish_reason": finish,
                "output": "4" if status == 200 else None,
                "cost_usd": 0.001 if status == 200 else 0.0,
                "costed": True,
            }
        )
        body = {"model": "claude-haiku-4-5", "messages": [{"role": "user", "content": q}]}
        raws.append(
            {
                "ledger_id": lid,
                "ts_utc": f"2026-09-12T00:00:0{lid}Z",
                "request": {"body": {"text": json.dumps(body)}},
            }
        )
    (arm / "records.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in records), encoding="utf-8"
    )
    (arm / "raw" / "drift-2026-09" / "anthropic.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in raws), encoding="utf-8"
    )
    return drift


def test_a_drift_tree_is_read_in_order_with_families_and_storability(tmp_path: Path) -> None:
    drift = _drift(tmp_path)
    assert families(drift / "suite" / "v1") == {"reason-1": "reason-1", "para-1-p1": "reason-1"}
    calls = load(drift)
    assert [c.item for c in calls] == ["reason-1", "para-1-p1", "reason-1"]
    assert [c.family for c in calls] == ["reason-1"] * 3
    assert [c.storable for c in calls] == [True, False, False]  # cut off; failed
    assert len({c.scope for c in calls}) == 1
    assert calls[1].question == "What do 2 and 2 make?"
