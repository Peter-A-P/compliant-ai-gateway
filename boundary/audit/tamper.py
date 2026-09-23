"""The tamper test: corrupt a sealed chain every way an operator could, and count detections.

PLAN.md B10 asks for a tamper test that "detects every injected corruption". This measures
that claim instead of asserting it, and splits it in two because the honest answer is in
two parts:

- **Before the last anchor**, every kind of corruption should be detected, including a
  forger who recomputes every hash after the change, because the anchor holds a head the
  forger cannot reach back and alter.
- **After the last anchor**, a naive edit is still detected by the chain, and a competent
  rewrite is not detected by anything. That is not a defect to fix; it is what a chain is,
  and the width of that window is the interval between anchors. A table reporting only the
  first half would be claiming more than the design delivers.

Everything is generated from a seed and runs in memory: no ledger of anybody's, no network,
no database. A control row with no corruption counts false alarms, which must be zero.
"""

from __future__ import annotations

import datetime as dt
import json
import random
import time
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from boundary.audit.chain import (
    GENESIS,
    Anchor,
    Record,
    build,
    canonical,
    ledger_body,
    verify,
)
from boundary.redact.evaluate import Rate

README_START = "<!-- audit:start -->"
README_END = "<!-- audit:end -->"
DOC_START = "<!-- audit-doc:start -->"
DOC_END = "<!-- audit-doc:end -->"

SEALED_UTC = "2026-09-22T00:00:00.000Z"

Ledger = list[dict[str, Any]]
Mutation = Callable[[list[Record], Ledger, int, random.Random], tuple[list[Record], Ledger]]


def synthetic_ledger(n: int, *, seed: int) -> Ledger:
    """Ledger rows with the shape of real ones and nothing of anybody's in them."""
    rng = random.Random(seed)
    start = dt.datetime(2026, 9, 1, tzinfo=dt.UTC)
    providers = [
        ("anthropic", "claude-haiku-4-5-20251001", None, None),
        ("openai", "gpt-5-nano", None, None),
        ("bedrock", "us.anthropic.claude-haiku-4-5-20251001-v1:0", "ca-central-1", "geo"),
        ("foundry-canada", "gpt-5.6-luna", "canadacentral", "global"),
    ]
    rows: Ledger = []
    for i in range(n):
        provider, model, region, residency = rng.choice(providers)
        tokens_in, tokens_out = rng.randint(10, 4000), rng.randint(1, 800)
        failed = rng.random() < 0.03
        costed = provider not in ("bedrock", "foundry-canada") and not failed
        ts = start + dt.timedelta(seconds=37 * i)
        rows.append(
            {
                "id": i + 1,
                "call_uid": f"{rng.getrandbits(128):032x}",
                "env": rng.choice(["laptop", "actions"]),
                "ts_utc": ts.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                "boundary_version": "0.7.0",
                "project": rng.choice(["drift-record", "model-selection", "redaction"]),
                "purpose": "tamper-test",
                "run_id": f"run-{i // 100}",
                "mode": rng.choice(["standard", "passthrough"]),
                "provider": provider,
                "alias": None,
                "model_requested": model,
                "model_returned": None if failed else model,
                "region": region,
                "residency": residency,
                "data_class": rng.choice([None, "public", "internal", "personal"]),
                "input_tokens": 0 if failed else tokens_in,
                "output_tokens": 0 if failed else tokens_out,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "price_list": "2026-09-14",
                "price_sha256": f"{rng.getrandbits(256):064x}",
                "cost_usd": round((tokens_in * 1.0 + tokens_out * 5.0) / 1e6, 9)
                if costed
                else None,
                "costed": int(costed),
                "cached": 0,
                "latency_ms": round(rng.uniform(200, 4000), 1),
                "http_status": 500 if failed else 200,
                "error_type": "http_500" if failed else None,
                "retries": 0,
                "request_sha256": f"{rng.getrandbits(256):064x}",
                "response_sha256": None if failed else f"{rng.getrandbits(256):064x}",
                "batch_id": None,
                "ttft_ms": None,
            }
        )
    return rows


