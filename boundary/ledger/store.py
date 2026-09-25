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
import shutil
import sqlite3
import tempfile
import threading
import uuid
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 8
_SCHEMA_PATH = Path(__file__).with_name("schema.sql")

IN_FLIGHT = "in_flight"

# Files SQLite may keep beside the database in WAL mode. Copied with it when merge reads a
# source, so the copy holds everything the source has committed.
_SIDECARS = ("", "-wal", "-shm")

# Namespace for the uids the v1-to-v2 migration invents for rows written before call_uid
# existed. Fixed forever: it is what makes migrating two copies of one v1 file produce the
# same uids, so merging both copies into a central ledger yields one row per call.
_LEGACY_NAMESPACE = uuid.UUID("b0f1d2c3-4a5b-6c7d-8e9f-0a1b2c3d4e5f")


def new_call_uid() -> str:
    """A uid for one call, minted in the process that makes it."""
    return uuid.uuid4().hex


def legacy_call_uid(
    row_id: object, ts_utc: object, project: object, run_id: object, request_sha256: object
) -> str:
    """The uid the v1-to-v2 migration gives a row written before call_uid existed.

    Derived from the row itself and from a fixed namespace, so two copies of one v1 file
    migrate to the same uids and merging both is still one row per call. The id is part of
    the input because two calls in one run can otherwise agree on every recorded field.
    """
    key = f"{row_id}|{ts_utc}|{project}|{run_id}|{request_sha256}"
    return uuid.uuid5(_LEGACY_NAMESPACE, key).hex


