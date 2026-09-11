"""The Rule C experiment behind docs/rejected.md. The claims on that page are these
assertions at a size CI can afford."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from boundary.config import BoundaryConfig
from boundary.experiment import remote_ledger as rl
from boundary.ledger.store import LedgerRow, utc_now

from .conftest import CONFIG_DIR, HAIKU

SMALL = {"repetitions": 6, "calls": 10}


@pytest.fixture(scope="module")
def result(tmp_path_factory: pytest.TempPathFactory) -> rl.ExperimentResult:
    return rl.run(CONFIG_DIR / "boundary.yaml", tmp_path_factory.mktemp("experiment"), **SMALL)


def test_local_first_finishes_every_run_and_loses_nothing(result: rl.ExperimentResult) -> None:
    arm = result.arms["local-first"]
    assert arm["runs_finished"] == SMALL["repetitions"]
    assert arm["calls_reached_vendor"] == SMALL["repetitions"] * SMALL["calls"]
    assert arm["rows_in_central"] == arm["calls_reached_vendor"]
    assert arm["calls_with_no_row"] == 0 and arm["rows_in_flight"] == 0
    assert arm["unrecorded"] == 0 and arm["unrecorded_share"] == 0.0
    assert arm["uncapped_calls"] == 0
    assert arm["merge_second_pass_inserted"] == 0, "a second merge must add nothing"


def test_the_strict_remote_ledger_stops_the_runs(result: rl.ExperimentResult) -> None:
    arm = result.arms["remote-strict"]
    assert arm["runs_finished"] == 0
    assert arm["calls_reached_vendor"] > 0, "the calls up to the outage were still billed"
    assert arm["calls_reached_vendor"] < SMALL["repetitions"] * SMALL["calls"]


def test_the_best_effort_remote_ledger_loses_rows_and_stops_checking_the_cap(
    result: rl.ExperimentResult,
) -> None:
    arm = result.arms["remote-best-effort"]
    assert arm["runs_finished"] == SMALL["repetitions"], "swallowing the error finishes the run"
    assert arm["calls_reached_vendor"] == SMALL["repetitions"] * SMALL["calls"]
    assert arm["calls_with_no_row"] > 0, "calls were billed with no row at all"
    assert arm["unrecorded_share"] > 0.0
    assert arm["uncapped_calls"] > 0, "calls were made with no cap check"


def test_every_arm_makes_the_same_calls_to_the_vendor_where_it_can(
    result: rl.ExperimentResult,
) -> None:
    """The arms differ in where the record goes, not in what is asked of the vendor."""
    planned = SMALL["repetitions"] * SMALL["calls"]
    assert result.arms["local-first"]["calls_attempted"] == planned
    assert result.arms["remote-best-effort"]["calls_attempted"] == planned
    # The strict arm stops early, so it attempts fewer.
    assert result.arms["remote-strict"]["calls_attempted"] < planned


def test_the_reported_share_carries_an_interval(result: rl.ExperimentResult) -> None:
    for arm in rl.ARMS:
        lo, hi = result.unrecorded_ci[arm]
        assert lo <= hi
        assert 0.0 <= lo <= 1.0 and 0.0 <= hi <= 1.0
        assert len(result.arms[arm]["unrecorded_rate_per_run"]) == SMALL["repetitions"]


def test_the_outage_window_is_counted_in_operations(tmp_path: Path) -> None:
    """Operations one and two reach the host, three and four do not, five does again."""
    store = rl._RemoteLedger(tmp_path / "remote.sqlite", down_from=2, down_until=4, strict=False)
    try:
        assert store.spend_usd(project="p") == 0.0  # operation 0, up: empty ledger
        row = LedgerRow(
            ts_utc=utc_now(),
            boundary_version="test",
            project="p",
            purpose="dev",
            mode="standard",
            provider="anthropic",
            model_requested=HAIKU,
            cost_usd=0.5,
        )
        store.begin(row)  # operation 1, up
        assert row.id is not None and store.count() == 1
        lost = dataclasses.replace(row, id=None, call_uid="deadbeef")
        store.begin(lost)  # operation 2, down: swallowed
        assert lost.id is None and store.count() == 1
        store.complete(lost)  # operation 3, down: swallowed
        assert store.count() == 1
        row.http_status = 200
        row.error_type = None
        store.complete(row)  # operation 4, up again
        assert store.rows()[0]["http_status"] == 200
        assert store.failed_operations == 2 and store.swallowed_writes == 2
    finally:
        store.close()


def test_the_strict_arm_raises_rather_than_dropping_a_row(tmp_path: Path) -> None:
    store = rl._RemoteLedger(tmp_path / "remote.sqlite", down_from=0, down_until=9, strict=True)
    try:
        with pytest.raises(rl.RemoteLedgerUnavailable):
            store.spend_usd(project="p")
    finally:
        store.close()


def test_write_doc_replaces_only_what_is_between_the_markers(tmp_path: Path) -> None:
    doc = tmp_path / "rejected.md"
    doc.write_text(
        f"before\n{rl.DOC_START}\n| old |\n{rl.DOC_END}\nafter\n",
        encoding="utf-8",
    )
    rl.write_doc(doc, "| new |")
    text = doc.read_text(encoding="utf-8")
    assert text == f"before\n{rl.DOC_START}\n| new |\n{rl.DOC_END}\nafter\n"


def test_the_doc_table_has_one_row_per_arm(result: rl.ExperimentResult) -> None:
    rows = result.doc_rows().splitlines()
    assert len(rows) == len(rl.ARMS)
    for arm, line in zip(rl.ARMS, rows, strict=True):
        assert line.startswith(f"| `{arm}` |") and line.endswith("|")
    assert "n/a" in rows[0], "the remote arms have no merge to run"
    assert rows[-1].endswith("| 0 |"), "the second merge of the local-first arm adds nothing"


def test_the_checked_in_doc_is_the_one_the_command_fills(repo_config: BoundaryConfig) -> None:
    """A hand-edited table would drift from bench/remote-ledger.json."""
    doc = (CONFIG_DIR.parent / "docs" / "rejected.md").read_text(encoding="utf-8")
    assert rl.DOC_START in doc and rl.DOC_END in doc
    body = doc[doc.index(rl.DOC_START) + len(rl.DOC_START) : doc.index(rl.DOC_END)].strip()
    for arm in rl.ARMS:
        assert f"| `{arm}` |" in body, f"{arm} is missing from docs/rejected.md"


def test_the_readme_figures_match_the_results_file() -> None:
    """Rule C's paragraph in the README quotes two numbers from the last run of the
    experiment. If the experiment is re-run, they move, and this fails rather than letting
    the README drift away from bench/remote-ledger.json."""
    import json
    import re

    repo = CONFIG_DIR.parent
    results = json.loads((repo / "bench" / "remote-ledger.json").read_text(encoding="utf-8"))
    arm = results["arms"]["remote-best-effort"]
    readme = (repo / "README.md").read_text(encoding="utf-8")
    share = re.search(r"left (\d+\.\d)% of the calls", readme)
    uncapped = re.search(r"made ([\d,]+) calls with no cap check", readme)
    assert share is not None and uncapped is not None, "the Rule C paragraph changed shape"
    assert float(share.group(1)) == pytest.approx(round(arm["unrecorded_share"] * 100, 1))
    assert int(uncapped.group(1).replace(",", "")) == arm["uncapped_calls"]
    assert results["repetitions"] == 100, "the README says 100 runs"
