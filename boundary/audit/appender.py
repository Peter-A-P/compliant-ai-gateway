"""Sealing a ledger as it grows: the proxy's audit append (0.18, PLAN.md B2.4).

`AuditLog.seal` rereads the whole log to learn what is already sealed, which is right for a
command run now and then and wrong once per request. The proxy knows more: rows only ever
arrive with higher ids, and a row it has sealed complete does not change. So the appender
seals everything once when it opens, then after each call appends a record for every new
row that has completed, and keeps the ids of rows still in flight to seal when they finish.
Each append is one transaction on the log, whatever else is running.

What this closes is the window docs/audit.md describes, between a call and the operator's
next `boundary audit seal`, in which only the ledger held it. What it does not close is the
window after the last anchor, which needs the anchor published somewhere the operator cannot
rewrite: B2.4's daily Action, which needs the proxy on an always-on host.
"""

from __future__ import annotations

import threading
from pathlib import Path

from boundary.audit.chain import ledger_body
from boundary.audit.log import AuditLog
from boundary.ledger.store import IN_FLIGHT, LedgerStore, utc_now


class AuditAppender:
    def __init__(self, path: Path, ledger: LedgerStore) -> None:
        self.log = AuditLog(path)
        self._ledger = ledger
        self._lock = threading.Lock()
        # One full seal on open, for rows written before this process started.
        self.log.seal(ledger.rows())
        rows = ledger.rows()
        self._pending: set[int] = {int(r["id"]) for r in rows if r["error_type"] == IN_FLIGHT}
        self._watermark = max((int(r["id"]) for r in rows), default=0)
        self.appended = 0

    def after_call(self) -> int:
        """Seal every row that has completed since the last call. Returns how many."""
        with self._lock:
            low = min(self._pending, default=self._watermark + 1) - 1
            bodies: list[str] = []
            stamp = utc_now()
            for row in self._ledger.rows_after(low):
                rid = int(row["id"])
                if rid <= self._watermark and rid not in self._pending:
                    continue
                self._watermark = max(self._watermark, rid)
                if row["error_type"] == IN_FLIGHT:
                    self._pending.add(rid)
                    continue
                self._pending.discard(rid)
                bodies.append(ledger_body(row, sealed_utc=stamp))
            if bodies:
                self.log.append_bodies(bodies)
                self.appended += len(bodies)
            return len(bodies)

    def close(self) -> None:
        self.log.close()


__all__ = ["AuditAppender"]
