"""The audit chain: every edit to a sealed call is visible, and what is not visible is stated.

Each test is a way somebody with the files could change the record, and the break it must
leave. The two tests at the end of the anchor section assert the limit rather than hide it:
a competent rewrite after the last anchor is invisible, and so is deleting the newest record.
"""

from __future__ import annotations

import datetime as dt
import itertools
import json
import sqlite3
from pathlib import Path

import pytest
import respx

from boundary.audit import (
    GENESIS,
    SEALED,
    Anchor,
    AuditLog,
    Verification,
    append_anchor,
    read_anchors,
    tamper,
    verify,
)
from boundary.audit.chain import Record, build, canonical, ledger_body, link
from boundary.cli import main
from boundary.config import BoundaryConfig
from boundary.types import ChatRequest

from .conftest import ANTHROPIC_URL, HAIKU, anthropic_ok, make_gateway

NOW = dt.datetime(2026, 9, 22, 12, 0, tzinfo=dt.UTC)
CONFIG = str(Path(__file__).resolve().parent.parent / "config" / "boundary.yaml")


def _ledger(n: int = 20) -> list[dict[str, object]]:
    return tamper.synthetic_ledger(n, seed=7)


def _chain(n: int = 20) -> tuple[list[Record], list[dict[str, object]]]:
    ledger = _ledger(n)
    return build(ledger_body(r, sealed_utc="t") for r in ledger), ledger


def _kinds(v: Verification) -> set[str]:
    return {b.kind for b in v.breaks}


# -- the chain -----------------------------------------------------------------------------


def test_an_honest_chain_verifies_and_links_to_genesis() -> None:
    records, ledger = _chain()
    v = verify(records, ledger_rows=ledger)
    assert v.ok and v.records == 20 and v.head == records[-1].record_hash
    assert records[0].prev_hash == GENESIS
    assert all(b.prev_hash == a.record_hash for a, b in itertools.pairwise(records))


def test_an_empty_log_verifies() -> None:
    v = verify([], [Anchor(0, GENESIS, "t")])
    assert v.ok and v.head == GENESIS


def test_the_link_is_sha256_of_the_previous_hash_bytes_and_the_body() -> None:
    import hashlib

    body = canonical({"a": 1})
    assert link(GENESIS, body) == hashlib.sha256(bytes(32) + b'{"a":1}').hexdigest()


def test_an_edited_record_breaks_its_own_hash() -> None:
    records, _ = _chain()
    r = records[5]
    records[5] = Record(
        r.seq, r.prev_hash, r.record_hash, r.body.replace('"costed":', '"costed": ')
    )
    assert {"hash", "body"} <= _kinds(verify(records))


def test_rehashing_an_edited_record_breaks_the_next_link() -> None:
    records, _ = _chain()
    body = records[5].body.replace("tamper-test", "tamper-tesT")
    records[5] = build([body], prev_hash=records[5].prev_hash, first_seq=6)[0]
    v = verify(records)
    assert [(b.kind, b.seq) for b in v.breaks] == [("link", 7)]


def test_a_body_that_is_not_canonical_is_refused_even_with_a_correct_hash() -> None:
    """Somebody else's JSON writer, not this library's, wrote it."""
    body = json.dumps({"kind": "ledger_row", "row": {}}, indent=1)
    v = verify(build([body]))
    assert _kinds(v) == {"body"}


def test_a_record_carries_the_sealed_columns_and_nothing_else() -> None:
    row = {**_ledger(1)[0], "raw_path": "C:/Users/somebody/raw/1.json", "trace_id": "abc"}
    body = json.loads(ledger_body(row, sealed_utc="t"))
    assert set(body) == {"kind", "schema", "sealed_utc", "row"}
    assert tuple(sorted(body["row"])) == tuple(sorted(SEALED))
    assert "raw_path" not in body["row"] and "trace_id" not in body["row"]


# -- anchors -------------------------------------------------------------------------------


def test_a_full_rewrite_before_an_anchor_is_caught_by_the_anchor() -> None:
    records, _ = _chain()
    anchors = [Anchor(10, records[9].record_hash, "t")]
    bodies = [r.body for r in records]
    bodies[3] = bodies[3].replace("tamper-test", "tamper-tesT")
    v = verify(build(bodies), anchors)
    assert _kinds(v) == {"anchor"}


def test_truncating_below_an_anchor_is_caught() -> None:
    records, _ = _chain()
    v = verify(records[:8], [Anchor(10, records[9].record_hash, "t")])
    assert _kinds(v) == {"truncated"}


def test_a_full_rewrite_after_the_last_anchor_is_not_caught_and_that_is_stated() -> None:
    """The limit of any hash chain. Its width is the anchor interval; docs/audit.md."""
    records, _ = _chain()
    anchors = [Anchor(10, records[9].record_hash, "t")]
    bodies = [r.body for r in records]
    bodies[15] = bodies[15].replace("tamper-test", "tamper-tesT")
    v = verify(build(bodies), anchors)
    assert v.ok and v.unanchored == 10
    assert "after the last anchor" in v.summary()


