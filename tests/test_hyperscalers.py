"""Goldens for the two hyperscaler adapters: Microsoft Foundry and Google Vertex.

Both serve Claude with the Anthropic Messages body, so the risk they carry is not in
parsing, which is inherited and already covered, but in the envelope: the URL, the
authentication header, and which of `model` and `anthropic_version` belongs in the body
against which belongs outside it. Getting either of those wrong fails at the vendor with an
error that does not say so.

The request goldens below are taken from the vendors' own documentation, read 2026-09-14:
    https://platform.claude.com/docs/en/build-with-claude/claude-in-microsoft-foundry
    https://platform.claude.com/docs/en/api/claude-on-vertex-ai

No network and no credentials. The live call each adapter still owes is a separate step and
is recorded in the ledger when it happens.
"""

from __future__ import annotations

import json

import pytest

from boundary.config import ProviderConfig, ProviderKind
from boundary.errors import ConfigError
from boundary.providers import ADAPTERS, BATCH_ADAPTERS, AzureFoundryAdapter, VertexAdapter
from boundary.providers.vertex import VERTEX_ANTHROPIC_VERSION, endpoint_host
from boundary.routes import ModelRef
from boundary.types import ChatRequest

FOUNDRY_RESOURCE_URL = "https://example-resource.services.ai.azure.com/anthropic"
VERTEX_PROJECT = "pap-portfolio"


def _request(model: str = "claude-opus-5") -> ChatRequest:
    return ChatRequest(
        model=model,
        messages=[{"role": "user", "content": "Hello!"}],
        max_tokens=1024,
    )


def _foundry_provider(**kw: object) -> ProviderConfig:
    return ProviderConfig(
        kind=ProviderKind.AZURE_FOUNDRY,
        base_url=FOUNDRY_RESOURCE_URL,
        api_key_env="AZURE_FOUNDRY_API_KEY",
        **kw,
    )


def _vertex_provider(**kw: object) -> ProviderConfig:
    defaults: dict[str, object] = {
        "kind": ProviderKind.GCP_VERTEX,
        "base_url": "https://northamerica-northeast1-aiplatform.googleapis.com",
        "project": VERTEX_PROJECT,
        "region": "northamerica-northeast1",
    }
    defaults.update(kw)
    return ProviderConfig(**defaults)


# -- Foundry ----------------------------------------------------------------------------


def test_foundry_request_matches_the_documented_shape() -> None:
    """Body identical to the Claude API's, including `model`; Azure key header."""
    provider = _foundry_provider()
    ref = ModelRef(provider="foundry", model="claude-opus-5", provider_config=provider)
    built = AzureFoundryAdapter().build_request(ref, _request(), provider, "azure-key-123")

    assert built.method == "POST"
    assert built.url == f"{FOUNDRY_RESOURCE_URL}/v1/messages"
    # The Azure-issued key goes in `api-key`, not `x-api-key`.
    assert built.headers["api-key"] == "azure-key-123"
    assert "x-api-key" not in built.headers
    assert built.headers["anthropic-version"] == "2023-06-01"
    assert built.headers["content-type"] == "application/json"

    body = json.loads(built.body)
    assert body == {
        "model": "claude-opus-5",
        "messages": [{"role": "user", "content": "Hello!"}],
        "max_tokens": 1024,
    }
    # There is no api-version query parameter: that is an Azure OpenAI convention.
    assert "api-version" not in built.url


def test_foundry_body_is_byte_identical_to_the_anthropic_body() -> None:
    """The same prompt to Foundry and to the Claude API builds the same bytes.

    If these ever diverge, the same call costs two different request hashes in the ledger
    and the two platforms stop being comparable.
    """
    from boundary.providers.anthropic import AnthropicAdapter

    req = _request()
    foundry = _foundry_provider()
    direct = ProviderConfig(kind=ProviderKind.ANTHROPIC, base_url="https://api.anthropic.com")
    model = "claude-opus-5"
    a = AnthropicAdapter().build_request(
        ModelRef(provider="anthropic", model=model, provider_config=direct), req, direct, "k"
    )
    f = AzureFoundryAdapter().build_request(
        ModelRef(provider="foundry", model=model, provider_config=foundry), req, foundry, "k"
    )
    assert a.body == f.body


def test_foundry_redacts_its_key() -> None:
    provider = _foundry_provider()
    ref = ModelRef(provider="foundry", model="claude-opus-5", provider_config=provider)
    built = AzureFoundryAdapter().build_request(ref, _request(), provider, "azure-key-123")
    assert built.redacted_headers()["api-key"] == "<redacted>"
    assert "azure-key-123" not in json.dumps(built.redacted_headers())


def test_foundry_parses_the_claude_response_and_its_usage() -> None:
    """Foundry returns the standard Messages response, usage object included."""
    body = json.dumps(
        {
            "id": "msg_01Foundry",
            "type": "message",
            "role": "assistant",
            "model": "claude-opus-5",
            "content": [{"type": "text", "text": "Hello there."}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 11, "output_tokens": 4},
        }
    ).encode()
    parsed = AzureFoundryAdapter().parse_response(200, {}, body)
    assert parsed.text == "Hello there."
    assert parsed.model_returned == "claude-opus-5"
    assert parsed.usage.input_tokens == 11
    assert parsed.usage.output_tokens == 4
    assert parsed.finish_reason == "end_turn"


def test_foundry_error_carries_the_vendor_message() -> None:
    body = json.dumps(
        {"type": "error", "error": {"type": "not_found_error", "message": "Deployment not found"}}
    ).encode()
    err = AzureFoundryAdapter().parse_error("foundry", 404, {}, body)
    assert err.status == 404
    assert "Deployment not found" in str(err)


