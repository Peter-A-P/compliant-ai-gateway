"""The hosted proxy's shared state on Postgres and Redis (0.28, PLAN.md B2 and B2.4).

One proxy process on SQLite saturated below 500 requests a second on every layer
(docs/loadtest.md), and more than one process cannot share SQLite files, an in-memory
request quota or an in-memory semantic cache. So the hosted proxy keeps them here instead:

| What | Where | Why there |
|---|---|---|
| The ledger | Postgres `ledger` | Every worker writes one ledger, and the caps read it |
| The cap check and its row | one function call under an advisory lock (0.29) | Two workers must not both pass a cap only one of them fits under |
| The audit chain | Postgres `audit`, INSERT and SELECT only for the proxy's role | B2.4: append-only by grant, not only by trigger |
| The central ledger | Postgres `central` and its two tables | The dashboard reads it with the ledger |
| The semantic cache | Postgres `semcache`, a pgvector column | Every worker sees every answer stored |
| Team request quotas | Redis, a sorted set per team and one Lua script | A quota is per team, not per worker |

The redaction vault stays in memory: a request is redacted, sent and rehydrated inside one
worker, and never needs another's vault.

**Two roles.** `boundary db init` runs as the database's owner and creates everything, then
grants the proxy's role only what it needs: on `ledger`, SELECT, INSERT and UPDATE (a row is
written before the call and completed after it), never DELETE; on `audit`, SELECT and INSERT
only. A trigger refuses UPDATE and DELETE on `audit` for the owner too. So the proxy cannot
rewrite its own history even if it is taken over, and the owner's password is not on the
proxy's side of the compose file.

The library itself never imports this module. A library caller keeps its SQLite ledger;
only `boundary serve --database-url` and the audit sealer use Postgres.
"""

from __future__ import annotations

import contextlib
import os
import queue
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import fields
from typing import Any

from boundary.audit.chain import GENESIS, Record, link
from boundary.audit.log import Sealing
from boundary.central import DROPPED, MAX_BATCH, IngestResult, SourceState
from boundary.ledger.store import (
    IN_FLIGHT,
    SCHEMA_VERSION,
    Admission,
    LedgerRow,
    MergeStats,
    next_day,
    utc_now,
)
from boundary.semcache import Embedder, Hit, Vector, question_of, scope_of
from boundary.server.teams import QuotaDecision
from boundary.types import ChatRequest

# The ledger's columns in Postgres types. The same names and meanings as schema.sql; the
# SQLite REAL is DOUBLE PRECISION here, and INTEGER is BIGINT.
_COLUMNS: tuple[tuple[str, str], ...] = (
    ("call_uid", "TEXT NOT NULL UNIQUE"),
    ("env", "TEXT"),
    ("ts_utc", "TEXT NOT NULL"),
    ("boundary_version", "TEXT NOT NULL"),
    ("project", "TEXT NOT NULL"),
    ("purpose", "TEXT NOT NULL"),
    ("run_id", "TEXT"),
    ("mode", "TEXT NOT NULL"),
    ("provider", "TEXT NOT NULL"),
    ("alias", "TEXT"),
    ("model_requested", "TEXT NOT NULL"),
    ("model_returned", "TEXT"),
    ("region", "TEXT"),
    ("residency", "TEXT"),
    ("input_tokens", "BIGINT NOT NULL DEFAULT 0"),
    ("output_tokens", "BIGINT NOT NULL DEFAULT 0"),
    ("cache_read_tokens", "BIGINT NOT NULL DEFAULT 0"),
    ("cache_write_tokens", "BIGINT NOT NULL DEFAULT 0"),
    ("price_list", "TEXT"),
    ("price_sha256", "TEXT"),
    ("cost_usd", "DOUBLE PRECISION"),
    ("costed", "BIGINT NOT NULL DEFAULT 0"),
    ("cached", "BIGINT NOT NULL DEFAULT 0"),
    ("latency_ms", "DOUBLE PRECISION"),
    ("http_status", "BIGINT"),
    ("error_type", "TEXT"),
    ("retries", "BIGINT NOT NULL DEFAULT 0"),
    ("request_sha256", "TEXT"),
    ("response_sha256", "TEXT"),
    ("trace_id", "TEXT"),
    ("span_id", "TEXT"),
    ("raw_path", "TEXT"),
    ("batch_id", "TEXT"),
    ("ttft_ms", "DOUBLE PRECISION"),
    ("data_class", "TEXT"),
    ("redacted", "BIGINT"),
    ("injection", "BIGINT"),
    ("cache_similarity", "DOUBLE PRECISION"),
    ("cache_source", "TEXT"),
)
COLUMNS = tuple(name for name, _ in _COLUMNS)
assert set(COLUMNS) == {f.name for f in fields(LedgerRow)} - {"id"}, "schema.sql and pg.py drift"