def test_deleting_the_newest_record_looks_like_a_row_not_yet_sealed() -> None:
    """Measured in the tamper test at about one trial in fifty in the unanchored tail."""
    records, ledger = _chain()
    v = verify(records[:-1], ledger_rows=ledger)
    assert v.ok and v.unsealed == 1


def test_anchor_lines_round_trip_and_the_file_only_grows(tmp_path: Path) -> None:
    path = tmp_path / "anchors.jsonl"
    a, b = Anchor(5, "a" * 64, "2026-09-21T00:00:00Z"), Anchor(9, "b" * 64, "2026-09-22T00:00:00Z")
    append_anchor(path, a)
    first = path.read_bytes()
    append_anchor(path, b)
    assert path.read_bytes().startswith(first)
    assert read_anchors(path) == [a, b]
    assert read_anchors(tmp_path / "missing.jsonl") == []


# -- the ledger against the chain ----------------------------------------------------------


def test_editing_a_sealed_ledger_row_is_caught_and_named() -> None:
    records, ledger = _chain()
    ledger[4] = {**ledger[4], "cost_usd": 0.0, "input_tokens": 1}
    v = verify(records, ledger_rows=ledger)
    assert _kinds(v) == {"ledger_changed"}
    assert "cost_usd" in v.breaks[0].detail and "input_tokens" in v.breaks[0].detail


def test_deleting_a_sealed_ledger_row_is_caught() -> None:
    records, ledger = _chain()
    del ledger[4]
    assert _kinds(verify(records, ledger_rows=ledger)) == {"ledger_missing"}


def test_a_row_written_since_the_last_seal_is_unsealed_not_a_break() -> None:
    records, ledger = _chain()
    ledger = [*ledger, *tamper.synthetic_ledger(3, seed=99)]
    v = verify(records, ledger_rows=ledger)
    assert v.ok and v.unsealed == 3


# -- the log on disk -----------------------------------------------------------------------


def test_the_table_refuses_update_and_delete(tmp_path: Path) -> None:
    path = tmp_path / "a.audit.sqlite"
    with AuditLog(path) as log:
        log.seal(_ledger(3), now=NOW)
    conn = sqlite3.connect(path)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE audit SET body = 'x' WHERE seq = 1")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM audit WHERE seq = 1")
    conn.close()


def test_dropping_the_trigger_does_not_make_an_edit_invisible(tmp_path: Path) -> None:
    """The trigger stops accidents. The chain is what holds against the owner of the file."""
    path = tmp_path / "a.audit.sqlite"
    ledger = _ledger(5)
    with AuditLog(path) as log:
        log.seal(ledger, now=NOW)
    conn = sqlite3.connect(path)
    conn.execute("DROP TRIGGER audit_no_update")
    conn.execute(
        "UPDATE audit SET body = replace(body, 'tamper-test', 'tamper-tesT') WHERE seq = 2"
    )
    conn.commit()
    conn.close()
    with AuditLog(path) as log:
        v = verify(log.records(), ledger_rows=ledger)
    assert "hash" in _kinds(v)


def test_sealing_twice_appends_nothing_the_second_time(tmp_path: Path) -> None:
    ledger = _ledger(10)
    with AuditLog(tmp_path / "a.sqlite") as log:
        first = log.seal(ledger, now=NOW)
        second = log.seal(ledger, now=NOW)
        assert (first.sealed, second.sealed, second.unchanged) == (10, 0, 10)
        assert first.head == second.head
        assert verify(log.records(), ledger_rows=ledger).ok


def test_a_changed_row_is_resealed_as_a_second_record(tmp_path: Path) -> None:
    ledger = _ledger(4)
    with AuditLog(tmp_path / "a.sqlite") as log:
        log.seal(ledger, now=NOW)
        ledger[1] = {**ledger[1], "retries": 2}
        stats = log.seal(ledger, now=NOW)
        assert (stats.resealed, stats.head_seq) == (1, 5)
        assert verify(log.records(), ledger_rows=ledger).ok


