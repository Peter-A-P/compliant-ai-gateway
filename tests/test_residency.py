"""`ledger residency`: where the data went, and the gate that fails closed.

The tests that matter here are the fail-closed ones. A report that groups rows correctly and
then lets an undeclared row satisfy `--require single-region` would be worse than no report,
because it would produce a clean-looking answer to a question it never actually asked.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from boundary.cli import main
from boundary.config import Residency
from boundary.ledger.residency import (
    UNDECLARED,
    UNKNOWN_REACH,
    is_known,
    label,
    reach,
    summarise,
    violations,
)
from boundary.ledger.store import LedgerRow, LedgerStore

from .conftest import CONFIG_DIR

CONFIG = str(CONFIG_DIR / "boundary.yaml")


def _row(**kw: Any) -> dict[str, Any]:
    """A ledger row as `store.rows()` hands it back: a plain mapping of columns."""
    base: dict[str, Any] = {
        "ts_utc": "2026-09-16T12:00:00.000Z",
        "project": "compliant-ai-gateway",
        "provider": "anthropic",
        "region": None,
        "residency": None,
        "model_requested": "anthropic/claude-haiku-4-5-20251001",
        "input_tokens": 10,
        "output_tokens": 4,
        "cached": 0,
        "error_type": None,
    }
    base.update(kw)
    return base


# -- the ordering ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "rank"),
    [
        ("single-region", 0),
        ("geo", 1),
        ("global", 2),
        # Null is not "global". It is "nobody said", and it ranks above every declared class
        # so that it fails every limit.
        (None, UNKNOWN_REACH),
        # A class a later schema version added. An older reader cannot tell whether it is
        # narrower or wider than global, so it refuses to let it satisfy anything.
        ("country", UNKNOWN_REACH),
        ("", UNKNOWN_REACH),
    ],
)
def test_reach_orders_by_how_far_a_request_may_travel(value: str | None, rank: int) -> None:
    assert reach(value) == rank


def test_the_three_known_classes_are_strictly_ordered() -> None:
    assert reach("single-region") < reach("geo") < reach("global") < UNKNOWN_REACH


def test_label_prints_an_unrecognised_value_rather_than_hiding_it() -> None:
    assert label(None) == UNDECLARED
    assert label("global") == "global"
    # Printed as stored. A reader seeing a word this version does not know is the point.
    assert label("country") == "country"
    assert is_known("geo") and not is_known("country") and not is_known(None)


# -- grouping ----------------------------------------------------------------------------


def test_groups_by_provider_region_and_residency_widest_first() -> None:
    groups = summarise(
        [
            _row(provider="bedrock", region="ca-central-1", residency="geo"),
            _row(provider="bedrock", region="ca-central-1", residency="geo", input_tokens=6),
            _row(provider="foundry-canada", region="canadacentral", residency="global"),
            _row(provider="local", region="localhost", residency="single-region"),
            _row(provider="anthropic"),
        ]
    )
    assert [(g.provider, g.residency) for g in groups] == [
        ("anthropic", UNDECLARED),
        ("foundry-canada", "global"),
        ("bedrock", "geo"),
        ("local", "single-region"),
    ]
    bedrock = next(g for g in groups if g.provider == "bedrock")
    assert bedrock.calls == 2
    assert bedrock.input_tokens == 16
    assert bedrock.output_tokens == 8


def test_the_same_provider_in_two_regions_is_two_groups() -> None:
    """The unit is the triple, not the provider. One entry pointed at two regions is two
    different residency answers and must not be added up into one line."""
    groups = summarise(
        [
            _row(provider="bedrock", region="ca-central-1", residency="geo"),
            _row(provider="bedrock", region="us-east-1", residency="global"),
        ]
    )
    assert [(g.region, g.residency) for g in groups] == [
        ("us-east-1", "global"),
        ("ca-central-1", "geo"),
    ]


def test_models_are_listed_sorted_and_deduplicated() -> None:
    groups = summarise(
        [
            _row(provider="bedrock", region="ca-central-1", residency="geo", model_requested="b"),
            _row(provider="bedrock", region="ca-central-1", residency="geo", model_requested="a"),
            _row(provider="bedrock", region="ca-central-1", residency="geo", model_requested="b"),
        ]
    )
    assert groups[0].models == ("a", "b")


def test_a_null_region_prints_as_a_dash_rather_than_none() -> None:
    assert summarise([_row(region=None)])[0].region == "-"


def test_month_and_project_filters() -> None:
    rows = [
        _row(ts_utc="2026-08-31T23:59:59.000Z"),
        _row(ts_utc="2026-09-01T00:00:00.000Z"),
        _row(ts_utc="2026-09-30T00:00:00.000Z", project="other"),
    ]
    assert sum(g.calls for g in summarise(rows, month="2026-09")) == 2
    assert sum(g.calls for g in summarise(rows, month="2026-08")) == 1
    assert sum(g.calls for g in summarise(rows, project="other")) == 1
    assert sum(g.calls for g in summarise(rows, month="2026-09", project="other")) == 1


def test_errors_are_counted_and_still_count_as_traffic() -> None:
    """A failed call still put bytes on the wire. The request reached the region whether or
    not the response was a 200, so it belongs in the residency answer."""
    groups = summarise(
        [
            _row(provider="vertex", region="global", residency="global", error_type="provider"),
            _row(provider="vertex", region="global", residency="global"),
        ]
    )
    assert groups[0].calls == 2
    assert groups[0].errors == 1
    assert groups[0].sent == 2


# -- the gate ----------------------------------------------------------------------------


def test_a_declared_limit_admits_anything_narrower() -> None:
    groups = summarise(
        [
            _row(provider="local", region="localhost", residency="single-region"),
            _row(provider="bedrock", region="ca-central-1", residency="geo"),
        ]
    )
    assert violations(groups, Residency.GEO) == []
    assert violations(groups, Residency.GLOBAL) == []
    # geo is wider than single-region, so the Bedrock group is the one that fails.
    bad = violations(groups, Residency.SINGLE_REGION)
    assert [g.provider for g in bad] == ["bedrock"]


@pytest.mark.parametrize("limit", list(Residency))
def test_an_undeclared_row_fails_every_limit_including_global(limit: Residency) -> None:
    """The fail-closed rule, stated once per limit. `--require global` is the weakest thing
    anybody can ask for and a row that declared nothing still does not satisfy it: there is
    no claim to check, and inventing one is the failure this whole column exists to prevent.
    """
    groups = summarise([_row(provider="anthropic")])
    assert [g.provider for g in violations(groups, limit)] == ["anthropic"]


@pytest.mark.parametrize("limit", list(Residency))
def test_a_residency_from_a_later_version_fails_every_limit(limit: Residency) -> None:
    groups = summarise([_row(provider="future", region="somewhere", residency="country")])
    assert [g.provider for g in violations(groups, limit)] == ["future"]


def test_a_group_of_pure_cache_hits_is_never_a_violation() -> None:
    """Nothing left the machine, so nothing travelled. The calls are still counted, because
    a residency report whose totals disagree with `ledger report` invites the reader to
    wonder which one is lying."""
    groups = summarise([_row(cached=1), _row(cached=1)])
    assert groups[0].calls == 2
    assert groups[0].cached == 2
    assert groups[0].sent == 0
    assert violations(groups, Residency.SINGLE_REGION) == []


def test_one_real_call_among_cache_hits_is_still_a_violation() -> None:
    groups = summarise([_row(cached=1), _row(cached=0)])
    assert groups[0].sent == 1
    assert [g.provider for g in violations(groups, Residency.GLOBAL)] == ["anthropic"]


# -- the command ---------------------------------------------------------------------------


def _ledger(tmp_path: Path) -> Path:
    path = tmp_path / "residency.sqlite"
    store = LedgerStore(path)
    try:
        for provider, region, residency in (
            ("bedrock", "ca-central-1", "geo"),
            ("foundry-canada", "canadacentral", "global"),
            ("anthropic", None, None),
        ):
            row = LedgerRow(
                ts_utc="2026-09-16T12:00:00.000Z",
                boundary_version="0.2.0",
                project="compliant-ai-gateway",
                purpose="smoke",
                mode="standard",
                provider=provider,
                model_requested=f"{provider}/model",
                region=region,
                residency=residency,
            )
            store.begin(row)
            row.http_status = 200
            store.complete(row)
    finally:
        store.close()
    return path


def test_command_reports_and_names_what_it_does_not_claim(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["--config", CONFIG, "ledger", "residency", "--ledger", str(_ledger(tmp_path))])
    out = capsys.readouterr().out
    assert code == 0
    assert "undeclared" in out and "geo" in out and "global" in out
    # The limitation is printed, not buried in a docstring: a clean report is exactly the
    # moment somebody is most likely to over-read it.
    assert "not what the vendor reported" in out
    assert "declared no residency at all" in out


def test_command_exits_2_and_says_which_group_failed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(
        [
            "--config",
            CONFIG,
            "ledger",
            "residency",
            "--ledger",
            str(_ledger(tmp_path)),
            "--require",
            "geo",
        ]
    )
    err = capsys.readouterr().err
    assert code == 2
    assert "FAIL: 2 group(s) not within geo" in err
    assert "foundry-canada/canadacentral: 1 call(s) sent, global is wider than geo" in err
    assert "anthropic/-: 1 call(s) sent, no residency declared" in err


def test_command_passes_when_every_group_is_within_the_limit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "clean.sqlite"
    store = LedgerStore(path)
    try:
        row = LedgerRow(
            ts_utc="2026-09-16T12:00:00.000Z",
            boundary_version="0.2.0",
            project="compliant-ai-gateway",
            purpose="smoke",
            mode="standard",
            provider="local",
            model_requested="local/llama3.2:3b",
            region="localhost",
            residency="single-region",
        )
        store.begin(row)
        row.http_status = 200
        store.complete(row)
    finally:
        store.close()
    code = main(
        ["--config", CONFIG, "ledger", "residency", "--ledger", str(path), "--require", "geo"]
    )
    assert code == 0
    assert "ok: every call was within geo" in capsys.readouterr().out


def test_command_month_filter_can_empty_the_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(
        [
            "--config",
            CONFIG,
            "ledger",
            "residency",
            "--ledger",
            str(_ledger(tmp_path)),
            "--month",
            "2026-01",
        ]
    )
    assert code == 0
    assert "no rows match" in capsys.readouterr().out


def test_command_says_so_when_there_is_no_ledger(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "nothing.sqlite"
    assert main(["--config", CONFIG, "ledger", "residency", "--ledger", str(missing)]) == 1
    assert "no ledger at" in capsys.readouterr().err


# -- the checked-in configuration ----------------------------------------------------------


def test_the_only_single_region_route_in_the_config_is_the_local_one(
    repo_config: Any,
) -> None:
    """A finding rather than a preference. `local` is the one provider entry that can claim
    `single-region`, because the request never reaches a network; every hosted entry declares
    `geo`, `global`, or nothing at all. If a future entry can honestly claim single-region,
    this test is the place that notices the market changed."""
    single = {
        name
        for name, pc in repo_config.providers.items()
        if pc.residency is Residency.SINGLE_REGION
    }
    assert single == {"local"}