def _understate(row: Mapping[str, Any]) -> dict[str, Any]:
    """The edit with a motive: a call made to look cheaper, or free."""
    out = dict(row)
    cost = out.get("cost_usd")
    out["cost_usd"] = 0.0 if cost else 0.5
    return out


def _edit_body(body: str) -> str:
    d = json.loads(body)
    d["row"] = _understate(d["row"])
    return canonical(d)


def _rewrite_from(records: list[Record], i: int) -> list[Record]:
    """Recompute every hash from position i, renumbering: what a competent forger does."""
    prev = records[i - 1].record_hash if i > 0 else GENESIS
    return records[:i] + build([r.body for r in records[i:]], prev_hash=prev, first_seq=i + 1)


def _none(
    records: list[Record], ledger: Ledger, i: int, rng: random.Random
) -> tuple[list[Record], Ledger]:
    return records, ledger


def _edit(
    records: list[Record], ledger: Ledger, i: int, rng: random.Random
) -> tuple[list[Record], Ledger]:
    r = records[i]
    out = list(records)
    out[i] = Record(r.seq, r.prev_hash, r.record_hash, _edit_body(r.body))
    return out, ledger


def _edit_rehash(
    records: list[Record], ledger: Ledger, i: int, rng: random.Random
) -> tuple[list[Record], Ledger]:
    r = records[i]
    body = _edit_body(r.body)
    out = list(records)
    out[i] = build([body], prev_hash=r.prev_hash, first_seq=r.seq)[0]
    return out, ledger


def _edit_rewrite(
    records: list[Record], ledger: Ledger, i: int, rng: random.Random
) -> tuple[list[Record], Ledger]:
    out, _ = _edit(records, ledger, i, rng)
    return _rewrite_from(out, i), ledger


def _delete(
    records: list[Record], ledger: Ledger, i: int, rng: random.Random
) -> tuple[list[Record], Ledger]:
    return records[:i] + records[i + 1 :], ledger


def _delete_rewrite(
    records: list[Record], ledger: Ledger, i: int, rng: random.Random
) -> tuple[list[Record], Ledger]:
    # The call vanishes from both, which is the only deletion worth a forger's effort.
    out = records[:i] + records[i + 1 :]
    gone = json.loads(records[i].body)["row"]["call_uid"]
    kept = [row for row in ledger if row["call_uid"] != gone]
    return (_rewrite_from(out, i) if i < len(out) else out), kept


def _insert_rewrite(
    records: list[Record], ledger: Ledger, i: int, rng: random.Random
) -> tuple[list[Record], Ledger]:
    forged = synthetic_ledger(1, seed=rng.getrandbits(32))[0]
    body = ledger_body(forged, sealed_utc=SEALED_UTC)
    placeholder = Record(0, "", "", body)
    out = [*records[:i], placeholder, *records[i:]]
    return _rewrite_from(out, i), [*ledger, forged]


def _swap_rewrite(
    records: list[Record], ledger: Ledger, i: int, rng: random.Random
) -> tuple[list[Record], Ledger]:
    j = i + 1 if i + 1 < len(records) else i - 1
    a, b = min(i, j), max(i, j)
    out = list(records)
    out[a], out[b] = out[b], out[a]
    return _rewrite_from(out, a), ledger


def _truncate(
    records: list[Record], ledger: Ledger, i: int, rng: random.Random
) -> tuple[list[Record], Ledger]:
    # Drop the tail from i, and the calls it sealed, so the ledger agrees with the log.
    gone = {json.loads(r.body)["row"]["call_uid"] for r in records[i:]}
    return records[:i], [row for row in ledger if row["call_uid"] not in gone]


def _replace(
    records: list[Record], ledger: Ledger, i: int, rng: random.Random
) -> tuple[list[Record], Ledger]:
    # A whole new log from genesis, identical but for one understated record, and a ledger
    # edited to match it: the strongest forgery available to somebody who owns every file.
    bodies = [r.body for r in records]
    bodies[i] = _edit_body(bodies[i])
    uid = json.loads(bodies[i])["row"]["call_uid"]
    new_ledger = [_understate(row) if row["call_uid"] == uid else row for row in ledger]
    return build(bodies), new_ledger


