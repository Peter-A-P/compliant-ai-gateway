"""Measuring `boundary.redact` against a corpus whose answers are known by construction.

Run it with `boundary redact eval`. It needs no key, no account, no network and no model:
the corpus is generated from a seed and the engine runs locally, which is the point. A
stranger reproduces every number in the table with one command.

**Four things are measured, and they are not the same question.**

1. **Detection recall**, per entity type: of the values planted, how many did a recogniser
   return a span for. This is the figure project 07 publishes and the one this library has
   quoted until now.
2. **Type accuracy**: of the values found, how many were returned under the right type.
   Recall counts a span that was found and called something else; a consumer deciding what
   to release acts on the label, not on the span. 07 found a third party's surname released
   because Presidio called it a `LOCATION`, with recall reporting it as a success, so the
   two are reported separately here and always will be.
3. **Precision**: of the spans returned, how many are inside a planted value. This is what
   a corpus of planted values in prose written to contain none makes possible, and no
   figure this library has published before had it. Without it, recall is a number that
   rises when the engine gets less careful.
4. **What the boundary actually does**: the leak rate after `Policy.outbound`, which is
   detection plus the second pass plus the refusal, and the over-redaction rate that costs.
   A value the detector missed is not a leak if the second pass masked it, and that gap
   between rows 1 and 4 is the case for having a second pass at all.

Plus the latency the policy adds per page, which Part B budgets at under 100 ms.

**Intervals are Wilson score intervals at 95%**, which is the right choice for a proportion
near 0 or 1: the textbook normal interval puts the lower bound of 100/100 above 1.0 and the
upper bound of 0/100 below 0, and several rows here sit at exactly those ends.
"""

from __future__ import annotations

import math
import re
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field

from boundary.redact.analyzer import Analyzer
from boundary.redact.corpus import Corpus, Label, build
from boundary.redact.policy import PLACEHOLDER, Policy, RedactionRefused
from boundary.redact.sweep import sweep
from boundary.redact.types import Recogniser, Span

# A word-like token: a run of letters, digits, apostrophes, hyphens and slashes. The
# denominator of the over-redaction rate, so it is written down rather than implied.
_TOKEN = re.compile(r"[^\W_][\w'/-]*", re.UNICODE)

README_START = "<!-- redact:start -->"
README_END = "<!-- redact:end -->"


def wilson(hits: int, total: int, *, z: float = 1.96) -> tuple[float, float]:
    """The Wilson score interval for a proportion, as a pair in [0, 1].

    A count of 0 out of n returns a lower bound of exactly 0 and n out of n an upper bound
    of exactly 1, which is why this rather than the normal approximation: this file reports
    several rows at both ends.
    """
    if total == 0:
        return (0.0, 0.0)
    p = hits / total
    d = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / d
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / d
    lo, hi = max(0.0, centre - half), min(1.0, centre + half)
    # The ends exactly, rather than 0.9999999999999999, because these rows are printed and
    # compared and a bound that is not quite its own limit invites a reader to wonder why.
    return (0.0 if hits == 0 else lo, 1.0 if hits == total else hi)


@dataclass(frozen=True, slots=True)
class Rate:
    """A proportion with its interval, so a bare number cannot leave this module."""

    hits: int
    total: int

    @property
    def value(self) -> float:
        return self.hits / self.total if self.total else 0.0

    @property
    def interval(self) -> tuple[float, float]:
        return wilson(self.hits, self.total)

    def __str__(self) -> str:
        lo, hi = self.interval
        return f"{self.value:.1%} ({lo:.1%} to {hi:.1%})"


@dataclass
class EntityRow:
    entity_type: str
    planted: int
    found: int
    typed: int

    @property
    def recall(self) -> Rate:
        return Rate(self.found, self.planted)

    @property
    def type_accuracy(self) -> Rate:
        return Rate(self.typed, self.found)


@dataclass
class ShapeRow:
    shape: str
    planted: int
    found: int
    leaked: int

    @property
    def recall(self) -> Rate:
        return Rate(self.found, self.planted)

    @property
    def leak_rate(self) -> Rate:
        return Rate(self.leaked, self.planted)


@dataclass
class Latency:
    detect_p50_ms: float
    detect_p95_ms: float
    outbound_p50_ms: float
    outbound_p95_ms: float
    pages: int


