"""Measured rates for self-hosted hosts (0.3), kept by the project that measured them.

Project 06 runs vLLM and llama.cpp on rented GPUs and derives a USD-per-million-output-token
rate from a dated GPU-hour price and a measured throughput. That is a finding of 06's, not a
vendor's list, so it lives in a directory 06 names in its configuration and is accepted only
for provider entries flagged `self_hosted: true`. The vendor list may not carry those
providers and the overlay may not carry a vendor, so "one copy of every vendor price" stays
true with the overlay beside it. A row costed from the overlay cites the overlay, so a
self-hosted cost is audited back to the measurement that produced it.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
import respx
import yaml

from boundary.config import (
    BoundaryConfig,
    PriceEntry,
    PriceList,
    ProviderConfig,
    ProviderKind,
    check_price_lists,
    load_config,
)
from boundary.errors import ConfigError
from boundary.types import ChatRequest

from .conftest import OPENWEIGHTS_URL, make_gateway, openai_ok

VLLM_URL = "http://10.0.0.7:8000/v1/chat/completions"
VLLM_MODEL = "vllm-l4/Qwen2.5-7B-Instruct-AWQ"

# The overlay 06 would write: input at 0, output derived from the GPU-hour rate and measured
# throughput, and a `source` that names the measurement run so the number can be audited.
OVERLAY = {
    "version": 1,
    "date": dt.date(2026, 10, 3),
    "currency": "USD",
    "source": (
        "06 load test 2026-10-03, L4 spot $0.39/h checked 2026-10-03, "
        "412 output tok/s at c=32, utilisation 0.5"
    ),
    "per_million_tokens": {
        # 0.39 / (412 tok/s x 3600 s x 0.5) x 1e6 = 0.5259 USD per million output tokens
        "vllm-l4": {"Qwen2.5-7B-Instruct-AWQ": {"input": 0.0, "output": 0.5259}},
    },
}


def _with_vllm(repo_config: BoundaryConfig) -> BoundaryConfig:
    return repo_config.model_copy(
        update={
            "providers": {
                **repo_config.providers,
                "vllm-l4": ProviderConfig(
                    kind=ProviderKind.OPENAI_COMPAT,
                    base_url="http://10.0.0.7:8000/v1",
                    self_hosted=True,
                    region="us-east-1",
                ),
            }
        }
    )


def _overlay_dir(tmp_path: Path, data: object = OVERLAY, name: str = "2026-10-03.yaml") -> Path:
    d = tmp_path / "self-hosted-prices"
    d.mkdir(exist_ok=True)
    (d / name).write_text(yaml.safe_dump(data), encoding="utf-8")
    return d


def _config(repo_config: BoundaryConfig, tmp_path: Path) -> BoundaryConfig:
    return _with_vllm(repo_config).model_copy(update={"self_hosted_prices": _overlay_dir(tmp_path)})


def _req(model: str = VLLM_MODEL) -> ChatRequest:
    return ChatRequest(model=model, messages=[{"role": "user", "content": "Q?"}], max_tokens=8)


# -- configuration ------------------------------------------------------------------------


def test_self_hosted_and_price_zero_together_are_refused() -> None:
    with pytest.raises(ValueError, match="cannot be both"):
        ProviderConfig(
            kind=ProviderKind.OPENAI_COMPAT,
            base_url="http://h.test/v1",
            self_hosted=True,
            price_zero=True,
        )


def test_the_overlay_directory_resolves_against_the_configuration_file(tmp_path: Path) -> None:
    cfg_dir = tmp_path / "config"
    (cfg_dir / "prices").mkdir(parents=True)
    (cfg_dir / "sh").mkdir()
    (cfg_dir / "caps.yaml").write_text(
        "version: 1\nportfolio_monthly_usd: 10\ndefault:\n  monthly_usd: 10\n", encoding="utf-8"
    )
    (cfg_dir / "boundary.yaml").write_text(
        "version: 1\n"
        "providers:\n"
        "  vllm:\n"
        "    kind: openai_compat\n"
        "    base_url: http://h.test/v1\n"
        "    self_hosted: true\n"
        "routes: {}\n"
        "prices: builtin\n"
        "self_hosted_prices: sh\n"
        "caps: caps.yaml\n",
        encoding="utf-8",
    )
    cfg = load_config(cfg_dir / "boundary.yaml")
    assert cfg.self_hosted_prices == (cfg_dir / "sh").resolve()
    assert cfg.providers["vllm"].self_hosted is True


def test_the_repo_configuration_declares_no_self_hosted_host(repo_config: BoundaryConfig) -> None:
    """This repository runs no GPU host. The flag and the overlay are for the projects that
    do; the checked-in configuration keeps every rate in the one vendor copy."""
    assert repo_config.self_hosted_prices is None
    assert not any(pc.self_hosted for pc in repo_config.providers.values())


# -- the two refusals that keep one copy of vendor prices -------------------------------


def test_the_overlay_may_not_carry_a_vendor(repo_config: BoundaryConfig, tmp_path: Path) -> None:
    bad = {**OVERLAY, "per_million_tokens": {"anthropic": {"claude-x": {"input": 1, "output": 5}}}}
    cfg = _with_vllm(repo_config).model_copy(
        update={"self_hosted_prices": _overlay_dir(tmp_path, bad)}
    )
    with pytest.raises(ConfigError, match=r"'anthropic'.*not flagged `self_hosted: true`"):
        make_gateway(cfg, tmp_path)


def test_the_overlay_may_not_name_a_provider_the_configuration_lacks(
    repo_config: BoundaryConfig, tmp_path: Path
) -> None:
    bad = {**OVERLAY, "per_million_tokens": {"vllm-typo": {"m": {"input": 0, "output": 1}}}}
    cfg = _with_vllm(repo_config).model_copy(
        update={"self_hosted_prices": _overlay_dir(tmp_path, bad)}
    )
    with pytest.raises(ConfigError, match="'vllm-typo', which is not in the configuration"):
        make_gateway(cfg, tmp_path)


def test_the_vendor_list_may_not_carry_a_self_hosted_provider(
    repo_config: BoundaryConfig, tmp_path: Path
) -> None:
    vendor = PriceList(
        version=1,
        date=dt.date(2026, 10, 3),
        currency="USD",
        source="test",
        per_million_tokens={"vllm-l4": {"m": PriceEntry(input=0.0, output=1.0)}},
    )
    with pytest.raises(ConfigError, match=r"self-hosted provider.*vllm-l4"):
        check_price_lists(_with_vllm(repo_config), vendor, None)


def test_the_shipped_vendor_lists_pass_the_check_with_no_overlay(
    repo_config: BoundaryConfig, tmp_path: Path, keys: None
) -> None:
    g = make_gateway(_with_vllm(repo_config), tmp_path)
    try:
        assert g.self_hosted_prices is None
    finally:
        g.close()


# -- costing from the overlay -------------------------------------------------------------


def test_a_self_hosted_row_is_costed_from_the_overlay_and_cites_it(
    repo_config: BoundaryConfig, tmp_path: Path, keys: None
) -> None:
    g = make_gateway(_config(repo_config, tmp_path), tmp_path)
    try:
        assert g.self_hosted_prices is not None and g.self_hosted_prices.name == "2026-10-03"
        with respx.mock(assert_all_called=True) as mock:
            mock.post(VLLM_URL).mock(return_value=openai_ok(prompt=500, completion=1000))
            resp = g.chat(_req(), purpose="load-test", run_id="lt-1")
        # Input at 0, output at the measured rate: 1000 tokens x 0.5259 per million.
        assert resp.costed and resp.cost_usd == pytest.approx(1000 / 1e6 * 0.5259)
        assert resp.price_list == "2026-10-03"
        assert resp.price_sha256 == g.self_hosted_prices.rates_sha256
        assert resp.price_sha256 != g.prices.rates_sha256

        row = g.ledger.rows()[0]
        assert row["provider"] == "vllm-l4" and row["costed"] == 1
        assert row["cost_usd"] == pytest.approx(resp.cost_usd)
        assert row["price_list"] == "2026-10-03"
        assert row["price_sha256"] == g.self_hosted_prices.rates_sha256
        assert g.ledger.uncosted_count() == 0
    finally:
        g.close()


def test_a_vendor_row_and_a_self_hosted_row_cite_different_lists_in_one_ledger(
    repo_config: BoundaryConfig, tmp_path: Path, keys: None
) -> None:
    g = make_gateway(_config(repo_config, tmp_path), tmp_path)
    try:
        with respx.mock(assert_all_called=True) as mock:
            mock.post(VLLM_URL).mock(return_value=openai_ok(prompt=10, completion=10))
            mock.post(OPENWEIGHTS_URL).mock(return_value=openai_ok(prompt=10, completion=10))
            g.chat(_req(), purpose="t")
            g.chat(_req("openweights/openai/gpt-oss-120b"), purpose="t")
        first, second = g.ledger.rows()
        assert first["price_list"] == "2026-10-03" and second["price_list"] == g.prices.name
        assert first["price_sha256"] != second["price_sha256"]
        assert first["costed"] == 1 and second["costed"] == 1
    finally:
        g.close()


def test_a_self_hosted_model_the_overlay_does_not_name_is_uncosted(
    repo_config: BoundaryConfig, tmp_path: Path, keys: None
) -> None:
    """Re-measured per model, quantisation and GPU means an unmeasured one has no rate. It
    is not costed at a neighbour's rate, and it is not costed at zero."""
    g = make_gateway(_config(repo_config, tmp_path), tmp_path)
    try:
        with respx.mock(assert_all_called=True) as mock:
            mock.post(VLLM_URL).mock(return_value=openai_ok(prompt=10, completion=10))
            resp = g.chat(_req("vllm-l4/Qwen2.5-7B-Instruct-FP8"), purpose="t")
        assert resp.ok and resp.costed is False and resp.cost_usd is None
        row = g.ledger.rows()[0]
        assert row["costed"] == 0 and row["price_list"] == "2026-10-03", (
            "the overlay was consulted and had no entry; the row says which list it asked"
        )
    finally:
        g.close()


