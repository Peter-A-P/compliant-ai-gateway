"""The injection screen measured (0.22, PLAN.md B2.6).

Detection rate on sets of injections and false-positive rate on sets of ordinary requests,
each with a Wilson interval, per set rather than pooled, because the sets answer different
questions:

| Set | What it is | Counts as |
|---|---|---|
| deepset test | deepset/prompt-injections, test split (Apache 2.0), English and German | both |
| 03 prompt_injection | PromptInject attacks hidden at the start, middle or end of a regulator page, with a question | injection |
| 03 pii_leakage | PromptInject prompt-leaking attacks as the user message | injection |
| 03 gold questions | 03's 100 consumer questions with their regulator pages, as 03's answer prompt sends them | ordinary |
| 03 over_refusal | XSTest safe prompts that look unsafe ("How can I kill a Python process?") | ordinary |
| 03 jailbreak | HarmBench requests for harm; not injection, so reported as a flag rate and in neither total |

deepset's train split is the development set and is reported beside the rest, marked as
such. Everything else was run once, after the rules were frozen.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from boundary.redact.evaluate import Rate
from boundary.screen import rules_fired, screen_text

# 03's answer prompt (gate/generate.py), so the ordinary set is the text 03 actually sends.
ANSWER_TEMPLATE = "DOCUMENT ({title})\n---\n{source}\n---\n\nQUESTION\n{question}"


@dataclass
class SetResult:
    name: str
    kind: str  # injection, ordinary, both, other
    development: bool = False
    positives: int = 0
    detected: int = 0
    negatives: int = 0
    flagged: int = 0
    by_rule: dict[str, int] = field(default_factory=dict)
    examples_missed: list[str] = field(default_factory=list)
    examples_flagged: list[str] = field(default_factory=list)

    @property
    def detection(self) -> Rate:
        return Rate(self.detected, self.positives)

    @property
    def false_positive(self) -> Rate:
        return Rate(self.flagged, self.negatives)


def _score(
    name: str, kind: str, items: Iterable[tuple[str, bool]], *, dev: bool = False
) -> SetResult:
    r = SetResult(name, kind, dev)
    for text, is_injection in items:
        fired = rules_fired(screen_text(text))
        for rule in fired:
            r.by_rule[rule] = r.by_rule.get(rule, 0) + 1
        if is_injection:
            r.positives += 1
            r.detected += bool(fired)
            if not fired and len(r.examples_missed) < 5:
                r.examples_missed.append(text[:120])
        else:
            r.negatives += 1
            r.flagged += bool(fired)
            if fired and len(r.examples_flagged) < 5:
                r.examples_flagged.append(f"{','.join(fired)}: {text[:120]}")
    return r


def _jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(x) for x in path.open(encoding="utf-8") if x.strip()]


@dataclass
class ScreenResults:
    boundary_version: str
    sets: list[SetResult]

    def table(self) -> str:
        lines = [
            f"boundary {self.boundary_version}, injection screen (rules), detection and "
            "false-positive rates per set, Wilson 95%",
            "",
            f"{'set':<30}{'detected':<34}{'false positives':<34}",
        ]
        for s in self.sets:
            det = f"{s.detection} of {s.positives}" if s.positives else ""
            fp = f"{s.false_positive} of {s.negatives}" if s.negatives else ""
            if s.kind == "other":
                fp = f"flagged {s.false_positive} of {s.negatives} (not injection)"
            name = s.name + (" (development)" if s.development else "")
            lines.append(f"{name:<30}{det:<34}{fp:<34}")
        lines.append("")
        for s in self.sets:
            rules = ", ".join(f"{k} {v}" for k, v in sorted(s.by_rule.items()))
            lines.append(f"{s.name}: rules fired: {rules or 'none'}")
            for e in s.examples_flagged:
                lines.append(f"  flagged  {e}")
            for e in s.examples_missed[:3]:
                lines.append(f"  missed   {e}")
        return "\n".join(lines)

    def readme_rows(self) -> str:
        rows = []
        for s in self.sets:
            det = f"{s.detected} of {s.positives}, {s.detection}" if s.positives else ""
            fp = f"{s.flagged} of {s.negatives}, {s.false_positive}" if s.negatives else ""
            if s.kind == "other":
                fp = f"{s.flagged} of {s.negatives} flagged; not injection"
            name = s.name + (" (development set)" if s.development else "")
            rows.append(f"| {name} | {det} | {fp} |")
        return "\n".join(rows)


def run(injection_dir: Path, gate: Path) -> ScreenResults:
    """`injection_dir` holds deepset's two splits; `gate` is the 03 checkout."""
    from boundary import __version__

    sets: list[SetResult] = []
    for split, dev in (("train", True), ("test", False)):
        rows = _jsonl(injection_dir / f"deepset-{split}.jsonl")
        sets.append(
            _score(
                f"deepset {split}",
                "both",
                ((str(r["text"]), bool(r["label"])) for r in rows),
                dev=dev,
            )
        )
    suite = gate / "gate" / "redteam" / "suite" / "v1"
    inj = _jsonl(suite / "prompt_injection.jsonl")
    sets.append(_score("03 prompt_injection", "injection", ((str(i["prompt"]), True) for i in inj)))
    pii = _jsonl(suite / "pii_leakage.jsonl")
    sets.append(_score("03 pii_leakage", "injection", ((str(i["prompt"]), True) for i in pii)))
    gold = gate / "gate" / "gold"
    sources = {str(s["id"]): s for s in _jsonl(gold / "sources.jsonl")}
    questions = _jsonl(gold / "questions.jsonl")
    sets.append(
        _score(
            "03 gold questions",
            "ordinary",
            (
                (
                    ANSWER_TEMPLATE.format(
                        title=sources[str(q["source_id"])]["title"],
                        source=str(sources[str(q["source_id"])]["text"]).strip(),
                        question=str(q["question"]).strip(),
                    ),
                    False,
                )
                for q in questions
            ),
        )
    )
    over = _jsonl(suite / "over_refusal.jsonl")
    sets.append(_score("03 over_refusal", "ordinary", ((str(i["prompt"]), False) for i in over)))
    jb = _jsonl(suite / "jailbreak.jsonl")
    sets.append(_score("03 jailbreak", "other", ((str(i["prompt"]), False) for i in jb)))
    return ScreenResults(__version__, sets)


README_START = "<!-- screen:start -->"
README_END = "<!-- screen:end -->"


def write_readme(readme: Path, rows: str) -> None:
    text = readme.read_text(encoding="utf-8")
    a, b = text.index(README_START), text.index(README_END)
    readme.write_text(
        text[: a + len(README_START)] + "\n" + rows + "\n" + text[b:], encoding="utf-8"
    )


__all__ = ["ScreenResults", "SetResult", "run", "write_readme"]
