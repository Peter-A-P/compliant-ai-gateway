"""Rule C candidate 2 (PLAN.md section 9): cost a call from a local token estimate instead
of waiting for the vendor's returned usage.

The claim to test, stated before the run: a local estimate of the input tokens is close
enough to bill from, so the gateway could report a cost immediately rather than depending on
what each vendor chooses to return. If that were true, `costed` would never have to be 0, an
unknown price would not have to write an uncosted row, and a caller could know a call's cost
before the response arrived.

The plan expected the estimate to be poor. This measures it rather than asserting it, over
16,800 real calls that were made for another purpose: project 03's first official drift run
of 2026-09-13, whose raw store holds every request body and whose ledger holds every returned
usage count. Nothing is called here and nothing is spent; the run already happened.

Method. For each successful call, the text the vendor was actually sent is recovered from the
raw store and reduced to a token estimate by three estimators. The estimate is compared with
the prompt token count the vendor returned for that same call, matched by ledger id, and then
with the money: how far the month's input count would have been out if the estimate had been
costed instead of the returned count.

    chars/4        the universal rule of thumb, no dependency, works for every vendor
    words x 1.3    the other common rule of thumb, same properties
    tiktoken       OpenAI's real BPE vocabulary (o200k_base), the strongest case available

The three are not equally available, and that asymmetry is part of the result. tiktoken is
OpenAI's own tokenizer. Anthropic publishes no local tokenizer at all and Google's counts come
from a network call to their own endpoint, so for two of the four vendors the best row in this
table is not reachable offline even in principle. Applying o200k_base to Anthropic and Google
is therefore generous to the estimating design, not unfair to it.

The comparison is against the whole prompt the vendor tokenised, which is
`input_tokens + cache_read_tokens`, not `input_tokens` alone. Those are separate columns
because they bill at different rates, and on this run the split is not a detail: OpenAI served
473,600 cached prompt tokens against 433,780 fresh ones, so comparing an estimate with the
fresh column alone measures how much of the prompt OpenAI happened to have cached, which is a
property of the vendor's infrastructure rather than of any estimator. Anthropic and the
open-weights host cached nothing on this run; Google cached a little.

One thing is deliberately not corrected for: the estimators see the message text only, so they
cannot know each vendor's chat template overhead. That is exactly the position a caller is in
before the call, which is the position the experiment is about.

The reported number is the signed relative error per call, summarised per vendor, and the
share of the vendor's real September input bill that costing from the estimate would have
missed. Per-call errors are summarised with a bootstrap confidence interval over calls.
"""

from __future__ import annotations

import json
import random
import re
import sqlite3
import statistics
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

DOC_START = "<!-- token-estimates:start -->"
DOC_END = "<!-- token-estimates:end -->"

_WORD = re.compile(r"\S+")

# The estimators that need no vendor and no network. Both are the shapes people reach for
# when they want a number before the call.
CHARS_PER_TOKEN = 4.0
TOKENS_PER_WORD = 1.3


class _Encoder(Protocol):
    def encode(self, text: str, /) -> list[int]: ...


def _load_tiktoken() -> _Encoder | None:
    """o200k_base if tiktoken is installed, else None.

    It is a development-group dependency, not a runtime one: the library must never need a
    tokenizer to do its job, because needing one is the design this experiment rejects.
    """
    try:
        import tiktoken
    except ImportError:
        return None
    encoder: _Encoder = tiktoken.get_encoding("o200k_base")
    return encoder


def _text_of(part: Any) -> str:
    """The text inside one message, whatever shape the vendor wraps it in."""
    if isinstance(part, str):
        return part
    if isinstance(part, dict):
        chunks: list[str] = []
        if isinstance(part.get("text"), str):
            chunks.append(part["text"])
        for key in ("content", "parts"):
            if key in part:
                chunks.append(_text_of(part[key]))
        return "\n".join(c for c in chunks if c)
    if isinstance(part, list):
        return "\n".join(t for t in (_text_of(p) for p in part) if t)
    return ""


