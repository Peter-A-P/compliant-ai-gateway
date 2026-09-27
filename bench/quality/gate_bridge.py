"""Project 03's code, run in 03's own environment, for PLAN.md B2.8.

The quality measurement has to be graded by 03's instrument and nothing else: its answer
prompt, its judge prompt, its verdict parser, its calibration counts, its paired bootstrap
and its red-team grader. 03 pins boundary 0.1.0 and installs dependencies 04 does not, so
its modules cannot be imported into 04's environment, and copying them would make a second
instrument that only looks like the first. So this script is run with 03's interpreter and
does no vendor call and no network read except `fetch`, which is 03's own page fetcher.
Every vendor call is made by `boundary.redact.quality`, through this repository's gateway
and ledger.

    <03 checkout>/.venv/bin/python bench/quality/gate_bridge.py <command> <request.json>

Each command reads one JSON request file and prints one JSON document on stdout.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def answer_prompts(req: dict[str, Any]) -> dict[str, Any]:
    """03's answer system prompt and user message for each question, with the question text
    replaced where `questions` gives a new one (the personalised variant)."""
    from gate import generate
    from gate import gold as g

    root = Path(req["gold"])
    sources = {s.id: s for s in g.read_sources(root / g.SOURCES_FILE)}
    replace: dict[str, str] = req.get("questions") or {}
    # A question served against another page (03's distractor stratum): question id to the
    # source id served, so the prompt is byte for byte the one 03's d- instances were sent.
    served: dict[str, str] = req.get("served") or {}
    out = {}
    for q in g.read_questions(root / g.QUESTIONS_FILE):
        if served:
            if q.id not in served:
                continue
            out[q.id] = {
                "source_id": served[q.id],
                "system": generate.ANSWER_SYSTEM,
                "prompt": generate.answer_prompt(sources[served[q.id]], q),
            }
            continue
        if replace:
            if q.id not in replace:
                continue
            q = q.model_copy(update={"question": replace[q.id]})
        out[q.id] = {
            "source_id": q.source_id,
            "system": generate.ANSWER_SYSTEM,
            "prompt": generate.answer_prompt(sources[q.source_id], q),
        }
    return {
        "max_tokens": generate.MAX_TOKENS,
        "temperature": generate.TEMPERATURE,
        "prompts": out,
    }


def judge_prompts(req: dict[str, Any]) -> dict[str, Any]:
    """03's judge prompt for each answer, against the ORIGINAL question and source, so that
    across arms the only thing that differs in what the judge reads is the answer."""
    from gate import gold as g
    from gate.judge import rubric

    root = Path(req["gold"])
    sources = {s.id: s for s in g.read_sources(root / g.SOURCES_FILE)}
    questions = {q.id: q for q in g.read_questions(root / g.QUESTIONS_FILE)}
    out = {}
    for inst in req["instances"]:
        q = questions[inst["question_id"]]
        out[inst["id"]] = rubric.judge_prompt(sources[q.source_id], q, inst["output"])
    return {"system": rubric.SYSTEM, "rubric_hash": rubric.rubric_hash(), "prompts": out}


def verdicts(req: dict[str, Any]) -> dict[str, Any]:
    """03's verdict records from the judge's raw replies, exactly as `gate judge run` stores
    them."""
    from gate import gold as g
    from gate.judge import rubric

    config = rubric.JudgeConfig(**req["judge"])
    out = []
    for r in req["replies"]:
        inst = g.AnswerInstance.model_validate(r["instance"])
        v = rubric.verdict_from_text(
            instance=inst,
            config=config,
            text=r["text"] or "",
            prompt=r["prompt"],
            model_returned=r["model_returned"],
            finish_reason=r["finish_reason"],
            latency_ms=r["latency_ms"],
            cost_usd=r["cost_usd"],
            judged_utc=r["judged_utc"],
        )
        out.append(json.loads(v.model_dump_json()))
    return {"verdicts": out}


def calibration(req: dict[str, Any]) -> dict[str, Any]:
    """The judge's counts on completeness, recomputed from 03's stored labels and verdicts,
    and checked against the constant 03's own A/A study uses."""
    from gate import gold as g
    from gate.judge import calibration as calib
    from gate.judge import runner
    from gate.live_aa import JUDGE_COUNTS

    root = Path(req["gold"])
    key = req["judge_key"]
    labels = {lab.instance_id: lab for lab in g.read_labels(root / g.LABELS_FILE)}
    mine = [v for v in runner.read_verdicts(root / runner.VERDICTS_FILE) if v.judge_key == key]
    c = calib.calibrate(labels, mine, judge_key=key)
    task = next(t for t in c.tasks if t.task == "complete")
    counts = task.counts
    got = (
        counts.true_positive,
        counts.false_negative,
        counts.false_positive,
        counts.true_negative,
    )
    return {
        "counts": list(got),
        "matches_03_constant": got == tuple(JUDGE_COUNTS),
        "kappa": task.kappa.point,
        "kappa_lo": task.kappa.lo,
        "kappa_hi": task.kappa.hi,
    }


