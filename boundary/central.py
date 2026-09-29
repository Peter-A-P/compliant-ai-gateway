"""The central ledger (0.27, PLAN.md B1's dashboard and its completeness figure).

Every environment keeps its own ledger, local-first, which is the Part A design Rule C
defends (docs/rejected.md): a remote ledger as the only record lost calls in every outage
simulated. The central ledger is the other half. Each environment pushes a copy of its rows
to the hosted proxy after the fact (`boundary ledger push`), and the proxy merges them by
`call_uid` into one file the dashboard reads. A push is safe to repeat, as a merge is.

**Completeness is measured, not assumed.** A push says how many rows its local ledger held
and sends every one; the central file records which `call_uid`s each source has delivered.
The dashboard's completeness panel is then "rows held centrally of rows the source says it
has", per source, with the time of its last push. A push that died half way shows as short.

What crosses the network is a ledger row, which holds no content by construction (CLAUDE.md:
hashes, counts and identifiers). One column is dropped on the way in: `raw_path`, a path on
the machine that made a pass-through call, which names that machine's directories and means
nothing anywhere else.

Standard library and `LedgerStore` only, so the push side needs no server extra.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
import threading
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from boundary.ledger.store import IN_FLIGHT, LedgerStore, MergeStats, utc_now

# Dropped from every row on the way in (see the module docstring).
DROPPED = ("raw_path",)
# The most rows one ingest request may carry: about 1 KB each, under Caddy's 2 MB body limit.
MAX_BATCH = 1000

_SOURCES = """
CREATE TABLE IF NOT EXISTS ingest_source (
    source      TEXT PRIMARY KEY,
    env         TEXT,
    local_rows  INTEGER NOT NULL,
    last_utc    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ingest_row (
    source      TEXT NOT NULL,
    call_uid    TEXT NOT NULL,
    PRIMARY KEY (source, call_uid)
);
"""


@dataclass(frozen=True, slots=True)
class SourceState:
    source: str
    env: str | None
    local_rows: int
    held: int
    last_utc: str

    @property
    def complete(self) -> bool:
        return self.held >= self.local_rows


@dataclass(frozen=True, slots=True)
class IngestResult:
    stats: MergeStats
    source: SourceState

    def to_json(self) -> dict[str, Any]:
        return {
            "inserted": self.stats.inserted,
            "completed": self.stats.completed,
            "already_held": self.stats.skipped,
            "source": self.source.source,
            "local_rows": self.source.local_rows,
            "held": self.source.held,
        }


class CentralLedger:
    """A ledger file that accepts pushed rows, and the record of who pushed what."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.ledger = LedgerStore(self.path)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.executescript(_SOURCES)
        self._lock = threading.Lock()

    def close(self) -> None:
        self.ledger.close()
        self._conn.close()

    def ingest(
        self,
        rows: Sequence[Mapping[str, Any]],
        *,
        source: str,
        env: str | None,
        local_rows: int,
    ) -> IngestResult:
        if not source or len(source) > 200:
            raise ValueError("a source is named, in at most 200 characters")
        if local_rows < 0:
            raise ValueError("local_rows cannot be negative")
        if len(rows) > MAX_BATCH:
            raise ValueError(f"at most {MAX_BATCH} rows a request; this one has {len(rows)}")
        clean = []
        for row in rows:
            uid = row.get("call_uid")
            if not isinstance(uid, str) or not uid:
                raise ValueError("every row needs its call_uid")
            clean.append({k: (None if k in DROPPED else v) for k, v in row.items()})
        with self._lock:
            stats = self.ledger.merge_rows(clean, source=source)
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.executemany(
                    "INSERT OR IGNORE INTO ingest_row (source, call_uid) VALUES (?, ?)",
                    [(source, str(r["call_uid"])) for r in clean],
                )
                self._conn.execute(
                    "INSERT INTO ingest_source (source, env, local_rows, last_utc) "
                    "VALUES (?, ?, ?, ?) ON CONFLICT(source) DO UPDATE SET "
                    "env = excluded.env, local_rows = excluded.local_rows, "
                    "last_utc = excluded.last_utc",
                    (source, env, local_rows, utc_now()),
                )
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
        state = next(s for s in self.sources() if s.source == source)
        return IngestResult(stats, state)

    def sources(self) -> list[SourceState]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT s.source, s.env, s.local_rows, s.last_utc, "
                "(SELECT COUNT(*) FROM ingest_row r WHERE r.source = s.source) "
                "FROM ingest_source s ORDER BY s.source"
            )
            return [
                SourceState(str(a), None if b is None else str(b), int(c), int(e), str(d))
                for a, b, c, d, e in cur.fetchall()
            ]

    def rows(self) -> list[dict[str, Any]]:
        return self.ledger.rows()


