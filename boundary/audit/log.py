"""The audit log on disk: an append-only SQLite table, sealing, and the anchor file.

Append-only is enforced twice and relied on once. Triggers refuse UPDATE and DELETE, which
stops an accident and a careless script. They do not stop the operator, who owns the file
and can drop a trigger; nothing on the operator's own machine can. The chain makes an edit
visible and the anchors make a rewrite visible, which is the part that holds against the
operator, and it is why PLAN.md B2.4 anchors in a public repository rather than locally.

Sealing reads a ledger and appends one record per row that is new or has changed since it
was last sealed. The gateway is not touched: a call still writes its ledger row before it
returns and nothing else, so the pass-through path and the overhead figure are exactly what
they were. Part B's proxy will append as it answers; until then a seal after a run is the
same chain written later, and the window between a call and its seal is stated rather than
hidden (docs/audit.md).
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from boundary.audit.chain import (
    GENESIS,
    Anchor,
    Record,
    latest_sealed,
    ledger_body,
    link,
    sealed_fields,
)
from boundary.ledger.store import IN_FLIGHT, utc_now

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit (
    seq          INTEGER PRIMARY KEY,
    prev_hash    TEXT NOT NULL,
    record_hash  TEXT NOT NULL,
    body         TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit
BEGIN SELECT RAISE(ABORT, 'audit records are append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit
BEGIN SELECT RAISE(ABORT, 'audit records are append-only'); END;
"""

# A row still in flight is held back this long before it is sealed as it stands. A call
# that is merely slow completes well inside it; one whose process was killed never
# completes, and has to be on the record anyway. When it is sealed in flight and completes
# later, the next seal appends its outcome as a second record for the same call.
DEFAULT_SETTLE_S = 24 * 3600.0


@dataclass(frozen=True, slots=True)
class SealStats:
    sealed: int
    resealed: int
    unchanged: int
    held_in_flight: int
    no_call_uid: int
    head_seq: int
    head: str

    def __str__(self) -> str:
        return (
            f"sealed {self.sealed} new, {self.resealed} changed since last seal, "
            f"{self.unchanged} unchanged, {self.held_in_flight} held in flight, "
            f"{self.no_call_uid} without a call_uid; head {self.head_seq} {self.head[:16]}"
        )


class AuditLog:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Autocommit, with explicit transactions around each append so that reading the head
        # and writing the next record are one step for any second writer.
        self._conn = sqlite3.connect(self.path, isolation_level=None)
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> AuditLog:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def records(self) -> list[Record]:
        cur = self._conn.execute("SELECT seq, prev_hash, record_hash, body FROM audit ORDER BY seq")
        return [Record(int(s), str(p), str(h), str(b)) for s, p, h, b in cur.fetchall()]

    def head(self) -> tuple[int, str]:
        row = self._conn.execute(
            "SELECT seq, record_hash FROM audit ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        return (0, GENESIS) if row is None else (int(row[0]), str(row[1]))

    def append_bodies(self, bodies: Sequence[str]) -> list[Record]:
        """Append canonical bodies in one transaction, each linked to the one before."""
        out: list[Record] = []
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            seq, prev = self.head()
            for body in bodies:
                seq += 1
                h = link(prev, body)
                self._conn.execute(
                    "INSERT INTO audit (seq, prev_hash, record_hash, body) VALUES (?, ?, ?, ?)",
                    (seq, prev, h, body),
                )
                out.append(Record(seq, prev, h, body))
                prev = h
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        return out

    def seal(
        self,
        ledger_rows: Sequence[Mapping[str, Any]],
        *,
        settle_s: float = DEFAULT_SETTLE_S,
        now: dt.datetime | None = None,
    ) -> SealStats:
        """Append a record for every ledger row that is new or has changed since last sealed.

        Idempotent: sealing the same ledger twice appends nothing the second time. Rows are
        taken in the order given, which for `LedgerStore.rows()` is the ledger's own order.
        """
        now = now or dt.datetime.now(dt.UTC)
        stamp = utc_now()
        latest = {uid: fields for uid, (_, fields) in latest_sealed(self.records()).items()}
        bodies: list[str] = []
        sealed = resealed = unchanged = held = no_uid = 0
        for row in ledger_rows:
            uid = row.get("call_uid")
            if uid is None:
                # A null call_uid in a v2 or later ledger was put there by hand (store.py).
                # Sealing it would give a record nothing can be checked against later.
                no_uid += 1
                continue
            fields = sealed_fields(row)
            before = latest.get(str(uid))
            if before == fields:
                unchanged += 1
                continue
            if row.get("error_type") == IN_FLIGHT and _age_s(row, now) < settle_s:
                held += 1
                continue
            bodies.append(ledger_body(row, sealed_utc=stamp))
            latest[str(uid)] = fields
            if before is None:
                sealed += 1
            else:
                resealed += 1
        if bodies:
            self.append_bodies(bodies)
        seq, head = self.head()
        return SealStats(sealed, resealed, unchanged, held, no_uid, seq, head)

    def anchor(self, *, ts_utc: str | None = None) -> Anchor:
        """The anchor for the current head. Publishing it is the caller's job."""
        seq, head = self.head()
        return Anchor(seq=seq, head=head, ts_utc=ts_utc or utc_now())


def _age_s(row: Mapping[str, Any], now: dt.datetime) -> float:
    ts = row.get("ts_utc")
    if not isinstance(ts, str):
        return float("inf")
    try:
        when = dt.datetime.fromisoformat(ts)
    except ValueError:
        return float("inf")
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.UTC)
    return (now - when).total_seconds()


def read_anchors(path: Path) -> list[Anchor]:
    """Every anchor in a file, one canonical JSON line each. A missing file is no anchors."""
    if not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [Anchor.from_line(line) for line in lines if line.strip()]


def append_anchor(path: Path, anchor: Anchor) -> None:
    """Add one line. Never rewrites the lines before it: an anchor file is history."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(anchor.to_line() + "\n")


__all__ = ["DEFAULT_SETTLE_S", "AuditLog", "SealStats", "append_anchor", "read_anchors"]
