"""Configuration schema and loaders for boundary.yaml, caps.yaml and the price files.

Everything is validated with pydantic on load so that a misconfiguration fails at start-up
and not on the first call. Relative paths inside a file resolve against that file's
directory, so a configuration can be checked in and used from any working directory.
"""

from __future__ import annotations

import datetime as dt
import re
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from boundary.errors import ConfigError

_PRICE_FILE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.ya?ml$")


class ProviderKind(StrEnum):
    """Which adapter serves a provider entry. One adapter can serve many providers."""

    ANTHROPIC = "anthropic"
    OPENAI_COMPAT = "openai_compat"
    GOOGLE = "google"
    AZURE_FOUNDRY = "azure_foundry"  # v0.2
    AWS_BEDROCK = "aws_bedrock"  # v0.2
    GCP_VERTEX = "gcp_vertex"  # v0.2


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProviderConfig(_Strict):
    """One upstream host. The key in the providers mapping is the provider name that
    appears in the explicit "provider/model-id" form."""

    kind: ProviderKind
    base_url: str = Field(pattern=r"^https?://")
    api_key_env: str | None = None
    api_version: str | None = None
    region: str | None = None
    price_zero: bool = False
    headers: dict[str, str] = Field(default_factory=dict)
    # OpenAI-compatible hosts only: OpenAI's newer models take max_completion_tokens, every
    # other compatible host takes max_tokens. Ignored by other adapter kinds.
    max_tokens_field: Literal["max_tokens", "max_completion_tokens"] = "max_tokens"


class Route(_Strict):
    """What an alias resolves to."""

    provider: str
    model: str = Field(min_length=1)
    api_version: str | None = None
    region: str | None = None


class Timeouts(_Strict):
    connect_s: float = Field(default=10.0, gt=0)
    read_s: float = Field(default=120.0, gt=0)


class Defaults(_Strict):
    """Filled in for standard-mode calls that leave a field unset. Never applied in
    pass-through mode."""

    max_tokens: int = Field(default=1024, gt=0)
    timeouts: Timeouts = Field(default_factory=Timeouts)


class RetryPolicy(_Strict):
    """Standard mode only. Pass-through never retries."""

    max_attempts: int = Field(default=4, ge=1, le=10)
    base_delay_s: float = Field(default=0.5, gt=0)
    max_delay_s: float = Field(default=30.0, gt=0)


class LedgerConfig(_Strict):
    path: Path = Path("boundary.sqlite")


class TelemetryConfig(_Strict):
    exporter: Literal["none", "console", "otlp"] = "none"
    endpoint: str | None = None


class CacheConfig(_Strict):
    """Exact-match development cache. Standard mode only."""

    enabled: bool = False
    path: Path = Path(".cache/boundary")


class BoundaryConfig(_Strict):
    version: Literal[1]
    providers: dict[str, ProviderConfig]
    routes: dict[str, Route]
    prices: Path
    caps: Path
    defaults: Defaults = Field(default_factory=Defaults)
    retry: RetryPolicy = Field(default_factory=RetryPolicy)
    ledger: LedgerConfig = Field(default_factory=LedgerConfig)
    telemetry: TelemetryConfig = Field(default_factory=TelemetryConfig)
    cache: CacheConfig = Field(default_factory=CacheConfig)

    @model_validator(mode="after")
    def _consistent(self) -> BoundaryConfig:
        for name in self.providers:
            if "/" in name or not name:
                raise ValueError(f"provider name {name!r} must be non-empty and contain no '/'")
        for alias, route in self.routes.items():
            if "/" in alias or not alias:
                raise ValueError(
                    f"alias {alias!r} must be non-empty and contain no '/'; "
                    "'/' marks the explicit provider/model form"
                )
            if route.provider not in self.providers:
                raise ValueError(
                    f"alias {alias!r} routes to unknown provider {route.provider!r}; "
                    f"known: {', '.join(sorted(self.providers))}"
                )
        return self


class ProjectCap(_Strict):
    monthly_usd: float = Field(gt=0)
    per_run_usd: float | None = Field(default=None, gt=0)