def _ledger_edit(
    records: list[Record], ledger: Ledger, i: int, rng: random.Random
) -> tuple[list[Record], Ledger]:
    uid = json.loads(records[i].body)["row"]["call_uid"]
    return records, [_understate(row) if row["call_uid"] == uid else row for row in ledger]


def _ledger_delete(
    records: list[Record], ledger: Ledger, i: int, rng: random.Random
) -> tuple[list[Record], Ledger]:
    uid = json.loads(records[i].body)["row"]["call_uid"]
    return records, [row for row in ledger if row["call_uid"] != uid]


def _ledger_and_log(
    records: list[Record], ledger: Ledger, i: int, rng: random.Random
) -> tuple[list[Record], Ledger]:
    out, _ = _edit_rewrite(records, ledger, i, rng)
    return _ledger_edit(out, ledger, i, rng)


CONTROL = "none (control)"

# Name to corruption. Ordered from what a careless edit does to what a forger who owns every
# file does, so the table reads as escalating effort.
MUTATIONS: dict[str, Mutation] = {
    CONTROL: _none,
    "edit a ledger row": _ledger_edit,
    "delete a ledger row": _ledger_delete,
    "edit a record": _edit,
    "edit a record, rehash it": _edit_rehash,
    "delete a record": _delete,
    "edit a record, rewrite after it": _edit_rewrite,
    "edit row and record, rewrite after": _ledger_and_log,
    "delete call from both, rewrite after": _delete_rewrite,
    "insert a forged call, rewrite after": _insert_rewrite,
    "swap two records, rewrite after": _swap_rewrite,
    "truncate the log and the ledger": _truncate,
    "replace the log and the ledger": _replace,
}


@dataclass
class KindRow:
    kind: str
    before: Rate
    after: Rate
    caught_by: Counter[str] = field(default_factory=Counter)


@dataclass
class TamperResults:
    boundary_version: str
    ran_utc: str
    seed: int
    records: int
    anchor_interval: int
    anchors: int
    trials: int
    rows: list[KindRow]
    verify_ms: float

    @property
    def control(self) -> KindRow:
        return next(r for r in self.rows if r.kind == CONTROL)

    @property
    def corruptions(self) -> list[KindRow]:
        return [r for r in self.rows if r.kind != CONTROL]

    @property
    def before(self) -> Rate:
        rs = self.corruptions
        return Rate(sum(r.before.hits for r in rs), sum(r.before.total for r in rs))

    @property
    def after(self) -> Rate:
        rs = self.corruptions
        return Rate(sum(r.after.hits for r in rs), sum(r.after.total for r in rs))

    @property
    def false_alarms(self) -> Rate:
        c = self.control
        return Rate(c.before.hits + c.after.hits, c.before.total + c.after.total)

    def kind_table(self) -> list[str]:
        w = max(len(r.kind) for r in self.rows)
        head = f"{'corruption':<{w}}  {'before the last anchor':<24}  {'after it':<24}  caught by"
        lines = [head]
        for r in self.rows:
            by = ", ".join(f"{k} {n}" for k, n in r.caught_by.most_common(3)) or "-"
            lines.append(f"{r.kind:<{w}}  {r.before!s:<24}  {r.after!s:<24}  {by}")
        return lines

    def header(self) -> str:
        return (
            f"boundary {self.boundary_version}, seed {self.seed}: {self.records} records, "
            f"anchored every {self.anchor_interval} ({self.anchors} anchors, the last "
            f"{self.records - self.anchors * self.anchor_interval} records unanchored), "
            f"{self.trials} trials per kind per region"
        )

    def doc_block(self) -> str:
        return "\n".join(["```", self.header(), "", *self.kind_table(), "```"])

    def table(self) -> str:
        lines = [self.header(), "", *self.kind_table()]
        lines += [
            "",
            f"detected before the last anchor  {self.before}",
            f"detected after it                {self.after}",
            f"false alarms (control)           {self.false_alarms}",
            f"verify, {self.records} records         {self.verify_ms:.2f} ms",
            "",
            "A rewrite after the last anchor is not detected, by design: the window is one "
            "anchor interval, which is why anchors are published daily.",
        ]
        return "\n".join(lines)

    def readme_row(self) -> str:
        undetected = [r.kind for r in self.corruptions if r.after.hits == 0]
        return (
            f"| {self.before.hits:,}/{self.before.total:,} = {self.before}, "
            f"{len(self.corruptions)} kinds | {self.false_alarms.hits}/"
            f"{self.false_alarms.total} = {self.false_alarms} | {self.after.hits:,}/"
            f"{self.after.total:,} = {self.after}; {len(undetected)} of "
            f"{len(self.corruptions)} kinds never, by design | "
            f"{self.verify_ms:.1f} ms for {self.records} records |"
        )


