"""Goldens for the Amazon Bedrock adapter, and for the residency guard that is most of it.

Bedrock serves Claude with the Anthropic Messages body, so parsing is inherited and already
covered. What is new here is that the *model identifier* decides where a request may be
processed, and the endpoint decides which identifiers are legal. Those two statements live
in two different configuration fields, nothing on the wire reconciles them, and the response
reports neither. So the tests that matter are the ones that prove a disagreement is refused.

The request goldens are taken from AWS's documentation, read 2026-09-15:
    https://docs.aws.amazon.com/bedrock/latest/userguide/inference-messages-api.html
    https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-anthropic-claude-haiku-4-5.html

The response and error goldens are not from the documentation. They are the bytes a live
`ca-central-1` call returned on 2026-09-15, which is why the response golden carries fields
the documentation's examples do not (`stop_details`, `cache_creation`, `service_tier`).

No network and no credentials.
"""

from __future__ import annotations

import json

import pytest

from boundary.config import ProviderConfig, ProviderKind, Residency
from boundary.errors import ConfigError
from boundary.providers import ADAPTERS, BATCH_ADAPTERS, BedrockAdapter
from boundary.providers.anthropic import AnthropicAdapter
from boundary.providers.base import BuiltRequest
from boundary.providers.bedrock import MESSAGES_PATH, residency_of, split_host
from boundary.routes import ModelRef
from boundary.types import ChatRequest

RUNTIME_CA = "https://bedrock-runtime.ca-central-1.amazonaws.com"
MANTLE_US = "https://bedrock-mantle.us-east-1.api.aws"

GEO_MODEL = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
GLOBAL_MODEL = "global.anthropic.claude-haiku-4-5-20251001-v1:0"
BARE_MODEL = "anthropic.claude-haiku-4-5"


def _request(model: str = GEO_MODEL) -> ChatRequest:
    return ChatRequest(
        model=model,
        messages=[{"role": "user", "content": "Hello!"}],
        max_tokens=1024,
    )


def _provider(**kw: object) -> ProviderConfig:
    defaults: dict[str, object] = {
        "kind": ProviderKind.AWS_BEDROCK,
        "base_url": RUNTIME_CA,
        "api_key_env": "AWS_BEARER_TOKEN_BEDROCK",
        "region": "ca-central-1",
    }
    defaults.update(kw)
    return ProviderConfig(**defaults)


def _build(
    model: str,
    provider: ProviderConfig,
    key: str = "bedrock-key",
    region: str | None = None,
) -> BuiltRequest:
    ref = ModelRef(provider="bedrock", model=model, provider_config=provider, region=region)
    return BedrockAdapter().build_request(ref, _request(model), provider, key)


# -- the envelope -----------------------------------------------------------------------


def test_runtime_request_matches_the_documented_shape() -> None:
    """Anthropic body, Anthropic header, one path segment deeper than the direct vendor."""
    built = _build(GEO_MODEL, _provider())

    assert built.method == "POST"
    assert built.url == f"{RUNTIME_CA}/anthropic/v1/messages"
    # The same header the direct vendor uses: a Bedrock API key, not a SigV4 signature.
    assert built.headers["x-api-key"] == "bedrock-key"
    assert "authorization" not in built.headers
    assert built.headers["anthropic-version"] == "2023-06-01"
    assert built.headers["content-type"] == "application/json"

    body = json.loads(built.body)
    assert body == {
        "model": GEO_MODEL,
        "messages": [{"role": "user", "content": "Hello!"}],
        "max_tokens": 1024,
    }


def test_mantle_request_uses_the_same_path_and_a_bare_model_id() -> None:
    """The single-region endpoint differs only in host and in which ids it accepts."""
    provider = _provider(base_url=MANTLE_US, region="us-east-1")
    built = _build(BARE_MODEL, provider)

    assert built.url == f"{MANTLE_US}/anthropic/v1/messages"
    assert json.loads(built.body)["model"] == BARE_MODEL


