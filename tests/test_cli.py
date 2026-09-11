"""The command line does what the plan lists, against the checked-in configuration."""

from __future__ import annotations

from pathlib import Path

import pytest
import respx

from boundary.cli import main
from boundary.config import BoundaryConfig
from boundary.types import ChatRequest

from .conftest import ANTHROPIC_URL, CONFIG_DIR, HAIKU, anthropic_ok, make_gateway

CONFIG = str(CONFIG_DIR / "boundary.yaml")


def test_routes_show(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--config", CONFIG, "routes", "show"]) == 0
    out = capsys.readouterr().out
    assert "fast" in out and "anthropic/claude-haiku-4-5-20251001" in out
    assert "provider openai" in out and "OPENAI_API_KEY" in out


def test_prices_check_validates_and_lists(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["--config", CONFIG, "prices", "check"])
    captured = capsys.readouterr()
    assert "latest price list:" in captured.out
    assert "anthropic/claude-haiku-4-5-20251001: in 1.0 out 5.0" in captured.out
    # Exit code 1 only when the list is stale or a route has no price; either is a warning
    # on stderr, never silent.
    assert code in (0, 1)
    if code == 1:
        assert captured.err.strip()


def test_ledger_report(
    repo_config: BoundaryConfig, tmp_path: Path, keys: None, capsys: pytest.CaptureFixture[str]
) -> None:
    g = make_gateway(repo_config, tmp_path)
    try:
        with respx.mock(assert_all_called=True) as mock:
            mock.post(ANTHROPIC_URL).mock(
                return_value=anthropic_ok(input_tokens=100, output_tokens=10)
            )
            for _ in range(3):
                g.chat(
                    ChatRequest(
                        model=HAIKU, messages=[{"role": "user", "content": "x"}], max_tokens=8
                    ),
                    purpose="dev",
                )
    finally:
        g.close()
    code = main(
        ["--config", CONFIG, "ledger", "report", "--ledger", str(tmp_path / "ledger.sqlite")]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "ai-release-gate" in out and HAIKU in out
    line = next(ln for ln in out.splitlines() if HAIKU in ln)
    cols = line.split()
    assert cols[-6:-1] == ["3", "0", "0", "300", "30"]
    assert float(cols[-1]) == pytest.approx(3 * (100e-6 * 1.0 + 10e-6 * 5.0), abs=1e-4)


def test_smoke_uses_the_gateway(
    tmp_path: Path, keys: None, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Point the ledger somewhere disposable by running from a temp directory with a copied
    # config whose ledger path is relative.
    cfg = (CONFIG_DIR / "boundary.yaml").read_text(encoding="utf-8")
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "boundary.yaml").write_text(
        cfg.replace("prices: prices", f"prices: {CONFIG_DIR.as_posix()}/prices").replace(
            "caps: caps.yaml", f"caps: {CONFIG_DIR.as_posix()}/caps.yaml"
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(ANTHROPIC_URL).mock(return_value=anthropic_ok(text="OK"))
        code = main(["--config", str(cfg_dir / "boundary.yaml"), "smoke", "anthropic"])
        assert route.call_count == 1
    out = capsys.readouterr().out
    assert code == 0 and "anthropic: status 200" in out and "'OK'" in out
    assert (tmp_path / "boundary.sqlite").is_file()


def test_smoke_unknown_provider_needs_a_model(capsys: pytest.CaptureFixture[str]) -> None:
    """A provider with no default model is refused before anything is built, which is also
    what keeps this test from making a call. `local` used to be the example here; it has a
    default now, and with one it reached a real server and wrote to the real ledger."""
    assert main(["--config", CONFIG, "smoke", "aws_bedrock"]) == 2
    assert "--model" in capsys.readouterr().err


def test_smoke_batch_submits_and_collects(
    tmp_path: Path, keys: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`boundary smoke anthropic --batch` is what the smoke workflow runs to exercise the
    batch path live. The wiring deserves its own test: a mistake in it would show up only as
    a failed job, after a real batch had been submitted and billed."""
    import json

    import httpx

    from boundary.config import load_config
    from tests.test_batches import (
        BATCH_ID,
        BATCHES_URL,
        RESULTS_URL,
        STATUS_URL,
        _results,
        _status,
        _submitted,
        _succeeded,
    )

    # The command builds its own gateway from the configuration, which would write to this
    # repository's real ledger. Point it at a temporary one instead.
    def fake_from_config(path: str, **kw: object) -> object:
        return make_gateway(load_config(path), tmp_path, project="compliant-ai-gateway")

    monkeypatch.setattr("boundary.cli.Gateway.from_config", fake_from_config)

    submitted: list[str] = []

    def on_submit(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        submitted.extend(item["custom_id"] for item in body["requests"])
        return _submitted()

    def on_results(request: httpx.Request) -> httpx.Response:
        return _results([_succeeded(custom_id) for custom_id in submitted])

    with respx.mock(assert_all_called=True) as mock:
        mock.post(BATCHES_URL).mock(side_effect=on_submit)
        mock.get(STATUS_URL).mock(return_value=_status())
        mock.get(RESULTS_URL).mock(side_effect=on_results)
        code = main(["--config", CONFIG, "smoke", "anthropic", "--batch", "--wait", "0"])

    out = capsys.readouterr().out
    assert code == 0, out
    assert f"submitted batch {BATCH_ID}" in out
    assert len(submitted) == 2, "the smoke batch is two requests"
    assert out.count("200 model claude-haiku-4-5-20251001") == 2


def _submit_a_batch(repo_config: BoundaryConfig, tmp_path: Path) -> tuple[Path, list[str]]:
    """Leave a ledger holding one submitted, uncollected batch, as a killed or timed-out
    process would."""
    from tests.test_batches import BATCHES_URL, _requests, _submitted

    gw = make_gateway(repo_config, tmp_path, project="compliant-ai-gateway")
    try:
        with respx.mock(assert_all_called=True) as mock:
            mock.post(BATCHES_URL).mock(return_value=_submitted())
            handle = gw.batch_submit(_requests(2), purpose="smoke-batch")
        return gw.ledger.path, list(handle.custom_ids)
    finally:
        gw.close()


def test_batch_collect_completes_rows_from_the_ledger_that_submitted_them(
    repo_config: BoundaryConfig, tmp_path: Path, keys: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """The cross-process path, which is the whole point of keeping the batch id in a column:
    a second process, holding nothing but that id and the ledger, finishes the rows."""
    from tests.test_batches import (
        BATCH_ID,
        RESULTS_URL,
        STATUS_URL,
        _results,
        _status,
        _succeeded,
    )

    ledger, custom_ids = _submit_a_batch(repo_config, tmp_path)
    capsys.readouterr()

    with respx.mock(assert_all_called=True) as mock:
        mock.get(STATUS_URL).mock(return_value=_status())
        mock.get(RESULTS_URL).mock(return_value=_results([_succeeded(uid) for uid in custom_ids]))
        code = main(["--config", CONFIG, "batch", "collect", BATCH_ID, "--ledger", str(ledger)])

    out = capsys.readouterr().out
    assert code == 0, out
    assert "collected 2 request(s) at the batch rate" in out
    from boundary.ledger.store import LedgerStore

    store = LedgerStore(ledger)
    try:
        rows = store.rows_for_batch(BATCH_ID)
        assert {r["error_type"] for r in rows} == {None}, "no row is left in flight"
        assert all(r["costed"] and r["cost_usd"] for r in rows)
    finally:
        store.close()


def test_batch_status_says_not_ended_without_changing_anything(
    repo_config: BoundaryConfig, tmp_path: Path, keys: None, capsys: pytest.CaptureFixture[str]
) -> None:
    from boundary.ledger.store import IN_FLIGHT, LedgerStore
    from tests.test_batches import BATCH_ID, STATUS_URL, _status

    ledger, _ = _submit_a_batch(repo_config, tmp_path)
    capsys.readouterr()

    with respx.mock(assert_all_called=True) as mock:
        mock.get(STATUS_URL).mock(return_value=_status("in_progress", results_url=None))
        code = main(["--config", CONFIG, "batch", "status", BATCH_ID, "--ledger", str(ledger)])

    out = capsys.readouterr().out
    assert code == 1, "not ended is a non-zero exit, so a script can wait on it"
    assert "in_progress" in out and "2 request(s)" in out
    store = LedgerStore(ledger)
    try:
        assert {r["error_type"] for r in store.rows_for_batch(BATCH_ID)} == {IN_FLIGHT}
    finally:
        store.close()


def test_collecting_a_batch_the_ledger_does_not_hold_is_refused(
    repo_config: BoundaryConfig, tmp_path: Path, keys: None
) -> None:
    """The wrong ledger, or the wrong run's artefact. Better to say so than to report that a
    batch had no requests in it."""
    ledger, _ = _submit_a_batch(repo_config, tmp_path)
    with pytest.raises(ValueError, match="no rows for batch"):
        main(["--config", CONFIG, "batch", "collect", "msgbatch_nope", "--ledger", str(ledger)])
