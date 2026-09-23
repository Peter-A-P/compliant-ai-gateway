"""Data-class enforcement: which provider a call carrying a given class of data may reach.

PLAN.md B2.2, the library half, pulled forward from Part B (0.12). Since 0.4 every call has
recorded the class of data its caller declared and every ledger row the residency its
provider declared; nothing refused a personal-data call to a provider that could process it
anywhere. This module is the refusal. It is **opt-in**: a gateway with no policy behaves
exactly as before, so the pinned 02 and 03 runs, and pass-through, are untouched.

A policy file names, for each class, the widest residency it tolerates, the regions it may
be sent to, the provider entries it may use, and whether the development cache may hold it.
Every check fails closed:

- A call that declared nothing is treated as the policy's `undeclared` class, which the
  checked-in policy sets to `personal`: absent means personal, as B2.2 says.
- A class the policy does not list is refused, rather than let through on a default.
- A provider entry that declares no residency fails any class with a residency limit, and
  so does a residency value a later version added: neither can be placed against a limit.
- A region limit refuses a call whose region is unset.

What this enforces is **declared** residency, the operator's configuration, not where a
vendor processed a request, which no vendor reports (docs/hyperscaler-setup.md). A clean
decision means the call went to an endpoint whose declaration fits the class. It says
nothing about the vendor's conduct, and the refusal message does not pretend otherwise.

Not enforced here, and why: that `personal` data is redacted before it leaves. The library
cannot tell a redacted body from a raw one without reading content, which it does not do;
Part B's proxy redacts as the policy requires and then passes the call on.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from boundary.config import ProviderConfig, Residency
from boundary.errors import ConfigError
from boundary.ledger.residency import is_known, reach
from boundary.types import DataClass


class ClassRule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    # The widest residency this class tolerates. None: no residency limit.
    max_residency: Residency | None = None
    # Regions a call carrying this class may be sent to. None: any region.
    regions: frozenset[str] | None = None
    # Provider entries this class may use. None: any entry.
    providers: frozenset[str] | None = None
    # Whether the development cache may store and serve these calls.
    cache: bool = False


class DataPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int = Field(1, ge=1, le=1)
    # The class a call that declared nothing is treated as. B2.2: absent means personal.
    undeclared: DataClass = DataClass.PERSONAL
    classes: dict[DataClass, ClassRule]


@dataclass(frozen=True, slots=True)
class Decision:
    allowed: bool
    # The class the decision was made for: the declared one, or `undeclared` standing in.
    data_class: str
    reason: str
    cache: bool = False


def load_policy(path: Path) -> DataPolicy:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return DataPolicy.model_validate(data)
    except (OSError, yaml.YAMLError, ValidationError) as e:
        raise ConfigError(f"{path} is not a valid data policy:\n{e}") from e


def decide(
    policy: DataPolicy,
    data_class: str | None,
    *,
    provider: str,
    provider_config: ProviderConfig,
    region: str | None,
) -> Decision:
    """Whether a call carrying `data_class` may go to this provider entry and region."""
    effective = data_class if data_class is not None else policy.undeclared.value
    try:
        rule = policy.classes[DataClass(effective)]
    except (ValueError, KeyError):
        return Decision(False, effective, f"class {effective!r} is not in the data policy")

    residency = provider_config.residency.value if provider_config.residency else None
    if rule.max_residency is not None:
        if residency is None:
            return Decision(
                False,
                effective,
                f"{effective} data needs residency no wider than {rule.max_residency.value}, "
                f"and provider {provider!r} declares none",
            )
        if not is_known(residency) or reach(residency) > reach(rule.max_residency.value):
            return Decision(
                False,
                effective,
                f"{effective} data needs residency no wider than {rule.max_residency.value}; "
                f"provider {provider!r} declares {residency}",
            )
    if rule.regions is not None and (
        region is None or region.casefold() not in {r.casefold() for r in rule.regions}
    ):
        allowed = ", ".join(sorted(rule.regions))
        return Decision(
            False,
            effective,
            f"{effective} data may be sent only to {allowed}; this call's region is "
            f"{region or 'unset'}",
        )
    if rule.providers is not None and provider not in rule.providers:
        return Decision(
            False,
            effective,
            f"{effective} data may use only {', '.join(sorted(rule.providers))}; not {provider!r}",
        )
    return Decision(True, effective, "within policy (declared residency, not observed)", rule.cache)


__all__ = ["ClassRule", "DataPolicy", "Decision", "decide", "load_policy"]
