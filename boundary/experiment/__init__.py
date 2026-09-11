"""Experiments that produce the Rule C evidence: approaches tried and rejected, with the
numbers behind the rejection. Each writes a JSON result and a section of docs/rejected.md.

    remote_ledger  a central ledger written over the network, against local-first + merge
"""

from __future__ import annotations

from boundary.experiment import remote_ledger

__all__ = ["remote_ledger"]
