"""The rehydration mutation rate: what models actually do to a placeholder in flight.

PLAN.md B1 asks for "the rate at which models mutate placeholders", and B8 for a system
prompt line asking the model to preserve them, "tested as an intervention with its effect
reported". `evaluate.rehydration` (0.6.4) measured the prior question without a model:
whether the library survives each mutation form. This measures which forms models produce,
on the same generated corpus, and how many of them the library gets back.

Redacted pages go to each model twice, under two tasks:

- **extract**: list every person, organisation, place and identifier, as written. Every
  placeholder in the page is expected in the answer, so this task has a denominator for
  **loss**: a placeholder the model dropped, merged or rewrote into something else.
- **reply**: write a short reply. The free-text case, where a model rewrites around the
  placeholders rather than copying them.

and under two arms: the task alone, and the task with one system line asking for
placeholders to be copied exactly (`PRESERVE`).

Every placeholder-shaped token in an answer is put in one of four classes:

| Class | Meaning |
|---|---|
| exact | Written as minted |
| tolerated | Changed (case, brackets, separators...) but `Policy.rehydrate` restores it |
| unresolvable | Placeholder-shaped, and the vault holds nothing for it: an invented or renumbered placeholder |
| degraded | An entity type word the matcher cannot read back (`Name Like 1`, `<EMAIL>`, `[PERSON]`) |

The **mutation rate** is everything but exact, over all tokens. The **unrecoverable rate** is
unresolvable plus degraded: what a reader loses. The outputs are stored, so `score` re-reads a
run without calling any model, and the numbers in docs/redact.md come from that function.

What this cannot see, stated rather than left out: a model that writes a real placeholder in
the wrong place (`<NAME_LIKE_2>` where the page meant `<NAME_LIKE_1>`) is counted exact, and
it rehydrates to the wrong person. Nothing short of reading the answer tells the two apart.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from boundary.redact.analyzer import Analyzer
from boundary.redact.corpus import build
from boundary.redact.evaluate import Rate, wilson
from boundary.redact.policy import PLACEHOLDER, Policy, placeholder_kind
from boundary.redact.sweep import sweep
from boundary.redact.types import EntityType

TASKS = ("extract", "reply")
ARMS = ("plain", "preserve")
SEED = 20260920

SYSTEM = "You work in a records office and handle requests for information."
PRESERVE = (
    "The document contains placeholders such as <PERSON_1> or <EMAIL_2> standing in for "
    "personal details that were removed. Copy every placeholder you use exactly, character "
    "for character, angle brackets included. Never invent, renumber, merge or replace one."
)
PROMPTS = {
    "extract": (
        "List every person, organisation, place, email address, phone number and file number "
        "mentioned in the document below, one per line, written as it appears in the "
        "document. Output the list only.\n\n---\n{page}"
    ),
    "reply": (
        "Write a short reply, under 120 words, to the document below, addressed to the "
        "person it concerns and mentioning the people and file references it contains.\n\n"
        "---\n{page}"
    ),
}
MAX_TOKENS = {"extract": 500, "reply": 300}

# An entity type's name, however a model has respelled it: any case, any separator, with or
# without an index, bracketed or not. Used only for what PLACEHOLDER did not match, so it
# finds the tokens the library cannot read back rather than the ones it can.
_TYPE_WORDS = "|".join(
    re.escape(e.value).replace("_", r"[ _\-]?")
    for e in sorted(EntityType, key=lambda e: -len(e.value))
)
# A bare type name with no index counts only when it is written with its underscore
# (`NAME_LIKE`), which nobody writes by accident. A bare single word does not count: `SIN`
# is how everyone abbreviates a social insurance number, and the first run's models wrote
# "SIN: <SIN_1>" as a label, which the first version of this pattern called a mutation.
_BARE = "|".join(re.escape(e.value) for e in EntityType if "_" in e.value)
_DEGRADED = re.compile(
    r"<\s*(?:" + _TYPE_WORDS + r")(?:[ _\-]?\d+)?\s*>"
    r"|\[\s*(?:" + _TYPE_WORDS + r")(?:[ _\-]?\d+)?\s*\]"
    r"|(?i:\b(?:" + _TYPE_WORDS + r")[ _\-]\d+\b)"
    r"|\b(?:" + _BARE + r")\b",
)
# The types the extract prompt asks for, read off its wording: people, organisations and
# places (which the rules-only engine mints as NAME_LIKE, or as the typed forms with a
# detector), email addresses, phone numbers and file numbers. Loss is counted over these
# only. The first scoring counted every placeholder on the page, and nearly all the "loss"
# it reported was dates of birth, health numbers, SINs and postal codes, which the prompt
# never asked for and the models were right to leave out (docs/redact.md).
ASKED = frozenset(
    {"NAME_LIKE", "PERSON", "ORGANISATION", "LOCATION", "ADDRESS", "EMAIL", "PHONE", "FILE_NUMBER"}
)


# A title in front of a placeholder. Redaction removed the name and, with it, anything the
# title said about the person; a model that writes "Mr. <NAME_LIKE_1>" has put a guess back.
_TITLED = re.compile(r"\b(?:Mr|Mrs|Ms|Miss|Mx|Dr|Sir|Madam)\.?\s+(" + PLACEHOLDER.pattern + ")")


def _titled(text: str) -> set[str]:
    out: set[str] = set()
    for m in _TITLED.finditer(text):
        inner = PLACEHOLDER.match(m.group(1))
        if inner is not None:
            out.add(_canonical(inner))
    return out


def corpus_policy(pages: int, seed: int = SEED) -> tuple[list[str], Policy]:
    """The redacted pages and the one policy that minted them, rebuilt from the seed so a
    stored run can be re-scored anywhere. Rules only: no model, no Presidio."""
    corpus = build(pages=pages, seed=seed)
    policy = Policy(sweep(corpus.pages, Analyzer().analyze(corpus.pages)))
    return [policy.redact(p) for p in corpus.pages], policy


def _canonical(m: re.Match[str]) -> str:
    kind, number = placeholder_kind(m)
    return f"<{kind}_{number}>"


@dataclass
class TokenCounts:
    exact: int = 0
    tolerated: int = 0
    unresolvable: int = 0
    degraded: int = 0
    # Extract task only: placeholders in the page, and how many came back in any form.
    expected: int = 0
    kept: int = 0
    # Placeholders the answer gave a title (Mr, Ms, Dr...) that the page did not.
    titled: int = 0
    forms: dict[str, int] = field(default_factory=dict)

    def add(self, other: TokenCounts) -> None:
        self.exact += other.exact
        self.tolerated += other.tolerated
        self.unresolvable += other.unresolvable
        self.degraded += other.degraded
        self.expected += other.expected
        self.kept += other.kept
        self.titled += other.titled
        for k, v in other.forms.items():
            self.forms[k] = self.forms.get(k, 0) + v

    @property
    def tokens(self) -> int:
        return self.exact + self.tolerated + self.unresolvable + self.degraded

    @property
    def mutation(self) -> Rate:
        return Rate(self.tokens - self.exact, self.tokens)

    @property
    def unrecoverable(self) -> Rate:
        return Rate(self.unresolvable + self.degraded, self.tokens)

    @property
    def loss(self) -> Rate:
        return Rate(self.expected - self.kept, self.expected)


def _form(written: str, canonical: str, before: str, after: str) -> str:
    """A short name for how a tolerated placeholder differs from its minted form. `before`
    and `after` are the characters around the match, which is how square brackets and
    markdown show: the matcher reads the bare name inside them."""
    inner = canonical.strip("<>")
    if written == canonical.lower():
        return "lower case"
    if written == inner:
        wrapped = {
            ("[", "]"): "square brackets",
            ("(", ")"): "round brackets",
            ("*", "*"): "bold markdown",
            ("`", "`"): "backticked",
        }
        return wrapped.get((before, after), "brackets dropped")
    if written.strip("<>").strip() == inner:
        return "spaces inside"
    if re.fullmatch(r"<?" + re.escape(inner).replace("_", r"[ \-]") + r">?", written):
        return "separator changed"
    return "other"


def classify(
    text: str, policy: Policy, page: str | None = None, *, expect: bool = True
) -> TokenCounts:
    """Put every placeholder-shaped token in `text` in its class. With `page`, count the
    titles the answer added that the page did not have, and, when `expect`, which of the
    page's placeholders came back in any resolvable form."""
    counts = TokenCounts()
    covered: list[tuple[int, int]] = []
    returned: set[str] = set()
    for m in PLACEHOLDER.finditer(text):
        covered.append(m.span())
        canonical = _canonical(m)
        if policy.vault.get(canonical) is None:
            counts.unresolvable += 1
            continue
        returned.add(canonical)
        if m.group(0) == canonical:
            counts.exact += 1
        else:
            counts.tolerated += 1
            a, b = m.span()
            form = _form(m.group(0), canonical, text[a - 1 : a], text[b : b + 1])
            counts.forms[form] = counts.forms.get(form, 0) + 1
    for m in _DEGRADED.finditer(text):
        if not any(a < m.end() and m.start() < b for a, b in covered):
            counts.degraded += 1
    if page is not None:
        counts.titled = len(_titled(text) - _titled(page))
    if page is not None and expect:
        wanted = {
            _canonical(m) for m in PLACEHOLDER.finditer(page) if placeholder_kind(m)[0] in ASKED
        }
        counts.expected = len(wanted)
        counts.kept = len(wanted & returned)
    return counts