def utc_now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass(slots=True)
class LedgerRow:
    """One row, schema v8. Field names are the column names."""

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
    # Where the request was SENT. Not where it was processed: no vendor reports that, which
    # is the finding this column is careful not to overstate. See docs/ledger.md.
    region: str | None = None
    # How far the request was allowed to travel from that region, as declared on the
    # provider entry (v4). Null means the entry did not declare one, which is what every row
    # written before 2026-09-15 means and is honestly different from "global".
    #
    # It is configuration rather than observation on purpose. Foundry does not report which
    # hosting version served a deployment, Vertex does not report the processing location,
    # and Bedrock strips the routing profile out of the model identifier it echoes back. So
    # a row can say what the operator chose and cannot say what the vendor did, and writing
    # the first while implying the second is the failure this project exists to prevent.
    residency: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    price_list: str | None = None
    # A fingerprint of the rates this row was costed with (v5), not of the file holding them.
    # `price_list` is a date, and a date is not unique across repositories: on 2026-09-18 this
    # library and project 02 both held a `2026-09-12.yaml`, byte-different and, as it turned
    # out, semantically identical. Nothing in a row could have shown it either way. This can.
    # Set on every row that had a price list in force, costed or not, exactly as `price_list`
    # is: the pair records which rates were CONSULTED, and `costed` says whether they had an
    # entry for the model. Null only on a row written before the column existed.
    price_sha256: str | None = None
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
    # Set only on a row that belongs to a vendor batch, and only once the vendor has
    # accepted the batch and named it (v3). Null for every ordinary call.
    batch_id: str | None = None
    # Streamed calls only (v6): wall time to the first content delta, in milliseconds. Null
    # for every call that was not streamed, and for a streamed call that produced no content
    # before it ended. `latency_ms` runs to the last byte either way.
    ttft_ms: float | None = None
    # What kind of data the caller said the request carried (v7): one of the four words in
    # `boundary.types.DataClass`, validated before the row is written. Null means the caller
    # declared nothing, which is kept apart from every declared class for the same reason
    # `residency` keeps null apart from `global`: no claim is not the weakest claim. The
    # library never fills this in from the content; a gateway that guessed a classification
    # would be making a compliance decision nobody reviewed.
    data_class: str | None = None
    # Whether the request was sent redacted (v8): True when the caller says so, which the
    # proxy does when it redacted the payload itself, and null otherwise. Never False: a
    # caller that says nothing has made no claim, and the library cannot look. Null on every
    # row written before the column existed.
    redacted: bool | None = None
    id: int | None = field(default=None)

    def as_columns(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("id")
        d["costed"] = int(self.costed)
        d["cached"] = int(self.cached)
        return d

    @classmethod
    def from_columns(cls, row: dict[str, Any]) -> LedgerRow:
        """Rebuild a row object from a ledger record, so that a process which did not write
        the row can still complete it. This is what lets a batch be submitted in one process
        and its results collected, hours later, in another."""
        known = {f.name for f in fields(cls)}
        values = {k: v for k, v in row.items() if k in known}
        values["costed"] = bool(values.get("costed"))
        values["cached"] = bool(values.get("cached"))
        return cls(**values)


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
    def __init__(self, path: Path, *, display_path: Path | None = None) -> None:
        self.path = path
        # What an error message calls this file. Merge opens a temporary copy of a source,
        # and a message naming the copy would send the reader to a file that no longer
        # exists by the time they read it.
        self._display = display_path or path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        try:
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=FULL")
            self._conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
            self.schema_version = self._migrate()
        except BaseException:
            # An open that fails part way through must not leave the file held. On Windows
            # a leaked handle stops the file being deleted or even reopened, which turns a
            # clear error about one ledger into a confusing one about another.
            self._conn.close()
            raise

    # -- schema -------------------------------------------------------------------------

    def _columns(self) -> set[str]:
        return {str(r[1]) for r in self._conn.execute("PRAGMA table_info(ledger)")}

    def _migrate(self) -> int:
        """Bring the file up to SCHEMA_VERSION in place. Additive only: the upgrade adds
        columns and indexes, and never rewrites a value a call recorded.

        One step per version, so that what a step does is decided by where the file started
        and not by where it ended. That matters for the uid backfill: uids are invented only
        for a file that predates the column. A null call_uid in a v2 or later file was put
        there by hand, and inventing one would let the same call merge twice.

        Every step is re-entrant rather than transactional, which is the safer property for
        a migration that runs on open: a column is added only if it is missing, a uid is
        backfilled only where there is none, and the indexes are `IF NOT EXISTS`. A process
        killed part way through leaves a file the next open finishes.
        """
        current = self._conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        if current is not None and current > SCHEMA_VERSION:
            raise RuntimeError(
                f"ledger {self._display} has schema v{current}, newer than this library's "
                f"v{SCHEMA_VERSION}. Upgrade boundary rather than writing an old row shape."
            )
        if current is None:
            # A file this library just created: the table already has every column.
            self._conn.execute(
                "INSERT INTO schema_version (version, applied_utc) VALUES (?, ?)",
                (SCHEMA_VERSION, utc_now()),
            )
        elif current < SCHEMA_VERSION:
            if current < 2:
                self._add_columns(("call_uid", "TEXT"), ("env", "TEXT"))
                self._backfill_call_uids()
                # `env` is left null for rows written before the column existed: which
                # machine made those calls is not recoverable from the row, and a guess in
                # the ledger would be worse than a null.
            if current < 3:
                self._add_columns(("batch_id", "TEXT"))
            if current < 4:
                # Left null for every existing row rather than backfilled. A row written
                # before the column existed was written by a configuration that did not
                # declare a residency, and inventing one would put a claim in the ledger
                # that nobody made.
                self._add_columns(("residency", "TEXT"))
            if current < 5:
                # Null for every existing row rather than backfilled. The rates a past row
                # used are not recoverable from the row: the file it named may have been
                # one of several with that date, which is the whole reason for the column.
                self._add_columns(("price_sha256", "TEXT"))
            if current < 6:
                # Null for every existing row: none of them was streamed.
                self._add_columns(("ttft_ms", "REAL"))
            if current < 7:
                # Null for every existing row. Nobody declared a class on a call made before
                # there was a way to, and a value here would be a claim the caller never
                # made about data the library never looked at.
                self._add_columns(("data_class", "TEXT"))
            if current < 8:
                # Null for every existing row: no call before 0.15 said it was redacted.
                self._add_columns(("redacted", "INTEGER"))
            self._conn.execute(
                "INSERT INTO schema_version (version, applied_utc) VALUES (?, ?)",
                (SCHEMA_VERSION, utc_now()),
            )
        self._conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ledger_call_uid ON ledger (call_uid)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS ledger_batch ON ledger (batch_id)")
        return SCHEMA_VERSION

    def _add_columns(self, *columns: tuple[str, str]) -> None:
        have = self._columns()
        for column, decl in columns:
            if column not in have:
                self._conn.execute(f"ALTER TABLE ledger ADD COLUMN {column} {decl}")

    def _backfill_call_uids(self) -> None:
        """Give every pre-v2 row a uid derived from the row itself. Run only on the v1 step:
        see `_migrate`."""
        rows = self._conn.execute(
            "SELECT id, ts_utc, project, run_id, request_sha256 FROM ledger WHERE call_uid IS NULL"
        ).fetchall()
        for r in rows:
            self._conn.execute(
                "UPDATE ledger SET call_uid = ? WHERE id = ?",
                (
                    legacy_call_uid(
                        r["id"], r["ts_utc"], r["project"], r["run_id"], r["request_sha256"]
                    ),
                    r["id"],
                ),
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

    # -- batches ------------------------------------------------------------------------

    def set_batch_id(self, ids: Sequence[int], batch_id: str) -> None:
        """Name the batch the given in-flight rows belong to.

        Separate from `complete` because these rows stay in flight: the vendor has accepted
        the requests and will bill for them, but no result has arrived yet. The rows are
        written before the submit leaves the process, so this is the one field that cannot
        be known until after it returns.
        """
        with self._lock:
            self._conn.executemany(
                "UPDATE ledger SET batch_id = ? WHERE id = ?", [(batch_id, i) for i in ids]
            )

    def rows_for_batch(self, batch_id: str) -> list[dict[str, Any]]:
        """Every row of one batch, in the order they were submitted. This is what makes a
        batch collectable by a process that did not submit it."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM ledger WHERE batch_id = ? ORDER BY id", (batch_id,)
            )
            return [dict(r) for r in cur.fetchall()]

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

        A source is never written to, whatever schema it is at: see `_read_source_rows`.
        """
        src_path = Path(source)
        if not src_path.is_file():
            raise FileNotFoundError(f"no ledger file at {src_path}")
        if src_path.resolve() == self.path.resolve():
            raise ValueError(f"cannot merge {src_path} into itself")
        rows = _read_source_rows(src_path)
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


def _read_source_rows(src_path: Path) -> list[dict[str, Any]]:
    """Read another environment's ledger without writing a byte to it.

    Merging must never modify a source. Opening a ledger upgrades it in place, and a
    library version older than the one that upgraded it then refuses to write to the file
    by design, so merging an environment's ledger would stop that environment appending to
    it. Project 03 pins boundary 0.1.0 and commits one ledger per arm per month; merging
    those files for the monthly invoice check must leave every one of them exactly as the
    runner left it, ready for the next run.

    The rows are therefore read from a copy: the copy is upgraded to the current schema,
    the source stays at whatever version it was, and a v1 source still merges to the same
    uids because the backfill derives them from the rows rather than inventing them. The
    write-ahead log is copied with the database so the copy holds everything the source
    has committed.
    """
    with tempfile.TemporaryDirectory(prefix="boundary-merge-") as tmp:
        copy = Path(tmp) / src_path.name
        for suffix in _SIDECARS:
            side = src_path.with_name(src_path.name + suffix)
            if side.is_file():
                shutil.copy2(side, copy.with_name(copy.name + suffix))
        store = LedgerStore(copy, display_path=src_path)
        try:
            return store.rows()
        finally:
            store.close()