# -- what the dashboard shows ----------------------------------------------------------------


def _pct(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    return s[min(len(s) - 1, round(q * (len(s) - 1)))]


@dataclass
class Group:
    """Calls, errors and cost for one slice of the ledger. Latency is over answered,
    uncached calls only: a cache hit or a refusal has no upstream time to report."""

    key: tuple[str, ...]
    calls: int = 0
    errors: int = 0
    refused: int = 0
    in_flight: int = 0
    cached: int = 0
    uncosted: int = 0
    cost_usd: float = 0.0
    latencies: list[float] | None = None

    def add(self, row: Mapping[str, Any]) -> None:
        self.calls += 1
        error = row.get("error_type")
        if error == IN_FLIGHT:
            self.in_flight += 1
        elif error in ("policy_refused", "redaction_refused", "injection_blocked"):
            self.refused += 1
        elif error:
            self.errors += 1
        if row.get("cached"):
            self.cached += 1
        status = row.get("http_status")
        if not row.get("costed") and not error and isinstance(status, int) and 200 <= status < 300:
            # The ledger's own definition (`uncosted_count`): an answered call with no price.
            self.uncosted += 1
        cost = row.get("cost_usd")
        if row.get("costed") and isinstance(cost, int | float):
            self.cost_usd += float(cost)
        lat = row.get("latency_ms")
        if not error and not row.get("cached") and isinstance(lat, int | float):
            if self.latencies is None:
                self.latencies = []
            self.latencies.append(float(lat))

    @property
    def p50_ms(self) -> float | None:
        return _pct(self.latencies or [], 0.50)

    @property
    def p95_ms(self) -> float | None:
        return _pct(self.latencies or [], 0.95)


def group_by(
    rows: Iterable[Mapping[str, Any]], *keys: str, since: str | None = None
) -> list[Group]:
    out: dict[tuple[str, ...], Group] = {}
    for row in rows:
        if since is not None and str(row.get("ts_utc") or "") < since:
            continue
        k = tuple(
            str(row.get(name) or "-") if name != "day" else str(row.get("ts_utc") or "")[:10]
            for name in keys
        )
        out.setdefault(k, Group(k)).add(row)
    return sorted(out.values(), key=lambda g: g.key)


def union(*ledgers: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Rows from several files, one per `call_uid`, a completed copy preferred over one
    still in flight."""
    seen: dict[str, dict[str, Any]] = {}
    for rows in ledgers:
        for row in rows:
            uid = str(row.get("call_uid"))
            held = seen.get(uid)
            if held is None or (
                held.get("error_type") == IN_FLIGHT and row.get("error_type") != IN_FLIGHT
            ):
                seen[uid] = dict(row)
    return list(seen.values())


def days_ago(n: int, *, now: dt.datetime | None = None) -> str:
    when = (now or dt.datetime.now(dt.UTC)) - dt.timedelta(days=n)
    return when.strftime("%Y-%m-%d")


__all__ = [
    "DROPPED",
    "MAX_BATCH",
    "CentralLedger",
    "Group",
    "IngestResult",
    "SourceState",
    "days_ago",
    "group_by",
    "union",
]