@dataclass
class Call:
    model: str
    arm: str
    task: str
    page: int
    status: int | str
    text: str | None
    cost_usd: float | None
    call_uid: str | None


@dataclass
class MutationRun:
    boundary_version: str
    ran_utc: str
    pages: int
    seed: int
    prompts_sha256: str
    calls: list[Call]

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=1, ensure_ascii=True) + "\n"

    @classmethod
    def from_json(cls, text: str) -> MutationRun:
        raw = json.loads(text)
        raw["calls"] = [Call(**c) for c in raw["calls"]]
        return cls(**raw)


def prompts_sha256() -> str:
    blob = json.dumps(
        {"system": SYSTEM, "preserve": PRESERVE, "prompts": PROMPTS, "max": MAX_TOKENS},
        sort_keys=True,
    )
    return hashlib.sha256(blob.encode()).hexdigest()


def newcombe(a: Rate, b: Rate, *, z: float = 1.96) -> tuple[float, float, float]:
    """The difference a - b between two independent proportions, with Newcombe's hybrid
    Wilson interval (method 10), which behaves at 0 and 1 where the Wald interval does not."""
    p1, p2 = a.value, b.value
    l1, u1 = wilson(a.hits, a.total, z=z)
    l2, u2 = wilson(b.hits, b.total, z=z)
    d = p1 - p2
    lower = d - ((p1 - l1) ** 2 + (u2 - p2) ** 2) ** 0.5
    upper = d + ((u1 - p1) ** 2 + (p2 - l2) ** 2) ** 0.5
    return d, lower, upper