@dataclass
class EvalResults:
    boundary_version: str
    ran_utc: str
    seed: int
    pages: int
    values: int
    detector: str
    entities: list[EntityRow]
    shapes: list[ShapeRow]
    precision_hits: int = 0
    precision_total: int = 0
    leaks: int = 0
    refusals: int = 0
    round_trips: int = 0
    over_redacted: int = 0
    over_redaction_total: int = 0
    latency: Latency = field(default_factory=lambda: Latency(0, 0, 0, 0, 0))

    @property
    def recall(self) -> Rate:
        return Rate(sum(e.found for e in self.entities), sum(e.planted for e in self.entities))

    @property
    def type_accuracy(self) -> Rate:
        return Rate(sum(e.typed for e in self.entities), sum(e.found for e in self.entities))

    @property
    def precision(self) -> Rate:
        return Rate(self.precision_hits, self.precision_total)

    @property
    def leak_rate(self) -> Rate:
        return Rate(self.leaks, self.values)

    @property
    def over_redaction(self) -> Rate:
        return Rate(self.over_redacted, self.over_redaction_total)

    @property
    def round_trip(self) -> Rate:
        return Rate(self.round_trips, self.pages)

    def readme_row(self) -> str:
        return (
            f"| {self.recall} | {self.type_accuracy} | {self.precision} | {self.leak_rate} "
            f"| {self.over_redaction} | {self.round_trip} "
            f"| {self.latency.outbound_p50_ms:.1f} / {self.latency.outbound_p95_ms:.1f} ms |"
        )

    def table(self) -> str:
        lines = [
            f"boundary {self.boundary_version}, {self.detector}, {self.pages} pages, "
            f"{self.recall.total} planted entities of which {self.values} personal, "
            f"seed {self.seed}",
            "",
            f"{'entity':<16}{'planted':>9}{'recall':>26}{'typed correctly':>26}",
        ]
        for row in sorted(self.entities, key=lambda r: -r.planted):
            lines.append(
                f"{row.entity_type.lower():<16}{row.planted:>9}"
                f"{row.recall!s:>26}{row.type_accuracy!s:>26}"
            )
        lines += [
            f"{'all':<16}{self.recall.total:>9}{self.recall!s:>26}{self.type_accuracy!s:>26}",
            "",
            f"{'name shape':<16}{'planted':>9}{'recall':>26}{'leaked in clear':>26}",
        ]
        for shape in sorted(self.shapes, key=lambda r: -r.planted):
            lines.append(
                f"{shape.shape:<16}{shape.planted:>9}{shape.recall!s:>26}{shape.leak_rate!s:>26}"
            )
        lines += [
            "",
            f"detection precision   {self.precision}",
            f"leak rate after outbound  {self.leak_rate}   "
            f"({self.leaks} of {self.values} planted values still in clear)",
            f"refusals              {self.refusals} of {self.pages} pages",
            f"over-redaction        {self.over_redaction}   "
            "(word-like tokens masked that are not part of a planted value)",
            f"round trip            {self.round_trip}   (rehydrate(outbound(page)) == page)",
            f"latency per page      detect {self.latency.detect_p50_ms:.1f} ms p50, "
            f"{self.latency.detect_p95_ms:.1f} ms p95; "
            f"outbound {self.latency.outbound_p50_ms:.1f} ms p50, "
            f"{self.latency.outbound_p95_ms:.1f} ms p95",
        ]
        return "\n".join(lines)


def _p(values: list[float], q: float) -> float:
    s = sorted(values)
    return s[min(len(s) - 1, round(q * (len(s) - 1)))] if s else 0.0


def _labels_by_page(corpus: Corpus) -> dict[int, list[Label]]:
    out: dict[int, list[Label]] = {}
    for lab in corpus.labels:
        out.setdefault(lab.page, []).append(lab)
    return out


def _inside(span: Span, labels: Sequence[Label]) -> Label | None:
    """The label a span sits inside, if any. Containment rather than exact offsets: a span
    over one part of a planted name has found a real personal value, and counting it as a
    false positive would punish the sweep for doing its job."""
    for lab in labels:
        if lab.start <= span.start and span.end <= lab.end:
            return lab
    return None


def _masked_values(output: str, policy: Policy) -> list[str]:
    """The values behind the placeholders in a redacted page, in order."""
    out = []
    for m in PLACEHOLDER.finditer(output):
        kind = (m.group(1) or m.group(3)).upper()
        number = m.group(2) or m.group(4)
        value = policy.vault.get(f"<{kind}_{number}>")
        if value is not None:
            out.append(value)
    return out