EMBEDDING_DIM = 384  # bge-small

# Connections per `PgLedger`: one for each of the few threads a worker has writing rows at
# once. The proxy has one ledger per worker, so four workers hold sixteen.
POOL_SIZE = 4

# Advisory lock keys: one for the caps' admission step, one for appending to the chain.
_ADMISSION_KEY = 0x62_6E_64_01
_AUDIT_KEY = 0x62_6E_64_02


def _table(name: str) -> str:
    cols = ",\n    ".join(f"{c} {t}" for c, t in _COLUMNS)
    return f"CREATE TABLE IF NOT EXISTS {name} (\n    id BIGSERIAL PRIMARY KEY,\n    {cols}\n);"


SCHEMA = f"""
CREATE EXTENSION IF NOT EXISTS vector;

{_table("ledger")}
CREATE INDEX IF NOT EXISTS ledger_project_ts ON ledger (project, ts_utc);
CREATE INDEX IF NOT EXISTS ledger_project_run ON ledger (project, run_id);

-- What the caps read: spend per project and month, kept by trigger, as ledger v9 does.
CREATE TABLE IF NOT EXISTS ledger_spend (
    project TEXT NOT NULL,
    month   TEXT NOT NULL,
    cost    DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (project, month)
);
CREATE OR REPLACE FUNCTION ledger_spend_keep() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE delta DOUBLE PRECISION;
BEGIN
    IF TG_OP = 'INSERT' THEN
        delta := COALESCE(NEW.cost_usd, 0);
    ELSE
        delta := COALESCE(NEW.cost_usd, 0) - COALESCE(OLD.cost_usd, 0);
    END IF;
    IF delta <> 0 OR (TG_OP = 'INSERT' AND NEW.cost_usd IS NOT NULL) THEN
        INSERT INTO ledger_spend (project, month, cost)
        VALUES (NEW.project, substr(NEW.ts_utc, 1, 7), delta)
        ON CONFLICT (project, month) DO UPDATE SET cost = ledger_spend.cost + EXCLUDED.cost;
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS ledger_spend_trigger ON ledger;
CREATE TRIGGER ledger_spend_trigger AFTER INSERT OR UPDATE OF cost_usd ON ledger
FOR EACH ROW EXECUTE FUNCTION ledger_spend_keep();

-- The caps' admission step as one statement (0.29): lock, check, write the row in flight.
-- 0.28 ran the same steps as a transaction from Python, and the lock was held across six
-- round trips and whatever else the holding worker's interpreter was doing between them;
-- at 500 requests a second the other three workers queued on it with CPU to spare
-- (docs/loadtest.md). Here the lock is held only while Postgres runs this function. Each
-- statement in a volatile function takes a fresh snapshot, so the sums after the lock see
-- every row the previous holder committed. The checks, their order and their arithmetic are
-- `Admission.check`'s; a refusal writes nothing and names the cap and what was spent.
-- 0.30 adds the day's cap, and with it two arguments; the 0.29 signature is dropped.
DROP FUNCTION IF EXISTS ledger_admit(
    jsonb, text, text, double precision, double precision, double precision, text,
    double precision
);
CREATE OR REPLACE FUNCTION ledger_admit(
    r jsonb, p_project text, p_month text, p_estimate double precision,
    p_project_cap double precision, p_portfolio_cap double precision,
    p_run_id text, p_run_cap double precision, p_day text, p_day_cap double precision
) RETURNS TABLE (row_id bigint, refused text, spent double precision)
LANGUAGE plpgsql VOLATILE AS $$
DECLARE s double precision; new_id bigint;
BEGIN
    PERFORM pg_advisory_xact_lock({_ADMISSION_KEY});
    SELECT COALESCE(SUM(cost), 0) INTO s FROM ledger_spend
        WHERE project = p_project AND month = p_month;
    IF s + p_estimate > p_project_cap THEN
        RETURN QUERY SELECT NULL::bigint, 'project'::text, s; RETURN;
    END IF;
    IF p_day IS NOT NULL AND p_day_cap IS NOT NULL THEN
        SELECT COALESCE(SUM(cost_usd), 0) INTO s FROM ledger
            WHERE cost_usd IS NOT NULL AND project = p_project
              AND ts_utc >= p_day AND ts_utc < to_char(p_day::date + 1, 'YYYY-MM-DD');
        IF s + p_estimate > p_day_cap THEN
            RETURN QUERY SELECT NULL::bigint, 'day'::text, s; RETURN;
        END IF;
    END IF;
    IF p_run_id IS NOT NULL AND p_run_cap IS NOT NULL THEN
        SELECT COALESCE(SUM(cost_usd), 0) INTO s FROM ledger
            WHERE cost_usd IS NOT NULL AND project = p_project AND run_id = p_run_id;
        IF s + p_estimate > p_run_cap THEN
            RETURN QUERY SELECT NULL::bigint, 'run'::text, s; RETURN;
        END IF;
    END IF;
    SELECT COALESCE(SUM(cost), 0) INTO s FROM ledger_spend WHERE month = p_month;
    IF s + p_estimate > p_portfolio_cap THEN
        RETURN QUERY SELECT NULL::bigint, 'portfolio'::text, s; RETURN;
    END IF;
    INSERT INTO ledger ({", ".join(COLUMNS)})
        SELECT {", ".join(COLUMNS)} FROM jsonb_populate_record(NULL::ledger, r)
        RETURNING id INTO new_id;
    RETURN QUERY SELECT new_id, NULL::text, NULL::double precision;
END $$;

CREATE TABLE IF NOT EXISTS schema_version (
    version     BIGINT NOT NULL,
    applied_utc TEXT   NOT NULL
);

CREATE TABLE IF NOT EXISTS audit (
    seq          BIGINT PRIMARY KEY,
    prev_hash    TEXT NOT NULL,
    record_hash  TEXT NOT NULL,
    body         TEXT NOT NULL
);
CREATE OR REPLACE FUNCTION audit_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'audit records are append-only'; END $$;
DROP TRIGGER IF EXISTS audit_no_change ON audit;
CREATE TRIGGER audit_no_change BEFORE UPDATE OR DELETE ON audit
FOR EACH ROW EXECUTE FUNCTION audit_append_only();
DROP TRIGGER IF EXISTS audit_no_truncate ON audit;
CREATE TRIGGER audit_no_truncate BEFORE TRUNCATE ON audit
FOR EACH STATEMENT EXECUTE FUNCTION audit_append_only();

{_table("central")}
CREATE TABLE IF NOT EXISTS ingest_source (
    source      TEXT PRIMARY KEY,
    env         TEXT,
    local_rows  BIGINT NOT NULL,
    last_utc    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ingest_row (
    source      TEXT NOT NULL,
    call_uid    TEXT NOT NULL,
    PRIMARY KEY (source, call_uid)
);

CREATE TABLE IF NOT EXISTS semcache (
    id            BIGSERIAL PRIMARY KEY,
    team          TEXT NOT NULL,
    scope         TEXT NOT NULL,
    embedding     vector({EMBEDDING_DIM}) NOT NULL,
    answer        TEXT NOT NULL,
    source        TEXT,
    model         TEXT,
    finish_reason TEXT,
    created_utc   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS semcache_team_scope ON semcache (team, scope);
-- The nearest stored question by an HNSW index (0.29), not by comparing every one: at the
-- 20,000 entries a team's cache is capped at, the exact scan was 19.6 ms a lookup on the VPS,
-- and 0.9 ms with the index. Approximate, so it can miss the nearest and answer a miss where
-- the scan would have hit; it cannot serve anything below the threshold, which is checked
-- against the similarity of what it returns. Searched with `hnsw.iterative_scan`, so the
-- team and scope filter never leaves it with nothing to return.
CREATE INDEX IF NOT EXISTS semcache_embedding_hnsw ON semcache
    USING hnsw (embedding vector_cosine_ops);
"""

