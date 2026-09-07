"""Alias resolution: explicit identifiers are never redirected, pass-through refuses aliases."""

from __future__ import annotations

import pytest

from boundary.config import BoundaryConfig
from boundary.errors import ConfigError, PassthroughViolation, UnknownAlias
from boundary.routes import ModelRef, resolve, split_explicit
from boundary.types import Mode


def test_split_explicit() -> None:
    assert split_explicit("anthropic/claude-haiku-4-5-20251001") == (
        "anthropic",
        "claude-haiku-4-5-20251001",
    )
    assert split_explicit("fast") is None
    # Model identifiers may themselves contain slashes (open-weights hosts do this).
    assert split_explicit("openweights/meta-llama/Llama-3.3-70B") == (
        "openweights",
        "meta-llama/Llama-3.3-70B",
    )
    with pytest.raises(ConfigError):
        split_explicit("/model")
    with pytest.raises(ConfigError):
        split_explicit("provider/")


@pytest.mark.parametrize("mode", [Mode.STANDARD, Mode.PASSTHROUGH])
def test_explicit_form_resolves_in_both_modes(repo_config: BoundaryConfig, mode: Mode) -> None:
    ref = resolve("anthropic/claude-haiku-4-5-20251001", repo_config, mode)
    assert isinstance(ref, ModelRef)
    assert ref.provider == "anthropic"
    assert ref.model == "claude-haiku-4-5-20251001"
    assert ref.alias is None
    assert ref.api_version == "2023-06-01"
    assert ref.explicit == "anthropic/claude-haiku-4-5-20251001"


def test_alias_resolves_in_standard_mode(repo_config: BoundaryConfig) -> None:
    ref = resolve("fast", repo_config, Mode.STANDARD)
    assert ref.alias == "fast"
    assert ref.provider == repo_config.routes["fast"].provider
    assert ref.model == repo_config.routes["fast"].model


def test_alias_refused_in_passthrough(repo_config: BoundaryConfig) -> None:
    with pytest.raises(PassthroughViolation, match="explicit provider/model-id"):
        resolve("fast", repo_config, Mode.PASSTHROUGH)


def test_unknown_alias(repo_config: BoundaryConfig) -> None:
    with pytest.raises(UnknownAlias) as ei:
        resolve("nonsense", repo_config, Mode.STANDARD)
    assert ei.value.alias == "nonsense"
    assert "fast" in ei.value.known


def test_unknown_provider_in_explicit_form(repo_config: BoundaryConfig) -> None:
    with pytest.raises(ConfigError, match="unknown provider 'mystery'"):
        resolve("mystery/model", repo_config, Mode.STANDARD)


def test_route_change_moves_the_alias_not_the_explicit_form(repo_config: BoundaryConfig) -> None:
    """The redirect test in miniature: editing the route moves alias callers only."""
    changed = repo_config.model_copy(
        update={
            "routes": {
                **repo_config.routes,
                "fast": repo_config.routes["fast"].model_copy(
                    update={"provider": "openai", "model": "gpt-x"}
                ),
            }
        }
    )
    assert resolve("fast", changed, Mode.STANDARD).explicit == "openai/gpt-x"
    assert (
        resolve("anthropic/claude-haiku-4-5-20251001", changed, Mode.STANDARD).explicit
        == "anthropic/claude-haiku-4-5-20251001"
    )
