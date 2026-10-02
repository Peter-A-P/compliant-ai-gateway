"""The semantic cache replayed on real traffic (0.36, PLAN.md B2.5).

0.25 measured the cache on questions made for the purpose (`semcache_eval`). This replays
the portfolio's own traffic through it: every call of project 03's monthly drift runs, the
corpus PLAN.md names for it, read back from the raw stores those runs commit. No call is
made; each one's answer and cost are on record.

**What is replayed.** Every call in `drift/runs/*/*/raw`, joined by `ledger_id` to the arm's
`records.jsonl` for its suite item, block and cost, in the order the calls were made. One
cache for all of them, as for one team, that keeps what it stores across runs, as the
hosted pgvector cache does. Each request is rebuilt as the `ChatRequest` the library sent,
so its scope is `semcache.scope_of`'s and its question `semcache.question_of`'s.

**The proxy's rules, as far as a replay can apply them** (docs/cache.md):

- Looked up only if the caller would mark it a bare question. Written down before the
  replay was run: every block whose final message is the question alone. Two blocks carry
  a document in it, `long_context_recall` (a book passage, then the question) and
  `structured_extraction` (a document, then what to extract), and a caller would not mark
  those. Every block is also reported as if marked, which is what the rule is for.
- Not if the injection screen flags it (`boundary.screen`).
- Stored only on a miss with a whole answer: status 200, text, finish reason `stop`.
- The class is `public` (03's suite is public benchmarks), so the class flag allows it;
  nothing is redacted; nothing was streamed.

**A hit is labelled by construction.** Correct when the stored question is the query's own
suite item, a paraphrase of it (the suite's `parent_id`: 03 wrote two paraphrases of 20 of
its reasoning items for its paraphrase-robustness block), or the same text. False otherwise:
a different question, answered with another question's answer. A hit on a byte-identical
question is an **exact** hit, which an exact-match cache would make too; the rest are
**semantic**, the hits only a semantic cache makes, and the ones its risk is in.

**The intervals.** Calls are not independent: each question is asked five times a run, in
twelve runs, of eight arms. So every call-weighted figure carries a bootstrap interval that
resamples question families (an item with its paraphrases) rather than calls, and the
semantic hits are counted again as distinct pairs, (query, stored question), each once,
with a Wilson interval.

**The threshold is 0.82**, the one 0.25 chose and the proxy runs. A grid is swept beside it,
which is PLAN.md B9's rejected alternative 2, a loose threshold, measured.
"""

from __future__ import annotations

import json
import random
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from boundary.redact.evaluate import Rate
from boundary.screen import screen_request
from boundary.semcache import Embedder, question_of, scope_of
from boundary.server.wire import finish_reason
from boundary.types import ChatRequest

THRESHOLD = 0.82
GRID = (0.80, 0.82, 0.85, 0.88, 0.90, 0.92, 0.95, 0.97, 0.99)
# Written before the replay was run: the blocks whose final user message carries a document
# as well as the question. A caller marks a bare question only (docs/cache.md).
UNMARKED_BLOCKS = frozenset({"long_context_recall", "structured_extraction"})
BOOTSTRAP = 2000
SEED = 20261002


@dataclass(frozen=True, slots=True)
class Call:
    ts_utc: str
    run: str
    arm: str
    block: str
    item: str
    family: str  # the item, or the item it paraphrases
    scope: str
    question: str
    flagged: bool
    storable: bool  # a whole answer, so a miss would be stored
    cost_usd: float | None


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
    return ""