# What the proxy's role may do, table by table. Nothing else is granted, and nothing is
# granted WITH GRANT OPTION.
GRANTS: tuple[tuple[str, str], ...] = (
    ("ledger", "SELECT, INSERT, UPDATE"),
    ("ledger_spend", "SELECT"),
    ("schema_version", "SELECT"),
    ("audit", "SELECT, INSERT"),
    ("central", "SELECT, INSERT, UPDATE"),
    ("ingest_source", "SELECT, INSERT, UPDATE"),
    ("ingest_row", "SELECT, INSERT"),
    ("semcache", "SELECT, INSERT"),
)
SEQUENCES = ("ledger_id_seq", "central_id_seq", "semcache_id_seq")


def _psycopg() -> Any:
    try:
        import psycopg
    except ImportError as e:  # pragma: no cover - the extra's absence is tested in CI
        raise ImportError("Postgres needs the hosted extra: uv sync --extra hosted") from e
    return psycopg


def connect(dsn: str, *, autocommit: bool = True) -> Any:
    psycopg = _psycopg()
    from psycopg.rows import dict_row

    return psycopg.connect(dsn, autocommit=autocommit, row_factory=dict_row)


def init_schema(admin_dsn: str, *, app_role: str | None = None) -> None:
    """Create every table, trigger and index, idempotently, and grant `app_role` what the
    proxy needs and nothing more. Run as the owner, never as the proxy."""
    if app_role is not None and not app_role.replace("_", "").isalnum():
        raise ValueError(f"not a role name: {app_role!r}")
    with connect(admin_dsn) as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (_ADMISSION_KEY + 99,))
        try:
            conn.execute(SCHEMA)
            held = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
            if held is None or held["v"] is None:
                conn.execute(
                    "INSERT INTO schema_version (version, applied_utc) VALUES (%s, %s)",
                    (SCHEMA_VERSION, utc_now()),
                )
            elif int(held["v"]) > SCHEMA_VERSION:
                raise RuntimeError(
                    f"the database has ledger schema v{held['v']}, newer than this "
                    f"library's v{SCHEMA_VERSION}"
                )
            if app_role is not None:
                for table, privileges in GRANTS:
                    conn.execute(f"REVOKE ALL ON {table} FROM {app_role}")
                    conn.execute(f"GRANT {privileges} ON {table} TO {app_role}")
                for seq in SEQUENCES:
                    conn.execute(f"GRANT USAGE ON SEQUENCE {seq} TO {app_role}")
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (_ADMISSION_KEY + 99,))


