"""Building the proxy from `boundary serve`'s settings, in whichever process runs it (0.28).

`boundary serve --workers N` starts N processes, and uvicorn can only give each one the app
by importing it. So `serve` writes its settings to `BOUNDARY_SERVE` as JSON and names
`build` here; every worker calls it and builds the same app. A single worker goes the same
way, so there is one path, not two.

Secrets never go through `BOUNDARY_SERVE`: the vendor keys, `BOUNDARY_DATABASE_URL` and
`BOUNDARY_REDIS_URL` are read from the environment, which is where compose puts them.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from fastapi import FastAPI

ENV = "BOUNDARY_SERVE"
# The threshold `boundary cache eval` chose on the odd half and reported on the even
# (0.25, docs/cache.md), and a bound on each team's cache for a long-running proxy.
SEMCACHE_THRESHOLD = 0.82
SEMCACHE_MAX_ENTRIES = 20_000


@dataclass(frozen=True)
class Settings:
    config: str
    teams: str
    policy: str
    ledger: str
    audit: str | None
    central: str | None
    semantic_cache: float | None
    # Postgres and Redis from BOUNDARY_DATABASE_URL and BOUNDARY_REDIS_URL (0.28).
    hosted: bool = False
    workers: int = 1

    def to_env(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_env(cls) -> Settings:
        raw = os.environ.get(ENV)
        if not raw:
            raise RuntimeError(f"{ENV} is not set; start the proxy with `boundary serve`")
        return cls(**json.loads(raw))


def _embedder(workers: int) -> Any:
    """bge-small, on one ONNX thread per worker when there is more than one worker. Four
    workers each fanning an embedding out over all four cores made a miss cost 220 ms at the
    median on the VPS against 44 ms with one thread each (docs/loadtest.md, 0.28).
    BOUNDARY_EMBED_THREADS overrides it."""
    from boundary.semcache import BgeSmall

    if os.environ.get("BOUNDARY_EMBED_THREADS"):
        return BgeSmall()
    return BgeSmall(threads=1 if workers > 1 else None)


def build_from(s: Settings) -> FastAPI:
    from boundary.config import load_config
    from boundary.enforce import load_policy
    from boundary.errors import ConfigError
    from boundary.semcache import SemanticCache
    from boundary.server.app import Hosted, create_app
    from boundary.server.teams import load_teams

    cfg = load_config(Path(s.config))
    hosted = None
    semantic_cache = None
    if s.hosted:
        url = os.environ.get("BOUNDARY_DATABASE_URL", "").strip()
        if not url:
            raise ConfigError("a hosted proxy needs BOUNDARY_DATABASE_URL")
        hosted = Hosted(
            database_url=url,
            redis_url=os.environ.get("BOUNDARY_REDIS_URL", "").strip() or None,
            semantic_threshold=s.semantic_cache,
            embedder=_embedder(s.workers) if s.semantic_cache is not None else None,
            semantic_max_entries=SEMCACHE_MAX_ENTRIES,
        )
    elif s.semantic_cache is not None:
        embedder = _embedder(s.workers)
        threshold = float(s.semantic_cache)

        def semantic_cache() -> SemanticCache:
            return SemanticCache(embedder, threshold=threshold, max_entries=SEMCACHE_MAX_ENTRIES)

    return create_app(
        cfg,
        load_teams(Path(s.teams)),
        ledger_path=Path(s.ledger),
        policy=load_policy(Path(s.policy)),
        audit_path=Path(s.audit) if s.audit else None,
        semantic_cache=semantic_cache,
        central_path=Path(s.central) if s.central else None,
        hosted=hosted,
    )


def build() -> FastAPI:
    """What each uvicorn worker imports."""
    return build_from(Settings.from_env())


__all__ = ["ENV", "SEMCACHE_MAX_ENTRIES", "SEMCACHE_THRESHOLD", "Settings", "build", "build_from"]
