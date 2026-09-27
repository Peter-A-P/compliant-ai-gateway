"""The semantic cache measured, labelled by construction (0.25, PLAN.md B2.5).

PLAN.md B2.5 measures the cache on replayed portfolio traffic with 200 hits hand-labelled.
The portfolio's traffic is not that yet (it is evaluation runs, which are pass-through and
never cached), and a hand label is Peter's time, so this is the measurement that can be made
now and that the hand-labelled one will be checked against: questions whose right answer is
known to be the same, or known to differ, by how they were made.

- **Questions**: project 03's 100 gold questions.
- **Same question, other words**: two paraphrases of each, written once by Haiku 4.5 and
  stored (`bench/cache/paraphrases.jsonl`). A paraphrase that changed the meaning would be
  counted as a correct hit, so the false-hit rate here is a floor; docs/cache.md reports a
  reading of them.
- **Same question, another customer**: `redact.quality.personalise` with two new seeds, the
  question in the voice of two different synthetic customers, sent raw and redacted.
- **A question the cache has not seen**: the cache holds only half the questions (every
  other one); a hit for any of the other half is false by construction.

A hit is **correct** when the stored entry is the same gold question, and **false** otherwise.
The false-hit rate is false hits over all hits, the number B2.5 asks for.

**The threshold is chosen on the odd-numbered questions and reported on the even ones**, by
a rule written before the even half was run: the lowest cosine whose false-hit rate on the
odd half's paraphrases is at most 2%, over a grid in steps of 0.01. Run on the odd half
alone first (chat 0.82; retrieval, no threshold qualifies) and committed before the even half
was run.

Two scopes are measured, because the difference between them is the finding:

- **chat**: the bare question is the final user message, as in a support assistant.
- **retrieval**: the final user message is 03's answer prompt, the page and then the
  question, as a retrieval pipeline sends it. Every question about one page shares most of
  its text.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from boundary.redact.evaluate import Rate
from boundary.redact.quality import personalise
from boundary.redact.request import redact_request
from boundary.semcache import Embedder, SemanticCache, Vector, cosine
from boundary.types import ChatRequest

SYSTEM = "Answer the customer's question using the document provided. Be brief and direct."
GRID = tuple(round(0.70 + 0.01 * i, 2) for i in range(30))
MAX_FALSE = 0.02
SEEDS = (20261001, 20261002)
PARAPHRASE_PROMPT = (
    "Rewrite the customer question below two different ways. Keep exactly the same meaning "
    "and every detail it asks about; change the wording. Output the two rewrites on two "
    "lines and nothing else.\n\nQuestion: {question}"
)
ANSWER_TEMPLATE = "DOCUMENT ({title})\n---\n{source}\n---\n\nQUESTION\n{question}"


@dataclass(frozen=True, slots=True)
class Query:
    qid: str
    kind: str  # paraphrase, persona-raw, persona-redacted
    text: str


@dataclass
class Point:
    threshold: float
    queries: int = 0
    seen: int = 0  # queries whose question the cache holds
    correct: int = 0
    false: int = 0

    @property
    def hit_rate(self) -> Rate:
        return Rate(self.correct, self.seen)

    @property
    def false_hit_rate(self) -> Rate:
        return Rate(self.false, self.correct + self.false)


@dataclass
class Curve:
    scope: str
    split: str
    kind: str
    points: list[Point] = field(default_factory=list)

    def at(self, t: float) -> Point:
        return next(p for p in self.points if p.threshold == t)


def _nearest_all(
    embed: Callable[[Sequence[str]], list[Vector]],
    stored: dict[str, str],
    queries: Sequence[Query],
) -> list[tuple[Query, str, float]]:
    """Each query's nearest stored question and its cosine. One embedding pass for each set,
    so the threshold grid costs nothing more."""
    keys = sorted(stored)
    vecs = embed([stored[k] for k in keys])
    qvecs = embed([q.text for q in queries])
    out = []
    for q, v in zip(queries, qvecs, strict=True):
        sims = [cosine(v, w) for w in vecs]
        i = max(range(len(sims)), key=sims.__getitem__)
        out.append((q, keys[i], sims[i]))
    return out


def _curve(
    scope: str, split: str, kind: str, near: Sequence[tuple[Query, str, float]], held: set[str]
) -> Curve:
    c = Curve(scope, split, kind)
    for t in GRID:
        p = Point(t)
        for q, key, sim in near:
            p.queries += 1
            p.seen += q.qid in held
            if sim >= t:
                if key == q.qid:
                    p.correct += 1
                else:
                    p.false += 1
        c.points.append(p)
    return c


def choose(curve: Curve) -> float | None:
    """The lowest threshold whose false-hit rate is at most MAX_FALSE (with at least one
    hit). None when no threshold on the grid qualifies."""
    for p in curve.points:
        if p.correct + p.false and p.false / (p.correct + p.false) <= MAX_FALSE:
            return p.threshold
    return None


@dataclass
class CacheResults:
    boundary_version: str
    model: str
    curves: list[Curve]
    chosen: dict[str, float | None]
    savings_per_call_usd: float | None = None

    def table(self) -> str:
        lines = [
            f"boundary {self.boundary_version}, semantic cache ({self.model}), 03's gold "
            "questions, labelled by construction; threshold chosen on odd, reported on even",
            "",
        ]
        for scope, t in self.chosen.items():
            lines.append(
                f"{scope}: threshold chosen on the odd half {t if t is not None else 'none qualifies'}"
            )
        lines.append("")
        lines.append(
            f"{'scope':<10}{'split':<6}{'kind':<18}{'t':>5}  {'hit rate':<28}{'false-hit rate':<28}"
        )
        for c in self.curves:
            t = self.chosen.get(c.scope)
            for p in c.points:
                if t is None or p.threshold != t:
                    continue
                lines.append(
                    f"{c.scope:<10}{c.split:<6}{c.kind:<18}{p.threshold:>5}  "
                    f"{p.hit_rate!s:<28}{p.false_hit_rate!s:<28}"
                )
        return "\n".join(lines)


def _questions(gate: Path) -> tuple[dict[str, dict[str, object]], dict[str, dict[str, object]]]:
    gold = gate / "gate" / "gold"
    qs = {
        str(q["id"]): q
        for q in (json.loads(x) for x in (gold / "questions.jsonl").open(encoding="utf-8"))
    }
    ss = {
        str(s["id"]): s
        for s in (json.loads(x) for x in (gold / "sources.jsonl").open(encoding="utf-8"))
    }
    return qs, ss


def _redacted(text: str) -> str:
    red = redact_request(ChatRequest(model="m", messages=[{"role": "user", "content": text}]))
    return str(red.request.messages[0]["content"])


def run(
    gate: Path, paraphrases: Path, embedder: Embedder, *, splits: Sequence[str] = ("odd", "even")
) -> CacheResults:
    from boundary import __version__

    qs, ss = _questions(gate)
    para = {
        str(r["qid"]): [str(p) for p in r["paraphrases"]]
        for r in (json.loads(x) for x in paraphrases.open(encoding="utf-8") if x.strip())
    }
    a = personalise({k: str(v["question"]) for k, v in qs.items()}, seed=SEEDS[0])
    b = personalise({k: str(v["question"]) for k, v in qs.items()}, seed=SEEDS[1])
    curves: list[Curve] = []

    def doc(qid: str, question: str) -> str:
        s = ss[str(qs[qid]["source_id"])]
        return ANSWER_TEMPLATE.format(
            title=s["title"], source=str(s["text"]).strip(), question=question.strip()
        )

    for split in splits:
        ids = sorted(k for k in qs if (int(k[2:]) % 2 == 1) == (split == "odd"))
        held = set(ids[::2])
        for scope in ("chat", "retrieval"):
            wrap = (lambda _q, t: t) if scope == "chat" else doc
            stored = {k: wrap(k, str(qs[k]["question"])) for k in held}
            queries = [Query(k, "paraphrase", wrap(k, p)) for k in ids for p in para.get(k, [])]
            near = _nearest_all(embedder.embed, stored, queries)
            curves.append(_curve(scope, split, "paraphrase", near, held))
            # Another customer asking the same question: the cache holds customer A's
            # version, raw or redacted, and customer B asks.
            for form, f in (("persona-raw", lambda t: t), ("persona-redacted", _redacted)):
                stored_p = {k: wrap(k, f(a[k][0])) for k in held}
                queries_p = [Query(k, form, wrap(k, f(b[k][0]))) for k in ids]
                near_p = _nearest_all(embedder.embed, stored_p, queries_p)
                curves.append(_curve(scope, split, form, near_p, held))
    chosen: dict[str, float | None] = {}
    for scope in ("chat", "retrieval"):
        dev = [
            c for c in curves if c.scope == scope and c.split == "odd" and c.kind == "paraphrase"
        ]
        chosen[scope] = choose(dev[0]) if dev else None
    return CacheResults(__version__, getattr(embedder, "model", "embedder"), curves, chosen)


PARAPHRASE_MODEL = "anthropic/claude-haiku-4-5-20251001"


async def paraphrase(gateway: object, gate: Path, out: Path, *, max_usd: float) -> float:
    """Two paraphrases of each gold question, written once and stored. The only paid step;
    everything after it re-reads `out`."""
    import asyncio

    from boundary.types import ChatRequest as Req

    qs, _ = _questions(gate)
    done = set()
    if out.is_file():
        done = {json.loads(x)["qid"] for x in out.open(encoding="utf-8") if x.strip()}
    spent = 0.0
    gate_ = asyncio.Semaphore(6)
    lock = asyncio.Lock()
    out.parent.mkdir(parents=True, exist_ok=True)

    async def one(qid: str) -> None:
        nonlocal spent
        async with gate_:
            async with lock:
                if spent >= max_usd:
                    return
            resp = await gateway.achat(  # type: ignore[attr-defined]
                Req(
                    model=PARAPHRASE_MODEL,
                    messages=[
                        {
                            "role": "user",
                            "content": PARAPHRASE_PROMPT.format(question=qs[qid]["question"]),
                        }
                    ],
                    max_tokens=200,
                    temperature=0.0,
                ),
                purpose="semantic cache paraphrases",
                run_id="cache-paraphrases",
                data_class="public",
            )
            lines = [x.strip() for x in (resp.text or "").splitlines() if x.strip()]
            async with lock:
                spent += resp.cost_usd or 0.0
                with out.open("a", encoding="utf-8", newline="\n") as f:
                    f.write(
                        json.dumps(
                            {
                                "qid": qid,
                                "paraphrases": lines[:2],
                                "model": resp.model_returned,
                                "cost_usd": resp.cost_usd,
                            },
                            ensure_ascii=False,
                        )
                        + "\n"
                    )

    await asyncio.gather(*(one(q) for q in sorted(qs) if q not in done))
    return spent


def cache_scope_check(embedder: Embedder) -> bool:
    """That the cache really keeps scopes apart: the same question under two system prompts
    is not a hit. A property of `SemanticCache`, checked on the real embedder."""
    cache = SemanticCache(embedder, threshold=0.5)
    q = [{"role": "user", "content": "How long can a bank hold a cheque?"}]
    cache.store(ChatRequest(model="m", messages=q, system="A"), "x")
    return cache.lookup(ChatRequest(model="m", messages=q, system="B")) is None


README_START = "<!-- cache:start -->"
README_END = "<!-- cache:end -->"


def readme_rows(results: CacheResults) -> str:
    rows = []
    for c in results.curves:
        t = results.chosen.get(c.scope)
        if c.split != "even" or t is None:
            continue
        p = c.at(t)
        rows.append(
            f"| {c.scope} | {c.kind} | {t:.2f} | {p.correct} of {p.seen}, {p.hit_rate} | "
            f"{p.false} of {p.correct + p.false}, {p.false_hit_rate} |"
        )
    return "\n".join(rows)


def write_readme(readme: Path, results: CacheResults) -> None:
    text = readme.read_text(encoding="utf-8")
    a, b = text.index(README_START), text.index(README_END)
    readme.write_text(
        text[: a + len(README_START)] + "\n" + readme_rows(results) + "\n" + text[b:],
        encoding="utf-8",
    )


__all__ = [
    "GRID",
    "PARAPHRASE_PROMPT",
    "CacheResults",
    "Curve",
    "Point",
    "Query",
    "choose",
    "paraphrase",
    "run",
    "write_readme",
]