class CapsConfig(_Strict):
    """Second line of defence behind the vendor console caps. Amounts in USD because that
    is the currency every vendor bills in; the plan converts to CAD at reporting time."""

    version: Literal[1]
    portfolio_monthly_usd: float = Field(gt=0)
    projects: dict[str, ProjectCap]
    default: ProjectCap | None = None

    def for_project(self, project: str) -> ProjectCap:
        cap = self.projects.get(project) or self.default
        if cap is None:
            raise ConfigError(
                f"no spend cap for project {project!r} and no default in caps.yaml; "
                "every project must have a cap before it makes a call"
            )
        return cap


class PriceEntry(_Strict):
    """USD per million tokens. A missing cache or batch rate means calls that use that
    feature are written uncosted; the library never fills a rate in."""

    input: float = Field(ge=0)
    output: float = Field(ge=0)
    cache_read: float | None = Field(default=None, ge=0)
    cache_write: float | None = Field(default=None, ge=0)
    batch_multiplier: float | None = Field(default=None, gt=0, le=1)


class PriceList(_Strict):
    version: Literal[1]
    date: dt.date
    currency: Literal["USD"]
    source: str = Field(min_length=1)
    per_million_tokens: dict[str, dict[str, PriceEntry]]

    @property
    def name(self) -> str:
        """The version string written into every ledger row costed with this list."""
        return self.date.isoformat()

    def lookup(self, provider: str, model: str) -> PriceEntry | None:
        return self.per_million_tokens.get(provider, {}).get(model)


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise ConfigError(f"cannot read {path}: {e}") from e
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ConfigError(f"{path} is not valid YAML: {e}") from e
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a mapping at the top level")
    return data


def _resolve(base: Path, p: Path) -> Path:
    return p if p.is_absolute() else (base / p).resolve()


def load_config(path: str | Path) -> BoundaryConfig:
    """Load boundary.yaml. `routes` may be an inline mapping or a path to a routes file
    with a top-level `routes:` mapping; PLAN.md calls the latter config/routes.yaml."""
    path = Path(path)
    data = _read_yaml(path)
    base = path.parent
    routes = data.get("routes")
    if isinstance(routes, str):
        routes_path = _resolve(base, Path(routes))
        routes_data = _read_yaml(routes_path)
        if "routes" not in routes_data:
            raise ConfigError(f"{routes_path} must have a top-level 'routes' mapping")
        data["routes"] = routes_data["routes"]
    try:
        cfg = BoundaryConfig.model_validate(data)
    except ValidationError as e:
        raise ConfigError(f"{path} is invalid:\n{e}") from e
    return cfg.model_copy(
        update={
            "prices": _resolve(base, cfg.prices),
            "caps": _resolve(base, cfg.caps),
            "ledger": cfg.ledger.model_copy(update={"path": _resolve(base, cfg.ledger.path)}),
            "cache": cfg.cache.model_copy(update={"path": _resolve(base, cfg.cache.path)}),
        }
    )


def load_caps(path: str | Path) -> CapsConfig:
    path = Path(path)
    try:
        return CapsConfig.model_validate(_read_yaml(path))
    except ValidationError as e:
        raise ConfigError(f"{path} is invalid:\n{e}") from e


def load_price_list(path: str | Path) -> PriceList:
    path = Path(path)
    m = _PRICE_FILE.match(path.name)
    if m is None:
        raise ConfigError(f"price file {path.name} must be named YYYY-MM-DD.yaml")
    try:
        pl = PriceList.model_validate(_read_yaml(path))
    except ValidationError as e:
        raise ConfigError(f"{path} is invalid:\n{e}") from e
    if pl.date.isoformat() != m.group(1):
        raise ConfigError(f"{path.name}: the date inside ({pl.date}) must match the file name")
    return pl


def price_files(directory: str | Path) -> list[Path]:
    """Dated price files in a directory, oldest first."""
    directory = Path(directory)
    if not directory.is_dir():
        raise ConfigError(f"price directory {directory} does not exist")
    files = [p for p in directory.iterdir() if _PRICE_FILE.match(p.name)]
    return sorted(files, key=lambda p: p.name)


def latest_price_list(directory: str | Path) -> PriceList:
    """The most recent dated price file. Repricing is a new file, never an edit."""
    files = price_files(directory)
    if not files:
        raise ConfigError(f"no YYYY-MM-DD.yaml price files in {directory}")
    return load_price_list(files[-1])
