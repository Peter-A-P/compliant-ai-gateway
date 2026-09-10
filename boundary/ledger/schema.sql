-- Ledger schema v1. Columns are additive only: never renamed, never removed.
-- One row per call. A row is inserted before the request leaves the process
-- (error_type = 'in_flight', cost_usd = the pre-call estimate) and completed after,
-- so a process killed mid-call still leaves its row.

CREATE TABLE IF NOT EXISTS ledger (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
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