class _Rollback(Exception):
    """Raised inside a transaction to roll it back: what a dry run does."""


def _pg_values(cols: Mapping[str, Any]) -> tuple[Any, ...]:
    """Column values as Postgres takes them. SQLite stores a Python bool in an INTEGER column
    as 0 or 1; Postgres refuses one in a BIGINT, so `redacted` and `injection` are written as
    the integers the SQLite ledger holds. The first hosted load test found this: every
    redacted request failed with a 500 until it was converted."""
    return tuple(int(v) if isinstance(v, bool) else v for v in cols.values())


def _jsonb(d: Mapping[str, Any]) -> Any:
    from psycopg.types.json import Jsonb

    return Jsonb(d)


def _row(d: Mapping[str, Any]) -> dict[str, Any]:
    return {k: d[k] for k in ("id", *COLUMNS) if k in d}


class PgLedger:
    """The ledger's write and read path on Postgres, for the proxy: what `Gateway` and the
    audit sealer call. A few connections per process (0.29), each used by one thread at a
    time: 0.28 had one, and at 500 requests a second a worker's threads spent a third of
    their time queued on it (docs/loadtest.md)."""

    def __init__(self, dsn: str, *, table: str = "ledger", pool: int = POOL_SIZE) -> None:
        self.dsn = dsn
        self.table = table
        self.path = f"postgres:{table}"
        self._pool = [connect(dsn) for _ in range(max(1, pool))]
        self._idle: queue.LifoQueue[Any] = queue.LifoQueue()
        for conn in self._pool:
            self._idle.put(conn)
        self._held = threading.local()
        self.schema_version = SCHEMA_VERSION

    def close(self) -> None:
        for conn in self._pool:
            conn.close()

    @contextlib.contextmanager
    def _use(self) -> Iterator[Any]:
        """A connection for this thread: the one it already holds inside `admission`, so
        the cap check and the row it admits share that transaction, or a free one."""
        held = getattr(self._held, "conn", None)
        if held is not None:
            yield held
            return
        conn = self._idle.get()
        self._held.conn = conn
        try:
            yield conn
        finally:
            self._held.conn = None
            self._idle.put(conn)

    @contextlib.contextmanager
    def admission(self) -> Iterator[None]:
        """The cap check and the row it admits, as one step for every process: a transaction
        holding an advisory lock that every worker's admission takes."""
        with self._use() as conn, conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (_ADMISSION_KEY,))
            yield

    def admit(self, row: LedgerRow, caps: Admission) -> int:
        """`Admission.check` and `begin` as one call to `ledger_admit`, which holds the
        admission lock only while Postgres runs it."""
        if self.table != "ledger":
            with self.admission():
                caps.check(self)
                return self.begin(row)
        cols = row.as_columns()
        cols["error_type"] = IN_FLIGHT
        record = dict(zip(cols, _pg_values(cols), strict=True))
        with self._use() as conn:
            got = conn.execute(
                "SELECT row_id, refused, spent "
                "FROM ledger_admit(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    _jsonb(record),
                    caps.project,
                    caps.month,
                    caps.estimate,
                    caps.project_monthly_usd,
                    caps.portfolio_monthly_usd,
                    caps.run_id,
                    caps.per_run_usd,
                    caps.day,
                    caps.project_daily_usd,
                ),
            ).fetchone()
        if got["refused"] is not None:
            raise caps.refuse(str(got["refused"]), float(got["spent"]))
        row.error_type = IN_FLIGHT
        row.id = int(got["row_id"])
        return row.id

    def begin(self, row: LedgerRow) -> int:
        row.error_type = IN_FLIGHT
        cols = row.as_columns()
        names = ", ".join(cols)
        marks = ", ".join(["%s"] * len(cols))
        with self._use() as conn:
            got = conn.execute(
                f"INSERT INTO {self.table} ({names}) VALUES ({marks}) RETURNING id",
                _pg_values(cols),
            ).fetchone()
        row.id = int(got["id"])
        return row.id

    def complete(self, row: LedgerRow) -> None:
        if row.id is None:
            raise ValueError("complete() needs a row that begin() returned")
        if row.error_type == IN_FLIGHT:
            row.error_type = None
        cols = row.as_columns()
        sets = ", ".join(f"{k} = %s" for k in cols)
        with self._use() as conn:
            conn.execute(
                f"UPDATE {self.table} SET {sets} WHERE id = %s", (*_pg_values(cols), row.id)
            )

    def spend_usd(
        self,
        *,
        project: str | None,
        year_month: str | None = None,
        run_id: str | None = None,
        day: str | None = None,
    ) -> float:
        with self._use() as conn:
            if year_month is not None and run_id is None and day is None:
                if project is not None:
                    got = conn.execute(
                        "SELECT COALESCE(SUM(cost), 0) AS s FROM ledger_spend "
                        "WHERE project = %s AND month = %s",
                        (project, year_month),
                    ).fetchone()
                else:
                    got = conn.execute(
                        "SELECT COALESCE(SUM(cost), 0) AS s FROM ledger_spend WHERE month = %s",
                        (year_month,),
                    ).fetchone()
                return float(got["s"])
            where = ["cost_usd IS NOT NULL"]
            args: list[Any] = []
            if project is not None:
                where.append("project = %s")
                args.append(project)
            if year_month is not None:
                where.append("substr(ts_utc, 1, 7) = %s")
                args.append(year_month)
            if run_id is not None:
                where.append("run_id = %s")
                args.append(run_id)
            if day is not None:
                where.append("ts_utc >= %s AND ts_utc < %s")
                args += [day, next_day(day)]
            got = conn.execute(
                f"SELECT COALESCE(SUM(cost_usd), 0) AS s FROM {self.table} "
                f"WHERE {' AND '.join(where)}",
                args,
            ).fetchone()
            return float(got["s"])

    def rows(self, *, project: str | None = None) -> list[dict[str, Any]]:
        with self._use() as conn:
            if project is None:
                cur = conn.execute(f"SELECT * FROM {self.table} ORDER BY id")
            else:
                cur = conn.execute(
                    f"SELECT * FROM {self.table} WHERE project = %s ORDER BY id", (project,)
                )
            return [_row(r) for r in cur.fetchall()]

    def rows_after(self, row_id: int) -> list[dict[str, Any]]:
        with self._use() as conn:
            cur = conn.execute(f"SELECT * FROM {self.table} WHERE id > %s ORDER BY id", (row_id,))
            return [_row(r) for r in cur.fetchall()]

    def count(self) -> int:
        with self._use() as conn:
            return int(conn.execute(f"SELECT COUNT(*) AS n FROM {self.table}").fetchone()["n"])

    def column_names(self) -> set[str]:
        return set(COLUMNS)

    def uncosted_count(self, *, project: str | None = None) -> int:
        where = "costed = 0 AND error_type IS NULL AND http_status BETWEEN 200 AND 299"
        args: list[Any] = []
        if project is not None:
            where += " AND project = %s"
            args.append(project)
        with self._use() as conn:
            got = conn.execute(
                f"SELECT COUNT(*) AS n FROM {self.table} WHERE {where}", args
            ).fetchone()
            return int(got["n"])

    def merge_rows(
        self, rows: Sequence[Mapping[str, Any]], *, source: str, dry_run: bool = False
    ) -> MergeStats:
        """`LedgerStore.merge_rows` on Postgres: insert a call not held, complete one held in
        flight, leave the rest. One transaction."""
        inserted = completed = skipped = 0
        with self._use() as conn, contextlib.suppress(_Rollback), conn.transaction():
            for row in rows:
                uid = row.get("call_uid")
                if not uid:
                    raise RuntimeError(f"{source}: a row has no call_uid")
                cols = {k: v for k, v in row.items() if k != "id"}
                unknown = set(cols) - set(COLUMNS)
                if unknown:
                    raise ValueError(
                        f"{source} has column(s) this ledger does not: "
                        f"{', '.join(sorted(unknown))}; upgrade boundary before merging"
                    )
                held = conn.execute(
                    f"SELECT id, error_type FROM {self.table} WHERE call_uid = %s", (uid,)
                ).fetchone()
                if held is None:
                    if not dry_run:
                        names = ", ".join(cols)
                        marks = ", ".join(["%s"] * len(cols))
                        conn.execute(
                            f"INSERT INTO {self.table} ({names}) VALUES ({marks})",
                            _pg_values(cols),
                        )
                    inserted += 1
                elif held["error_type"] == IN_FLIGHT and cols.get("error_type") != IN_FLIGHT:
                    if not dry_run:
                        sets = ", ".join(f"{k} = %s" for k in cols)
                        conn.execute(
                            f"UPDATE {self.table} SET {sets} WHERE id = %s",
                            (*_pg_values(cols), held["id"]),
                        )
                    completed += 1
                else:
                    skipped += 1
            if dry_run:
                raise _Rollback
        return MergeStats(source, len(rows), inserted, completed, skipped, dry_run)

    def rows_for_batch(self, batch_id: str) -> list[dict[str, Any]]:
        raise NotImplementedError("batches run through a library ledger, not the proxy's")

    def set_batch_id(self, ids: Sequence[int], batch_id: str) -> None:
        raise NotImplementedError("batches run through a library ledger, not the proxy's")


