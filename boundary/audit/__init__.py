"""boundary.audit: a hash-chained, anchored record of every call (PLAN.md B2.4, 0.7).

Pulled forward from Part B because nothing in it needs the proxy: the chain seals ledger
rows, which every call already writes, and the anchors are lines in a file. What Part B adds
is the Postgres store, the proxy appending as it answers, and the daily anchor committed to
the public repository by an Action. See docs/audit.md.
"""

from boundary.audit.chain import (
    BREAK_KINDS,
    GENESIS,
    SEALED,
    Anchor,
    Break,
    Record,
    Verification,
    verify,
)
from boundary.audit.log import AuditLog, SealStats, append_anchor, read_anchors

__all__ = [
    "BREAK_KINDS",
    "GENESIS",
    "SEALED",
    "Anchor",
    "AuditLog",
    "Break",
    "Record",
    "SealStats",
    "Verification",
    "append_anchor",
    "read_anchors",
    "verify",
]