def test_body_is_byte_identical_to_the_anthropic_body() -> None:
    """The same prompt to Bedrock and to the Claude API builds the same bytes.

    If these diverge the same call gets two request hashes in the ledger and the platforms
    stop being comparable, which is the whole reason the parser is inherited.
    """
    direct = ProviderConfig(kind=ProviderKind.ANTHROPIC, base_url="https://api.anthropic.com")
    req = _request(GEO_MODEL)
    a = AnthropicAdapter().build_request(
        ModelRef(provider="anthropic", model=GEO_MODEL, provider_config=direct), req, direct, "k"
    )
    b = _build(GEO_MODEL, _provider(), key="k")
    assert a.body == b.body


def test_bedrock_redacts_its_key() -> None:
    built = _build(GEO_MODEL, _provider(), key="abcd.secret.token")
    assert built.redacted_headers()["x-api-key"] == "<redacted>"
    assert "abcd.secret.token" not in json.dumps(built.redacted_headers())


# -- parsing, against bytes a live call actually returned --------------------------------

LIVE_RESPONSE = {
    "model": "anthropic.claude-haiku-4-5-20251001-v1:0",
    "id": "msg_bdrk_zizjjtfh7lw3uhlgjfyu736fxgsi3jpdthfnw3hylqpbh37e44sa",
    "type": "message",
    "role": "assistant",
    "content": [{"type": "text", "text": "ok"}],
    "stop_reason": "end_turn",
    "stop_sequence": None,
    "stop_details": None,
    "usage": {
        "input_tokens": 13,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 0},
        "output_tokens": 4,
        "service_tier": "standard",
    },
}


def test_parses_the_live_response_and_its_usage() -> None:
    parsed = BedrockAdapter().parse_response(200, {}, json.dumps(LIVE_RESPONSE).encode())
    assert parsed.text == "ok"
    assert parsed.finish_reason == "end_turn"
    assert parsed.usage.input_tokens == 13
    assert parsed.usage.output_tokens == 4


def test_the_response_does_not_say_which_profile_served_it() -> None:
    """A finding, pinned as a test so a future vendor change is noticed rather than assumed.

    The request named `us.anthropic.claude-haiku-4-5-20251001-v1:0`. The response echoes the
    bare id with the routing prefix stripped, and names no region. So nothing on the wire
    records which of ca-central-1, us-east-1, us-east-2 or us-west-2 processed it, and a
    ledger row cannot be read as evidence of where the tokens went. That is why residency is
    configuration here rather than a parsed value.
    """
    parsed = BedrockAdapter().parse_response(200, {}, json.dumps(LIVE_RESPONSE).encode())
    assert parsed.model_returned == "anthropic.claude-haiku-4-5-20251001-v1:0"
    assert parsed.model_returned != GEO_MODEL
    assert residency_of(str(parsed.model_returned)) is Residency.SINGLE_REGION
    assert residency_of(GEO_MODEL) is Residency.GEO


def test_error_carries_the_vendor_message() -> None:
    """The live error envelope is Anthropic's, so parse_error is inherited unchanged."""
    body = json.dumps(
        {
            "type": "error",
            "error": {
                "type": "invalid_request_error",
                "message": (
                    "Invalid content-type in request header. Retry your request with one "
                    "of the following valid values for content-type: 'application/json'"
                ),
            },
        }
    ).encode()
    err = BedrockAdapter().parse_error("bedrock", 400, {}, body)
    assert err.status == 400
    assert "Invalid content-type" in str(err)


# -- reading the two halves of a residency decision --------------------------------------


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        (BARE_MODEL, Residency.SINGLE_REGION),
        ("anthropic.claude-haiku-4-5-20251001-v1:0", Residency.SINGLE_REGION),
        ("us.anthropic.claude-haiku-4-5-20251001-v1:0", Residency.GEO),
        ("eu.anthropic.claude-haiku-4-5-20251001-v1:0", Residency.GEO),
        ("au.anthropic.claude-haiku-4-5-20251001-v1:0", Residency.GEO),
        ("jp.anthropic.claude-haiku-4-5-20251001-v1:0", Residency.GEO),
        ("global.anthropic.claude-haiku-4-5-20251001-v1:0", Residency.GLOBAL),
    ],
)
def test_residency_is_read_from_the_model_prefix(model: str, expected: Residency) -> None:
    """One word of a model string decides the processing geography, and only this reads it."""
    assert residency_of(model) is expected


