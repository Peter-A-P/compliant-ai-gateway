-- Ledger schema v2. Columns are additive only: never renamed, never removed.
-- One row per call. A row is inserted before the request leaves the process
-- (error_type = 'in_flight', cost_usd = the pre-call estimate) and completed after,
-- so a process killed mid-call still leaves its row.
--
-- v2 (0.2, October 2026) adds the two columns `ledger merge` needs and nothing else:
--   call_uid  a uid minted in the process that made the call, so the same call merged
--             twice from the same or a different copy of a file is one row in the
--             destination. `id` cannot do this job: it is per file, so two environments
--             both hold an id 1 for different calls.
--   env       which environment made the call (laptop, actions, vps). After a merge the
--             destination holds rows from several, and a row has to say which.
-- A v1 file is migrated in place on open: the columns are added, call_uid is backfilled
-- deterministically from the row's own contents, then the unique index is built.

CREATE TABLE IF NOT EXISTS ledger (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    call_uid            TEXT,
    env                 TEXT,
    ts_utc              TEXT    NOT NULL,
    boundary_version    TEXT    NOT NULL,
    project             TEXT    NOT NULL,
    purpose             TEXT    NOT NULL,
    run_id              TEXT,
    mode                TEXT    NOT NULL,
    provider            TEXT    NOT NULL,
    alias               TEXT,
    model_requested     TEXT    NOT NULL,
    model_returned      TEXT,
    region              TEXT,
    input_tokens        INTEGER NOT NULL DEFAULT 0,
    output_tokens       INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens   INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens  INTEGER NOT NULL DEFAULT 0,
    price_list          TEXT,
    cost_usd            REAL,
    costed              INTEGER NOT NULL DEFAULT 0,
    cached              INTEGER NOT NULL DEFAULT 0,
    latency_ms          REAL,
    http_status         INTEGER,
    error_type          TEXT,
    retries             INTEGER NOT NULL DEFAULT 0,
    request_sha256      TEXT,
    response_sha256     TEXT,
    trace_id            TEXT,
    span_id             TEXT,
    raw_path            TEXT
);

CREATE INDEX IF NOT EXISTS ledger_project_ts ON ledger (project, ts_utc);
CREATE INDEX IF NOT EXISTS ledger_project_run ON ledger (project, run_id);

CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER NOT NULL,
    applied_utc TEXT    NOT NULL
);