def test_a_self_hosted_provider_with_no_overlay_configured_is_uncosted_with_no_list(
    repo_config: BoundaryConfig, tmp_path: Path, keys: None
) -> None:
    g = make_gateway(_with_vllm(repo_config), tmp_path)
    try:
        with respx.mock(assert_all_called=True) as mock:
            mock.post(VLLM_URL).mock(return_value=openai_ok(prompt=10, completion=10))
            resp = g.chat(_req(), purpose="t")
        assert resp.ok and resp.costed is False
        row = g.ledger.rows()[0]
        assert row["price_list"] is None and row["price_sha256"] is None
        assert g.ledger.uncosted_count() == 1
    finally:
        g.close()


def test_the_estimate_that_gates_the_caps_uses_the_overlay_too(
    repo_config: BoundaryConfig, tmp_path: Path, keys: None
) -> None:
    """The pre-call estimate is what a cap refuses on. With input at 0 it is max_tokens at the
    measured output rate, so a self-hosted run is gated by a real number rather than by
    nothing."""
    from boundary.config import CapsConfig, ProjectCap
    from boundary.errors import SpendCapExceeded

    tiny = CapsConfig(
        version=1,
        portfolio_monthly_usd=1000,
        projects={"ai-release-gate": ProjectCap(monthly_usd=1e-9, per_run_usd=None)},
    )
    g = make_gateway(_config(repo_config, tmp_path), tmp_path, caps=tiny)
    try:
        with respx.mock(assert_all_called=False) as mock:
            route = mock.post(VLLM_URL).mock(return_value=openai_ok())
            with pytest.raises(SpendCapExceeded) as ei:
                g.chat(_req(), purpose="t")
            assert route.call_count == 0
        assert ei.value.estimate_usd == pytest.approx(8 / 1e6 * 0.5259)
    finally:
        g.close()