def run(
    *,
    pages: int = 200,
    seed: int = 20260920,
    extra: Sequence[Recogniser] = (),
    detector: str = "built-in recognisers only",
) -> EvalResults:
    """Measure the engine over a generated corpus. `extra` adds recognisers, which is how
    the Presidio run differs from the default one."""
    import datetime as dt

    from boundary import __version__

    corpus = build(pages=pages, seed=seed)
    by_page = _labels_by_page(corpus)
    analyzer = Analyzer(extra=extra)

    entities: dict[str, EntityRow] = {}
    shapes: dict[str, ShapeRow] = {}
    results = EvalResults(
        boundary_version=__version__,
        ran_utc=dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        seed=seed,
        pages=pages,
        values=len(corpus.personal),
        detector=detector,
        entities=[],
        shapes=[],
    )

    # Timed per page, because a total divided by the page count has no spread and this
    # table prints a p95.
    detect_ms: list[float] = []
    spans: list[Span] = []
    for i, text in enumerate(corpus.pages):
        t0 = time.perf_counter()
        spans.extend(analyzer.analyze_page(text, i + 1))
        detect_ms.append((time.perf_counter() - t0) * 1000)
    spans = sweep(corpus.pages, spans)
    spans_by_page: dict[int, list[Span]] = {}
    for s in spans:
        spans_by_page.setdefault(s.page, []).append(s)

    # Detection, per planted value.
    for lab in corpus.labels:
        row = entities.setdefault(lab.entity_type.value, EntityRow(lab.entity_type.value, 0, 0, 0))
        row.planted += 1
        key = lab.shape or "not a name"
        shape = shapes.setdefault(key, ShapeRow(key, 0, 0, 0))
        shape.planted += 1
        hit = [
            s for s in spans_by_page.get(lab.page, ()) if s.start < lab.end and lab.start < s.end
        ]
        if hit:
            row.found += 1
            shape.found += 1
            if any(s.entity_type is lab.entity_type for s in hit):
                row.typed += 1

    # Precision, over every span the detector returned.
    for page, found in spans_by_page.items():
        labels = by_page.get(page, [])
        results.precision_total += len(found)
        results.precision_hits += sum(1 for s in found if _inside(s, labels) is not None)

    # The boundary itself: one policy over the whole document, as a caller would build it.
    policy = Policy(spans)
    values_by_page = {p: {lab.text for lab in labs} for p, labs in by_page.items()}
    outbound_ms: list[float] = []
    leaked_shapes: dict[str, int] = {}
    for i, text in enumerate(corpus.pages):
        page = i + 1
        t1 = time.perf_counter()
        try:
            out = policy.outbound(text)
        except RedactionRefused:
            results.refusals += 1
            outbound_ms.append((time.perf_counter() - t1) * 1000)
            continue
        outbound_ms.append((time.perf_counter() - t1) * 1000)
        if policy.rehydrate(out) == text:
            results.round_trips += 1
        for lab in by_page.get(page, []):
            if lab.personal and lab.text in out:
                results.leaks += 1
                leaked_shapes[lab.shape or "not a name"] = (
                    leaked_shapes.get(lab.shape or "not a name", 0) + 1
                )
        # Over-redaction: masked tokens that are not part of a planted value, against every
        # word-like token on the page that is not part of one.
        planted = values_by_page.get(page, set())
        masked = _masked_values(out, policy)
        results.over_redacted += sum(
            1 for v in masked if not any(v in value or value in v for value in planted)
        )
        covered = [(lab.start, lab.end) for lab in by_page.get(page, [])]
        results.over_redaction_total += sum(
            1
            for m in _TOKEN.finditer(text)
            if not any(s <= m.start() and m.end() <= e for s, e in covered)
        )

    for leaked_shape, count in leaked_shapes.items():
        shapes[leaked_shape].leaked = count
    results.entities = list(entities.values())
    results.shapes = list(shapes.values())
    results.latency = Latency(
        detect_p50_ms=_p(detect_ms, 0.5),
        detect_p95_ms=_p(detect_ms, 0.95),
        outbound_p50_ms=_p(outbound_ms, 0.5),
        outbound_p95_ms=_p(outbound_ms, 0.95),
        pages=pages,
    )
    return results


@dataclass
class FamilyRow:
    family: str
    expect: str
    cases: int
    detected: int
    typed: int
    masked: int
    # Masked with every recogniser taken away: the second pass on its own.
    backstopped: int = 0

    @property
    def detection(self) -> Rate:
        return Rate(self.detected, self.cases)

    @property
    def type_accuracy(self) -> Rate:
        return Rate(self.typed, self.detected)

    @property
    def masking(self) -> Rate:
        return Rate(self.masked, self.cases)

    @property
    def backstop(self) -> Rate:
        return Rate(self.backstopped, self.cases)


