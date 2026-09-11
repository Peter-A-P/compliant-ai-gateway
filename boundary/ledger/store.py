"""SQLite ledger store. Local-first: one file per environment, merged by `ledger merge`.

Two-phase writes: `begin` inserts the row before the request leaves the process, carrying
the pre-call estimate as cost_usd and error_type 'in_flight'; `complete` replaces the
estimate with the actual cost and the outcome. A process killed between the two leaves an
in_flight row, which is what "no call escapes the ledger" means in practice.

Local-first is a decision with evidence behind it, not a convenience: writing to a central
ledger over the network loses rows or stops the run whenever the network does
(docs/rejected.md). The price of local-first is that the copies have to be combined, which
is what `merge_from` does, and it has to be safe to run twice.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
import threading
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 2
_SCHEMA_PATH = Path(__file__).with_name("schema.sql")

IN_FLIGHT = "in_flight"

# Namespace for the uids the v1-to-v2 migration invents for rows written before call_uid
# existed. Fixed forever: it is what makes migrating two copies of one v1 file produce the
# same uids, so merging both copies into a central ledger yields one row per call.
_LEGACY_NAMESPACE = uuid.UUID("b0f1d2c3-4a5b-6c7d-8e9f-0a1b2c3d4e5f")


def new_call_uid() -> str:
    """A uid for one call, minted in the process that makes it."""
    return uuid.uuid4().hex


def utc_now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass(slots=True)
class LedgerRow:
    """One row, schema v2. Field names are the column names."""

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
    # Minted here rather than by the database: it identifies the call, so it has to be the
    # same value wherever the row is later copied to.
    call_uid: str = field(default_factory=new_call_uid)
    env: str | None = None
    id: int | None = field(default=None)

    def as_columns(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("id")
        d["costed"] = int(self.costed)
        d["cached"] = int(self.cached)
        return d


@dataclass(frozen=True, slots=True)
class MergeStats:
    """What one `merge_from` did. `inserted + completed` is what changed in the
    destination; `skipped` is the count already held, which is what makes a second merge
    of the same source a no-op."""

    source: str
    source_rows: int
    inserted: int
    completed: int
    skipped: int
    dry_run: bool = False

    @property
    def changed(self) -> int:
        return self.inserted + self.completed

    def __str__(self) -> str:
        would = "would insert" if self.dry_run else "inserted"
        return (
            f"{self.source}: {self.source_rows} row(s), {would} {self.inserted}, "
            f"completed {self.completed}, already held {self.skipped}"
        )


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
        self.schema_version = self._migrate()

    # -- schema -------------------------------------------------------------------------

    def _columns(self) -> set[str]:
        return {str(r[1]) for r in self._conn.execute("PRAGMA table_info(ledger)")}

    def _migrate(self) -> int:
        """Bring the file up to SCHEMA_VERSION in place. Additive only: the upgrade adds
        columns and an index, and never rewrites a value a call recorded.

        Every step is re-entrant rather than transactional, which is the safer property for
        a migration that runs on open: a column is added only if it is missing, a uid is
        backfilled only where there is none, and the index is `IF NOT EXISTS`. A process
        killed part way through leaves a file the next open finishes.
        """
        current = self._conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        if current is not None and current > SCHEMA_VERSION:
            raise RuntimeError(
                f"ledger {self.path} has schema v{current}, newer than this library's "
                f"v{SCHEMA_VERSION}. Upgrade boundary rather than writing an old row shape."
            )
        if current is None:
            # A file this library just created: the table already has every v2 column.
            self._conn.execute(
                "INSERT INTO schema_version (version, applied_utc) VALUES (?, ?)",
                (SCHEMA_VERSION, utc_now()),
            )
        elif current < SCHEMA_VERSION:
            have = self._columns()
            for column, decl in (("call_uid", "TEXT"), ("env", "TEXT")):
                if column not in have:
                    self._conn.execute(f"ALTER TABLE ledger ADD COLUMN {column} {decl}")
            self._backfill_call_uids()
            # `env` is left null for rows written before the column existed: which machine
            # made those calls is not recoverable from the row, and a guess in the ledger
            # would be worse than a null.
            self._conn.execute(
                "INSERT INTO schema_version (version, applied_utc) VALUES (?, ?)",
                (SCHEMA_VERSION, utc_now()),
            )
        self._conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ledger_call_uid ON ledger (call_uid)")
        return SCHEMA_VERSION

    def _backfill_call_uids(self) -> None:
        """Give every pre-v2 row a uid derived from the row itself, so that two copies of
        the same v1 file migrate to the same uids and merging both is still one row per
        call. The id is part of the input because two calls in one run can otherwise agree
        on every recorded field."""
        rows = self._conn.execute(
            "SELECT id, ts_utc, project, run_id, request_sha256 FROM ledger WHERE call_uid IS NULL"
        ).fetchall()
        for r in rows:
            key = f"{r['id']}|{r['ts_utc']}|{r['project']}|{r['run_id']}|{r['request_sha256']}"
            self._conn.execute(
                "UPDATE ledger SET call_uid = ? WHERE id = ?",
                (uuid.uuid5(_LEGACY_NAMESPACE, key).hex, r["id"]),
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

    # -- merge --------------------------------------------------------------------------

    def merge_from(self, source: str | Path, *, dry_run: bool = False) -> MergeStats:
        """Copy every row of another ledger file into this one, keyed on `call_uid`.

        Safe to run any number of times on the same source: a call already held is not
        inserted again. A row that was in flight when it was last merged, and has since
        been completed in the source, is completed here too, so repeated merges converge
        on the outcome instead of freezing the first thing they saw. Nothing else is ever
        overwritten: the destination is an accumulation of calls, not a mirror of a file.

        Row ids are not carried across. An id is local to a file; `call_uid` is the call.
        A pass-through row's `raw_path` is copied verbatim and points into the raw store
        the environment that made the call owns, which is why the environment is recorded.

        A schema v1 source is upgraded in place first (the two v2 columns added, uids
        backfilled from the rows themselves). That is additive and changes no recorded
        value, and it happens even under `dry_run`, which suppresses only the writes to
        this ledger.
        """
        src_path = Path(source)
        if not src_path.is_file():
            raise FileNotFoundError(f"no ledger file at {src_path}")
        if src_path.resolve() == self.path.resolve():
            raise ValueError(f"cannot merge {src_path} into itself")
        src = LedgerStore(src_path)
        try:
            rows = src.rows()
        finally:
            src.close()
        inserted = completed = skipped = 0
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                for row in rows:
                    uid = row.get("call_uid")
                    if not uid:
                        raise RuntimeError(
                            f"{src_path} row id {row.get('id')} has no call_uid; merging it "
                            "would duplicate the call on the next merge"
                        )
                    held = self._conn.execute(
                        "SELECT id, error_type FROM ledger WHERE call_uid = ?", (uid,)
                    ).fetchone()
                    cols = {k: v for k, v in row.items() if k != "id"}
                    if held is None:
                        if not dry_run:
                            names = ", ".join(cols)
                            marks = ", ".join("?" for _ in cols)
                            self._conn.execute(
                                f"INSERT INTO ledger ({names}) VALUES ({marks})",
                                tuple(cols.values()),
                            )
                        inserted += 1
                    elif held["error_type"] == IN_FLIGHT and row["error_type"] != IN_FLIGHT:
                        if not dry_run:
                            sets = ", ".join(f"{k} = ?" for k in cols)
                            self._conn.execute(
                                f"UPDATE ledger SET {sets} WHERE id = ?",
                                (*cols.values(), held["id"]),
                            )
                        completed += 1
                    else:
                        skipped += 1
                self._conn.execute("ROLLBACK" if dry_run else "COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
        return MergeStats(
            source=str(src_path),
            source_rows=len(rows),
            inserted=inserted,
            completed=completed,
            skipped=skipped,
            dry_run=dry_run,
        )

    def close(self) -> None:
        with self._lock:
            self._conn.close()
