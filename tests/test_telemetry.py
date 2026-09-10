"""One span per call, ids in the ledger, and never any content."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from boundary.config import BoundaryConfig
from boundary.errors import ConfigError, ProviderError
from boundary.telemetry import ALLOWED_ATTRIBUTES, Telemetry
from boundary.types import ChatRequest

from .conftest import ANTHROPIC_URL, HAIKU, anthropic_ok, make_gateway

SECRET_PROMPT = "The patient Jane Q Public has diagnosis code Z99."


def _console_config(repo_config: BoundaryConfig) -> BoundaryConfig:
    return repo_config.model_copy(
        update={"telemetry": repo_config.telemetry.model_copy(update={"exporter": "console"})}
    )


def test_span_per_call_with_ids_in_ledger_and_no_content(
    repo_config: BoundaryConfig, tmp_path: Path, keys: None, capsys: pytest.CaptureFixture[str]
) -> None:
    g = make_gateway(_console_config(repo_config), tmp_path)
    try:
        with respx.mock(assert_all_called=True) as mock:
            mock.post(ANTHROPIC_URL).mock(return_value=anthropic_ok(text="Jane Q Public"))
            resp = g.chat(
                ChatRequest(
                    model=HAIKU,
                    messages=[{"role": "user", "content": SECRET_PROMPT}],
                    system="You are a clinician.",
                    max_tokens=8,
                ),
                purpose="dev",
                run_id="t1",
            )
        row = g.ledger.rows()[0]
    finally:
        g.close()
    out = capsys.readouterr().out
    assert resp.trace_id is not None and len(resp.trace_id) == 32
    assert row["trace_id"] == resp.trace_id and len(row["span_id"]) == 16
    assert resp.trace_id in out and row["span_id"] in out
    assert '"name": "boundary.chat"' in out
    assert "gen_ai.request.model" in out and "boundary.cost_usd" in out
    for leak in ("Jane", "Z99", "clinician", "patient"):
        assert leak not in out, f"content leaked into telemetry: {leak!r}"


def test_error_calls_produce_an_error_span(
    repo_config: BoundaryConfig, tmp_path: Path, keys: None, capsys: pytest.CaptureFixture[str]
) -> None:
    g = make_gateway(_console_config(repo_config), tmp_path)
    try:
        with respx.mock(assert_all_called=True) as mock:
            mock.post(ANTHROPIC_URL).mock(
                return_value=httpx.Response(
                    400, json={"type": "error", "error": {"message": "bad"}}
                )
            )
            with pytest.raises(ProviderError):
                g.chat(
                    ChatRequest(
                        model=HAIKU, messages=[{"role": "user", "content": "x"}], max_tokens=1
                    ),
                    purpose="dev",
                )
    finally:
        g.close()
    out = capsys.readouterr().out
    assert '"error.type": "http_400"' in out
    assert '"status_code": "ERROR"' in out


def test_exporter_none_means_no_ids_and_no_output(
    repo_config: BoundaryConfig, tmp_path: Path, keys: None, capsys: pytest.CaptureFixture[str]
) -> None:
    g = make_gateway(repo_config, tmp_path)
    try:
        with respx.mock(assert_all_called=True) as mock:
            mock.post(ANTHROPIC_URL).mock(return_value=anthropic_ok())
            resp = g.chat(
                ChatRequest(model=HAIKU, messages=[{"role": "user", "content": "x"}], max_tokens=1),
                purpose="dev",
            )
        row = g.ledger.rows()[0]
    finally:
        g.close()
    assert resp.trace_id is None and row["trace_id"] is None
    assert capsys.readouterr().out == ""


def test_attribute_allow_list_is_enforced(repo_config: BoundaryConfig) -> None:
    t = Telemetry(repo_config.telemetry.model_copy(update={"exporter": "console"}), version="t")
    span = t.start("x")
    with pytest.raises(ValueError, match="allow list"):
        Telemetry.set_attributes(span, {"gen_ai.prompt": "never"})
    span.end()
    t.shutdown()
    assert "gen_ai.prompt" not in ALLOWED_ATTRIBUTES
    assert "gen_ai.completion" not in ALLOWED_ATTRIBUTES


def test_otlp_is_part_b(repo_config: BoundaryConfig) -> None:
    with pytest.raises(ConfigError, match="Part B"):
        Telemetry(repo_config.telemetry.model_copy(update={"exporter": "otlp"}), version="t")