def test_a_recent_in_flight_row_is_held_and_an_old_one_is_sealed_as_it_stands(
    tmp_path: Path,
) -> None:
    ledger = _ledger(2)
    recent = (NOW - dt.timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
    stale = (NOW - dt.timedelta(days=3)).isoformat().replace("+00:00", "Z")
    ledger[0] = {**ledger[0], "error_type": "in_flight", "ts_utc": recent}
    ledger[1] = {**ledger[1], "error_type": "in_flight", "ts_utc": stale}
    with AuditLog(tmp_path / "a.sqlite") as log:
        stats = log.seal(ledger, now=NOW)
        assert (stats.sealed, stats.held_in_flight) == (1, 1)
        # The killed call completes after all: pending a reseal, not tampering.
        ledger[1] = {**ledger[1], "error_type": None, "http_status": 200}
        v = verify(log.records(), ledger_rows=ledger)
        assert v.ok and v.resealable == 1 and v.unsealed == 1
        log.seal(ledger, now=NOW + dt.timedelta(days=2))
        v = verify(log.records(), ledger_rows=ledger)
        assert v.ok and v.resealable == 0 and v.unsealed == 0


def test_a_row_without_a_call_uid_is_counted_not_sealed(tmp_path: Path) -> None:
    ledger = _ledger(2)
    ledger[0] = {**ledger[0], "call_uid": None}
    with AuditLog(tmp_path / "a.sqlite") as log:
        stats = log.seal(ledger, now=NOW)
    assert (stats.sealed, stats.no_call_uid) == (1, 1)


def test_a_real_gateway_ledger_seals_and_verifies_without_content(
    repo_config: BoundaryConfig, tmp_path: Path, keys: None
) -> None:
    secret = "The applicant's name is Mary-Anne O'Brien"
    gw = make_gateway(repo_config, tmp_path)
    try:
        with respx.mock(assert_all_called=True) as mock:
            mock.post(ANTHROPIC_URL).mock(return_value=anthropic_ok(text="B"))
            gw.chat(
                ChatRequest(
                    model=HAIKU, messages=[{"role": "user", "content": secret}], max_tokens=8
                ),
                purpose="dev",
            )
        rows = gw.ledger.rows()
    finally:
        gw.close()
    with AuditLog(tmp_path / "ledger.audit.sqlite") as log:
        log.seal(rows)
        records = log.records()
    assert verify(records, ledger_rows=rows).ok
    assert all("O'Brien" not in r.body and "applicant" not in r.body for r in records)


# -- the measurement -----------------------------------------------------------------------


def test_the_tamper_test_detects_everything_before_the_last_anchor() -> None:
    r = tamper.run(records=120, anchor_interval=20, trials=15, seed=3)
    assert r.before.hits == r.before.total
    assert r.false_alarms.hits == 0


def test_the_tamper_test_reports_the_window_after_the_last_anchor() -> None:
    r = tamper.run(records=120, anchor_interval=20, trials=15, seed=3)
    rows = {row.kind: row for row in r.rows}
    for kind in ("edit a record", "edit a ledger row", "edit a record, rehash it"):
        assert rows[kind].after.hits == rows[kind].after.total, kind
    for kind in (
        "edit row and record, rewrite after",
        "delete call from both, rewrite after",
        "insert a forged call, rewrite after",
        "swap two records, rewrite after",
        "truncate the log and the ledger",
        "replace the log and the ledger",
    ):
        assert rows[kind].after.hits == 0, kind
    assert "by design" in r.readme_row()


# -- the command line ----------------------------------------------------------------------


def test_seal_anchor_verify_from_the_command_line(
    repo_config: BoundaryConfig, tmp_path: Path, keys: None, capsys: pytest.CaptureFixture[str]
) -> None:
    gw = make_gateway(repo_config, tmp_path)
    try:
        with respx.mock() as mock:
            mock.post(ANTHROPIC_URL).mock(return_value=anthropic_ok())
            for _ in range(3):
                gw.chat(
                    ChatRequest(
                        model=HAIKU, messages=[{"role": "user", "content": "Q"}], max_tokens=8
                    ),
                    purpose="dev",
                )
    finally:
        gw.close()
    ledger = str(tmp_path / "ledger.sqlite")
    anchors = str(tmp_path / "anchors.jsonl")
    base = ["--config", CONFIG, "audit"]
    assert main([*base, "seal", "--ledger", ledger]) == 0
    assert (tmp_path / "ledger.audit.sqlite").is_file()
    assert main([*base, "anchor", "--ledger", ledger, "--anchors", anchors]) == 0
    assert main([*base, "verify", "--ledger", ledger, "--anchors", anchors]) == 0
    assert "chain intact" in capsys.readouterr().out

    conn = sqlite3.connect(ledger)
    conn.execute("UPDATE ledger SET cost_usd = 0 WHERE id = 2")
    conn.commit()
    conn.close()
    assert main([*base, "verify", "--ledger", ledger, "--anchors", anchors]) == 1
    assert "ledger_changed" in capsys.readouterr().out


def test_anchoring_an_empty_log_is_refused(tmp_path: Path) -> None:
    audit = tmp_path / "empty.audit.sqlite"
    AuditLog(audit).close()
    code = main(
        [
            "--config",
            CONFIG,
            "audit",
            "anchor",
            "--audit",
            str(audit),
            "--anchors",
            str(tmp_path / "a"),
        ]
    )
    assert code == 1 and not (tmp_path / "a").exists()