def rebuild(model: str, body: dict[str, Any]) -> ChatRequest:
    """The `ChatRequest` behind one adapter's request body: Anthropic's messages, OpenAI's and
    Together's chat completions, Google's generateContent. Every field that is not the
    system prompt or a message goes into the scope, whatever it is called, so two requests
    share a scope only when the library sent them the same settings."""
    rest = dict(body)
    rest.pop("model", None)
    system: str | None = None
    messages: list[dict[str, Any]] = []
    if "contents" in rest:
        for m in rest.pop("contents"):
            role = "user" if m.get("role") == "user" else "assistant"
            messages.append({"role": role, "content": _text(m.get("parts", []))})
        si = rest.pop("systemInstruction", None)
        system = _text(si.get("parts", [])) if isinstance(si, dict) else None
    else:
        for m in rest.pop("messages", []):
            if m.get("role") == "system":
                system = _text(m.get("content"))
            else:
                messages.append({"role": str(m.get("role")), "content": _text(m.get("content"))})
        if "system" in rest:
            system = _text(rest.pop("system"))
    return ChatRequest(model=model, messages=messages, system=system, extra=rest)


def families(suite: Path) -> dict[str, str]:
    """Each suite item's family: its `parent_id` when it is a paraphrase, else itself."""
    out: dict[str, str] = {}
    for f in sorted(suite.glob("*.jsonl")):
        for line in f.open(encoding="utf-8"):
            if line.strip():
                item = json.loads(line)
                out[str(item["id"])] = str(item.get("parent_id") or item["id"])
    return out


def load(drift: Path) -> list[Call]:
    """Every drift call with a record, in the order made."""
    fam = families(drift / "suite" / "v1")
    calls: list[Call] = []
    for arm_dir in sorted(p for p in (drift / "runs").glob("*/*") if (p / "raw").is_dir()):
        records = {}
        for line in (arm_dir / "records.jsonl").open(encoding="utf-8"):
            if line.strip():
                r = json.loads(line)
                records[r["ledger_id"]] = r
        for raw in sorted((arm_dir / "raw").glob("*/*.jsonl")):
            for line in raw.open(encoding="utf-8"):
                if not line.strip():
                    continue
                x = json.loads(line)
                r = records.get(x["ledger_id"])
                if r is None:
                    continue
                req = rebuild(str(r["model_requested"]), json.loads(x["request"]["body"]["text"]))
                cost = r.get("cost_usd") if r.get("costed") else None
                calls.append(
                    Call(
                        ts_utc=str(x["ts_utc"]),
                        run=arm_dir.parent.name,
                        arm=arm_dir.name,
                        block=str(r["block"]),
                        item=str(r["item_id"]),
                        family=fam.get(str(r["item_id"]), str(r["item_id"])),
                        scope=scope_of(req),
                        question=question_of(req),
                        flagged=bool(screen_request(req)),
                        storable=(
                            r.get("status") == 200
                            and bool(r.get("output"))
                            and finish_reason(r.get("finish_reason")) == "stop"
                        ),
                        cost_usd=float(cost) if cost is not None else None,
                    )
                )
    calls.sort(key=lambda c: (c.ts_utc, c.run, c.arm))
    return calls


@dataclass
class Outcome:
    """One call's fate in one replay."""

    looked_up: bool
    hit: bool = False
    exact: bool = False
    correct: bool = False
    similarity: float | None = None
    source: int | None = None  # index of the call whose answer was served


def replay(
    calls: Sequence[Call],
    sims: Callable[[str, str], float],
    *,
    threshold: float,
    marked: Callable[[Call], bool],
) -> list[Outcome]:
    """The cache's decision for every call, in order. `sims(a, b)` is the cosine of two
    questions. One store per scope, as `SemanticCache` keeps; the nearest stored question is
    the one served, as `SemanticCache.lookup` serves it."""
    stored: dict[str, list[int]] = defaultdict(list)
    # The nearest so far for each (scope, question), and how many of the scope's entries it
    # has been compared with: a store only grows, so a repeat compares only what is new.
    near: dict[tuple[str, str], tuple[int | None, float, int]] = {}
    out: list[Outcome] = []
    for i, c in enumerate(calls):
        if not marked(c) or c.flagged:
            out.append(Outcome(looked_up=False))
            continue
        entries = stored[c.scope]
        best, best_sim, checked = near.get((c.scope, c.question), (None, -2.0, 0))
        for j in entries[checked:]:
            s = sims(c.question, calls[j].question)
            if s > best_sim:
                best, best_sim = j, s
        near[(c.scope, c.question)] = (best, best_sim, len(entries))
        if best is not None and best_sim >= threshold:
            src = calls[best]
            exact = src.question == c.question
            out.append(
                Outcome(
                    looked_up=True,
                    hit=True,
                    exact=exact,
                    correct=exact or src.family == c.family,
                    similarity=best_sim,
                    source=best,
                )
            )
            continue
        out.append(Outcome(looked_up=True, similarity=best_sim if best is not None else None))
        if c.storable:
            stored[c.scope].append(i)
    return out


