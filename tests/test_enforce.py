"""The data policy: every rule fails closed, a refusal sends nothing and leaves a row.

PLAN.md B2.2. The adversarial suite (boundary/enforce_eval.py) drives every provider entry,
alias, class and entry point in the checked-in configuration; these tests pin the rules it
rests on one at a time, so a change to any of them fails here with its name on it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import respx

from boundary.cli import SMOKE_MODELS
from boundary.config import BoundaryConfig, CacheConfig, ProviderConfig, Residency
from boundary.enforce import ClassRule, DataPolicy, decide, load_policy
from boundary.enforce_eval import oracle, run
from boundary.errors import PolicyRefused
from boundary.gateway import POLICY_REFUSED
from boundary.types import ChatRequest, DataClass

from .conftest import ANTHROPIC_URL, CONFIG_DIR, HAIKU, anthropic_ok, make_gateway

POLICY = CONFIG_DIR / "policy.yaml"


def _pc(residency: Residency | None, region: str | None = None) -> ProviderConfig:
    return ProviderConfig(
        kind="openai_compat",
        base_url="https://example.invalid/v1",
        residency=residency,
        region=region,
    )


def _policy(**classes: ClassRule) -> DataPolicy:
    return DataPolicy(classes={DataClass(k): v for k, v in classes.items()})


def _req(model: str = HAIKU) -> ChatRequest:
    return ChatRequest(model=model, messages=[{"role": "user", "content": "Q"}], max_tokens=8)


# -- the rules ---------------------------------------------------------------------------


def test_a_call_that_declared_nothing_is_judged_as_personal() -> None:
    policy = _policy(public=ClassRule(), personal=ClassRule(max_residency=Residency.SINGLE_REGION))
    d = decide(policy, None, provider="p", provider_config=_pc(Residency.GLOBAL), region="x")
    assert not d.allowed and d.data_class == "personal"


def test_a_class_the_policy_does_not_list_is_refused() -> None:
    d = decide(_policy(public=ClassRule()), "internal", provider="p",
               provider_config=_pc(Residency.GLOBAL), region=None)  # fmt: skip
    assert not d.allowed and "not in the data policy" in d.reason


def test_an_undeclared_residency_fails_any_limit_even_global() -> None:
    policy = _policy(internal=ClassRule(max_residency=Residency.GLOBAL))
    d = decide(policy, "internal", provider="p", provider_config=_pc(None), region=None)
    assert not d.allowed and "declares none" in d.reason


def test_a_wider_residency_is_refused_and_a_narrower_one_allowed() -> None:
    policy = _policy(personal=ClassRule(max_residency=Residency.GEO))
    wide = decide(policy, "personal", provider="p", provider_config=_pc(Residency.GLOBAL),
                  region=None)  # fmt: skip
    narrow = decide(policy, "personal", provider="p",
                    provider_config=_pc(Residency.SINGLE_REGION), region=None)  # fmt: skip
    assert not wide.allowed and narrow.allowed


def test_a_region_limit_refuses_an_unset_region_and_ignores_case() -> None:
    policy = _policy(personal=ClassRule(regions=frozenset({"ca-central-1"})))
    pc = _pc(Residency.SINGLE_REGION)
    assert not decide(policy, "personal", provider="p", provider_config=pc, region=None).allowed
    assert decide(
        policy, "personal", provider="p", provider_config=pc, region="CA-Central-1"
    ).allowed


def test_a_provider_list_is_enforced() -> None:
    policy = _policy(public=ClassRule(providers=frozenset({"local"})))
    pc = _pc(None)
    assert decide(policy, "public", provider="local", provider_config=pc, region=None).allowed
    assert not decide(policy, "public", provider="openai", provider_config=pc, region=None).allowed


def test_the_checked_in_policy_leaves_personal_data_only_the_local_model(
    repo_config: BoundaryConfig,
) -> None:
    """The Canadian worked example: no hosted vendor here can declare single-region
    processing in Canada, so the compliant set for personal data is the local server."""
    policy = load_policy(POLICY)
    allowed = sorted(
        name
        for name, pc in repo_config.providers.items()
        if decide(policy, "personal", provider=name, provider_config=pc, region=pc.region).allowed
    )
    assert allowed == ["local"]


def test_the_oracle_agrees_with_decide_on_the_checked_in_policy(
    repo_config: BoundaryConfig,
) -> None:
    import yaml

    raw = yaml.safe_load(POLICY.read_text(encoding="utf-8"))
    policy = load_policy(POLICY)
    for name, pc in repo_config.providers.items():
        residency = pc.residency.value if pc.residency else None
        for cls in (None, *(c.value for c in DataClass)):
            d = decide(policy, cls, provider=name, provider_config=pc, region=pc.region)
            assert d.allowed == oracle(raw, cls, residency, pc.region, name), (name, cls)


# -- the gateway -------------------------------------------------------------------------


def test_a_refusal_sends_nothing_reads_no_key_and_writes_one_row(
    repo_config: BoundaryConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    gw = make_gateway(repo_config, tmp_path, policy=load_policy(POLICY))
    try:
        with respx.mock(assert_all_called=False) as mock:
            route = mock.post(ANTHROPIC_URL)
            with pytest.raises(PolicyRefused) as info:
                gw.chat(_req(), purpose="dev", data_class="personal")
            assert not route.called
        rows = gw.ledger.rows()
    finally:
        gw.close()
    assert len(rows) == 1 and rows[0]["error_type"] == POLICY_REFUSED
    assert rows[0]["id"] == info.value.ledger_id
    assert rows[0]["cost_usd"] == 0.0 and rows[0]["costed"] == 1
    assert rows[0]["data_class"] == "personal"
    assert "single-region" in info.value.reason


def test_an_undeclared_call_is_refused_and_its_row_still_says_undeclared(
    repo_config: BoundaryConfig, tmp_path: Path, keys: None
) -> None:
    """The row records what the caller said; the error records what the boundary decided."""
    gw = make_gateway(repo_config, tmp_path, policy=load_policy(POLICY))
    try:
        with pytest.raises(PolicyRefused) as info:
            gw.chat(_req(), purpose="dev")
        row = gw.ledger.rows()[0]
    finally:
        gw.close()
    assert row["data_class"] is None and info.value.data_class == "personal"


def test_an_allowed_class_goes_through(
    repo_config: BoundaryConfig, tmp_path: Path, keys: None
) -> None:
    gw = make_gateway(repo_config, tmp_path, policy=load_policy(POLICY))
    try:
        with respx.mock(assert_all_called=True) as mock:
            mock.post(ANTHROPIC_URL).mock(return_value=anthropic_ok())
            resp = gw.chat(_req(), purpose="dev", data_class="public")
    finally:
        gw.close()
    assert resp.ok


def test_a_refused_batch_writes_a_row_for_every_request_and_submits_nothing(
    repo_config: BoundaryConfig, tmp_path: Path, keys: None
) -> None:
    gw = make_gateway(repo_config, tmp_path, policy=load_policy(POLICY))
    try:
        with respx.mock(assert_all_called=False) as mock:
            route = mock.route()
            with pytest.raises(PolicyRefused):
                gw.batch_submit([_req(), _req(), _req()], purpose="dev", data_class="sensitive")
            assert not route.called
        rows = gw.ledger.rows()
    finally:
        gw.close()
    assert [r["error_type"] for r in rows] == [POLICY_REFUSED] * 3


def test_raw_is_held_to_the_same_policy(
    repo_config: BoundaryConfig, tmp_path: Path, keys: None
) -> None:
    gw = make_gateway(repo_config, tmp_path, policy=load_policy(POLICY))
    try:
        with pytest.raises(PolicyRefused):
            gw.raw("anthropic", "POST", "v1/messages", {}, purpose="dev", data_class="personal")
    finally:
        gw.close()


def test_a_class_the_policy_keeps_out_of_the_cache_is_never_cached(
    repo_config: BoundaryConfig, tmp_path: Path, keys: None
) -> None:
    cfg = repo_config.model_copy(
        update={"cache": CacheConfig(enabled=True, path=tmp_path / "cache")}
    )
    policy = _policy(
        public=ClassRule(cache=True),
        internal=ClassRule(cache=False),
    )
    gw = make_gateway(cfg, tmp_path, policy=policy)
    try:
        with respx.mock(assert_all_called=True) as mock:
            route = mock.post(ANTHROPIC_URL).mock(return_value=anthropic_ok())
            for _ in range(2):
                gw.chat(_req(), purpose="dev", data_class="internal")
            assert route.call_count == 2
            for _ in range(2):
                gw.chat(_req(), purpose="dev", data_class="public")
            assert route.call_count == 3
    finally:
        gw.close()


def test_no_policy_means_no_change(repo_config: BoundaryConfig, tmp_path: Path, keys: None) -> None:
    assert repo_config.policy is None
    gw = make_gateway(repo_config, tmp_path)
    try:
        with respx.mock(assert_all_called=True) as mock:
            mock.post(ANTHROPIC_URL).mock(return_value=anthropic_ok())
            assert gw.chat(_req(), purpose="dev", data_class="personal").ok
    finally:
        gw.close()


# -- the adversarial suite ---------------------------------------------------------------


def test_the_adversarial_suite_finds_no_violation_and_no_false_refusal(
    repo_config: BoundaryConfig,
) -> None:
    r = run(repo_config, POLICY, models=SMOKE_MODELS)
    assert r.violations.hits == 0 and r.violations.total > 500
    assert r.false_refusals.hits == 0
    assert r.audited.hits == r.audited.total > 0
