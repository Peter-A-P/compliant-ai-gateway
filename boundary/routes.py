"""Alias resolution and the explicit provider/model form. PLAN.md section 2.6.

A caller that passes an alias is redirected when the routes file changes. A caller that
passes "provider/model-id" is never redirected, and pass-through mode requires that form.
"""

from __future__ import annotations

from dataclasses import dataclass

from boundary.config import BoundaryConfig, ProviderConfig
from boundary.errors import ConfigError, PassthroughViolation, UnknownAlias
from boundary.types import Mode


@dataclass(frozen=True, slots=True)
class ModelRef:
    """A fully resolved target: which provider entry, which model identifier, and the
    alias it came from (None when the caller was explicit)."""

    provider: str
    model: str
    provider_config: ProviderConfig
    api_version: str | None = None
    region: str | None = None
    alias: str | None = None

    @property
    def explicit(self) -> str:
        """The "provider/model-id" form, written to the ledger as model_requested."""
        return f"{self.provider}/{self.model}"


def split_explicit(model: str) -> tuple[str, str] | None:
    """("provider", "model-id") for the explicit form, None for an alias."""
    if "/" not in model:
        return None
    provider, _, model_id = model.partition("/")
    if not provider or not model_id:
        raise ConfigError(f"model {model!r} is not a valid provider/model-id")
    return provider, model_id


def resolve(model: str, config: BoundaryConfig, mode: Mode) -> ModelRef:
    """Resolve what a request's `model` field points at.

    Explicit form: the provider must exist; nothing is rewritten.
    Alias: refused in pass-through mode (PassthroughViolation), unknown aliases raise
    UnknownAlias, otherwise the route is applied.
    """
    parts = split_explicit(model)
    if parts is not None:
        provider, model_id = parts
        pc = config.providers.get(provider)
        if pc is None:
            raise ConfigError(
                f"unknown provider {provider!r} in {model!r}; "
                f"known: {', '.join(sorted(config.providers))}"
            )
        return ModelRef(
            provider=provider,
            model=model_id,
            provider_config=pc,
            api_version=pc.api_version,
            region=pc.region,
        )

    if mode is Mode.PASSTHROUGH:
        raise PassthroughViolation(
            f"pass-through mode requires the explicit provider/model-id form, got alias {model!r}; "
            "a routing change would be a change to the data"
        )
    route = config.routes.get(model)
    if route is None:
        raise UnknownAlias(model, config.routes)
    pc = config.providers[route.provider]
    return ModelRef(
        provider=route.provider,
        model=route.model,
        provider_config=pc,
        api_version=route.api_version or pc.api_version,
        region=route.region or pc.region,
        alias=model,
    )