def prompt_text(provider: str, body: dict[str, Any]) -> str:
    """Every piece of text the request carried, in the order the vendor received it.

    Deliberately not the serialised JSON: a caller estimating a prompt estimates the prompt,
    not the envelope. Counting the JSON would flatter the estimators on structure and punish
    them on field names, and neither is the question.
    """
    chunks: list[str] = []
    if provider == "google":
        chunks.append(_text_of(body.get("systemInstruction")))
        chunks.append(_text_of(body.get("contents")))
    else:
        chunks.append(_text_of(body.get("system")))
        chunks.append(_text_of(body.get("messages")))
    return "\n".join(c for c in chunks if c)


@dataclass(frozen=True)
class Call:
    """One call that succeeded, with what was sent and what the vendor said it counted.

    `returned_prompt_tokens` is the whole prompt the vendor tokenised, fresh plus cache reads.
    """

    provider: str
    arm: str
    model: str
    text: str
    returned_prompt_tokens: int
    cost_usd: float


def _iter_raw(run_dir: Path) -> Iterator[tuple[str, int, str, dict[str, Any]]]:
    """(arm, ledger_id, provider, parsed request body) for every raw record in a run."""
    for arm_dir in sorted(p for p in run_dir.iterdir() if p.is_dir()):
        for raw in sorted(arm_dir.glob("raw/*/*.jsonl")):
            with raw.open(encoding="utf-8") as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    rec = json.loads(line)
                    body = rec["request"]["body"]
                    text = body["text"] if isinstance(body, dict) else body
                    yield arm_dir.name, int(rec["ledger_id"]), rec["provider"], json.loads(text)


def load_calls(run_dir: Path) -> list[Call]:
    """Join each arm's raw store to that arm's ledger on the ledger id.

    The join is per arm, because `id` is only unique inside one ledger file. Rows that
    errored, that carry no returned count, or that were never costed are dropped: an
    estimator cannot be scored against a number the vendor never sent.
    """
    usage: dict[tuple[str, int], tuple[int, float, str]] = {}
    for arm_dir in sorted(p for p in run_dir.iterdir() if p.is_dir()):
        db = arm_dir / "ledger.sqlite"
        if not db.is_file():
            continue
        uri = f"file:{db.as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        try:
            rows = conn.execute(
                "select id, input_tokens + cache_read_tokens, cost_usd, model_requested "
                "from ledger where error_type is null and costed = 1 "
                "and input_tokens + cache_read_tokens > 0"
            ).fetchall()
        finally:
            conn.close()
        for row_id, tokens, cost, model in rows:
            usage[(arm_dir.name, int(row_id))] = (int(tokens), float(cost or 0.0), str(model))

    calls: list[Call] = []
    for arm, ledger_id, provider, body in _iter_raw(run_dir):
        found = usage.get((arm, ledger_id))
        if found is None:
            continue
        tokens, cost, model = found
        text = prompt_text(provider, body)
        if not text:
            continue
        calls.append(
            Call(
                provider=provider,
                arm=arm,
                model=model,
                text=text,
                returned_prompt_tokens=tokens,
                cost_usd=cost,
            )
        )
    return calls


def _bootstrap_ci(
    values: list[float], stat: Callable[[list[float]], float], *, n: int, rng: random.Random
) -> tuple[float, float]:
    k = len(values)
    if k == 0:
        return (0.0, 0.0)
    samples = sorted(stat([values[rng.randrange(k)] for _ in range(k)]) for _ in range(n))
    return samples[int(0.025 * n)], samples[int(0.975 * n) - 1]


@dataclass
class EstimatorResult:
    """How one estimator did against one vendor."""

    estimator: str
    provider: str
    calls: int
    returned_tokens: int
    estimated_tokens: int
    median_rel_error: float
    median_rel_error_ci: tuple[float, float]
    mean_abs_rel_error: float
    mean_abs_rel_error_ci: tuple[float, float]
    worst_abs_rel_error: float
    within_5pc: float
    within_10pc: float
    billed_share: float

    @property
    def token_error_pc(self) -> float:
        """How far the whole month's input count would have been out, in percent."""
        if not self.returned_tokens:
            return 0.0
        return (self.estimated_tokens - self.returned_tokens) / self.returned_tokens * 100.0


@dataclass
class ExperimentResult:
    # The run's NAME, not the path it happened to be read from. An absolute path would put
    # one machine's filesystem layout into a published artefact, where it is both useless to
    # a reader and more than they asked to know. `run_id` is what identifies the run.
    run_dir: str
    run_id: str
    calls_scored: int
    tiktoken_available: bool
    results: list[EstimatorResult] = field(default_factory=list)