def paired(req: dict[str, Any]) -> dict[str, Any]:
    """03's paired bootstrap, candidate minus baseline, judge-corrected, for each comparison."""
    from gate.stats import paired_difference

    counts = tuple(req["judge_counts"]) if req.get("judge_counts") else None
    out = {}
    for name, c in req["comparisons"].items():
        t = paired_difference(
            c["baseline"],
            c["candidate"],
            delta=req.get("delta", 0.10),
            resamples=req.get("resamples", 4000),
            seed=req.get("seed", 0),
            judge_counts=counts,  # type: ignore[arg-type]
        )
        out[name] = {
            "n": t.paired_items,
            "point": t.difference.point,
            "lo": t.difference.lo,
            "hi": t.difference.hi,
            "worse": t.worse,
            "better": t.better,
            "mcnemar_p": t.mcnemar_p,
            "youden": t.judge_youden,
            "dropped_resamples": t.dropped_resamples,
        }
    return {"tests": out}


def redteam_items(req: dict[str, Any]) -> dict[str, Any]:
    """03's pii_leakage suite, and the settings 03's red-team run sends it with."""
    from gate.redteam import run, suite

    items = [i for i in suite.load_suite() if i.suite == "pii_leakage"]
    return {
        "suite_hash": suite.committed_hash(),
        "max_tokens": run.MAX_TOKENS,
        "temperature": run.TEMPERATURE,
        "items": [json.loads(i.model_dump_json()) for i in items],
    }


def redteam_grade(req: dict[str, Any]) -> dict[str, Any]:
    """03's `withholds_pii` grader over each text, as `gate redteam` grades an answer."""
    from gate.redteam import graders, suite

    items = {i.id: i for i in suite.load_suite() if i.suite == "pii_leakage"}
    out = []
    for a in req["answers"]:
        o = graders.grade(
            items[a["item_id"]], text=a["text"], ok=a["ok"], finish_reason=a["finish_reason"]
        )
        out.append({"key": a["key"], "passed": o.passed, "detail": o.detail})
    return {"graders_hash": graders.REDTEAM_GRADERS_HASH, "grades": out}


def fetch(req: dict[str, Any]) -> dict[str, Any]:
    """03's page fetcher and passage extractor over a source spec."""
    from gate import sources

    got = sources.fetch_all(sources.load_specs(Path(req["spec"])))
    return {
        "pages": [json.loads(f.document.model_dump_json()) for f in got if f.document is not None],
        "failed": {f.spec.id: f.error for f in got if f.document is None},
    }


COMMANDS = {
    "answer-prompts": answer_prompts,
    "judge-prompts": judge_prompts,
    "verdicts": verdicts,
    "calibration": calibration,
    "paired": paired,
    "redteam-items": redteam_items,
    "redteam-grade": redteam_grade,
    "fetch": fetch,
}


def main() -> int:
    command, request = sys.argv[1], Path(sys.argv[2])
    result = COMMANDS[command](json.loads(request.read_text(encoding="utf-8")))
    json.dump(result, sys.stdout, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
