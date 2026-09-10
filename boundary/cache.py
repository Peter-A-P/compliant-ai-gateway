"""Exact-match development cache. Standard mode only; pass-through never touches it.

Keyed by the sha256 of method, URL and body. Headers are excluded from the key so that a
rotated key does not miss the cache, and are never stored. Stores only 2xx responses.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

from boundary.providers.base import BuiltRequest
from boundary.transport import HttpResult


class ExactMatchCache:
    def __init__(self, root: Path) -> None:
        self.root = root

    @staticmethod
    def key(built: BuiltRequest) -> str:
        h = hashlib.sha256()
        h.update(built.method.encode())
        h.update(b"\n")
        h.update(built.url.encode())
        h.update(b"\n")
        h.update(built.body)
        return h.hexdigest()

    def _path(self, key: str) -> Path:
        return self.root / key[:2] / f"{key}.json"

    def get(self, built: BuiltRequest) -> HttpResult | None:
        p = self._path(self.key(built))
        if not p.is_file():
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
        return HttpResult(
            status=int(data["status"]),
            headers=dict(data["headers"]),
            body=base64.b64decode(data["body_base64"]),
            elapsed_ms=0.0,
        )

    def put(self, built: BuiltRequest, result: HttpResult) -> None:
        if not 200 <= result.status < 300:
            return
        p = self._path(self.key(built))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(
                {
                    "status": result.status,
                    "headers": dict(result.headers),
                    "body_base64": base64.b64encode(result.body).decode("ascii"),
                }
            ),
            encoding="utf-8",
        )