class PgAuditLog(Sealing):
    """The audit chain on Postgres. Appends take an advisory lock inside their transaction,
    so two sealers can never both link to the same head; the primary key on `seq` is the
    second guard."""

    def __init__(self, dsn: str) -> None:
        self._conn = connect(dsn)
        self._lock = threading.RLock()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def records(self) -> list[Record]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT seq, prev_hash, record_hash, body FROM audit ORDER BY seq"
            )
            return [
                Record(int(r["seq"]), str(r["prev_hash"]), str(r["record_hash"]), str(r["body"]))
                for r in cur.fetchall()
            ]

    def head(self) -> tuple[int, str]:
        with self._lock:
            got = self._conn.execute(
                "SELECT seq, record_hash FROM audit ORDER BY seq DESC LIMIT 1"
            ).fetchone()
        return (0, GENESIS) if got is None else (int(got["seq"]), str(got["record_hash"]))

    def append_bodies(self, bodies: Sequence[str]) -> list[Record]:
        out: list[Record] = []
        with self._lock, self._conn.transaction():
            self._conn.execute("SELECT pg_advisory_xact_lock(%s)", (_AUDIT_KEY,))
            seq, prev = self.head()
            for body in bodies:
                seq += 1
                h = link(prev, body)
                self._conn.execute(
                    "INSERT INTO audit (seq, prev_hash, record_hash, body) VALUES (%s, %s, %s, %s)",
                    (seq, prev, h, body),
                )
                out.append(Record(seq, prev, h, body))
                prev = h
        return out

    def import_records(self, records: Sequence[Record]) -> int:
        """Copy an existing chain in verbatim, so that anchors already published still name
        its records. Only into an empty table, and every link is checked first."""
        prev = GENESIS
        for i, r in enumerate(records, start=1):
            if r.seq != i or r.prev_hash != prev or link(prev, r.body) != r.record_hash:
                raise ValueError(f"record {r.seq} does not link; not importing a broken chain")
            prev = r.record_hash
        with self._lock, self._conn.transaction():
            self._conn.execute("SELECT pg_advisory_xact_lock(%s)", (_AUDIT_KEY,))
            if self.head()[0] != 0:
                raise ValueError("the audit table is not empty; a chain is imported only once")
            for r in records:
                self._conn.execute(
                    "INSERT INTO audit (seq, prev_hash, record_hash, body) VALUES (%s, %s, %s, %s)",
                    (r.seq, r.prev_hash, r.record_hash, r.body),
                )
        return len(records)


