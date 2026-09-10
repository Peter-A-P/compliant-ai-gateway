"""Spend caps refuse with zero upstream calls, and the estimate is never below the actual."""

from __future__ import annotations

from pathlib import Path

import pytest
import respx

from boundary.config import BoundaryConfig, CapsConfig, PriceEntry, ProjectCap
from boundary.errors import SpendCapExceeded
from boundary.ledger.prices import cost_usd, estimate_usd
from boundary.providers.anthropic import AnthropicAdapter
from boundary.routes import resolve
from boundary.types import ChatRequest, Mode, Usage

from .conftest import ANTHROPIC_URL, HAIKU, anthropic_ok, make_gateway


def _req(**kw: object) -> ChatRequest:
    base: dict[str, object] = {
        "model": HAIKU,
        "messages": [{"role": "user", "content": "Q?"}],
        "max_tokens": 8,
    }
    base.update(kw)
    return ChatRequest(**base)  # type: ignore[arg-type]


def _caps(
    *, monthly: float = 100.0, per_run: float | None = None, portfolio: float = 1000.0
) -> CapsConfig:
    return CapsConfig(
        version=1,
        portfolio_monthly_usd=portfolio,
        projects={"ai-release-gate": ProjectCap(monthly_usd=monthly, per_run_usd=per_run)},
    )


@pytest.mark.parametrize(
    ("caps", "scope"),
    [
        (_caps(monthly=1e-9), "project ai-release-gate monthly"),
        (_caps(per_run=1e-9), "run r1"),
        (_caps(portfolio=1e-9), "portfolio monthly"),
    ],
)
def test_cap_refuses_with_zero_upstream_calls(
    repo_config: BoundaryConfig, tmp_path: Path, keys: None, caps: CapsConfig, scope: str
) -> None:
    g = make_gateway(repo_config, tmp_path, caps=caps)
    try:
        with respx.mock(assert_all_called=False) as mock:
            route = mock.post(ANTHROPIC_URL).mock(return_value=anthropic_ok())
            with pytest.raises(SpendCapExceeded) as ei:
                g.chat(_req(), purpose="dev", run_id="r1")
            assert route.call_count == 0
        assert scope in ei.value.scope
        assert ei.value.estimate_usd > 0 and ei.value.spent_usd == 0
        assert g.ledger.count() == 0, "a refused call is not a call"
    finally:
        g.close()


def test_actual_replaces_estimate_and_the_cap_binds_on_the_running_total(
    repo_config: BoundaryConfig, tmp_path: Path, keys: None
) -> None:
    ref = resolve(HAIKU, repo_config, Mode.STANDARD)
    entry = PriceEntry(input=1.0, output=5.0)
    built = AnthropicAdapter().build_request(ref, _req(), ref.provider_config, "k")
    one_estimate = estimate_usd(len(built.body), 8, entry)
    # Cap that admits one call by estimate but not two.
    g = make_gateway(repo_config, tmp_path, caps=_caps(monthly=one_estimate * 1.5))
    try:
        with respx.mock(assert_all_called=True) as mock:
            route = mock.post(ANTHROPIC_URL).mock(
                return_value=anthropic_ok(input_tokens=10, output_tokens=2)
            )
            first = g.chat(_req(), purpose="dev")
            # The actual (tiny) replaced the estimate, so there is room for a second call.
            second = g.chat(_req(), purpose="dev")
            assert route.call_count == 2
        assert first.cost_usd is not None and first.cost_usd < one_estimate
        assert g.ledger.spend_usd(project="ai-release-gate") == pytest.approx(
            (first.cost_usd or 0) + (second.cost_usd or 0)
        )
    finally:
        g.close()


def test_estimate_is_never_below_actual_on_the_golden_corpus() -> None:
    """For any response whose tokens fit the request, the pessimistic estimate covers it.
    The corpus grows with the goldens; today it is the shapes the adapters are tested on."""
    entry = PriceEntry(input=1.0, output=5.0, cache_read=0.1, cache_write=1.25)
    corpus: list[tuple[int, int, Usage]] = [
        # (request body chars, max_tokens, usage the vendor returned)
        (200, 8, Usage(input_tokens=50, output_tokens=8)),
        (2000, 64, Usage(input_tokens=500, output_tokens=64)),
        (350, 16, Usage(input_tokens=90, output_tokens=16)),
    ]
    for chars, max_tokens, usage in corpus:
        actual = cost_usd(usage, entry)
        assert actual is not None
        assert estimate_usd(chars, max_tokens, entry) >= actual


def test_cost_arithmetic_needs_rates_for_the_features_used() -> None:
    plain = PriceEntry(input=1.0, output=5.0)
    assert cost_usd(Usage(input_tokens=1_000_000, output_tokens=1_000_000), plain) == 6.0
    assert cost_usd(Usage(input_tokens=1, cache_read_tokens=1), plain) is None
    assert cost_usd(Usage(input_tokens=1, cache_write_tokens=1), plain) is None
    assert cost_usd(Usage(input_tokens=1), plain, batch=True) is None
    full = PriceEntry(input=1.0, output=5.0, cache_read=0.1, cache_write=1.25, batch_multiplier=0.5)
    assert cost_usd(
        Usage(input_tokens=1_000_000, cache_read_tokens=1_000_000, cache_write_tokens=1_000_000),
        full,
    ) == pytest.approx(1.0 + 0.1 + 1.25)
    assert cost_usd(Usage(output_tokens=1_000_000), full, batch=True) == pytest.approx(2.5)
