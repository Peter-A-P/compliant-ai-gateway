-- Ledger schema v9. Columns are additive only: never renamed, never removed.
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
-- v3 (0.2, September 2026) adds one column for the Anthropic Message Batches support:
--   batch_id  the vendor's batch identifier, null for every ordinary call. A batch is
--             submitted in one process and its results collected in another, possibly
--             hours later, so the rows written at submit have to be findable again by
--             something the vendor also knows. The per-request custom_id sent to the
--             vendor is the row's call_uid, so a result maps back to exactly one row.
-- v4 (0.2.1) adds `residency`, v5 (0.2.2) adds `price_sha256`; both are documented on the
-- LedgerRow fields in store.py.
-- v6 (0.3, September 2026) adds one column for streamed calls:
--   ttft_ms   wall time from sending the request to the first content delta arriving,
--             null for every call that was not streamed. `latency_ms` on a streamed call
--             runs to the last byte, so the two together say how long the caller waited
--             before anything appeared and how long the whole answer took.
-- v7 (0.4, September 2026) adds one column for the caller's data classification:
--   data_class  one of public, internal, personal, sensitive (PLAN.md B2.2), as the
--               caller declared it on the call, or null when nothing was declared. It
--               is a declaration and never an inference from content. Part B's policy
--               will act on it; in 0.4 it is recorded and reported, which is what lets
--               an audit ask "which calls carried personal data, and where did they go".
--
-- A file is migrated in place on open, one step at a time and additively: v1 gains
-- call_uid and env with call_uid backfilled deterministically from the row's own
-- contents, v2 gains batch_id. A uid is only ever invented during the v1 step; a null
-- call_uid in a v2 or later file was put there by hand and stays null, because merging a
-- row whose identity was guessed would duplicate it.

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
    residency           TEXT,
    input_tokens        INTEGER NOT NULL DEFAULT 0,
    output_tokens       INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens   INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens  INTEGER NOT NULL DEFAULT 0,
    price_list          TEXT,
    price_sha256        TEXT,
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
    raw_path            TEXT,
    batch_id            TEXT,
    ttft_ms             REAL,
    data_class          TEXT,
    redacted            INTEGER
);

-- Indexes over columns that every schema version has. The indexes over call_uid and
-- batch_id are built by the migration instead, because this script also runs against a
-- file written by an older version, where those columns do not exist yet.
-- v8 (0.15, September 2026) adds one column for the proxy's redaction:
--   redacted  1 when the caller says the request was sent redacted (the proxy says so when
--             it redacted the payload itself), null otherwise. Like data_class it is the
--             caller's statement: the library cannot tell a redacted body from a raw one
--             without reading content, which it does not do.
CREATE INDEX IF NOT EXISTS ledger_project_ts ON ledger (project, ts_utc);
CREATE INDEX IF NOT EXISTS ledger_project_run ON ledger (project, run_id);

-- v9 (0.19, September 2026) adds no column. It adds a table of spend per project and month,
-- kept by triggers, so that the spend caps checked before every call read one row instead
-- of summing the whole ledger. The load test found the cost: `spend_usd` filtered on
-- substr(ts_utc, 1, 7), which no index serves, and the gateway-wide ceiling summed every
-- project, so every call scanned every row written before it. Triggers rather than a
-- running total in the process, because a ledger file has more than one writer: every team's
-- gateway in the proxy holds its own connection, and the ceiling is over all of them. The
-- table is derived, never a source: `ledger_spend` can always be rebuilt from `ledger`.
CREATE TABLE IF NOT EXISTS ledger_spend (
    project TEXT NOT NULL,
    month   TEXT NOT NULL,
    cost    REAL NOT NULL,
    PRIMARY KEY (project, month)
);
CREATE TRIGGER IF NOT EXISTS ledger_spend_insert AFTER INSERT ON ledger
WHEN NEW.cost_usd IS NOT NULL
BEGIN
    INSERT INTO ledger_spend (project, month, cost)
    VALUES (NEW.project, substr(NEW.ts_utc, 1, 7), NEW.cost_usd)
    ON CONFLICT (project, month) DO UPDATE SET cost = cost + excluded.cost;
END;
CREATE TRIGGER IF NOT EXISTS ledger_spend_update AFTER UPDATE OF cost_usd ON ledger
WHEN COALESCE(NEW.cost_usd, 0) != COALESCE(OLD.cost_usd, 0)
BEGIN
    INSERT INTO ledger_spend (project, month, cost)
    VALUES (NEW.project, substr(NEW.ts_utc, 1, 7), COALESCE(NEW.cost_usd, 0) - COALESCE(OLD.cost_usd, 0))
    ON CONFLICT (project, month) DO UPDATE SET cost = cost + excluded.cost;
END;

CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER NOT NULL,
    applied_utc TEXT    NOT NULL
);