class PgCentral:
    """`CentralLedger` on Postgres: the `central` table and the record of what each source
    has delivered."""

    def __init__(self, dsn: str) -> None:
        self.ledger = PgLedger(dsn, table="central", pool=1)
        self._conn = connect(dsn)
        self._lock = threading.RLock()

    def close(self) -> None:
        self.ledger.close()
        with self._lock:
            self._conn.close()

    def ingest(
        self, rows: Sequence[Mapping[str, Any]], *, source: str, env: str | None, local_rows: int
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
        stats = self.ledger.merge_rows(clean, source=source)
        with self._lock, self._conn.transaction():
            with self._conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO ingest_row (source, call_uid) VALUES (%s, %s) "
                    "ON CONFLICT DO NOTHING",
                    [(source, str(r["call_uid"])) for r in clean],
                )
            self._conn.execute(
                "INSERT INTO ingest_source (source, env, local_rows, last_utc) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (source) DO UPDATE SET "
                "env = EXCLUDED.env, local_rows = EXCLUDED.local_rows, "
                "last_utc = EXCLUDED.last_utc",
                (source, env, local_rows, utc_now()),
            )
        state = next(s for s in self.sources() if s.source == source)
        return IngestResult(stats, state)

    def sources(self) -> list[SourceState]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT s.source, s.env, s.local_rows, s.last_utc, "
                "(SELECT COUNT(*) FROM ingest_row r WHERE r.source = s.source) AS held "
                "FROM ingest_source s ORDER BY s.source"
            )
            return [
                SourceState(
                    str(r["source"]),
                    None if r["env"] is None else str(r["env"]),
                    int(r["local_rows"]),
                    int(r["held"]),
                    str(r["last_utc"]),
                )
                for r in cur.fetchall()
            ]

    def rows(self) -> list[dict[str, Any]]:
        return self.ledger.rows()

    def import_sources(
        self, sources: Sequence[SourceState], uids: Sequence[tuple[str, str]]
    ) -> None:
        """Carry a SQLite central file's source records across verbatim (the migration)."""
        with self._lock, self._conn.transaction(), self._conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO ingest_row (source, call_uid) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                list(uids),
            )
            cur.executemany(
                "INSERT INTO ingest_source (source, env, local_rows, last_utc) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (source) DO NOTHING",
                [(s.source, s.env, s.local_rows, s.last_utc) for s in sources],
            )


