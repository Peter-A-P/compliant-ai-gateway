"""Canonical JSON for request bodies: deterministic bytes for the same inputs, so retries
send identical bytes and the request hash in the ledger means something."""

from __future__ import annotations

import json
from typing import Any


def dumps(obj: Any) -> bytes:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False, sort_keys=True).encode(
        "utf-8"
    )


def loads(body: bytes) -> Any:
    return json.loads(body.decode("utf-8"))