class Similarity:
    """Cosines between the distinct questions, each embedded once."""

    def __init__(self, embedder: Embedder, questions: Iterable[str]) -> None:
        import numpy as np

        texts = sorted(set(questions))
        self.index = {t: i for i, t in enumerate(texts)}
        m = np.asarray(embedder.embed(texts), dtype=np.float64)
        self.matrix = m @ m.T

    def __call__(self, a: str, b: str) -> float:
        return float(self.matrix[self.index[a], self.index[b]])


@dataclass
class Interval:
    value: float
    low: float
    high: float

    def __str__(self) -> str:
        return f"{self.value:.1%} ({self.low:.1%} to {self.high:.1%})"


def _family_bootstrap(
    calls: Sequence[Call],
    num: Callable[[int], float],
    den: Callable[[int], float],
    *,
    rng: random.Random,
) -> str:
    """A ratio of sums over calls, with a 95% interval from resampling question families;
    "none" when the denominator is empty. A resample whose denominator is empty has no
    ratio and is left out, rather than counted as zero."""
    import numpy as np

    nums: dict[str, float] = defaultdict(float)
    dens: dict[str, float] = defaultdict(float)
    for i, c in enumerate(calls):
        nums[c.family] += num(i)
        dens[c.family] += den(i)
    total_d = sum(dens.values())
    if not total_d:
        return "none"
    keys = sorted(dens)
    n = np.array([nums[k] for k in keys])
    d = np.array([dens[k] for k in keys])
    picks = np.random.default_rng(rng.randrange(2**32)).integers(
        0, len(keys), (BOOTSTRAP, len(keys))
    )
    sn, sd = n[picks].sum(axis=1), d[picks].sum(axis=1)
    ratios = sn[sd > 0] / sd[sd > 0]
    low, high = np.quantile(ratios, [0.025, 0.975])
    return str(Interval(sum(nums.values()) / total_d, float(low), float(high)))


@dataclass
class Summary:
    """One replay, one threshold, one set of calls."""

    label: str
    threshold: float
    calls: int
    looked_up: int
    hits: int
    exact_hits: int
    semantic_hits: int
    semantic_false: int
    spend_usd: float
    saved_usd: float
    saved_semantic_usd: float
    hit_rate: str  # of lookups, call-weighted, family bootstrap
    false_of_hits: str  # false hits of all hits, call-weighted, family bootstrap
    false_of_semantic: str  # false of semantic hits, call-weighted, family bootstrap
    saved_share: str  # of the spend on these calls, family bootstrap
    pairs: int  # distinct semantic (query, stored) pairs
    pairs_false: int
    pairs_false_rate: str  # Wilson
    uncosted_hits: int


