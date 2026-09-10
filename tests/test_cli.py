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
    assert main(["--config", CONFIG, "smoke", "local"]) == 2
    assert "--model" in capsys.readouterr().err
