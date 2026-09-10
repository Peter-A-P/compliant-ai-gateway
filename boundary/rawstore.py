"""Pass-through raw store: the request bytes, the response bytes and the response headers
for every call, in JSONL files the caller owns. Only pass-through mode writes here, and
only because a measurement needs the record. Credential headers are redacted.

Layout: <root>/<run_id or 'no-run'>/<provider>.jsonl, one line per call.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from boundary.providers.base import BuiltRequest
from boundary.transport import HttpResult


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _body_field(body: bytes) -> dict[str, Any]:
    try:
        return {"text": body.decode("utf-8")}
    except UnicodeDecodeError:
        return {"base64": base64.b64encode(body).decode("ascii")}


class RawStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def path_for(self, run_id: str | None, provider: str) -> Path:
        safe_run = (run_id or "no-run").replace("/", "_").replace("\\", "_")
        safe_provider = provider.replace("/", "_").replace("\\", "_")
        return self.root / safe_run / f"{safe_provider}.jsonl"

    def write(
        self,
        *,
        ledger_id: int,
        ts_utc: str,
        run_id: str | None,
        provider: str,
        built: BuiltRequest,
        result: HttpResult | None,
        error_type: str | None,
        extra_headers: Mapping[str, str] | None = None,
    ) -> Path:
        record: dict[str, Any] = {
            "ledger_id": ledger_id,
            "ts_utc": ts_utc,
            "provider": provider,
            "request": {
                "method": built.method,
                "url": built.url,
                "headers": built.redacted_headers(),
                "body": _body_field(built.body),
                "sha256": sha256_hex(built.body),
            },
        }
        if result is not None:
            record["response"] = {
                "status": result.status,
                "headers": dict(result.headers),
                "body": _body_field(result.body),
                "sha256": sha256_hex(result.body),
                "elapsed_ms": result.elapsed_ms,
            }
        else:
            record["response"] = {"error": error_type}
        if extra_headers:
            record["request"]["headers"].update(extra_headers)
        path = self.path_for(run_id, provider)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return path