def summarise(
    label: str,
    calls: Sequence[Call],
    out: Sequence[Outcome],
    threshold: float,
    *,
    keep: Callable[[Call], bool] = lambda _c: True,
) -> Summary:
    rng = random.Random(SEED)
    idx = [i for i, c in enumerate(calls) if keep(c)]
    sub = [calls[i] for i in idx]
    o = [out[i] for i in idx]
    cost = [c.cost_usd or 0.0 for c in sub]

    def ind(f: Callable[[Outcome], bool]) -> Callable[[int], float]:
        return lambda k: 1.0 if f(o[k]) else 0.0

    semantic = [(k, x) for k, x in enumerate(o) if x.hit and not x.exact]
    pairs: dict[tuple[str, str], bool] = {}
    for k, x in semantic:
        assert x.source is not None
        pairs[(sub[k].question, calls[x.source].question)] = x.correct
    pairs_false = sum(1 for v in pairs.values() if not v)
    return Summary(
        label=label,
        threshold=threshold,
        calls=len(sub),
        looked_up=sum(x.looked_up for x in o),
        hits=sum(x.hit for x in o),
        exact_hits=sum(x.hit and x.exact for x in o),
        semantic_hits=len(semantic),
        semantic_false=sum(not x.correct for _, x in semantic),
        spend_usd=round(sum(cost), 6),
        saved_usd=round(sum(cost[k] for k, x in enumerate(o) if x.hit), 6),
        saved_semantic_usd=round(sum(cost[k] for k, _ in semantic), 6),
        hit_rate=_family_bootstrap(sub, ind(lambda x: x.hit), ind(lambda x: x.looked_up), rng=rng),
        false_of_hits=_family_bootstrap(
            sub, ind(lambda x: x.hit and not x.correct), ind(lambda x: x.hit), rng=rng
        ),
        false_of_semantic=_family_bootstrap(
            sub,
            ind(lambda x: x.hit and not x.exact and not x.correct),
            ind(lambda x: x.hit and not x.exact),
            rng=rng,
        ),
        saved_share=_family_bootstrap(
            sub, lambda k: cost[k] if o[k].hit else 0.0, lambda k: cost[k], rng=rng
        ),
        pairs=len(pairs),
        pairs_false=pairs_false,
        pairs_false_rate=str(Rate(pairs_false, len(pairs))) if pairs else "none",
        uncosted_hits=sum(1 for k, x in enumerate(o) if x.hit and sub[k].cost_usd is None),
    )


@dataclass
class Pair:
    """One distinct semantic hit, for reading: the two questions and how it was labelled."""

    block: str
    query_item: str
    stored_item: str
    similarity: float
    correct: bool
    times: int
    query: str  # the text, or for a long one its start and end (`excerpt`)
    stored: str


EXCERPT = 300


def excerpt(text: str) -> str:
    """A long question's first and last EXCERPT characters, which is where a document's
    title and its question are; the whole text is in 03's raw store."""
    if len(text) <= 2 * EXCERPT + 20:
        return text
    return f"{text[:EXCERPT]} [... {len(text) - 2 * EXCERPT} characters ...] {text[-EXCERPT:]}"


@dataclass
class ReplayResults:
    boundary_version: str
    model: str
    source: str
    first_call_utc: str
    last_call_utc: str
    runs: list[str]
    threshold: float
    as_marked: Summary
    every_block_marked: Summary
    by_block: list[Summary]
    sweep: list[Summary]
    pairs: list[Pair] = field(default_factory=list)

    def table(self) -> str:
        head = (
            f"{'set':<34}{'t':>5}  {'lookups':>8} {'hits':>7} {'exact':>7} {'sem':>5} "
            f"{'sem false':>9}  {'hit rate':<26}{'false of semantic':<26}"
            f"{'saved of spend':<26}{'pairs false':<24}"
        )
        lines = [
            f"boundary {self.boundary_version}, semantic cache ({self.model}) replayed on "
            f"{self.source}, {self.first_call_utc} to {self.last_call_utc}, labelled by "
            "construction",
            "",
            head,
        ]
        for s in [self.as_marked, self.every_block_marked, *self.by_block, *self.sweep]:
            lines.append(
                f"{s.label:<34}{s.threshold:>5}  {s.looked_up:>8} {s.hits:>7} {s.exact_hits:>7} "
                f"{s.semantic_hits:>5} {s.semantic_false:>9}  {s.hit_rate:<26}"
                f"{s.false_of_semantic:<26}{s.saved_share:<26}"
                f"{s.pairs_false} of {s.pairs}, {s.pairs_false_rate}"
            )
        return "\n".join(lines)


