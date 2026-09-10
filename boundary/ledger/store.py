"""SQLite ledger store. Local-first: one file per environment, merged later by the CLI.

Two-phase writes: `begin` inserts the row before the request leaves the process, carrying
the pre-call estimate as cost_usd and error_type 'in_flight'; `complete` replaces the
estimate with the actual cost and the outcome. A process killed between the two leaves an
in_flight row, which is what "no call escapes the ledger" means in practice.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
_SCHEMA_PATH = Path(__file__).with_name("schema.sql")

IN_FLIGHT = "in_flight"


def utc_now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass(slots=True)
class LedgerRow:
    """One row, schema v1. Field names are the column names."""

    ts_utc: str
    boundary_version: str
    project: str
    purpose: str
    mode: str
    provider: str
    model_requested: str
    run_id: str | None = None
    alias: str | None = None
    model_returned: str | None = None
    region: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    price_list: str | None = None
    cost_usd: float | None = None
    costed: bool = False
    cached: bool = False
    latency_ms: float | None = None
    http_status: int | None = None
    error_type: str | None = None
    retries: int = 0
    request_sha256: str | None = None
    response_sha256: str | None = None
    trace_id: str | None = None
    span_id: str | None = None
    raw_path: str | None = None
    id: int | None = field(default=None)

    def as_columns(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("id")
        d["costed"] = int(self.costed)
        d["cached"] = int(self.cached)
        return d


class LedgerStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
        cur = self._conn.execute("SELECT MAX(version) FROM schema_version")
        current = cur.fetchone()[0]
        if current is None:
            self._conn.execute(
                "INSERT INTO schema_version (version, applied_utc) VALUES (?, ?)",
                (SCHEMA_VERSION, utc_now()),
            )
        elif current > SCHEMA_VERSION:
            raise RuntimeError(
                f"ledger {path} has schema v{current}, newer than this library's v{SCHEMA_VERSION}"
            )

    def begin(self, row: LedgerRow) -> int:
        """Insert the row before the request is sent. Returns the row id."""
        row.error_type = IN_FLIGHT
        cols = row.as_columns()
        names = ", ".join(cols)
        marks = ", ".join("?" for _ in cols)
        with self._lock:
            cur = self._conn.execute(
                f"INSERT INTO ledger ({names}) VALUES ({marks})", tuple(cols.values())
            )
            row.id = int(cur.lastrowid or 0)
        return row.id

    def complete(self, row: LedgerRow) -> None:
        """Write the outcome over the in_flight row. Must be called with the row's id set."""
        if row.id is None:
            raise ValueError("complete() needs a row that begin() returned")
        if row.error_type == IN_FLIGHT:
            row.error_type = None
        cols = row.as_columns()
        sets = ", ".join(f"{k} = ?" for k in cols)
        with self._lock:
            self._conn.execute(f"UPDATE ledger SET {sets} WHERE id = ?", (*cols.values(), row.id))

    def spend_usd(
        self,
        *,
        project: str | None,
        year_month: str | None = None,
        run_id: str | None = None,
    ) -> float:
        """Sum of cost_usd for the scope. In-flight rows count at their estimate. Uncosted
        rows have no cost and cannot count; the uncosted count is reported separately."""
        where = ["cost_usd IS NOT NULL"]
        args: list[Any] = []
        if project is not None:
            where.append("project = ?")
            args.append(project)
        if year_month is not None:
            where.append("substr(ts_utc, 1, 7) = ?")
            args.append(year_month)
        if run_id is not None:
            where.append("run_id = ?")
            args.append(run_id)
        with self._lock:
            cur = self._conn.execute(
                f"SELECT COALESCE(SUM(cost_usd), 0) FROM ledger WHERE {' AND '.join(where)}", args
            )
            return float(cur.fetchone()[0])

    def uncosted_count(self, *, project: str | None = None) -> int:
        where = "costed = 0 AND error_type IS NULL AND http_status BETWEEN 200 AND 299"
        args: list[Any] = []
        if project is not None:
            where += " AND project = ?"
            args.append(project)
        with self._lock:
            cur = self._conn.execute(f"SELECT COUNT(*) FROM ledger WHERE {where}", args)
            return int(cur.fetchone()[0])

    def rows(self, *, project: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            if project is None:
                cur = self._conn.execute("SELECT * FROM ledger ORDER BY id")
            else:
                cur = self._conn.execute(
                    "SELECT * FROM ledger WHERE project = ? ORDER BY id", (project,)
                )
            return [dict(r) for r in cur.fetchall()]

    def count(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0])

    def close(self) -> None:
        with self._lock:
            self._conn.close()
