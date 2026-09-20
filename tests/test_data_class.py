"""`data_class` (0.4): a caller's declaration of what a request carries, written to the row
and the span, read back by both ledger commands, and never inferred from content.

The tests that matter are the closed-vocabulary one and the null one. A class the policy in
Part B cannot place would be a row nobody can act on, so it is refused before the call; and a
row with no class must stay distinguishable from every declared one, because "nobody said"
is the list an auditor asks for first.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from boundary import DataClass
from boundary.cli import main
from boundary.config import BoundaryConfig
from boundary.ledger import data_class as data_class_filter
from boundary.ledger.residency import UNDECLARED, summarise
from boundary.types import ChatRequest, data_class_value

from .conftest import ANTHROPIC_URL, CONFIG_DIR, HAIKU, OPENWEIGHTS_URL, anthropic_ok, make_gateway

CONFIG = str(CONFIG_DIR / "boundary.yaml")
BATCHES_URL = "https://api.anthropic.com/v1/messages/batches"


def _req(content: str = "Q?") -> ChatRequest:
    return ChatRequest(model=HAIKU, messages=[{"role": "user", "content": content}], max_tokens=8)


def _console(repo_config: BoundaryConfig) -> BoundaryConfig:
    return repo_config.model_copy(
        update={"telemetry": repo_config.telemetry.model_copy(update={"exporter": "console"})}
    )


# -- the vocabulary -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("given", "stored"),
    [
        (DataClass.PERSONAL, "personal"),
        ("personal", "personal"),
        (DataClass.SENSITIVE, "sensitive"),
        ("public", "public"),
        ("internal", "internal"),
        (None, None),
    ],
)
def test_the_vocabulary_is_the_plans_four_words_and_none(
    given: DataClass | str | None, stored: str | None
) -> None:
    assert data_class_value(given) == stored


@pytest.mark.parametrize("bad", ["Personal", "pii", "", "sever.decide:personal"])
def test_an_unknown_class_is_refused_by_name(bad: str) -> None:
    with pytest.raises(ValueError, match="public, internal, personal, sensitive"):
        data_class_value(bad)


# -- on a call ------------------------------------------------------------------------------


def test_the_declared_class_lands_on_the_row_the_response_and_the_span(
    repo_config: BoundaryConfig, tmp_path: Path, keys: None, capsys: pytest.CaptureFixture[str]
) -> None:
    g = make_gateway(_console(repo_config), tmp_path)
    try:
        with respx.mock(assert_all_called=True) as mock:
            mock.post(ANTHROPIC_URL).mock(return_value=anthropic_ok())
            resp = g.chat(
                _req("Marie Chaulk's file"), purpose="sever.decide", data_class="personal"
            )
        row = g.ledger.rows()[0]
    finally:
        g.close()
    out = capsys.readouterr().out
    assert row["data_class"] == "personal"
    assert resp.data_class == "personal"
    # The uid on the response is the row's, so a caller's own records join on the identifier
    # that survives `ledger merge`, not on the per-file id.
    assert resp.call_uid is not None and len(resp.call_uid) == 32
    assert resp.call_uid == row["call_uid"]
    assert '"boundary.data_class": "personal"' in out
    assert "Marie" not in out and "Chaulk" not in out


def test_no_declaration_is_a_null_column_not_a_default(
    repo_config: BoundaryConfig, tmp_path: Path, keys: None
) -> None:
    g = make_gateway(repo_config, tmp_path)
    try:
        with respx.mock(assert_all_called=True) as mock:
            mock.post(ANTHROPIC_URL).mock(return_value=anthropic_ok())
            resp = g.chat(_req(), purpose="dev")
        row = g.ledger.rows()[0]
    finally:
        g.close()
    assert row["data_class"] is None and resp.data_class is None


def test_an_unknown_class_makes_no_call_and_writes_no_row(
    repo_config: BoundaryConfig, tmp_path: Path, keys: None
) -> None:
    g = make_gateway(repo_config, tmp_path)
    try:
        with respx.mock(assert_all_called=False) as mock:
            route = mock.post(ANTHROPIC_URL).mock(return_value=anthropic_ok())
            with pytest.raises(ValueError, match="not one of"):
                g.chat(_req(), purpose="dev", data_class="pii")
            assert route.call_count == 0
        assert g.ledger.count() == 0
    finally:
        g.close()


async def test_the_async_and_streamed_calls_carry_it_too(
    repo_config: BoundaryConfig, tmp_path: Path, keys: None
) -> None:
    from tests.test_streaming import _SSE, _stream_response, openai_shaped_events

    g = make_gateway(repo_config, tmp_path)
    try:
        with respx.mock(assert_all_called=True) as mock:
            mock.post(ANTHROPIC_URL).mock(return_value=anthropic_ok())
            mock.post(OPENWEIGHTS_URL).mock(
                return_value=_stream_response(_SSE(openai_shaped_events(), delay_s=0.0))
            )
            a = await g.achat(_req(), purpose="dev", data_class=DataClass.INTERNAL)
            s = g.chat_stream(
                ChatRequest(
                    model="openweights/openai/gpt-oss-120b",
                    messages=[{"role": "user", "content": "hi"}],
                    max_tokens=8,
                ),
                purpose="dev",
                data_class="sensitive",
            )
        rows = {r["id"]: r for r in g.ledger.rows()}
    finally:
        g.close()
    assert a.data_class == "internal" and rows[a.ledger_id]["data_class"] == "internal"
    assert s.data_class == "sensitive" and rows[s.ledger_id]["data_class"] == "sensitive"


def test_a_batch_writes_one_class_to_every_row(
    repo_config: BoundaryConfig, tmp_path: Path, keys: None
) -> None:
    g = make_gateway(repo_config, tmp_path)
    try:
        with respx.mock(assert_all_called=True) as mock:
            mock.post(BATCHES_URL).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "id": "msgbatch_dc",
                        "type": "message_batch",
                        "processing_status": "in_progress",
                        "request_counts": {"processing": 2, "succeeded": 0, "errored": 0},
                        "results_url": None,
                    },
                )
            )
            g.batch_submit([_req("a"), _req("b")], purpose="own-run", data_class="personal")
        rows = g.ledger.rows()
    finally:
        g.close()
    assert len(rows) == 2 and {r["data_class"] for r in rows} == {"personal"}


# -- reading it back --------------------------------------------------------------------------


def _rows() -> list[dict[str, object]]:
    base = {
        "ts_utc": "2026-09-19T12:00:00.000Z",
        "project": "access-to-information-redaction",
        "region": None,
        "residency": None,
        "model_requested": HAIKU,
        "input_tokens": 10,
        "output_tokens": 4,
        "cached": 0,
        "error_type": None,
    }
    return [
        {**base, "provider": "anthropic", "data_class": "personal"},
        {**base, "provider": "anthropic", "data_class": "personal"},
        {**base, "provider": "bedrock", "region": "ca-central-1", "data_class": "public"},
        {**base, "provider": "openai", "data_class": None},
    ]


def test_the_filter_matches_exactly_and_finds_the_undeclared() -> None:
    rows = _rows()
    assert [r["provider"] for r in rows if data_class_filter.matches(r, "personal")] == [
        "anthropic",
        "anthropic",
    ]
    assert [r["provider"] for r in rows if data_class_filter.matches(r, UNDECLARED)] == ["openai"]
    assert sum(data_class_filter.matches(r, None) for r in rows) == 4
    assert data_class_filter.label(None) == UNDECLARED and data_class_filter.label("x") == "x"


def test_a_misspelt_filter_is_refused_rather_than_matching_nothing() -> None:
    with pytest.raises(ValueError, match="undeclared"):
        data_class_filter.check_filter("Personal")
    assert data_class_filter.check_filter("undeclared") == "undeclared"


def test_a_class_a_later_version_wrote_can_still_be_queried() -> None:
    """The vocabulary is closed for writing and open for reading. A ledger written by a
    later version can hold a class this one has never heard of, and an auditor has to be
    able to ask about the rows in front of them; refusing there would make an old reader
    unable to query its own data. A word that matches nothing anywhere is still refused,
    and the message now names what the ledger does hold."""
    present = {"personal", "restricted-future"}
    assert data_class_filter.check_filter("restricted-future", present) == "restricted-future"
    with pytest.raises(ValueError, match="this ledger also holds restricted-future"):
        data_class_filter.check_filter("persnal", present)
    assert data_class_filter.present_in(_rows()) == {"personal", "public"}


def test_residency_can_answer_where_the_personal_calls_went() -> None:
    groups = summarise(_rows(), data_class="personal")
    assert [(g.provider, g.calls) for g in groups] == [("anthropic", 2)]
    groups = summarise(_rows(), data_class=UNDECLARED)
    assert [(g.provider, g.calls) for g in groups] == [("openai", 1)]


def _ledger_with_two_classes(repo_config: BoundaryConfig, tmp_path: Path) -> Path:
    g = make_gateway(repo_config, tmp_path)
    try:
        with respx.mock(assert_all_called=True) as mock:
            mock.post(ANTHROPIC_URL).mock(return_value=anthropic_ok())
            g.chat(_req(), purpose="dev", data_class="personal")
            g.chat(_req(), purpose="dev", data_class="personal")
            g.chat(_req(), purpose="dev")
    finally:
        g.close()
    return tmp_path / "ledger.sqlite"


def test_report_groups_by_class_and_filters_on_it(
    repo_config: BoundaryConfig, tmp_path: Path, keys: None, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger = str(_ledger_with_two_classes(repo_config, tmp_path))
    assert main(["--config", CONFIG, "ledger", "report", "--ledger", ledger]) == 0
    out = capsys.readouterr().out
    lines = [ln for ln in out.splitlines() if HAIKU in ln]
    assert len(lines) == 2, "one line per declared class, and the undeclared row is its own"
    assert any(" personal " in ln and ln.split()[-6] == "2" for ln in lines)
    assert any(" undeclared " in ln and ln.split()[-6] == "1" for ln in lines)

    code = main(
        ["--config", CONFIG, "ledger", "report", "--ledger", ledger, "--data-class", "personal"]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "filtered to data class personal: 2 row(s)" in out
    assert "undeclared" not in out


def test_report_columns_stay_aligned_under_a_long_project_name(
    repo_config: BoundaryConfig, tmp_path: Path, keys: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """The plan's project names run to 31 characters and the column was 24, so one long
    name pushed every column after it out of line and the table stopped being readable."""
    g = make_gateway(repo_config, tmp_path, project="access-to-information-redaction")
    try:
        with respx.mock(assert_all_called=True) as mock:
            mock.post(ANTHROPIC_URL).mock(return_value=anthropic_ok())
            g.chat(_req(), purpose="dev", data_class="personal")
    finally:
        g.close()
    assert (
        main(["--config", CONFIG, "ledger", "report", "--ledger", str(tmp_path / "ledger.sqlite")])
        == 0
    )
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    header, row = lines[0], lines[1]
    assert header.index("class") == row.index("personal")
    assert header.index("model") == row.index(HAIKU)


def test_residency_command_filters_on_it_and_refuses_a_bad_filter(
    repo_config: BoundaryConfig, tmp_path: Path, keys: None, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger = str(_ledger_with_two_classes(repo_config, tmp_path))
    code = main(
        [
            "--config",
            CONFIG,
            "ledger",
            "residency",
            "--ledger",
            ledger,
            "--data-class",
            "personal",
        ]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "calls whose caller declared data class personal" in out
    line = next(ln for ln in out.splitlines() if ln.startswith("undeclared") and "anthropic" in ln)
    assert line.split()[3] == "2", "two personal calls, both to a provider declaring no residency"

    code = main(
        ["--config", CONFIG, "ledger", "residency", "--ledger", ledger, "--data-class", "pii"]
    )
    err = capsys.readouterr().err
    assert code == 2 and "not one of" in err
