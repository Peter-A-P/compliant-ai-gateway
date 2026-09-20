"""`ledger merge`: local-first ledgers combine into one file, and running it twice is a
no-op. The reason the library writes locally at all is in docs/rejected.md."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
import respx

from boundary.config import BoundaryConfig
from boundary.ledger.store import IN_FLIGHT, SCHEMA_VERSION, LedgerRow, LedgerStore, utc_now
from boundary.types import ChatRequest

from .conftest import ANTHROPIC_URL, HAIKU, anthropic_ok, make_gateway

# Schema v1 exactly as 0.1.0 shipped it, for the migration test. Kept verbatim rather than
# generated: the point is to open a file a released version wrote.
V1_SCHEMA = """
CREATE TABLE ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc TEXT NOT NULL, boundary_version TEXT NOT NULL, project TEXT NOT NULL,
    purpose TEXT NOT NULL, run_id TEXT, mode TEXT NOT NULL, provider TEXT NOT NULL,
    alias TEXT, model_requested TEXT NOT NULL, model_returned TEXT, region TEXT,
    input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0, cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    price_list TEXT, cost_usd REAL, costed INTEGER NOT NULL DEFAULT 0,
    cached INTEGER NOT NULL DEFAULT 0, latency_ms REAL, http_status INTEGER, error_type TEXT,
    retries INTEGER NOT NULL DEFAULT 0, request_sha256 TEXT, response_sha256 TEXT,
    trace_id TEXT, span_id TEXT, raw_path TEXT
);
CREATE TABLE schema_version (version INTEGER NOT NULL, applied_utc TEXT NOT NULL);
INSERT INTO schema_version (version, applied_utc) VALUES (1, '2026-09-08T00:00:00.000Z');
"""


def _row(env: str, *, purpose: str = "dev", cost: float | None = 0.001, **kw: object) -> LedgerRow:
    base: dict[str, object] = {
        "ts_utc": utc_now(),
        "boundary_version": "0.1.0",
        "project": "ai-release-gate",
        "purpose": purpose,
        "mode": "standard",
        "provider": "anthropic",
        "model_requested": HAIKU,
        "env": env,
        "cost_usd": cost,
        "costed": cost is not None,
    }
    base.update(kw)
    return LedgerRow(**base)  # type: ignore[arg-type]


def _store(tmp_path: Path, name: str) -> LedgerStore:
    return LedgerStore(tmp_path / f"{name}.sqlite")


def _write(store: LedgerStore, env: str, n: int, *, complete: bool = True) -> list[LedgerRow]:
    rows = []
    for i in range(n):
        row = _row(env, purpose=f"call-{i}")
        store.begin(row)
        if complete:
            row.http_status = 200
            store.complete(row)
        rows.append(row)
    return rows


def test_a_new_file_is_current_with_a_unique_index_on_call_uid(tmp_path: Path) -> None:
    store = _store(tmp_path, "fresh")
    try:
        assert store.schema_version == SCHEMA_VERSION == 7
        row = _row("laptop")
        store.begin(row)
        assert store.rows()[0]["call_uid"] == row.call_uid
        assert store.rows()[0]["env"] == "laptop"
        with pytest.raises(sqlite3.IntegrityError):
            store.begin(_row("laptop", call_uid=row.call_uid))
    finally:
        store.close()


def test_merge_combines_environments_and_renumbers_ids(tmp_path: Path) -> None:
    laptop, actions, central = (_store(tmp_path, n) for n in ("laptop", "actions", "central"))
    try:
        _write(laptop, "laptop", 3)
        _write(actions, "actions", 2)
        # Both files start their ids at 1: only call_uid identifies a call across files.
        assert [r["id"] for r in laptop.rows()] == [1, 2, 3]
        assert [r["id"] for r in actions.rows()] == [1, 2]
        for src in (laptop, actions):
            src.close()
        first = central.merge_from(tmp_path / "laptop.sqlite")
        second = central.merge_from(tmp_path / "actions.sqlite")
        assert (first.inserted, first.completed, first.skipped) == (3, 0, 0)
        assert (second.inserted, second.completed, second.skipped) == (2, 0, 0)
        rows = central.rows()
        assert [r["id"] for r in rows] == [1, 2, 3, 4, 5]
        assert [r["env"] for r in rows] == ["laptop"] * 3 + ["actions"] * 2
        assert len({r["call_uid"] for r in rows}) == 5
        # The portfolio cap is checked against the ledger the gateway can see, so a merged
        # file has to sum both environments.
        assert central.spend_usd(project=None) == pytest.approx(0.005)
    finally:
        central.close()


def test_merging_the_same_source_twice_inserts_nothing(tmp_path: Path) -> None:
    laptop, central = _store(tmp_path, "laptop"), _store(tmp_path, "central")
    try:
        _write(laptop, "laptop", 4)
        laptop.close()
        first = central.merge_from(tmp_path / "laptop.sqlite")
        again = central.merge_from(tmp_path / "laptop.sqlite")
        assert first.inserted == 4 and first.skipped == 0
        assert (again.inserted, again.completed, again.skipped) == (0, 0, 4)
        assert central.count() == 4
    finally:
        central.close()


def test_a_row_completed_after_it_was_merged_is_completed_in_the_destination(
    tmp_path: Path,
) -> None:
    """The runner merges nightly. A call in flight at merge time is completed later; the
    next merge has to carry the outcome across rather than keep the estimate for ever."""
    laptop, central = _store(tmp_path, "laptop"), _store(tmp_path, "central")
    try:
        row = _row("laptop", cost=0.004)
        laptop.begin(row)
        first = central.merge_from(tmp_path / "laptop.sqlite")
        assert first.inserted == 1
        held = central.rows()[0]
        assert held["error_type"] == IN_FLIGHT and held["cost_usd"] == pytest.approx(0.004)

        row.http_status = 200
        row.cost_usd = 0.0009
        row.costed = True
        row.input_tokens, row.output_tokens = 300, 40
        laptop.complete(row)
        second = central.merge_from(tmp_path / "laptop.sqlite")
        assert (second.inserted, second.completed, second.skipped) == (0, 1, 0)
        done = central.rows()
        assert len(done) == 1, "completing a row must not add one"
        assert done[0]["id"] == held["id"] and done[0]["call_uid"] == row.call_uid
        assert done[0]["error_type"] is None and done[0]["http_status"] == 200
        assert done[0]["cost_usd"] == pytest.approx(0.0009)
        assert (done[0]["input_tokens"], done[0]["output_tokens"]) == (300, 40)
        # Idempotent again from the new state.
        assert central.merge_from(tmp_path / "laptop.sqlite").changed == 0
    finally:
        laptop.close()
        central.close()


def test_an_in_flight_source_row_never_overwrites_a_completed_one(tmp_path: Path) -> None:
    """Merging an older copy of a file must not undo what the destination already knows."""
    stale, central = _store(tmp_path, "stale"), _store(tmp_path, "central")
    try:
        row = _row("laptop", cost=0.004)
        stale.begin(row)  # the copy on disk still says in_flight
        done = _row("laptop", call_uid=row.call_uid, cost=0.0009, http_status=200)
        central.begin(done)
        done.error_type = None
        central.complete(done)
        stats = central.merge_from(tmp_path / "stale.sqlite")
        assert (stats.inserted, stats.completed, stats.skipped) == (0, 0, 1)
        assert central.rows()[0]["cost_usd"] == pytest.approx(0.0009)
        assert central.rows()[0]["error_type"] is None
    finally:
        stale.close()
        central.close()


def test_dry_run_reports_without_writing(tmp_path: Path) -> None:
    laptop, central = _store(tmp_path, "laptop"), _store(tmp_path, "central")
    try:
        _write(laptop, "laptop", 3)
        laptop.close()
        stats = central.merge_from(tmp_path / "laptop.sqlite", dry_run=True)
        assert stats.inserted == 3 and stats.dry_run and central.count() == 0
        assert central.merge_from(tmp_path / "laptop.sqlite").inserted == 3
    finally:
        central.close()


def test_a_v1_file_is_upgraded_in_place_and_keeps_its_rows(tmp_path: Path) -> None:
    path = tmp_path / "v1.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(V1_SCHEMA)
    conn.execute(
        "INSERT INTO ledger (ts_utc, boundary_version, project, purpose, mode, provider,"
        " model_requested, cost_usd, costed, http_status, request_sha256)"
        " VALUES ('2026-09-10T12:00:00.000Z', '0.1.0', 'compliant-ai-gateway', 'smoke',"
        " 'standard', 'anthropic', ?, 0.0009, 1, 200, 'abc')",
        (HAIKU,),
    )
    conn.commit()
    conn.close()

    store = LedgerStore(path)
    try:
        assert store.schema_version == SCHEMA_VERSION
        rows = store.rows()
        assert len(rows) == 1
        assert rows[0]["cost_usd"] == pytest.approx(0.0009)
        assert rows[0]["call_uid"] and len(rows[0]["call_uid"]) == 32
        # No environment is invented for a row written before the column existed.
        assert rows[0]["env"] is None
        # Nor a residency (v4). The row was written by a configuration that declared none,
        # and a value here would be a claim about where the data went that nobody made.
        assert rows[0]["residency"] is None
        # Nor a rate fingerprint (v5). The row names a price list by date, and which file
        # with that date it meant is exactly what cannot be recovered afterwards. Computing
        # one now from whatever this checkout happens to hold would be a fabricated match.
        assert rows[0]["price_sha256"] is None
        # Nor a time to first token (v6). The call was not streamed; nothing arrived first.
        assert rows[0]["ttft_ms"] is None
        # Nor a data class (v7). Nobody declared one on a call made before there was a way
        # to, and a value here would be a claim about data the library never looked at.
        assert rows[0]["data_class"] is None
        uid = rows[0]["call_uid"]
    finally:
        store.close()
    reopened = LedgerStore(path)
    try:
        assert reopened.rows()[0]["call_uid"] == uid, "the uid is stable across opens"
    finally:
        reopened.close()


def _v1_file(path: Path, rows: int = 2) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(V1_SCHEMA)
    for i in range(rows):
        conn.execute(
            "INSERT INTO ledger (ts_utc, boundary_version, project, purpose, mode, provider,"
            " model_requested, cost_usd, costed, http_status, request_sha256)"
            " VALUES (?, '0.1.0', 'ai-release-gate', 'drift-run', 'passthrough', 'anthropic',"
            " ?, 0.0009, 1, 200, ?)",
            (f"2026-09-27T12:00:0{i}.000Z", HAIKU, f"sha{i}"),
        )
    conn.commit()
    conn.close()


def test_merge_never_writes_to_a_source(tmp_path: Path) -> None:
    """Merging must leave a source exactly as its owner left it.

    Project 03 pins boundary 0.1.0 and commits one ledger per arm per month. Opening one of
    those files upgrades it, and 0.1.0 then refuses to write to it by design, so a merge
    that upgraded its sources would stop the drift runner appending to its own ledgers. The
    bytes are the assertion because that is the only thing the next run cares about.
    """
    src = tmp_path / "arm.sqlite"
    _v1_file(src, rows=2)
    before = src.read_bytes()

    central = _store(tmp_path, "central")
    try:
        stats = central.merge_from(src)
        assert stats.inserted == 2, "the rows still have to arrive"
        assert src.read_bytes() == before, "the source was modified by a merge"
        # No write-ahead log or shared-memory file left beside it either.
        assert sorted(p.name for p in tmp_path.iterdir() if p.name.startswith("arm")) == [
            "arm.sqlite"
        ]

        # Still v1, still without the v2 columns, so 0.1.0 can carry on writing to it.
        conn = sqlite3.connect(src)
        try:
            assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 1
            columns = {r[1] for r in conn.execute("PRAGMA table_info(ledger)")}
            assert "call_uid" not in columns and "batch_id" not in columns
        finally:
            conn.close()

        # And the uids are still derived from the rows, so a second merge inserts nothing.
        assert central.merge_from(src).changed == 0
    finally:
        central.close()


def test_two_copies_of_one_v1_file_merge_to_one_row_per_call(tmp_path: Path) -> None:
    """The uid the migration invents comes from the row, so a v1 ledger copied to two
    places before either was upgraded still merges to one row per call."""
    import shutil

    original = tmp_path / "v1.sqlite"
    conn = sqlite3.connect(original)
    conn.executescript(V1_SCHEMA)
    for i in range(3):
        conn.execute(
            "INSERT INTO ledger (ts_utc, boundary_version, project, purpose, mode, provider,"
            " model_requested, request_sha256) VALUES (?, '0.1.0', 'p', 'dev', 'standard',"
            " 'anthropic', ?, ?)",
            (f"2026-09-10T12:00:0{i}.000Z", HAIKU, f"sha{i}"),
        )
    conn.commit()
    conn.close()
    copy_a, copy_b = tmp_path / "a.sqlite", tmp_path / "b.sqlite"
    shutil.copy2(original, copy_a)
    shutil.copy2(original, copy_b)

    central = _store(tmp_path, "central")
    try:
        assert central.merge_from(copy_a).inserted == 3
        assert central.merge_from(copy_b).inserted == 0
        assert central.count() == 3
    finally:
        central.close()


def test_a_newer_schema_is_refused_rather_than_written_to(tmp_path: Path) -> None:
    path = tmp_path / "future.sqlite"
    store = LedgerStore(path)
    store._conn.execute(
        "INSERT INTO schema_version (version, applied_utc) VALUES (99, '2027-05-23T00:00:00.000Z')"
    )
    store.close()
    with pytest.raises(RuntimeError, match="newer than this library"):
        LedgerStore(path)


def test_merge_refuses_a_file_into_itself_and_a_missing_source(tmp_path: Path) -> None:
    central = _store(tmp_path, "central")
    try:
        with pytest.raises(ValueError, match="into itself"):
            central.merge_from(tmp_path / "central.sqlite")
        with pytest.raises(FileNotFoundError):
            central.merge_from(tmp_path / "nope.sqlite")
    finally:
        central.close()


def test_a_failed_merge_leaves_the_destination_untouched(tmp_path: Path) -> None:
    """One source, one transaction: a row that cannot be merged rolls the whole source
    back, so the operation can simply be run again once the source is fixed."""
    laptop, central = _store(tmp_path, "laptop"), _store(tmp_path, "central")
    try:
        _write(laptop, "laptop", 2)
        # A row with no uid: 0.1.0 could not write one, and the migration backfills them,
        # so this only happens if a file is edited by hand. It must not merge silently.
        laptop._conn.execute("UPDATE ledger SET call_uid = NULL WHERE id = 2")
        laptop.close()
        with pytest.raises(RuntimeError, match="no call_uid"):
            central.merge_from(tmp_path / "laptop.sqlite")
        assert central.count() == 0, "the first row must not survive the rollback"
    finally:
        central.close()


def test_the_gateway_records_the_environment_and_merges_across_two(
    repo_config: BoundaryConfig, tmp_path: Path, keys: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end: two gateways with their own ledgers, then one central file."""
    req = ChatRequest(model=HAIKU, messages=[{"role": "user", "content": "Q?"}], max_tokens=8)
    for env in ("laptop", "actions"):
        monkeypatch.setenv("BOUNDARY_ENV", env)
        gw = make_gateway(repo_config, tmp_path / env)
        try:
            assert gw.env == env
            with respx.mock(assert_all_called=True) as mock:
                mock.post(ANTHROPIC_URL).mock(return_value=anthropic_ok())
                gw.chat(req, purpose="dev", run_id=f"r-{env}")
            assert gw.ledger.rows()[0]["env"] == env
        finally:
            gw.close()
    central = _store(tmp_path, "central")
    try:
        for env in ("laptop", "actions"):
            central.merge_from(tmp_path / env / "ledger.sqlite")
        rows = central.rows()
        assert {r["env"] for r in rows} == {"laptop", "actions"}
        assert {r["run_id"] for r in rows} == {"r-laptop", "r-actions"}
        assert central.merge_from(tmp_path / "laptop" / "ledger.sqlite").changed == 0
    finally:
        central.close()


def test_the_config_env_is_used_when_no_environment_variable_is_set(
    repo_config: BoundaryConfig, tmp_path: Path, keys: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BOUNDARY_ENV", raising=False)
    gw = make_gateway(repo_config, tmp_path)
    try:
        assert gw.env == repo_config.ledger.env == "local"
    finally:
        gw.close()
    monkeypatch.setenv("BOUNDARY_ENV", "vps")
    explicit = make_gateway(repo_config, tmp_path / "x", env="declared")
    try:
        assert explicit.env == "declared", "an explicit argument beats the environment"
    finally:
        explicit.close()