def run(drift: Path, embedder: Embedder) -> ReplayResults:
    from boundary import __version__

    calls = load(drift)
    sims = Similarity(embedder, (c.question for c in calls))

    def as_marked(c: Call) -> bool:
        return c.block not in UNMARKED_BLOCKS

    def every(_c: Call) -> bool:
        return True

    main = replay(calls, sims, threshold=THRESHOLD, marked=as_marked)
    all_marked = replay(calls, sims, threshold=THRESHOLD, marked=every)
    blocks = sorted({c.block for c in calls})

    def in_block(b: str) -> Callable[[Call], bool]:
        return lambda c: c.block == b

    by_block = [
        summarise(
            f"{b}{' (unmarked)' if b in UNMARKED_BLOCKS else ''}",
            calls,
            all_marked,
            THRESHOLD,
            keep=in_block(b),
        )
        for b in blocks
    ]
    sweep = [
        summarise(
            "as marked",
            calls,
            replay(calls, sims, threshold=t, marked=as_marked),
            t,
            keep=as_marked,
        )
        for t in GRID
    ]
    seen: dict[tuple[str, str], Pair] = {}
    for i, x in enumerate(all_marked):
        if x.hit and not x.exact:
            assert x.source is not None and x.similarity is not None
            c, s = calls[i], calls[x.source]
            key = (c.question, s.question)
            if key in seen:
                seen[key].times += 1
            else:
                seen[key] = Pair(
                    c.block,
                    c.item,
                    s.item,
                    round(x.similarity, 4),
                    x.correct,
                    1,
                    excerpt(c.question),
                    excerpt(s.question),
                )
    return ReplayResults(
        boundary_version=__version__,
        model=getattr(embedder, "model", "embedder"),
        source="project 03's drift runs",
        first_call_utc=calls[0].ts_utc if calls else "",
        last_call_utc=calls[-1].ts_utc if calls else "",
        runs=sorted({c.run for c in calls}),
        threshold=THRESHOLD,
        as_marked=summarise("as marked", calls, main, THRESHOLD, keep=as_marked),
        every_block_marked=summarise("every block marked", calls, all_marked, THRESHOLD),
        by_block=by_block,
        sweep=sweep,
        pairs=sorted(seen.values(), key=lambda p: (p.correct, -p.times, p.block, p.query_item)),
    )


README_START = "<!-- cache-replay:start -->"
README_END = "<!-- cache-replay:end -->"


def readme_rows(results: ReplayResults) -> str:
    rows = []
    for s in [results.as_marked, results.every_block_marked]:
        rows.append(
            f"| {s.label} | {s.threshold:.2f} | {s.hits:,} of {s.looked_up:,}, {s.hit_rate} | "
            f"{s.exact_hits:,} | {s.semantic_hits:,} | "
            f"{s.semantic_false:,} of {s.semantic_hits:,}, {s.false_of_semantic} | "
            f"{s.pairs_false} of {s.pairs}, {s.pairs_false_rate} | "
            f"US${s.saved_usd:.2f} of US${s.spend_usd:.2f}, {s.saved_share} | "
            f"US${s.saved_semantic_usd:.2f} |"
        )
    return "\n".join(rows)


def write_readme(readme: Path, results: ReplayResults) -> None:
    text = readme.read_text(encoding="utf-8")
    a, b = text.index(README_START), text.index(README_END)
    readme.write_text(
        text[: a + len(README_START)] + "\n" + readme_rows(results) + "\n" + text[b:],
        encoding="utf-8",
    )


__all__ = [
    "GRID",
    "THRESHOLD",
    "UNMARKED_BLOCKS",
    "Call",
    "Outcome",
    "ReplayResults",
    "Similarity",
    "Summary",
    "families",
    "load",
    "rebuild",
    "replay",
    "run",
    "summarise",
    "write_readme",
]