@dataclass
class Scored:
    run: MutationRun
    # (model, arm) -> counts over both tasks; (model, arm, task) -> counts for one task.
    by_arm: dict[tuple[str, str], TokenCounts]
    by_task: dict[tuple[str, str, str], TokenCounts]
    failed: int

    def models(self) -> list[str]:
        return sorted({m for m, _ in self.by_arm})

    def readme_rows(self) -> str:
        """One README row per model: both arms' mutation and unrecoverable rates and the
        extract task's loss, and the preserve line's effect with its interval."""
        rows = []
        for model in self.models():
            plain, keep = self.by_arm.get((model, "plain")), self.by_arm.get((model, "preserve"))
            if plain is None or keep is None:
                continue
            lost_p = self.by_task.get((model, "plain", "extract"), TokenCounts()).loss
            lost_k = self.by_task.get((model, "preserve", "extract"), TokenCounts()).loss
            d, lo, hi = newcombe(keep.mutation, plain.mutation)
            rows.append(
                f"| {model} | {plain.mutation} of {plain.tokens} | {keep.mutation} of "
                f"{keep.tokens} | {d * 100:+.1f} points ({lo * 100:+.1f} to {hi * 100:+.1f}) | "
                f"{plain.unrecoverable} / {keep.unrecoverable} | {lost_p} / {lost_k} |"
            )
        return "\n".join(rows)

    def table(self) -> str:
        r = self.run
        lines = [
            f"boundary {r.boundary_version}, placeholder mutation under a model: {r.pages} "
            f"redacted pages, seed {r.seed}, {len(r.calls)} calls ({self.failed} failed), "
            f"prompts {r.prompts_sha256[:12]}",
            "",
            f"{'model':<54}{'arm':<10}{'tokens':>7}  {'mutated':<24}{'unrecoverable':<24}"
            f"{'lost (extract)':<24}{'titles added':>12}",
        ]
        for model in self.models():
            for arm in ARMS:
                c = self.by_arm.get((model, arm))
                if c is None:
                    continue
                ex = self.by_task.get((model, arm, "extract"), TokenCounts())
                lines.append(
                    f"{model:<54}{arm:<10}{c.tokens:>7}  {c.mutation!s:<24}"
                    f"{c.unrecoverable!s:<24}{ex.loss!s:<24}{c.titled:>12}"
                )
        lines += ["", "the preserve line's effect on the mutation rate (preserve minus plain):"]
        for model in self.models():
            a, b = self.by_arm.get((model, "preserve")), self.by_arm.get((model, "plain"))
            if a is None or b is None or not a.tokens or not b.tokens:
                continue
            d, lo, hi = newcombe(a.mutation, b.mutation)
            lines.append(f"  {model:<52}{d * 100:+.1f} points ({lo * 100:+.1f} to {hi * 100:+.1f})")
        forms: dict[str, int] = {}
        for c in self.by_arm.values():
            for k, v in c.forms.items():
                forms[k] = forms.get(k, 0) + v
        if forms:
            lines += [
                "",
                "tolerated forms, all models: "
                + ", ".join(f"{k} {v}" for k, v in sorted(forms.items(), key=lambda kv: -kv[1])),
            ]
        lines += [
            "",
            "A placeholder written correctly in the wrong place is counted exact and rehydrates "
            "to the wrong value; nothing here can see that.",
        ]
        return "\n".join(lines)