def _estimators(enc: _Encoder | None) -> dict[str, Callable[[str], float]]:
    est: dict[str, Callable[[str], float]] = {
        "chars/4": lambda t: len(t) / CHARS_PER_TOKEN,
        "words x 1.3": lambda t: len(_WORD.findall(t)) * TOKENS_PER_WORD,
    }
    if enc is not None:
        est["tiktoken o200k"] = lambda t: float(len(enc.encode(t)))
    return est


def run(run_dir: Path, *, seed: int = 20260913, bootstrap: int = 1000) -> ExperimentResult:
    calls = load_calls(run_dir)
    enc = _load_tiktoken()
    estimators = _estimators(enc)
    rng = random.Random(seed)

    run_id = ""
    run_json = run_dir / "RUN.json"
    if run_json.is_file():
        run_id = str(json.loads(run_json.read_text(encoding="utf-8")).get("run_id", ""))

    providers = sorted({c.provider for c in calls})
    results: list[EstimatorResult] = []
    for name, fn in estimators.items():
        for provider in providers:
            subset = [c for c in calls if c.provider == provider]
            if not subset:
                continue
            est_tokens = [fn(c.text) for c in subset]
            rel = [
                (e - c.returned_prompt_tokens) / c.returned_prompt_tokens
                for e, c in zip(est_tokens, subset, strict=True)
            ]
            abs_rel = [abs(r) for r in rel]
            returned = sum(c.returned_prompt_tokens for c in subset)
            estimated = round(sum(est_tokens))
            results.append(
                EstimatorResult(
                    estimator=name,
                    provider=provider,
                    calls=len(subset),
                    returned_tokens=returned,
                    estimated_tokens=estimated,
                    median_rel_error=statistics.median(rel) * 100.0,
                    median_rel_error_ci=tuple(  # type: ignore[arg-type]
                        v * 100.0
                        for v in _bootstrap_ci(rel, statistics.median, n=bootstrap, rng=rng)
                    ),
                    mean_abs_rel_error=statistics.fmean(abs_rel) * 100.0,
                    mean_abs_rel_error_ci=tuple(  # type: ignore[arg-type]
                        v * 100.0
                        for v in _bootstrap_ci(abs_rel, statistics.fmean, n=bootstrap, rng=rng)
                    ),
                    worst_abs_rel_error=max(abs_rel) * 100.0,
                    within_5pc=sum(1 for r in abs_rel if r <= 0.05) / len(abs_rel) * 100.0,
                    within_10pc=sum(1 for r in abs_rel if r <= 0.10) / len(abs_rel) * 100.0,
                    billed_share=sum(c.cost_usd for c in subset),
                )
            )

    return ExperimentResult(
        run_dir=run_dir.name,
        run_id=run_id,
        calls_scored=len(calls),
        tiktoken_available=enc is not None,
        results=results,
    )


def to_json(result: ExperimentResult) -> str:
    return json.dumps(asdict(result), indent=2)


def format_rows(result: ExperimentResult) -> str:
    """The table that goes into docs/rejected.md."""
    lines = [
        "| Estimator | Vendor | Calls | Median error (95% CI) | Mean absolute error (95% CI) "
        "| Worst | Within 10% | Month's input count out by |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for r in result.results:
        mlo, mhi = r.median_rel_error_ci
        alo, ahi = r.mean_abs_rel_error_ci
        lines.append(
            f"| {r.estimator} | {r.provider} | {r.calls:,} | "
            f"{r.median_rel_error:+.1f}% ({mlo:+.1f} to {mhi:+.1f}) | "
            f"{r.mean_abs_rel_error:.1f}% ({alo:.1f} to {ahi:.1f}) | "
            f"{r.worst_abs_rel_error:.0f}% | {r.within_10pc:.1f}% | "
            f"{r.token_error_pc:+.1f}% |"
        )
    return "\n".join(lines)


def write_doc(doc: Path, rows: str) -> None:
    text = doc.read_text(encoding="utf-8")
    start = text.index(DOC_START)
    end = text.index(DOC_END)
    text = text[: start + len(DOC_START)] + "\n" + rows + "\n" + text[end:]
    doc.write_text(text, encoding="utf-8")