def run(
    *, records: int = 500, anchor_interval: int = 50, trials: int = 200, seed: int = 20260922
) -> TamperResults:
    from boundary import __version__

    if records < 2 * anchor_interval:
        raise ValueError("need at least two anchor intervals: one anchored, one not")
    rng = random.Random(seed)
    ledger = synthetic_ledger(records, seed=seed)
    chain = build(ledger_body(row, sealed_utc=SEALED_UTC) for row in ledger)
    # Anchored every interval, except the last: the tail is the window since the last
    # anchor, which is where a real log always has some records.
    anchors = [
        Anchor(seq=s, head=chain[s - 1].record_hash, ts_utc=SEALED_UTC)
        for s in range(anchor_interval, records - anchor_interval + 1, anchor_interval)
    ]
    last = anchors[-1].seq

    t0 = time.perf_counter()
    baseline = verify(chain, anchors, ledger_rows=ledger)
    verify_ms = (time.perf_counter() - t0) * 1000
    if not baseline.ok:
        raise AssertionError(f"an untouched chain failed verification: {baseline.breaks[:3]}")

    rows: list[KindRow] = []
    for kind, mutate in MUTATIONS.items():
        caught: Counter[str] = Counter()
        hits = {"before": 0, "after": 0}
        for region, lo, hi in (("before", 0, last - 1), ("after", last, records - 1)):
            for _ in range(trials):
                i = rng.randint(lo, hi)
                recs, led = mutate(list(chain), [dict(r) for r in ledger], i, rng)
                v = verify(recs, anchors, ledger_rows=led)
                if not v.ok:
                    hits[region] += 1
                    caught[v.breaks[0].kind] += 1
        rows.append(
            KindRow(
                kind,
                before=Rate(hits["before"], trials),
                after=Rate(hits["after"], trials),
                caught_by=caught,
            )
        )
    return TamperResults(
        boundary_version=__version__,
        ran_utc=dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        seed=seed,
        records=records,
        anchor_interval=anchor_interval,
        anchors=len(anchors),
        trials=trials,
        rows=rows,
        verify_ms=verify_ms,
    )


def to_json(results: TamperResults) -> str:
    payload = {
        "boundary_version": results.boundary_version,
        "ran_utc": results.ran_utc,
        "seed": results.seed,
        "records": results.records,
        "anchor_interval": results.anchor_interval,
        "anchors": results.anchors,
        "trials": results.trials,
        "verify_ms": round(results.verify_ms, 3),
        "rows": [
            {
                "kind": r.kind,
                "before": [r.before.hits, r.before.total],
                "after": [r.after.hits, r.after.total],
                "caught_by": dict(r.caught_by),
            }
            for r in results.rows
        ],
        "before": str(results.before),
        "after": str(results.after),
        "false_alarms": str(results.false_alarms),
    }
    return json.dumps(payload, indent=2)


def _fill(path: Path, start_marker: str, end_marker: str, block: str) -> None:
    text = path.read_text(encoding="utf-8")
    start = text.index(start_marker)
    end = text.index(end_marker)
    path.write_text(
        text[: start + len(start_marker)] + "\n" + block + "\n" + text[end:], encoding="utf-8"
    )


def write_readme(readme: Path, row: str) -> None:
    _fill(readme, README_START, README_END, row)


def write_doc(doc: Path, block: str) -> None:
    """The per-kind table in docs/audit.md, between its markers."""
    _fill(doc, DOC_START, DOC_END, block)


__all__ = [
    "CONTROL",
    "MUTATIONS",
    "KindRow",
    "TamperResults",
    "run",
    "synthetic_ledger",
    "to_json",
    "write_doc",
    "write_readme",
]
