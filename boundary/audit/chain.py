"""The hash chain itself: canonical records, links, anchors and verification.

Pure functions over plain values, with no database, so the same code verifies a chain read
from SQLite today and from Postgres in Part B, and so the tamper measurement can corrupt a
chain thousands of times without touching a file.

A record's hash is `sha256(previous_hash || body)`: the previous record's hash as its 32 raw
bytes, followed by the record's canonical JSON as ASCII. The first record links to 32 zero
bytes. Editing any record changes its hash; fixing its hash breaks the link from the next
record; fixing every hash after it produces a chain that is internally perfect, and that is
the one thing a chain cannot catch on its own. An anchor is what catches it: the head hash
at some sequence number, copied somewhere the operator cannot rewrite. PLAN.md B2.4.

A body carries hashes, counts and identifiers from a ledger row and never content, because
it is built from the ledger row and the ledger carries none (CLAUDE.md, no content in
telemetry). The fields are listed in SEALED rather than taken from whatever the row holds,
so a column a later schema adds is not silently pulled into a chain whose records an older
reader has to reproduce.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

GENESIS = "0" * 64
RECORD_SCHEMA = 1
KIND_LEDGER_ROW = "ledger_row"

# The ledger columns a record seals. Not `id`, which is per file and changes in a merge;
# not `raw_path`, which is a path on somebody's disk; not `trace_id` and `span_id`, which
# say where to look in the telemetry rather than what happened. Everything a reviewer would
# ask about a call, and everything that decides what it cost, is here.
SEALED: tuple[str, ...] = (
    "call_uid",
    "env",
    "ts_utc",
    "boundary_version",
    "project",
    "purpose",
    "run_id",
    "mode",
    "provider",
    "alias",
    "model_requested",
    "model_returned",
    "region",
    "residency",
    "data_class",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "price_list",
    "price_sha256",
    "cost_usd",
    "costed",
    "cached",
    "latency_ms",
    "http_status",
    "error_type",
    "retries",
    "request_sha256",
    "response_sha256",
    "batch_id",
    "ttft_ms",
)


def canonical(value: Mapping[str, Any]) -> str:
    """The one JSON spelling of a record: sorted keys, no whitespace, ASCII only.

    `allow_nan=False` because NaN has no JSON spelling and a record that cannot be written
    down exactly cannot be hashed reproducibly.
    """
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


def link(prev_hash: str, body: str) -> str:
    """sha256(previous hash as raw bytes || canonical body as ASCII), as hex."""
    return hashlib.sha256(bytes.fromhex(prev_hash) + body.encode("ascii")).hexdigest()


def sealed_fields(row: Mapping[str, Any]) -> dict[str, Any]:
    """The part of a ledger row a record seals. A column an older ledger lacks is null."""
    return {name: row.get(name) for name in SEALED}


def ledger_body(row: Mapping[str, Any], *, sealed_utc: str) -> str:
    """The canonical body of a record sealing one ledger row as it stands."""
    return canonical(
        {
            "kind": KIND_LEDGER_ROW,
            "schema": RECORD_SCHEMA,
            "sealed_utc": sealed_utc,
            "row": sealed_fields(row),
        }
    )


@dataclass(frozen=True, slots=True)
class Record:
    """One link: its position, the hash it claims to follow, its own hash and its body."""

    seq: int
    prev_hash: str
    record_hash: str
    body: str


@dataclass(frozen=True, slots=True)
class Anchor:
    """The head of a chain at `seq`, published at `ts_utc` somewhere the operator cannot
    rewrite. In Part B, a line committed daily to the public repository."""

    seq: int
    head: str
    ts_utc: str

    def to_line(self) -> str:
        return canonical({"head": self.head, "seq": self.seq, "ts_utc": self.ts_utc})

    @classmethod
    def from_line(cls, line: str) -> Anchor:
        d = json.loads(line)
        return cls(seq=int(d["seq"]), head=str(d["head"]), ts_utc=str(d["ts_utc"]))


def build(bodies: Iterable[str], *, prev_hash: str = GENESIS, first_seq: int = 1) -> list[Record]:
    """Chain bodies in order. What an honest writer does, and what a forger re-runs."""
    out: list[Record] = []
    seq = first_seq
    for body in bodies:
        h = link(prev_hash, body)
        out.append(Record(seq=seq, prev_hash=prev_hash, record_hash=h, body=body))
        prev_hash, seq = h, seq + 1
    return out


@dataclass(frozen=True, slots=True)
class Break:
    """One thing verification found wrong. `seq` is null when it is not about one record."""

    kind: str
    seq: int | None
    detail: str


# What each break kind means, for the report and for docs/audit.md.
BREAK_KINDS: dict[str, str] = {
    "sequence": "a record is not at the position after the one before it",
    "link": "a record does not follow the hash of the record before it",
    "hash": "a record's hash is not the hash of its own body and link",
    "body": "a record's body is not canonical JSON, so it was not written by this library",
    "anchor": "the record at an anchored position is not the one that was anchored",
    "truncated": "an anchor names a position the log no longer reaches",
    "ledger_changed": "a sealed ledger row no longer says what its latest record sealed",
    "ledger_missing": "a sealed call is no longer in the ledger",
}


@dataclass(frozen=True, slots=True)
class Verification:
    records: int
    head: str
    anchors_checked: int
    # Records after the last anchor. A forger who rewrites the chain from inside this window
    # onwards leaves nothing any check here can see; see docs/audit.md.
    unanchored: int
    breaks: tuple[Break, ...]
    ledger_rows: int | None = None
    # Ledger rows with no record yet: written since the last seal, not a break.
    unsealed: int = 0
    # Sealed in flight and completed since: the next seal records the outcome, not a break.
    resealable: int = 0

    @property
    def ok(self) -> bool:
        return not self.breaks

    def summary(self) -> str:
        lines = [
            f"records {self.records}, head {self.head[:16]}, anchors checked "
            f"{self.anchors_checked}, unanchored tail {self.unanchored}",
        ]
        if self.ledger_rows is not None:
            lines.append(
                f"ledger rows {self.ledger_rows}, unsealed {self.unsealed}, "
                f"resealable {self.resealable}"
            )
        if self.ok:
            lines.append("chain intact")
        else:
            lines.append(f"{len(self.breaks)} break(s):")
            for b in self.breaks[:20]:
                where = f"seq {b.seq}" if b.seq is not None else "log"
                lines.append(f"  {b.kind:<15} {where:<10} {b.detail}")
            if len(self.breaks) > 20:
                lines.append(f"  ... and {len(self.breaks) - 20} more")
        lines.append(
            "What an intact chain does not prove: that nothing was rewritten after the last "
            "anchor, or that the ledger recorded every call. It proves the records up to the "
            "last anchor are the ones that were anchored."
        )
        return "\n".join(lines)


def verify(
    records: Sequence[Record],
    anchors: Sequence[Anchor] = (),
    *,
    ledger_rows: Sequence[Mapping[str, Any]] | None = None,
) -> Verification:
    """Recompute the chain, check it against every anchor, and optionally against a ledger.

    Every break is reported rather than the first, because a reviewer deciding what was
    changed needs the extent of it and not only the fact.
    """
    breaks: list[Break] = []
    prev, expected = GENESIS, 1
    latest: dict[str, tuple[int, dict[str, Any]]] = {}
    for r in records:
        if r.seq != expected:
            breaks.append(Break("sequence", r.seq, f"expected {expected}"))
        if r.prev_hash != prev:
            breaks.append(Break("link", r.seq, "does not follow the record before it"))
        if link(r.prev_hash, r.body) != r.record_hash:
            breaks.append(Break("hash", r.seq, "hash does not match body"))
        try:
            parsed = json.loads(r.body)
        except ValueError:
            breaks.append(Break("body", r.seq, "not JSON"))
        else:
            if not isinstance(parsed, dict) or canonical(parsed) != r.body:
                breaks.append(Break("body", r.seq, "not canonical"))
            else:
                _note_sealed(latest, r.seq, parsed)
        # Follow the STORED hash, so one edited record is one break at that record plus one
        # at the next, rather than every record after it.
        prev, expected = r.record_hash, r.seq + 1

    by_seq = {r.seq: r.record_hash for r in records}
    last_seq = records[-1].seq if records else 0
    checked = 0
    anchored_to = 0
    for a in anchors:
        checked += 1
        if a.seq == 0 and a.head == GENESIS:
            continue
        if a.seq > last_seq:
            breaks.append(Break("truncated", a.seq, f"log ends at {last_seq}"))
            continue
        if by_seq.get(a.seq) != a.head:
            breaks.append(Break("anchor", a.seq, f"anchored {a.head[:16]} at {a.ts_utc}"))
        anchored_to = max(anchored_to, a.seq)

    unsealed = resealable = 0
    n_ledger: int | None = None
    if ledger_rows is not None:
        n_ledger = len(ledger_rows)
        present: set[str] = set()
        for row in ledger_rows:
            uid = row.get("call_uid")
            if uid is None:
                continue
            present.add(str(uid))
            sealed = latest.get(str(uid))
            if sealed is None:
                unsealed += 1
                continue
            seq, fields = sealed
            if fields == sealed_fields(row):
                continue
            if fields.get("error_type") == "in_flight" and row.get("error_type") != "in_flight":
                resealable += 1
                continue
            changed = sorted(k for k in SEALED if fields.get(k) != row.get(k))
            breaks.append(Break("ledger_changed", seq, f"call {uid}: {', '.join(changed)}"))
        for uid, (seq, _) in latest.items():
            if uid not in present:
                breaks.append(Break("ledger_missing", seq, f"call {uid}"))

    return Verification(
        records=len(records),
        head=records[-1].record_hash if records else GENESIS,
        anchors_checked=checked,
        unanchored=last_seq - anchored_to,
        breaks=tuple(breaks),
        ledger_rows=n_ledger,
        unsealed=unsealed,
        resealable=resealable,
    )


def latest_sealed(records: Iterable[Record]) -> dict[str, tuple[int, dict[str, Any]]]:
    """call_uid to (seq, sealed fields) of the newest record for that call.

    A body that does not parse is skipped here; `verify` reports it as a break already.
    """
    out: dict[str, tuple[int, dict[str, Any]]] = {}
    for r in records:
        try:
            d = json.loads(r.body)
        except ValueError:
            continue
        if isinstance(d, dict):
            _note_sealed(out, r.seq, d)
    return out


def _note_sealed(
    out: dict[str, tuple[int, dict[str, Any]]], seq: int, body: Mapping[str, Any]
) -> None:
    if body.get("kind") != KIND_LEDGER_ROW:
        return
    row = body.get("row")
    if isinstance(row, dict) and row.get("call_uid") is not None:
        out[str(row["call_uid"])] = (seq, row)


__all__ = [
    "BREAK_KINDS",
    "GENESIS",
    "KIND_LEDGER_ROW",
    "RECORD_SCHEMA",
    "SEALED",
    "Anchor",
    "Break",
    "Record",
    "Verification",
    "build",
    "canonical",
    "latest_sealed",
    "ledger_body",
    "link",
    "sealed_fields",
    "verify",
]
