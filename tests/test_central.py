"""The central ledger (0.27): pushed rows merge by call_uid, completeness is counted per
source, and nothing that names a machine's directories crosses."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from boundary.central import MAX_BATCH, CentralLedger, group_by, union
from boundary.ledger.store import LedgerRow, LedgerStore, utc_now


def _rows(
    tmp_path: Path, n: int, *, project: str = "alpha", cost: float = 0.01
) -> list[dict[str, Any]]:
    store = LedgerStore(tmp_path / f"{project}-{n}.sqlite")
    try:
        for i in range(n):
            row = LedgerRow(
                ts_utc=utc_now(),
                boundary_version="0.27.0",
                project=project,
                purpose="t",
                mode="passthrough" if i == 0 else "standard",
                provider="anthropic",
                model_requested="anthropic/m",
                cost_usd=cost,
                env="local",
            )
            store.begin(row)
            row.costed = True
            row.http_status = 200
            row.latency_ms = 100.0 + i
            row.raw_path = "/Users/somebody/raw/1.json" if i == 0 else None
            store.complete(row)
        return [{k: v for k, v in r.items() if k != "id"} for r in store.rows()]
    finally:
        store.close()


def test_a_push_merges_by_call_uid_and_is_safe_to_repeat(tmp_path: Path) -> None:
    rows = _rows(tmp_path, 5)
    central = CentralLedger(tmp_path / "central.sqlite")
    try:
        first = central.ingest(rows, source="local:a.sqlite", env="local", local_rows=5)
        assert (first.stats.inserted, first.source.held, first.source.complete) == (5, 5, True)
        again = central.ingest(rows, source="local:a.sqlite", env="local", local_rows=5)
        assert (again.stats.inserted, again.stats.skipped) == (0, 5)
        assert len(central.rows()) == 5
    finally:
        central.close()


def test_a_short_push_shows_as_short(tmp_path: Path) -> None:
    rows = _rows(tmp_path, 5)
    central = CentralLedger(tmp_path / "central.sqlite")
    try:
        r = central.ingest(rows[:3], source="s", env="local", local_rows=5)
        assert r.source.held == 3 and not r.source.complete
    finally:
        central.close()


def test_a_path_on_the_pushing_machine_never_arrives(tmp_path: Path) -> None:
    rows = _rows(tmp_path, 2)
    assert rows[0]["raw_path"]
    central = CentralLedger(tmp_path / "central.sqlite")
    try:
        central.ingest(rows, source="s", env="local", local_rows=2)
        assert all(r["raw_path"] is None for r in central.rows())
    finally:
        central.close()


def test_what_a_push_cannot_carry(tmp_path: Path) -> None:
    rows = _rows(tmp_path, 1)
    central = CentralLedger(tmp_path / "central.sqlite")
    try:
        with pytest.raises(ValueError, match="does not"):
            central.ingest([{**rows[0], "prompt": "x"}], source="s", env=None, local_rows=1)
        with pytest.raises(ValueError, match="call_uid"):
            central.ingest([{"project": "x"}], source="s", env=None, local_rows=1)
        with pytest.raises(ValueError, match="at most"):
            central.ingest(rows * (MAX_BATCH + 1), source="s", env=None, local_rows=1)
        assert central.rows() == []
    finally:
        central.close()


def test_groups_and_union(tmp_path: Path) -> None:
    a = _rows(tmp_path, 3, project="alpha", cost=0.5)
    b = _rows(tmp_path, 2, project="beta", cost=0.25)
    merged = union(a, b, a)
    assert len(merged) == 5
    (alpha, beta) = group_by(merged, "project")
    assert alpha.key == ("alpha",) and alpha.calls == 3 and alpha.cost_usd == pytest.approx(1.5)
    assert beta.calls == 2 and beta.p50_ms == 100.0


def test_push_sends_every_row_in_batches_and_fails_on_a_short_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import json

    import httpx
    import respx

    from boundary.cli import main

    rows = _rows(tmp_path, 5)
    store = LedgerStore(tmp_path / "local.sqlite")
    try:
        store.merge_rows(rows, source="t")
    finally:
        store.close()
    seen: list[dict[str, Any]] = []

    def reply(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        held = sum(len(b["rows"]) for b in seen)
        return httpx.Response(
            200,
            json={"inserted": len(body["rows"]), "completed": 0, "already_held": 0,
                  "source": body["source"], "local_rows": body["local_rows"], "held": held},
        )  # fmt: skip

    monkeypatch.delenv("BOUNDARY_INGEST_KEY", raising=False)
    args = [
        "ledger",
        "push",
        "--url",
        "https://g.example",
        "--ledger",
        str(tmp_path / "local.sqlite"),
    ]
    assert main(args) == 2, "no key, no push"
    monkeypatch.setenv("BOUNDARY_INGEST_KEY", "bnd_k")
    with respx.mock() as mock:
        mock.post("https://g.example/v1/ledger/ingest").mock(side_effect=reply)
        assert main([*args, "--batch", "2"]) == 0
    assert [len(b["rows"]) for b in seen] == [2, 2, 1]
    assert {b["local_rows"] for b in seen} == {5} and seen[0]["source"] == "local:local.sqlite"
    assert "holds 5 of this source's 5" in capsys.readouterr().out


def test_push_never_writes_to_the_file_it_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hashlib

    import httpx
    import respx

    from boundary.cli import main

    rows = _rows(tmp_path, 2)
    path = tmp_path / "local.sqlite"
    store = LedgerStore(path)
    try:
        store.merge_rows(rows, source="t")
    finally:
        store.close()
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    monkeypatch.setenv("BOUNDARY_INGEST_KEY", "bnd_k")
    ok = {
        "inserted": 2,
        "completed": 0,
        "already_held": 0,
        "source": "s",
        "local_rows": 2,
        "held": 2,
    }
    with respx.mock() as mock:
        mock.post("https://g.example/v1/ledger/ingest").mock(
            return_value=httpx.Response(200, json=ok)
        )
        assert main(["ledger", "push", "--url", "https://g.example", "--ledger", str(path)]) == 0
        # Another project pushes with no configuration of this library's at all.
        missing = str(tmp_path / "no-such" / "boundary.yaml")
        assert main(["--config", missing, "ledger", "push", "--url", "https://g.example",
                     "--ledger", str(path), "--source", "other-repo:runs/a.sqlite"]) == 0  # fmt: skip
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_the_days_table_is_newest_first(tmp_path: Path) -> None:
    import datetime as dt

    from boundary.server.dashboard import render

    today = dt.datetime.now(dt.UTC).date()
    days = [(today - dt.timedelta(days=d)).isoformat() for d in (3, 0, 1)]
    rows = _rows(tmp_path, 3)
    for r, day in zip(rows, days, strict=True):
        r["ts_utc"] = f"{day}T12:00:00.000Z"
    page = render(rows, [], proxy_rows=0, generated_utc="now")
    days_section = page[page.index('id="days"') :]
    shown = [d for d in sorted(days, reverse=True) if d in days_section]
    assert shown == sorted(days, reverse=True)
    assert [days_section.index(d) for d in shown] == sorted(days_section.index(d) for d in shown)
