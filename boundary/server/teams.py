"""Teams, their keys, their budgets and their request quotas. PLAN.md B2.7.

A team is a ledger `project`. That is the whole of the mapping, and it is chosen so that the
proxy adds no second place where spend is counted: a team's monthly budget is a Part A spend
cap on its project, enforced by the same `Gateway._check_caps` over the same ledger, and a
project that later calls through the proxy under its own name keeps one line in
`ledger report` rather than two.

Keys are never stored. `teams.yaml` holds the SHA-256 of each key, which is enough to
recognise one and useless to anybody who reads the file, so the file can be committed and
reviewed like `caps.yaml`. `boundary teams key` mints a key, prints it once, and prints the
line to paste.

The request quota is per process and in memory. It resets when the proxy restarts, which is
stated rather than hidden: the budget is the limit that protects money and it lives in the
ledger; the quota protects the upstream from a runaway loop, and a restart is not a loop.
"""

from __future__ import annotations

import hashlib
import secrets
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from boundary.config import CapsConfig, ProjectCap
from boundary.errors import ConfigError

# Every key minted here starts with this, so a key pasted somewhere it should not be is
# recognisable for what it is (and a secret scanner can be told the shape).
KEY_PREFIX = "bnd_"
_HEX = frozenset("0123456789abcdef")
QUOTA_WINDOW_S = 60.0


def hash_key(key: str) -> str:
    """The SHA-256 of a key, in lower-case hex, as `teams.yaml` stores it."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def new_key() -> str:
    """A fresh key: the prefix and 256 bits from the operating system's generator."""
    return KEY_PREFIX + secrets.token_urlsafe(32)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Team(_Strict):
    """One team. `key_sha256` is a list so a key can be rotated without an outage: add the
    new hash, move the callers, remove the old one."""

    key_sha256: list[str] = Field(min_length=1)
    monthly_usd: float = Field(gt=0)
    per_run_usd: float | None = Field(default=None, gt=0)
    requests_per_minute: int = Field(gt=0)

    @field_validator("key_sha256")
    @classmethod
    def _hashes(cls, v: list[str]) -> list[str]:
        for h in v:
            if len(h) != 64 or not set(h) <= _HEX:
                raise ValueError(
                    "each key_sha256 must be 64 lower-case hex characters, the SHA-256 of a "
                    "key; a raw key in this file is refused (boundary teams key prints the hash)"
                )
        return v


class TeamsConfig(_Strict):
    """`teams.yaml`. `gateway_monthly_usd` is the ceiling over every team together, the
    proxy's equivalent of `portfolio_monthly_usd` in `caps.yaml`."""

    version: Literal[1]
    gateway_monthly_usd: float = Field(gt=0)
    teams: dict[str, Team] = Field(min_length=1)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        seen: dict[str, str] = {}
        for name, team in self.teams.items():
            if not name or name.strip() != name:
                raise ValueError(f"team name {name!r} must be non-empty with no surrounding space")
            for h in team.key_sha256:
                if h in seen:
                    raise ValueError(
                        f"one key hash is listed for both {seen[h]!r} and {name!r}; a key "
                        "must identify exactly one team"
                    )
                seen[h] = name
        total = sum(t.monthly_usd for t in self.teams.values())
        if total > self.gateway_monthly_usd:
            # The same check caps.yaml has for its projects: a ceiling below the sum of the
            # budgets under it means some team's budget is a number that can never be spent.
            raise ValueError(
                f"team budgets sum to US${total:g}, above gateway_monthly_usd "
                f"US${self.gateway_monthly_usd:g}; raise the ceiling or lower a budget"
            )
        return self

    def caps(self) -> CapsConfig:
        """The teams as Part A spend caps: one project per team, no default, so a call can
        only be made on behalf of a team this file names."""
        return CapsConfig(
            version=1,
            portfolio_monthly_usd=self.gateway_monthly_usd,
            projects={
                name: ProjectCap(monthly_usd=t.monthly_usd, per_run_usd=t.per_run_usd)
                for name, t in self.teams.items()
            },
            default=None,
        )

    def by_hash(self) -> dict[str, str]:
        """Key hash to team name."""
        return {h: name for name, t in self.teams.items() for h in t.key_sha256}


def load_teams(path: str | Path) -> TeamsConfig:
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as e:
        raise ConfigError(f"cannot read teams file {path}: {e}") from e
    try:
        return TeamsConfig.model_validate(raw)
    except ValidationError as e:
        raise ConfigError(f"invalid teams file {path}: {e}") from e


@dataclass(frozen=True, slots=True)
class QuotaDecision:
    allowed: bool
    limit: int
    used: int
    # Seconds until one more request would be allowed; 0.0 when allowed.
    retry_after_s: float


class RequestQuota:
    """Requests per minute per team, over a sliding sixty-second window.

    Sliding rather than fixed so that a caller cannot put twice the quota through by
    straddling a minute boundary. A refused request does not count against the window,
    so a client that backs off by `retry_after_s` is let through when it returns.
    """

    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._seen: dict[str, deque[float]] = {}

    def take(self, team: str, limit: int) -> QuotaDecision:
        now = self._clock()
        window = self._seen.setdefault(team, deque())
        while window and window[0] <= now - QUOTA_WINDOW_S:
            window.popleft()
        if len(window) >= limit:
            return QuotaDecision(False, limit, len(window), window[0] + QUOTA_WINDOW_S - now)
        window.append(now)
        return QuotaDecision(True, limit, len(window), 0.0)