@pytest.mark.parametrize(
    ("base_url", "is_mantle", "region"),
    [
        (RUNTIME_CA, False, "ca-central-1"),
        ("https://bedrock-runtime.us-east-1.amazonaws.com", False, "us-east-1"),
        (MANTLE_US, True, "us-east-1"),
        ("https://bedrock-mantle.ap-northeast-1.api.aws", True, "ap-northeast-1"),
    ],
)
def test_split_host_reads_the_endpoint_and_region(
    base_url: str, is_mantle: bool, region: str
) -> None:
    assert split_host(base_url) == (is_mantle, region)


@pytest.mark.parametrize(
    "base_url",
    [
        "https://bedrock-runtime.ca-central-1.amazonaws.com.evil.test",
        "https://evil.test/bedrock-runtime.ca-central-1.amazonaws.com",
        "https://bedrock-mantle.us-east-1.api.aws.evil.test",
        "https://api.anthropic.com",
    ],
)
def test_split_host_refuses_a_host_it_cannot_describe(base_url: str) -> None:
    """A look-alike host is refused rather than guessed at.

    An unanchored match here would let `...amazonaws.com.evil.test` read as a Bedrock
    endpoint in ca-central-1, which is the one mistake a residency guard must not make.
    """
    with pytest.raises(ConfigError, match="not a Bedrock endpoint"):
        split_host(base_url)


# -- the guard: every way the three statements can disagree ------------------------------


def test_geo_profile_on_the_mantle_endpoint_is_refused() -> None:
    """Mantle takes bare ids only, and the reason is the residency, not the syntax."""
    provider = _provider(base_url=MANTLE_US, region="us-east-1")
    with pytest.raises(ConfigError, match="bare model id only"):
        _build(GEO_MODEL, provider)


def test_global_profile_on_the_mantle_endpoint_is_refused() -> None:
    provider = _provider(base_url=MANTLE_US, region="us-east-1")
    with pytest.raises(ConfigError, match="bare model id only"):
        _build(GLOBAL_MODEL, provider)


def test_bare_model_id_on_the_runtime_endpoint_is_refused() -> None:
    """bedrock-runtime requires a profile for on-demand throughput, and the error says
    which regions could serve the single-region intent instead."""
    with pytest.raises(ConfigError, match="requires a geo or global inference profile"):
        _build(BARE_MODEL, _provider())


def test_a_region_that_disagrees_with_the_host_is_refused() -> None:
    """The host decides where the request goes; a route pinning another region is a bug."""
    provider = _provider(region="us-east-1")
    with pytest.raises(ConfigError, match="does not match base_url"):
        _build(GEO_MODEL, provider)


def test_a_route_region_that_disagrees_with_the_host_is_refused() -> None:
    provider = _provider(region=None)
    with pytest.raises(ConfigError, match="does not match base_url"):
        _build(GEO_MODEL, provider, region="eu-west-1")


def test_a_declared_residency_that_the_model_id_contradicts_is_refused() -> None:
    """The check that survives an edit.

    Swapping `global.` for `us.` in a model string is one word of diff and moves the
    processing geography. The declaration on the provider entry is the only thing in the
    system that disagrees with it.
    """
    provider = _provider(residency=Residency.GLOBAL)
    with pytest.raises(ConfigError, match="declares residency 'global'"):
        _build(GEO_MODEL, provider)


def test_a_declared_residency_that_agrees_is_allowed() -> None:
    provider = _provider(residency=Residency.GEO)
    assert _build(GEO_MODEL, provider).url.endswith(MESSAGES_PATH)


def test_an_undeclared_residency_still_checks_the_endpoint_and_the_region() -> None:
    """Not declaring residency is allowed, and buys no leniency on the other two checks."""
    provider = _provider(residency=None)
    assert _build(GLOBAL_MODEL, provider).url.endswith(MESSAGES_PATH)
    with pytest.raises(ConfigError, match="requires a geo or global inference profile"):
        _build(BARE_MODEL, provider)


# -- registration -----------------------------------------------------------------------


def test_bedrock_is_registered_and_serves_no_batches() -> None:
    """Bedrock batching is CreateModelInvocationJob over S3, a different API entirely, so a
    batch aimed here is refused by name rather than failing somewhere deeper."""
    assert isinstance(ADAPTERS[ProviderKind.AWS_BEDROCK], BedrockAdapter)
    assert ProviderKind.AWS_BEDROCK not in BATCH_ADAPTERS
    assert _provider().serves_batches is False
