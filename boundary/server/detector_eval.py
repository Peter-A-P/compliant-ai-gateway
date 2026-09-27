"""The proxy's redaction settings compared (0.23): detector and allow list.

Four configurations of the policy's `redaction` block, `rules` or `presidio` for the
detector and with or without the allow list derived for PLAN.md B2.8, each measured three
ways, all offline:

- **Leaks and the round trip**: `redaction_eval` through the proxy on the generated corpus:
  planted personal values that reached the wire (plain requests), and answers, plain and
  streamed, that came back as sent.
- **Over-masking**: `overmask` on project 03's gold set, which holds no personal data: the
  share of required phrases masked out of the page, and placeholders per request.
- **Time**: redaction of each gold request as the proxy does it, per request, median and
  95th percentile, the part of the proxy's overhead the detector decides.
"""

from __future__ import annotations

import contextlib
import statistics
import time
from dataclasses import dataclass
from pathlib import Path

from boundary.config import BoundaryConfig
from boundary.enforce import DataPolicy, RedactionSettings, load_policy
from boundary.redact import overmask
from boundary.redact.analyzer import Analyzer
from boundary.redact.evaluate import Rate
from boundary.redact.policy import RedactionRefused
from boundary.redact.request import redact_request
from boundary.server import redaction_eval
from boundary.types import ChatRequest

CONFIGS: tuple[tuple[str, str, bool], ...] = (
    ("rules", "rules", False),
    ("rules + allow list", "rules", True),
    ("Presidio", "presidio", False),
    ("Presidio + allow list", "presidio", True),
)


@dataclass
class ConfigResult:
    name: str
    leaked: Rate
    round_trip: Rate
    refused: int
    masked: Rate
    placeholders_median: int
    ms_p50: float
    ms_p95: float

    def row(self) -> str:
        return (
            f"| {self.name} | {self.leaked.hits} of {self.leaked.total}, {self.leaked} | "
            f"{self.round_trip} | {self.refused} | {self.masked} | {self.placeholders_median} | "
            f"{self.ms_p50:.1f} / {self.ms_p95:.1f} |"
        )


def _gold_requests(gold: Path) -> list[ChatRequest]:
    import json

    sources = {
        s["id"]: s["text"]
        for s in (json.loads(x) for x in (gold / "sources.jsonl").open(encoding="utf-8"))
    }
    out = []
    for line in (gold / "questions.jsonl").open(encoding="utf-8"):
        q = json.loads(line)
        out.append(
            ChatRequest(
                model="m",
                system=overmask.SYSTEM,
                messages=[
                    {
                        "role": "user",
                        "content": f"Document:\n{sources[q['source_id']]}\n\nQuestion: {q['question']}",
                    }
                ],
            )
        )
    return out


def _timing(
    requests: list[ChatRequest], allow: tuple[str, ...], analyzer: Analyzer | None
) -> tuple[float, float]:
    times = []
    for r in requests:
        started = time.perf_counter()
        # A refusal still costs its time, so it is timed and not skipped.
        with contextlib.suppress(RedactionRefused):
            redact_request(r, allow=allow, analyzer=analyzer)
        times.append((time.perf_counter() - started) * 1000.0)
    times.sort()
    return statistics.median(times), times[min(len(times) - 1, int(0.95 * len(times)))]


def run(
    config: BoundaryConfig,
    policy_path: Path,
    gold: Path,
    allow_file: Path,
    *,
    pages: int = 200,
) -> list[ConfigResult]:
    base = load_policy(policy_path)
    allow = tuple(
        t.strip()
        for t in allow_file.read_text(encoding="utf-8").splitlines()
        if t.strip() and not t.startswith("#")
    )
    requests = _gold_requests(gold)
    presidio: Analyzer | None = None
    out = []
    for name, detector, with_list in CONFIGS:
        settings = RedactionSettings(detector=detector, allow=allow if with_list else ())
        policy: DataPolicy = base.model_copy(update={"redaction": settings})
        analyzer: Analyzer | None = None
        if detector == "presidio":
            if presidio is None:
                from boundary.redact.presidio import PresidioRecogniser

                presidio = Analyzer(extra=[PresidioRecogniser()])
            analyzer = presidio
        proxied = redaction_eval.run(config, policy_path, pages=pages, policy=policy)
        # The plain mode's values only: the streamed mode sends the same ones, and adding the
        # two would count each value twice (redaction_eval.readme_row says the same).
        plain = next(r for r in proxied.rows if r.mode == "plain")
        planted, leaked = plain.planted, plain.leaked
        trips = sum(r.round_trips for r in proxied.rows)
        sent = sum(r.requests - r.refused for r in proxied.rows)
        om = overmask.run(gold, allow=settings.allow, analyzer=analyzer)
        ph = sorted(om.placeholders)
        p50, p95 = _timing(requests, settings.allow, analyzer)
        out.append(
            ConfigResult(
                name,
                Rate(leaked, planted),
                Rate(trips, sent),
                sum(r.refused for r in proxied.rows),
                om.rate,
                ph[len(ph) // 2] if ph else 0,
                p50,
                p95,
            )
        )
    return out


README_START = "<!-- detectors:start -->"
README_END = "<!-- detectors:end -->"


def write_readme(readme: Path, results: list[ConfigResult]) -> None:
    text = readme.read_text(encoding="utf-8")
    a, b = text.index(README_START), text.index(README_END)
    rows = "\n".join(r.row() for r in results)
    readme.write_text(
        text[: a + len(README_START)] + "\n" + rows + "\n" + text[b:], encoding="utf-8"
    )


__all__ = ["CONFIGS", "ConfigResult", "run", "write_readme"]
