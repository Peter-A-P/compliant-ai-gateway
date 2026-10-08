"""Prices by the length of the prompt (1.2.0, Claude Haiku 5.5), and a route's fixed vendor
fields, which is how the demo key's model is asked for low effort."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import respx

from boundary.config import BoundaryConfig, LongPromptRates, PriceEntry, Route, load_price_list
from boundary.ledger.prices import cost_usd, estimate_usd
from boundary.routes import resolve, with_route_extra
from boundary.types import ChatRequest, Mode, Usage

from .conftest import ANTHROPIC_URL, anthropic_ok, make_gateway

PRICES = Path(__file__).resolve().parents[1] / "boundary" / "prices"
HAIKU_5_5 = PriceEntry(
    input=0.10,
    output=0.50,
    cache_read=0.01,
    cache_write=0.125,
    batch_multiplier=0.5,
    long_prompt=LongPromptRates(
        above_tokens=100_000, input=0.50, output=2.50, cache_read=0.05, cache_write=0.625
    ),
)


def test_a_prompt_over_the_threshold_costs_the_higher_rate_on_every_token() -> None:
    short = Usage(input_tokens=100_000, output_tokens=1_000)
    assert cost_usd(short, HAIKU_5_5) == pytest.approx((100_000 * 0.10 + 1_000 * 0.50) / 1e6)
    long = Usage(input_tokens=100_001, output_tokens=1_000)
    assert cost_usd(long, HAIKU_5_5) == pytest.approx((100_001 * 0.50 + 1_000 * 2.50) / 1e6)
    assert cost_usd(long, HAIKU_5_5, batch=True) == pytest.approx(
        (100_001 * 0.50 + 1_000 * 2.50) / 1e6 * 0.5
    )


def test_cached_tokens_count_toward_the_prompt() -> None:
    """60,000 fresh and 50,000 read from the cache is a prompt of 110,000: the higher rate,
    never the lower one for a call the vendor may bill at the higher."""
    u = Usage(input_tokens=60_000, output_tokens=10, cache_read_tokens=50_000)
    assert cost_usd(u, HAIKU_5_5) == pytest.approx(
        (60_000 * 0.50 + 50_000 * 0.05 + 10 * 2.50) / 1e6
    )


def test_the_estimate_takes_the_higher_rate_well_before_the_threshold() -> None:
    """The estimate's 3.5 characters a token undercounts the newer tokenizer, so it prices a
    prompt at the long rates from half the threshold."""
    chars = int(60_000 * 3.5)  # an estimated 60,000 tokens
    assert estimate_usd(chars, 100, HAIKU_5_5) == pytest.approx((60_000 * 0.50 + 100 * 2.50) / 1e6)
    small = int(40_000 * 3.5)
    assert estimate_usd(small, 100, HAIKU_5_5) == pytest.approx((40_000 * 0.10 + 100 * 0.50) / 1e6)


def test_an_entry_without_the_tier_is_priced_as_before() -> None:
    flat = PriceEntry(input=1.0, output=5.0)
    assert cost_usd(Usage(input_tokens=500_000, output_tokens=0), flat) == pytest.approx(0.5)


def test_older_price_lists_keep_the_fingerprint_their_rows_carry() -> None:
    """long_prompt is left out of the fingerprint when it is not set, so adding the field
    moved no earlier list's rates_sha256."""
    assert load_price_list(PRICES / "2026-10-01.yaml").rates_sha256 == (
        "ebba73fee3a23a3e573930c2b16a8704ca6dd01b3a8a3dd5c2a78bf563017a35"
    )


def test_the_2026_10_08_list_only_adds_haiku_5_5() -> None:
    before = load_price_list(PRICES / "2026-10-01.yaml").per_million_tokens
    after = load_price_list(PRICES / "2026-10-08.yaml").per_million_tokens
    for provider, models in before.items():
        for model, entry in models.items():
            assert after[provider][model] == entry, f"{provider}/{model} was repriced"
    added = {f"{p}/{m}" for p, ms in after.items() for m in ms if m not in before.get(p, {})}
    assert added == {"anthropic/claude-haiku-5-5"}
    assert after["anthropic"]["claude-haiku-5-5"] == HAIKU_5_5


def test_a_routes_fields_go_under_the_callers_and_never_on_an_explicit_model(
    repo_config: BoundaryConfig,
) -> None:
    demo = resolve("demo", repo_config, Mode.STANDARD)
    assert demo.model == "claude-haiku-5-5"
    assert dict(demo.extra) == {"output_config": {"effort": "low"}}
    req = ChatRequest(model="demo", messages=[{"role": "user", "content": "hi"}])
    assert dict(with_route_extra(req, demo).extra) == {"output_config": {"effort": "low"}}
    own = ChatRequest(
        model="demo",
        messages=[{"role": "user", "content": "hi"}],
        extra={"output_config": {"effort": "high"}},
    )
    assert dict(with_route_extra(own, demo).extra) == {"output_config": {"effort": "high"}}
    explicit = resolve("anthropic/claude-haiku-5-5", repo_config, Mode.PASSTHROUGH)
    assert not explicit.extra and with_route_extra(req, explicit) is req
    assert Route(provider="anthropic", model="m").extra == {}


def test_the_alias_sends_low_effort_and_is_costed_at_the_new_rate(
    repo_config: BoundaryConfig, tmp_path: Path, keys: None
) -> None:
    with respx.mock() as mock:
        route = mock.post(ANTHROPIC_URL).mock(
            return_value=anthropic_ok(input_tokens=20, output_tokens=40, model="claude-haiku-5-5")
        )
        with make_gateway(repo_config, tmp_path) as gw:
            resp = gw.chat(
                ChatRequest(
                    model="demo", messages=[{"role": "user", "content": "hi"}], max_tokens=300
                ),
                purpose="test",
            )
        sent = json.loads(route.calls.last.request.content)
    assert sent["model"] == "claude-haiku-5-5"
    assert sent["output_config"] == {"effort": "low"}
    assert resp.costed and resp.cost_usd == pytest.approx((20 * 0.10 + 40 * 0.50) / 1e6)
