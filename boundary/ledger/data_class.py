"""Read the ledger's `data_class` column back (schema v7): which calls carried what.

The column is a declaration the caller made, from the closed vocabulary in
`boundary.types.DataClass`, and never an inference from content. This module is the filter
both `ledger report` and `ledger residency` apply, so that "which calls carried personal
data" and "where did they go" are the same rows looked at two ways.

Null is its own answer. A row with no class is not `public`; it is a row whose caller said
nothing, and it is filtered for by name (`undeclared`) so that an audit can find every call
that made no claim, which is the list a reviewer most wants before the policy in Part B
starts refusing them.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from typing import Any

from boundary.types import DataClass

# The filter word for a null column. The same label `ledger residency` prints for a null
# residency, so the two reports say "nobody said" the same way.
UNDECLARED = "undeclared"

__all__ = ["UNDECLARED", "check_filter", "label", "matches", "present_in"]


def present_in(rows: Iterable[Mapping[str, Any]]) -> set[str]:
    """Every data class these rows actually carry, null excluded."""
    return {str(r["data_class"]) for r in rows if r.get("data_class")}


def label(value: Any) -> str:
    """The class to print for a raw column value: `undeclared` for null, otherwise the value
    exactly as stored, including one this version does not know, because hiding it would be
    worse than showing it."""
    return UNDECLARED if value is None else str(value)


def check_filter(wanted: str, present: Collection[str] = ()) -> str:
    """Validate a `--data-class` argument: one of the vocabulary, `undeclared`, or a value
    the ledger being read actually holds.

    A word that matches nothing anywhere is refused rather than filtered on, because an
    empty report reads as "no personal data left", which is the one wrong answer this
    column must never give, and a typo is the likeliest way to produce one.

    `present` is what the ledger holds, and it is why this is not a closed check. A file
    written by a later version can carry a class this version has never heard of, and an
    auditor must be able to ask about the rows in front of them. Refusing there would make
    an old reader unable to query its own data, which is a different failure from the one
    the vocabulary protects against. The report prints such a value as stored either way.
    """
    if wanted == UNDECLARED or wanted in {c.value for c in DataClass}:
        return wanted
    if wanted in present:
        return wanted
    known = ", ".join(c.value for c in DataClass)
    extra = sorted(v for v in present if v and v not in {c.value for c in DataClass})
    also = f"; this ledger also holds {', '.join(extra)}" if extra else ""
    raise ValueError(f"data class {wanted!r} is not one of {known}, {UNDECLARED}{also}")


def matches(row: Mapping[str, Any], wanted: str | None) -> bool:
    """Whether a ledger row passes the filter. None is no filter; `undeclared` matches a
    null column; anything else matches the column exactly."""
    if wanted is None:
        return True
    value = row.get("data_class")
    if wanted == UNDECLARED:
        return value is None
    return value == wanted