README_START = "<!-- mutation:start -->"
README_END = "<!-- mutation:end -->"


def write_readme(readme: Path, rows: str) -> None:
    text = readme.read_text(encoding="utf-8")
    start = text.index(README_START)
    end = text.index(README_END)
    readme.write_text(
        text[: start + len(README_START)] + "\n" + rows + "\n" + text[end:], encoding="utf-8"
    )


def score(run: MutationRun) -> Scored:
    pages, policy = corpus_policy(run.pages, run.seed)
    by_arm: dict[tuple[str, str], TokenCounts] = {}
    by_task: dict[tuple[str, str, str], TokenCounts] = {}
    failed = 0
    for c in run.calls:
        if c.text is None:
            failed += 1
            continue
        counts = classify(c.text, policy, pages[c.page], expect=c.task == "extract")
        by_arm.setdefault((c.model, c.arm), TokenCounts()).add(counts)
        by_task.setdefault((c.model, c.arm, c.task), TokenCounts()).add(counts)
    return Scored(run, by_arm, by_task, failed)


async def collect(
    gateway: Any,
    models: Sequence[str],
    *,
    pages: int,
    seed: int = SEED,
    run_id: str,
    max_usd: float,
    concurrency: int = 6,
) -> MutationRun:
    """Every model, arm, task and page, through `gateway.achat`. Stops sending once the run's
    returned cost passes `max_usd`; the calls not made are absent from the run, not guessed."""
    from boundary import __version__
    from boundary.errors import BoundaryError
    from boundary.types import ChatRequest

    redacted, _ = corpus_policy(pages, seed)
    spent = 0.0
    lock = asyncio.Lock()
    gate = asyncio.Semaphore(concurrency)
    calls: list[Call] = []

    async def one(model: str, arm: str, task: str, i: int) -> None:
        nonlocal spent
        async with gate:
            async with lock:
                if spent >= max_usd:
                    return
            system = SYSTEM if arm == "plain" else f"{SYSTEM}\n\n{PRESERVE}"
            request = ChatRequest(
                model=model,
                messages=[{"role": "user", "content": PROMPTS[task].format(page=redacted[i])}],
                system=system,
                max_tokens=MAX_TOKENS[task],
            )
            try:
                resp = await gateway.achat(
                    request,
                    purpose="mutation-rate",
                    run_id=run_id,
                    # The corpus is generated and holds no real person, and what is sent is
                    # already redacted.
                    data_class="public",
                )
                call = Call(
                    model, arm, task, i, resp.status, resp.text, resp.cost_usd, resp.call_uid
                )
            except BoundaryError as e:
                call = Call(model, arm, task, i, type(e).__name__, None, None, None)
            async with lock:
                spent += call.cost_usd or 0.0
                calls.append(call)

    await asyncio.gather(
        *(
            one(model, arm, task, i)
            for model in models
            for arm in ARMS
            for task in TASKS
            for i in range(pages)
        )
    )
    calls.sort(key=lambda c: (c.model, c.arm, c.task, c.page))
    return MutationRun(
        boundary_version=__version__,
        ran_utc=dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        pages=pages,
        seed=seed,
        prompts_sha256=prompts_sha256(),
        calls=calls,
    )


def write(path: Path, run: MutationRun) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(run.to_json(), encoding="utf-8")


def read(path: Path) -> MutationRun:
    return MutationRun.from_json(path.read_text(encoding="utf-8"))


__all__ = [
    "ARMS",
    "ASKED",
    "PRESERVE",
    "PROMPTS",
    "SYSTEM",
    "TASKS",
    "Call",
    "MutationRun",
    "Scored",
    "TokenCounts",
    "classify",
    "collect",
    "corpus_policy",
    "newcombe",
    "read",
    "score",
    "write",
    "write_readme",
]