# -- Vertex -----------------------------------------------------------------------------


def test_vertex_moves_the_model_out_of_the_body_and_the_version_into_it() -> None:
    """The two differences from the Claude API, both of them at once."""
    provider = _vertex_provider()
    ref = ModelRef(provider="vertex", model="claude-sonnet-4-6", provider_config=provider)
    built = VertexAdapter().build_request(ref, _request(), provider, "ya29.token")

    assert built.method == "POST"
    assert built.url == (
        "https://northamerica-northeast1-aiplatform.googleapis.com"
        f"/v1/projects/{VERTEX_PROJECT}/locations/northamerica-northeast1"
        "/publishers/anthropic/models/claude-sonnet-4-6:rawPredict"
    )
    assert built.headers["authorization"] == "Bearer ya29.token"

    body = json.loads(built.body)
    assert "model" not in body, "Vertex takes the model in the URL, never in the body"
    assert body["anthropic_version"] == VERTEX_ANTHROPIC_VERSION == "vertex-2023-10-16"
    assert body["messages"] == [{"role": "user", "content": "Hello!"}]
    assert body["max_tokens"] == 1024
    # The pinned header the direct API uses must not leak onto this request.
    assert "anthropic-version" not in built.headers


@pytest.mark.parametrize(
    ("region", "host"),
    [
        ("global", "https://aiplatform.googleapis.com"),
        ("us", "https://aiplatform.us.rep.googleapis.com"),
        ("eu", "https://aiplatform.eu.rep.googleapis.com"),
        ("northamerica-northeast1", "https://northamerica-northeast1-aiplatform.googleapis.com"),
        ("us-east5", "https://us-east5-aiplatform.googleapis.com"),
    ],
)
def test_vertex_endpoint_host_per_region_type(region: str, host: str) -> None:
    """Global, multi-region and regional endpoints have three different host shapes.

    They are not interchangeable, and which one is used is a residency decision.
    """
    assert endpoint_host(region) == host


def test_vertex_url_uses_the_route_region_when_the_host_agrees() -> None:
    """A route may pin a region, which is how one provider entry serves two residencies."""
    provider = _vertex_provider(base_url="https://aiplatform.googleapis.com", region="global")
    ref = ModelRef(
        provider="vertex",
        model="claude-opus-5",
        provider_config=provider,
        region="global",
    )
    built = VertexAdapter().build_request(ref, _request(), provider, "t")
    assert built.url.startswith("https://aiplatform.googleapis.com/v1/projects/")
    assert "/locations/global/" in built.url


def test_vertex_refuses_a_host_and_a_region_that_disagree() -> None:
    """The residency guard: a Montreal host with a global route is refused, not sent.

    The host decides where the request actually lands, so a mismatch would quietly move
    data to another geography while the ledger row recorded the region the route asked for.
    """
    provider = _vertex_provider()  # base_url is the northamerica-northeast1 host
    ref = ModelRef(
        provider="vertex",
        model="claude-opus-5",
        provider_config=provider,
        region="global",
    )
    with pytest.raises(ConfigError, match="must name the same place"):
        VertexAdapter().build_request(ref, _request(), provider, "t")


def test_vertex_refuses_without_a_project() -> None:
    provider = _vertex_provider(project=None)
    ref = ModelRef(provider="vertex", model="claude-opus-5", provider_config=provider)
    with pytest.raises(ConfigError, match="project"):
        VertexAdapter().build_request(ref, _request(), provider, "t")


def test_vertex_refuses_without_a_region() -> None:
    """A missing region is refused rather than defaulted.

    Defaulting to `global` would silently send a request somewhere the caller did not
    choose, which is the one thing a residency feature cannot do.
    """
    provider = _vertex_provider(region=None)
    ref = ModelRef(provider="vertex", model="claude-opus-5", provider_config=provider)
    with pytest.raises(ConfigError, match="region"):
        VertexAdapter().build_request(ref, _request(), provider, "t")


def test_vertex_redacts_its_bearer_token() -> None:
    provider = _vertex_provider()
    ref = ModelRef(provider="vertex", model="claude-opus-5", provider_config=provider)
    built = VertexAdapter().build_request(ref, _request(), provider, "ya29.secret")
    assert "ya29.secret" not in json.dumps(built.redacted_headers())


def test_vertex_parses_the_claude_response() -> None:
    body = json.dumps(
        {
            "id": "msg_vertex_01",
            "type": "message",
            "role": "assistant",
            "model": "claude-sonnet-4-6",
            "content": [{"type": "text", "text": "Bonjour."}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 9, "output_tokens": 3},
        }
    ).encode()
    parsed = VertexAdapter().parse_response(200, {}, body)
    assert parsed.text == "Bonjour."
    assert parsed.usage.input_tokens == 9


# -- registration -----------------------------------------------------------------------


def test_both_kinds_resolve_to_their_adapter() -> None:
    assert isinstance(ADAPTERS[ProviderKind.AZURE_FOUNDRY], AzureFoundryAdapter)
    assert isinstance(ADAPTERS[ProviderKind.GCP_VERTEX], VertexAdapter)


def test_neither_platform_offers_batches() -> None:
    """Message Batches are on both platforms' unsupported lists.

    Absence from BATCH_ADAPTERS is what makes a batch submitted to them fail by name,
    before anything is sent, rather than fail at the vendor.
    """
    assert ProviderKind.AZURE_FOUNDRY not in BATCH_ADAPTERS
    assert ProviderKind.GCP_VERTEX not in BATCH_ADAPTERS
