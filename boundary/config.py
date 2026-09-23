"""Configuration schema and loaders for boundary.yaml, caps.yaml and the price files.

Everything is validated with pydantic on load so that a misconfiguration fails at start-up
and not on the first call. Relative paths inside a file resolve against that file's
directory, so a configuration can be checked in and used from any working directory.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
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


class CredentialSource(StrEnum):
    """Where a provider entry's credential comes from.

    `env` is every provider in this portfolio except one: a key copied into `.env` once,
    which does not expire. `google_adc` is for Vertex, whose OAuth access tokens last about
    an hour, so a long run has to mint them rather than be handed one.
    """

    ENV = "env"
    GOOGLE_ADC = "google_adc"


class Residency(StrEnum):
    """How far a request may travel from the endpoint it was sent to.

    This is a statement of intent, written by whoever configures a provider entry, and it
    is checked against what the request will actually do. It is not parsed from a response:
    no vendor reports where a request was processed, which is the finding that made this
    field necessary (see docs/hyperscaler-setup.md).

    single-region  processing stays in the endpoint's own region, guaranteed by the vendor
    geo           processing stays inside a named geography, which may span countries
    global        processing may go to any region the vendor operates
    """

    SINGLE_REGION = "single-region"
    GEO = "geo"
    GLOBAL = "global"


# Which provider kinds serve a batch endpoint unless a provider entry says otherwise. See
# ProviderConfig.batches for why openai_compat is not in here.
_BATCHES_BY_DEFAULT = frozenset({ProviderKind.ANTHROPIC, ProviderKind.GOOGLE})


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
    # gcp_vertex only: the Google Cloud project id, which Vertex carries in the URL rather
    # than in a header. Additive and optional, so every existing configuration still loads.
    project: str | None = None
    # How far a request sent here may travel to be processed, as an explicit declaration.
    # Optional and additive: unset means "not declared", which is what every provider entry
    # written before 2026-09-15 means, and nothing is inferred on its behalf.
    #
    # aws_bedrock enforces it: the residency a Bedrock call actually gets is encoded in the
    # model identifier's prefix and nowhere else, so a one-word edit to a model string moves
    # the processing geography with no other diff. Declaring it here gives that edit
    # something to disagree with. See boundary/providers/bedrock.py.
    #
    # Other kinds accept it and record it without checking, because their platforms offer no
    # equivalent signal to check it against. Part B's residency policy is built on this
    # field; until then it documents a choice rather than enforcing one.
    residency: Residency | None = None
    # Where this entry's credential comes from. `env` reads `api_key_env` and is the default,
    # so every provider entry written before 2026-09-15 keeps working unchanged.
    #
    # `google_adc` mints a cloud-platform access token from Application Default Credentials
    # and refreshes it before it expires, which is what Vertex needs and what nothing else
    # does. It requires the optional `google-auth` dependency: install `boundary[vertex]`.
    #
    # Under `google_adc`, a non-empty `api_key_env` variable still wins. That precedence is
    # deliberate and is the difference between one configuration file and two: a laptop has
    # `gcloud auth application-default login` and no variable, while CI has a token minted by
    # a previous step and no gcloud, and both read this same checked-in entry. Setting the
    # variable is how CI says "use this one"; leaving it unset is how a laptop says "mint it".
    credentials: CredentialSource = CredentialSource.ENV
    # Whether this host serves a batch endpoint. None means "the default for this kind":
    # on for anthropic and google, where batches are part of the vendor's API, and OFF for
    # openai_compat, where they are not part of the compatibility surface. Ollama, vLLM and
    # a dozen other hosts answer /v1/chat/completions and have no /v1/batches, so a
    # compatible host has to say so rather than be assumed to have one. Set it explicitly to
    # override either default.
    batches: bool | None = None
    price_zero: bool = False
    # A host this portfolio runs itself (0.3): a vLLM or llama.cpp server on a rented GPU.
    # Its rates do not come from a vendor's price page, because there is no vendor; they come
    # from a measurement the project running the host made, a dated GPU-hour rate divided by
    # a measured throughput, and they are re-measured for every model, quantisation and GPU.
    # Those rates live in the directory `self_hosted_prices` names, in the same dated file
    # format as the vendor lists, and a rate for a provider flagged here is read from there
    # and only from there. The vendor lists refuse to carry a self-hosted provider, and the
    # overlay refuses to carry a vendor, so there is still one copy of every vendor price.
    # Incompatible with `price_zero`: free and measured are different claims.
    self_hosted: bool = False
    headers: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _self_hosted_is_not_free(self) -> ProviderConfig:
        if self.self_hosted and self.price_zero:
            raise ValueError(
                "a provider entry cannot be both `self_hosted` and `price_zero`: one says the "
                "rate was measured, the other says there is none"
            )
        return self

    @property
    def serves_batches(self) -> bool:
        """Whether a batch may be submitted to this provider entry."""
        if self.batches is not None:
            return self.batches
        return self.kind in _BATCHES_BY_DEFAULT

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
    # Which environment writes this file. Recorded in every row (schema v2) so that a
    # central ledger built by `boundary ledger merge` can still say where a call was made
    # and which raw store its `raw_path` points into. The BOUNDARY_ENV environment
    # variable overrides it, which is how a runner labels itself without editing a
    # checked-in file.
    env: str = Field(default="local", min_length=1, pattern=r"^[a-z0-9][a-z0-9._-]*$")


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
    # Optional (0.3): a directory of dated price files for providers flagged `self_hosted`,
    # supplied by the project that measured them and kept beside its configuration rather
    # than in this package, because a measured rate is that project's finding and not a
    # vendor's list. Accepted only for self-hosted providers; a vendor named in it is refused
    # at gateway construction, so `prices` stays the one copy of every vendor rate.
    self_hosted_prices: Path | None = None
    defaults: Defaults = Field(default_factory=Defaults)
    retry: RetryPolicy = Field(default_factory=RetryPolicy)
    ledger: LedgerConfig = Field(default_factory=LedgerConfig)
    telemetry: TelemetryConfig = Field(default_factory=TelemetryConfig)
    cache: CacheConfig = Field(default_factory=CacheConfig)
    # Optional (0.12): a data policy file (boundary.enforce). Absent, nothing is enforced and
    # the gateway behaves exactly as it did before 0.12.
    policy: Path | None = None

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

    @property
    def rates_sha256(self) -> str:
        """A fingerprint of the rates themselves, written into every row costed with them.

        `name` is a date, and a date is not unique across repositories. On 2026-09-18 this
        library and project 02 both held a `2026-09-12.yaml`, byte-different and semantically
        identical, while every ledger row from both said only `price_list: 2026-09-12`. The
        rates agreed, and nothing in the ledger could have shown it if they had not.

        So this hashes the **parsed** rates rather than the file: same rates, same fingerprint,
        whatever the comments or the key order or the line endings say. Two rows that cite the
        same date and disagree here were costed differently, and that is now visible in the
        ledger instead of being a thing somebody has to go and check by hand in two checkouts.

        Deliberately not the file's bytes. A comment is not a rate, and a reformatting that
        changed every row's fingerprint would make the column noise rather than evidence.
        """
        canonical = {
            provider: {
                model: entry.model_dump(mode="json", exclude_none=False)
                for model, entry in sorted(models.items())
            }
            for provider, models in sorted(self.per_million_tokens.items())
        }
        payload = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()

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


# A `prices:` value of exactly this means the price files that ship inside the package,
# rather than a directory beside the configuration file. It is how a project that installs
# `boundary` gets the same rates the library was released with, without copying them: the
# version pin then determines the costing, which is what makes a published cost table
# reproducible from a checkout alone. See docs/prices.md.
BUILTIN_PRICES = "builtin"

PACKAGED_PRICES = Path(__file__).resolve().parent / "prices"


def _resolve_prices(base: Path, p: Path) -> Path:
    if str(p) == BUILTIN_PRICES:
        return PACKAGED_PRICES
    return _resolve(base, p)


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
            "prices": _resolve_prices(base, cfg.prices),
            "self_hosted_prices": (
                _resolve(base, cfg.self_hosted_prices)
                if cfg.self_hosted_prices is not None
                else None
            ),
            "caps": _resolve(base, cfg.caps),
            "policy": _resolve(base, cfg.policy) if cfg.policy is not None else None,
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


def check_price_lists(
    config: BoundaryConfig, vendor: PriceList, self_hosted: PriceList | None
) -> None:
    """Refuse a price list that carries a provider it has no business pricing (0.3).

    Two rules, one in each direction, and together they keep "one copy of every vendor
    price" true after the overlay exists:

    - The vendor list may not price a provider flagged `self_hosted`. A self-hosted rate is a
      measurement made by the project running the host, and it changes when the GPU or the
      quantisation does; a copy in the package would be the second copy that this library
      consolidated the vendor files to get rid of.
    - The overlay may not price a provider that is not flagged `self_hosted`, or that the
      configuration does not know. A vendor rate in a project's own directory is exactly the
      drift `prices: builtin` exists to prevent, and a rate for an unknown provider is a typo
      waiting to cost nothing.

    Checked at gateway construction rather than per call, so a misconfiguration fails before
    the first request and not on the first row.
    """
    flagged = {name for name, pc in config.providers.items() if pc.self_hosted}
    in_vendor = sorted(flagged & set(vendor.per_million_tokens))
    if in_vendor:
        raise ConfigError(
            f"price list {vendor.name} carries rates for self-hosted provider(s) "
            f"{', '.join(in_vendor)}; a self-hosted rate is a measurement and belongs in "
            "the directory `self_hosted_prices` names, not in the vendor list"
        )
    if self_hosted is None:
        return
    for provider in sorted(self_hosted.per_million_tokens):
        if provider not in config.providers:
            raise ConfigError(
                f"self-hosted price list {self_hosted.name} names provider {provider!r}, "
                f"which is not in the configuration; known: {', '.join(sorted(config.providers))}"
            )
        if provider not in flagged:
            raise ConfigError(
                f"self-hosted price list {self_hosted.name} carries rates for {provider!r}, "
                "which is not flagged `self_hosted: true`. Vendor rates have one copy, in the "
                "package; a project may not overlay them"
            )