# The columns the dashboard reads (boundary.central.Group), and nothing else.
DASHBOARD_COLUMNS = (
    "call_uid", "ts_utc", "project", "provider", "model_requested", "error_type", "cached",
    "costed", "cost_usd", "latency_ms", "http_status",
)  # fmt: skip


def dashboard_rows(dsn: str) -> list[dict[str, Any]]:
    """The proxy's ledger and the central ledger as one set of rows, one per `call_uid`, a
    completed copy preferred over one in flight: `boundary.central.union` in one query, and
    only the columns the page shows."""
    cols = ", ".join(DASHBOARD_COLUMNS)
    with connect(dsn) as conn:
        cur = conn.execute(
            f"SELECT DISTINCT ON (call_uid) {cols} FROM ("
            f"SELECT {cols}, 0 AS pri FROM ledger UNION ALL SELECT {cols}, 1 AS pri FROM central"
            ") t ORDER BY call_uid, (error_type IS NOT DISTINCT FROM 'in_flight'), pri"
        )
        return [dict(r) for r in cur.fetchall()]


def _vec(v: Vector) -> str:
    return "[" + ",".join(f"{x:.7g}" for x in v) + "]"


class PgSemanticCache:
    """`SemanticCache` with its store in pgvector, one instance per team, the scope and the
    rules unchanged. Cosine similarity is pgvector's `1 - (a <=> b)` over unit vectors."""

    def __init__(
        self,
        dsn: str,
        embedder: Embedder,
        *,
        team: str,
        threshold: float,
        max_entries: int | None = None,
        conn: Any = None,
        lock: Any = None,
    ) -> None:
        if not 0.0 < threshold <= 1.0:
            raise ValueError("threshold must be in (0, 1]")
        self.embedder = embedder
        self.team = team
        self.threshold = threshold
        self.max_entries = max_entries
        self._conn = conn if conn is not None else connect(dsn)
        self._lock = lock if lock is not None else threading.RLock()
        with self._lock:
            self._conn.execute("SET hnsw.iterative_scan = strict_order")

    def embed(self, request: ChatRequest) -> Vector:
        return self.embedder.embed([question_of(request)])[0]

    def nearest(self, request: ChatRequest, vector: Vector | None = None) -> Hit | None:
        v = vector if vector is not None else self.embed(request)
        with self._lock:
            got = self._conn.execute(
                "SELECT id, answer, source, model, finish_reason, "
                "1 - (embedding <=> %s::vector) AS similarity FROM semcache "
                "WHERE team = %s AND scope = %s ORDER BY embedding <=> %s::vector LIMIT 1",
                (_vec(v), self.team, scope_of(request), _vec(v)),
            ).fetchone()
        if got is None:
            return None
        return Hit(
            int(got["id"]),
            float(got["similarity"]),
            str(got["answer"]),
            got["source"],
            got["model"],
            got["finish_reason"],
        )

    def lookup(self, request: ChatRequest, vector: Vector | None = None) -> Hit | None:
        hit = self.nearest(request, vector)
        return hit if hit is not None and hit.similarity >= self.threshold else None

    def store(
        self,
        request: ChatRequest,
        answer: str,
        *,
        vector: Vector | None = None,
        source: str | None = None,
        model: str | None = None,
        finish_reason: str | None = None,
    ) -> int | None:
        v = vector if vector is not None else self.embed(request)
        with self._lock:
            if self.max_entries is not None and len(self) >= self.max_entries:
                return None
            got = self._conn.execute(
                "INSERT INTO semcache (team, scope, embedding, answer, source, model, "
                "finish_reason, created_utc) VALUES (%s, %s, %s::vector, %s, %s, %s, %s, %s) "
                "RETURNING id",
                (self.team, scope_of(request), _vec(v), answer, source, model, finish_reason,
                 utc_now()),
            ).fetchone()  # fmt: skip
        return int(got["id"])

    def __len__(self) -> int:
        with self._lock:
            got = self._conn.execute(
                "SELECT COUNT(*) AS n FROM semcache WHERE team = %s", (self.team,)
            ).fetchone()
        return int(got["n"])


