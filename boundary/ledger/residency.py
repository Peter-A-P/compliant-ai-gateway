"""Read the ledger's `residency` column back: where did the data go, and did it stay inside
the limit somebody promised.

Schema v4 records a residency on every row, and until now nothing could ask the question the
column exists to answer. `boundary ledger report` groups by project, model and month, which
is a spend question. This module groups by provider, region and residency, which is a
compliance question, and adds the part that makes it more than a table: a declared limit a
run can be checked against, which fails closed.

## Reach, and why undeclared is the loudest row

Residency is ordered by how far a request may travel from the endpoint it was sent to:

    single-region  <  geo  <  global  <  anything else

"Anything else" is two things, and both rank above `global` on purpose:

- **Undeclared** (a null column). Every row written before 2026-09-15 is one, and so is
  every row from a provider entry with no `residency:` line. An undeclared row is not a
  `global` row. It is a row whose operator made no claim at all, and a report that quietly
  folded those into the weakest declared class would be inventing the claim on their behalf.
- **A value this version does not recognise**, which is what an older `boundary` sees when a
  newer one has added a residency class. The old reader cannot know whether the new class is
  narrower or wider than `global`, so it refuses to let it satisfy any limit.

Both are the same rule: a row that cannot be shown to satisfy a limit does not satisfy it.
That is the fail-closed direction, and it is the only direction available to a feature whose
whole purpose is to be believed by somebody who did not run the call.

## What a clean report does not claim

`residency` is configuration, not observation. No vendor reports where a request was actually
processed; docs/hyperscaler-setup.md has the three separate ways they each avoid saying. So a
clean report proves that every call went to an endpoint whose declared limit was within the
one required, and it proves nothing whatsoever about the vendor's conduct. That sentence is
printed by the command itself rather than left in this docstring, because the person who most
needs to read it is the one running the command and not the one reading the source.

## Cache hits

A cached row sent no bytes anywhere, so it cannot have breached a residency limit and is
counted in its own column rather than silently dropped. Dropping it would make a run's call
count disagree with `ledger report` for no visible reason; counting it as traffic would
attribute travel to a request that never left the machine.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from boundary.config import Residency

# Null in the column. A label rather than None so that it prints, sorts and compares like
# every other value everywhere downstream.
UNDECLARED = "undeclared"

# Lower is narrower. Only the classes this version knows about are in here; see reach().
_REACH: Mapping[str, int] = {
    Residency.SINGLE_REGION.value: 0,
    Residency.GEO.value: 1,
    Residency.GLOBAL.value: 2,
}

# Undeclared, and any class added after this version, sit above every known one so that they
# fail every limit. See the module docstring.
UNKNOWN_REACH = max(_REACH.values()) + 1

__all__ = [
    "UNDECLARED",
    "UNKNOWN_REACH",
    "ResidencyGroup",
    "is_known",
    "label",
    "reach",
    "summarise",
    "violations",
]


def label(value: str | None) -> str:
    """The residency to print for a raw column value. Null becomes `undeclared`; a value
    this version does not recognise is printed exactly as it was stored, because guessing at
    it is the failure this module exists to avoid and hiding it is worse."""
    return UNDECLARED if value is None else value


def reach(value: str | None) -> int:
    """How far this residency lets a request travel, as a sortable rank.

    Anything not in `_REACH`, including null, returns `UNKNOWN_REACH`. That covers both the
    undeclared row and the row written by a later version of the schema.
    """
    if value is None:
        return UNKNOWN_REACH
    return _REACH.get(value, UNKNOWN_REACH)


def is_known(value: str | None) -> bool:
    """Whether this version can place the value in the ordering at all."""
    return value is not None and value in _REACH


@dataclass(frozen=True, slots=True)
class ResidencyGroup:
    """One (provider, region, residency) triple and the traffic that went to it.

    The triple is the unit because all three have to be read together. `region` alone says
    where the request was sent and invites the reader to slide from that to where it stayed;
    `residency` alone says how far it could travel from a region it does not name. The pair
    is the honest statement, and the provider is what makes it actionable.
    """

    provider: str
    region: str
    residency: str
    calls: int
    cached: int
    errors: int
    input_tokens: int
    output_tokens: int
    # Sorted and deduplicated. The models are here because "which of my models went global"
    # is the first question anybody asks of a row they do not like.
    models: tuple[str, ...]

    @property
    def reach(self) -> int:
        return reach(None if self.residency == UNDECLARED else self.residency)

    @property
    def sent(self) -> int:
        """Calls that actually put bytes on the wire. A cache hit did not."""
        return self.calls - self.cached


@dataclass(slots=True)
class _Acc:
    calls: int = 0
    cached: int = 0
    errors: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    models: set[str] = field(default_factory=set)


def _opt_str(value: Any) -> str | None:
    return None if value is None else str(value)


def summarise(
    rows: Iterable[Mapping[str, Any]],
    *,
    month: str | None = None,
    project: str | None = None,
) -> list[ResidencyGroup]:
    """Group ledger rows by provider, region and residency.

    Widest reach first, so the rows a compliance reader cares about are at the top instead of
    in alphabetical order somewhere in the middle. `month` is a `YYYY-MM` prefix match on
    `ts_utc`, the same filter `ledger report` uses.
    """
    acc: dict[tuple[str, str, str], _Acc] = defaultdict(_Acc)
    for r in rows:
        if month and not str(r["ts_utc"]).startswith(month):
            continue
        if project and str(r["project"]) != project:
            continue
        key = (str(r["provider"]), str(r["region"] or "-"), label(_opt_str(r.get("residency"))))
        a = acc[key]
        a.calls += 1
        if r.get("cached"):
            a.cached += 1
        if r.get("error_type"):
            a.errors += 1
        a.input_tokens += int(r.get("input_tokens") or 0)
        a.output_tokens += int(r.get("output_tokens") or 0)
        a.models.add(str(r["model_requested"]))

    groups = [
        ResidencyGroup(
            provider=provider,
            region=region,
            residency=residency,
            calls=a.calls,
            cached=a.cached,
            errors=a.errors,
            input_tokens=a.input_tokens,
            output_tokens=a.output_tokens,
            models=tuple(sorted(a.models)),
        )
        for (provider, region, residency), a in acc.items()
    ]
    groups.sort(key=lambda g: (-g.reach, g.provider, g.region, g.residency))
    return groups


def violations(groups: Iterable[ResidencyGroup], limit: Residency) -> list[ResidencyGroup]:
    """The groups that are not within `limit`, in the order `summarise` returned them.

    A group is a violation when its reach is wider than the limit's, and that includes every
    undeclared and unrecognised group whatever the limit is: `--require global` is not
    satisfied by a row that never said anything. Groups whose calls were all cache hits are
    never violations, because nothing left the machine to travel anywhere.
    """
    ceiling = reach(limit.value)
    return [g for g in groups if g.reach > ceiling and g.sent > 0]
