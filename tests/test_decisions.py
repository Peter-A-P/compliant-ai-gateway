"""Decision models through the gateway: Jev on OpenRouter, Kev on this machine (1.5).

Goldens are OpenRouter's documented request and response, copied from
https://openrouter.ai/blog/insights/what-is-jev/ on 2026-10-20, and Kev's error shape from
its README. No network: the live exercise is a separate step and is in the ledger when it
happens.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from boundary.config import BoundaryConfig
from boundary.errors import ProviderError
from boundary.providers.decisions import DecisionsAdapter
from boundary.routes import resolve
from boundary.types import ChatRequest, Mode

from .conftest import make_gateway

JEV = "openrouter/typesafe/jev-1.14"
JEV_URL = "https://openrouter.ai/api/alpha/decisions"
KEV = "kev/kev-latest"
KEV_URL = "http://127.0.0.1:8009/v1/systemone"

STATE = "My invoice for September shows two charges for the Pro plan. I only have one workspace."
QUESTIONS = {
    "team": {
        "type": "choice",
        "instructions": "Which team should handle this ticket?",
        "criteria": {
            "billing": "Charges, invoices, and refunds",
            "technical": "Bugs, outages, and broken features",
            "account": "Login, password, and profile changes",
        },
    }
}


def _request(model: str = JEV, **kw: object) -> ChatRequest:
    base: dict[str, object] = {
        "model": model,
        "messages": [{"role": "user", "content": STATE}],
        "extra": {"questions": QUESTIONS},
    }
    base.update(kw)
    return ChatRequest(**base)  # type: ignore[arg-type]


def _jev_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": "typesafe/jev-1.14-20261014",
            "answers": {
                "team": {
                    "type": "choice",
                    "choice": "billing",
                    "probabilities": {"billing": 0.88, "technical": 0.11, "account": 0.01},
                    "confidence": 0.82,
                }
            },
            "usage": {"input_tokens": 357, "output_tokens": 38, "cost": 1.4994e-05},
            "id": "gen-dec-1792170400-UBIaakN5nEoW6gLSGZGD",
            "provider": "TypeSafe",
        },
    )


def test_the_jev_golden_request(repo_config: BoundaryConfig) -> None:
    ref = resolve(JEV, repo_config, Mode.STANDARD)
    assert (ref.provider, ref.model) == ("openrouter", "typesafe/jev-1.14")
    built = DecisionsAdapter().build_request(ref, _request(max_tokens=1024), ref.provider_config, "k")
    assert built.method == "POST" and built.url == JEV_URL
    assert built.headers["authorization"] == "Bearer k"
    body = json.loads(built.body)
    # max_tokens is accepted and not sent: the interface has no such field.
    assert body == {"model": "typesafe/jev-1.14", "state": STATE, "questions": QUESTIONS}
    again = DecisionsAdapter().build_request(ref, _request(max_tokens=1024), ref.provider_config, "k")
    assert again.body == built.body


def test_kev_is_the_same_body_at_its_own_path_with_no_key(repo_config: BoundaryConfig) -> None:
    ref = resolve(KEV, repo_config, Mode.STANDARD)
    built = DecisionsAdapter().build_request(ref, _request(KEV), ref.provider_config, None)
    assert built.url == KEV_URL
    assert "authorization" not in {k.lower() for k in built.headers}
    assert json.loads(built.body)["model"] == "kev-latest"
    assert ref.provider_config.price_zero is True


@pytest.mark.parametrize(
    ("change", "says"),
    [
        ({"system": "Be careful."}, "system prompt"),
        ({"temperature": 0.0}, "temperature"),
        ({"messages": [{"role": "user", "content": "a"}, {"role": "user", "content": "b"}]}, "one user message"),
        ({"extra": {}}, "questions"),
    ],
)
def test_what_the_interface_cannot_carry_is_refused_not_dropped(
    repo_config: BoundaryConfig, change: dict[str, object], says: str
) -> None:
    ref = resolve(JEV, repo_config, Mode.STANDARD)
    with pytest.raises(ValueError, match=says):
        DecisionsAdapter().build_request(ref, _request(**change), ref.provider_config, "k")


def test_the_answers_come_back_as_json_text_and_usage() -> None:
    parsed = DecisionsAdapter().parse_response(200, {}, _jev_response().content)
    answers = json.loads(parsed.text or "")
    assert answers["team"]["choice"] == "billing"
    assert answers["team"]["probabilities"]["billing"] == 0.88
    assert parsed.model_returned == "typesafe/jev-1.14-20261014"
    assert (parsed.usage.input_tokens, parsed.usage.output_tokens) == (357, 38)


def test_a_success_without_answers_is_an_error_not_an_empty_decision() -> None:
    with pytest.raises(ProviderError):
        DecisionsAdapter().parse_response(200, {}, b'{"model": "x", "usage": {}}')


def test_both_error_shapes_say_what_was_wrong() -> None:
    openrouter = DecisionsAdapter().parse_error(
        "openrouter", 400, {}, b'{"error": {"code": 400, "message": "criteria must not be empty"}}'
    )
    assert "criteria must not be empty" in str(openrouter)
    kev = DecisionsAdapter().parse_error(
        "kev", 422, {}, b'{"detail": [{"loc": ["body", "state"], "msg": "too long", "type": "value_error"}]}'
    )
    assert "body.state: too long" in str(kev)


def test_a_jev_call_is_costed_from_the_price_file_and_not_from_its_cost_field(
    repo_config: BoundaryConfig, tmp_path: Path, keys: None
) -> None:
    gw = make_gateway(repo_config, tmp_path)
    try:
        with respx.mock(assert_all_called=True) as mock:
            mock.post(JEV_URL).mock(return_value=_jev_response())
            resp = gw.chat(_request(), purpose="decide", run_id="jev-1", data_class="personal")
        assert resp.ok and json.loads(resp.text or "")["team"]["choice"] == "billing"
        # 357 input tokens at US$0.042 a million, output free.
        assert resp.cost_usd == pytest.approx(357 * 0.042 / 1_000_000)
        row = gw.ledger.rows()[0]
        assert row["provider"] == "openrouter" and row["costed"] == 1
        assert row["data_class"] == "personal"
        assert gw.ledger.uncosted_count() == 0
    finally:
        gw.close()