# A sliding window of one minute per team, in one script, so that checking and counting a
# request is one step for every worker: the same rule as `RequestQuota`, shared.
_QUOTA_LUA = """
local key = KEYS[1]
local limit = tonumber(ARGV[1])
local id = ARGV[2]
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
redis.call('ZREMRANGEBYSCORE', key, '-inf', now - 60)
local used = redis.call('ZCARD', key)
if used >= limit then
  local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
  local wait = 60 - (now - tonumber(oldest[2]))
  return {0, used, tostring(wait)}
end
redis.call('ZADD', key, now, id)
redis.call('EXPIRE', key, 61)
return {1, used + 1, '0'}
"""


class RedisQuota:
    """`RequestQuota` on Redis: the same `take(team, limit)`, across every worker."""

    def __init__(self, url: str, *, prefix: str = "boundary:quota:") -> None:
        try:
            import redis
        except ImportError as e:  # pragma: no cover
            raise ImportError("Redis needs the hosted extra: uv sync --extra hosted") from e
        import redis.asyncio

        self._redis = redis.Redis.from_url(url)
        self._script = self._redis.register_script(_QUOTA_LUA)
        # The proxy's path (0.29): the same script from the event loop, without blocking it.
        self._aredis = redis.asyncio.Redis.from_url(url)
        self._ascript = self._aredis.register_script(_QUOTA_LUA)
        self._prefix = prefix
        self._n = 0
        self._lock = threading.Lock()

    def _member(self) -> str:
        with self._lock:
            self._n += 1
            return f"{time.time_ns()}-{os.getpid()}-{threading.get_ident()}-{self._n}"

    def take(self, team: str, limit: int) -> QuotaDecision:
        got = self._script(keys=[self._prefix + team], args=[limit, self._member()])
        return self._decision(got, limit)

    async def atake(self, team: str, limit: int) -> QuotaDecision:
        got = await self._ascript(keys=[self._prefix + team], args=[limit, self._member()])
        return self._decision(got, limit)

    @staticmethod
    def _decision(got: Any, limit: int) -> QuotaDecision:
        allowed, used, wait = got
        return QuotaDecision(
            allowed=bool(int(allowed)),
            limit=limit,
            used=int(used),
            retry_after_s=max(0.0, float(wait)),
        )


__all__ = [
    "COLUMNS",
    "DASHBOARD_COLUMNS",
    "GRANTS",
    "SCHEMA",
    "PgAuditLog",
    "PgCentral",
    "PgLedger",
    "PgSemanticCache",
    "RedisQuota",
    "connect",
    "dashboard_rows",
    "init_schema",
]