@dataclass
class IdentifierResults:
    boundary_version: str
    ran_utc: str
    seed: int
    detector: str
    families: list[FamilyRow]

    def table(self) -> str:
        titles = {
            "detect": "claimed: a recogniser must find it",
            "mask only": "not claimed: no recogniser, and it must still not leave",
            "ignore": "near-misses: nothing should fire, and masking one is a cost",
        }
        lines = [
            f"boundary {self.boundary_version}, {self.detector}, "
            f"the Canadian identifier set, seed {self.seed}",
        ]
        for expect in ("detect", "mask only", "ignore"):
            rows = [r for r in self.families if r.expect == expect]
            if not rows:
                continue
            third = "masked by the policy" if expect != "ignore" else "masked (over-redaction)"
            fourth = "second pass alone" if expect != "ignore" else "second pass alone (cost)"
            lines += [
                "",
                titles[expect],
                f"{'family':<18}{'cases':>7}{'detected':>26}{third:>26}{fourth:>26}",
            ]
            for row in sorted(rows, key=lambda r: r.family):
                lines.append(
                    f"{row.family:<18}{row.cases:>7}{row.detection!s:>26}"
                    f"{row.masking!s:>26}{row.backstop!s:>26}"
                )
        wrong = [r for r in self.families if r.expect == "detect" and r.typed < r.detected]
        if wrong:
            lines += ["", "found but typed as something else:"]
            for row in wrong:
                lines.append(f"  {row.family}: {row.detected - row.typed} of {row.detected}")
        leaked = [r for r in self.families if r.expect != "ignore" and r.masked < r.cases]
        lines += [
            "",
            "leaks: "
            + (
                "none"
                if not leaked
                else ", ".join(f"{r.family} {r.cases - r.masked} of {r.cases}" for r in leaked)
            ),
        ]
        return chr(10).join(lines)


def _redacted(text: str, spans: Sequence[Span]) -> str:
    policy = Policy(spans)
    try:
        return policy.outbound(text)
    except RedactionRefused:
        # A refusal is not a release: nothing left. Report it as the strongest possible
        # masking rather than as an absence.
        return ""


def _gone(value: str, output: str) -> bool:
    """Whether none of `value` survives in `output`.

    Not `value not in output`, which passes a half-masked value: `(709) 555-0199` reduced to
    `(709) <ID_LIKE_1>` no longer contains the value and has published the area code. Any
    run of three or more digits from the value counts as surviving, and so does the value
    itself.
    """
    if value in output:
        return False
    return all(run not in output for run in re.findall(r"\d{3,}", value.replace(" ", "")))


def identifiers(
    *,
    per_family: int = 50,
    seed: int = 20260920,
    extra: Sequence[Recogniser] = (),
    detector: str = "built-in recognisers only",
) -> IdentifierResults:
    """The Canadian identifier set: every claimed shape in every written form, the shapes
    this library claims no recogniser for, and the near-misses that must not fire.

    Each case is its own document, because an identifier written in a sentence is what a
    recogniser sees, and a policy built over one page is what decides whether it leaves.
    """
    import datetime as dt

    from boundary import __version__
    from boundary.redact.identifiers import build_cases

    analyzer = Analyzer(extra=extra)
    rows: dict[tuple[str, str], FamilyRow] = {}
    for case in build_cases(per_family=per_family, seed=seed):
        row = rows.setdefault(
            (case.family, case.expect), FamilyRow(case.family, case.expect, 0, 0, 0, 0)
        )
        row.cases += 1
        start = case.text.index(case.value)
        end = start + len(case.value)
        found = [s for s in analyzer.analyze_page(case.text, 1) if s.start < end and start < s.end]
        if found:
            row.detected += 1
            if case.entity_type is not None and any(
                s.entity_type is case.entity_type for s in found
            ):
                row.typed += 1
        if _gone(case.value, _redacted(case.text, analyzer.analyze_page(case.text, 1))):
            row.masked += 1
        # The same case with every recogniser taken away. Project 07 made this point on
        # 2026-09-20 and it lands here: its EMAIL recogniser found every address, so its
        # corpus never asked the second pass whether it could, and the backstop was
        # untested rather than working. A column where a recogniser is doing the work says
        # nothing about the layer that exists for when a recogniser is wrong.
        if _gone(case.value, _redacted(case.text, [])):
            row.backstopped += 1
    return IdentifierResults(
        boundary_version=__version__,
        ran_utc=dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        seed=seed,
        detector=detector,
        families=list(rows.values()),
    )


def to_json(results: EvalResults) -> str:
    import json

    payload = asdict(results)
    payload["summary"] = {
        "recall": str(results.recall),
        "type_accuracy": str(results.type_accuracy),
        "precision": str(results.precision),
        "leak_rate": str(results.leak_rate),
        "over_redaction": str(results.over_redaction),
        "round_trip": str(results.round_trip),
    }
    return json.dumps(payload, indent=2)


def write_readme(readme, row: str) -> None:  # type: ignore[no-untyped-def]
    text = readme.read_text(encoding="utf-8")
    start = text.index(README_START)
    end = text.index(README_END)
    readme.write_text(
        text[: start + len(README_START)] + "\n" + row + "\n" + text[end:], encoding="utf-8"
    )


__all__ = [
    "EntityRow",
    "EvalResults",
    "FamilyRow",
    "IdentifierResults",
    "Rate",
    "ShapeRow",
    "identifiers",
    "run",
    "to_json",
    "wilson",
    "write_readme",
]
