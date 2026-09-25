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


# -- redacted_as (0.15) ------------------------------------------------------------------


def test_redacted_personal_data_is_judged_as_internal_and_raw_is_not(
    repo_config: BoundaryConfig,
) -> None:
    """The Canadian policy, decided 2026-09-25: redacted personal data goes where internal
    data may. Raw, it still reaches only the local model; redacted, every provider entry that
    declares a residency, which is local, the Canadian Foundry, Bedrock and Vertex. The
    direct Anthropic, OpenAI, Google and Together entries declare none and stay refused."""
    policy = load_policy(POLICY)

    def allowed(redacted: bool) -> list[str]:
        return sorted(
            name
            for name, pc in repo_config.providers.items()
            if decide(
                policy,
                "personal",
                provider=name,
                provider_config=pc,
                region=pc.region,
                redacted=redacted,
            ).allowed
        )

    assert allowed(False) == ["local"]
    assert allowed(True) == ["bedrock", "foundry-canada", "local", "vertex"]


def test_a_redacted_decision_says_what_it_was_judged_as() -> None:
    policy = _policy(
        personal=ClassRule(max_residency=Residency.SINGLE_REGION, redacted_as=DataClass.INTERNAL),
        internal=ClassRule(max_residency=Residency.GLOBAL, cache=True),
    )
    d = decide(
        policy,
        "personal",
        provider="p",
        provider_config=_pc(Residency.GLOBAL),
        region=None,
        redacted=True,
    )
    assert d.allowed and d.judged_as == "internal" and d.data_class == "personal"
    assert "judged as internal" in d.reason
    assert d.cache is False, "the cache needs both rules to allow it; personal's does not"


def test_redaction_unlocks_nothing_without_a_redacted_as() -> None:
    policy = _policy(sensitive=ClassRule(regions=frozenset({"localhost"})))
    d = decide(
        policy,
        "sensitive",
        provider="p",
        provider_config=_pc(None, "us-east-1"),
        region="us-east-1",
        redacted=True,
    )
    assert not d.allowed and d.judged_as is None


def test_redacted_as_must_name_a_listed_class_with_no_redacted_as_of_its_own() -> None:
    with pytest.raises(ValueError, match="does not list"):
        _policy(personal=ClassRule(redacted_as=DataClass.INTERNAL))
    with pytest.raises(ValueError, match="one step only"):
        _policy(
            personal=ClassRule(redacted_as=DataClass.INTERNAL),
            internal=ClassRule(redacted_as=DataClass.PUBLIC),
            public=ClassRule(),
        )


def test_the_gateway_writes_redacted_on_the_row_and_routes_by_it(
    repo_config: BoundaryConfig, tmp_path: Path, keys: None
) -> None:
    from boundary.errors import PolicyRefused as Refused

    gw = make_gateway(repo_config, tmp_path, policy=load_policy(POLICY))
    try:
        with respx.mock(assert_all_called=False) as mock:
            route = mock.post(ANTHROPIC_URL).mock(return_value=anthropic_ok())
            # Direct Anthropic declares no residency: refused raw and redacted alike.
            with pytest.raises(Refused, match="judged as internal"):
                gw.chat(_req(), purpose="t", data_class="personal", redacted=True)
            assert route.call_count == 0
        rows = gw.ledger.rows()
        assert rows[-1]["redacted"] == 1 and rows[-1]["data_class"] == "personal"
        with respx.mock(assert_all_called=False) as mock:
            mock.post(ANTHROPIC_URL).mock(return_value=anthropic_ok())
            resp = gw.chat(_req(), purpose="t", data_class="public")
        assert resp.redacted is False and gw.ledger.rows()[-1]["redacted"] is None
    finally:
        gw.close()


def test_record_refusal_writes_a_row_and_accepts_only_caller_refusals(
    repo_config: BoundaryConfig, tmp_path: Path, keys: None
) -> None:
    gw = make_gateway(repo_config, tmp_path)
    try:
        with respx.mock(assert_all_called=False) as mock:
            route = mock.post(ANTHROPIC_URL).mock(return_value=anthropic_ok())
            row_id = gw.record_refusal(
                _req(), purpose="t", error_type="redaction_refused", data_class="personal"
            )
            assert route.call_count == 0
        (row,) = gw.ledger.rows()
        assert row["id"] == row_id and row["error_type"] == "redaction_refused"
        assert row["request_sha256"] is None and row["costed"] == 1 and row["cost_usd"] == 0.0
        with pytest.raises(ValueError, match="not a caller refusal"):
            gw.record_refusal(_req(), purpose="t", error_type="policy_refused")
        with pytest.raises(ValueError, match="data_class"):
            gw.record_refusal(
                _req(), purpose="t", error_type="redaction_refused", data_class="secret"
            )
    finally:
        gw.close()


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
