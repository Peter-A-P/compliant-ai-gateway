"""Over-masking of context the answer needs, measured without a model (0.16).

The proxy redacts a whole request, and a question answered from a document carries the
document with it. The rules-only second pass masks anything capitalised that the vocabulary
does not know, so on public text it can mask the very terms an answer has to use. This counts
that, offline, on project 03's gold set: 100 consumer questions, each answered from a
regulator page and judged on a list of phrases it must mention (`must_mention`).

A phrase counts as **masked** when it is in the source page and no longer in the page as the
proxy would send it (the question is left out of that comparison, so a phrase the question
repeats cannot count as surviving). It is the input side of PLAN.md B2.8's question, and it predicts rather than
measures the quality cost: a model may still answer from what survives, and a masked phrase
can come back through rehydration. It does say where a cost would come from, before any
money is spent finding out.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from boundary.redact.evaluate import Rate
from boundary.redact.policy import PLACEHOLDER
from boundary.redact.request import redact_request
from boundary.types import ChatRequest

SYSTEM = "Answer the question from the document."


@dataclass
class OvermaskResults:
    questions: int
    refused: int
    phrases: int
    masked: int
    placeholders: list[int]
    questions_hit: int
    examples: list[tuple[str, str]] = field(default_factory=list)

    @property
    def rate(self) -> Rate:
        return Rate(self.masked, self.phrases)

    @property
    def question_rate(self) -> Rate:
        return Rate(self.questions_hit, self.questions - self.refused)

    def table(self) -> str:
        ph = sorted(self.placeholders)
        median = ph[len(ph) // 2] if ph else 0
        lines = [
            f"over-masking of the answer's context: {self.questions} gold questions, each "
            "redacted with its source page as the proxy would send it (rules only)",
            "",
            f"refused by the guard                    {self.refused}",
            f"placeholders per request                median {median}, max {max(ph, default=0)}",
            f"must_mention phrases masked             {self.rate}  ({self.masked} of {self.phrases})",
            f"questions with at least one masked      {self.question_rate}",
            "",
            "examples: " + "; ".join(f"{q} {p!r}" for q, p in self.examples[:6]),
            "",
            "The page holds no personal data. Every masked phrase is over-masking, and the "
            "quality cost it predicts is PLAN.md B2.8's to measure.",
        ]
        return "\n".join(lines)


def run(gold: Path, *, allow: Sequence[str] = ()) -> OvermaskResults:
    """`gold` is 03's `gate/gold` directory: `questions.jsonl` and `sources.jsonl`."""
    sources = {
        s["id"]: s["text"]
        for s in (json.loads(line) for line in (gold / "sources.jsonl").open(encoding="utf-8"))
    }
    questions = [json.loads(line) for line in (gold / "questions.jsonl").open(encoding="utf-8")]
    res = OvermaskResults(len(questions), 0, 0, 0, [], 0)
    for q in questions:
        doc = sources[q["source_id"]]
        request = ChatRequest(
            model="m",
            messages=[
                {"role": "user", "content": f"Document:\n{doc}\n\nQuestion: {q['question']}"}
            ],
            system=SYSTEM,
        )
        try:
            sent = str(redact_request(request, allow=allow).request.messages[0]["content"])
        except Exception:
            res.refused += 1
            continue
        res.placeholders.append(len(PLACEHOLDER.findall(sent)))
        # Only the page as sent: a phrase the question repeats would otherwise count as
        # surviving because the question carried it, which the first version of this did.
        # Split on the last blank line, not on the word "Question", which the second pass
        # masks like any other capitalised word it does not know.
        sent = sent.rsplit("\n\n", 1)[0]
        hit = False
        for phrase in q["must_mention"]:
            if phrase.lower() not in doc.lower():
                continue
            res.phrases += 1
            if phrase.lower() not in sent.lower():
                res.masked += 1
                hit = True
                res.examples.append((q["id"], phrase))
        res.questions_hit += hit
    return res


__all__ = ["OvermaskResults", "run"]
