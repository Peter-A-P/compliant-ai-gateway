"""Minimal .env loader. Keys come only from the environment; this fills the environment
from a gitignored file for local use, and never overrides a variable that is already set.

Deliberately small rather than a dependency: KEY=value lines, optional surrounding quotes,
comments and blank lines ignored. No interpolation, no export keyword, no multi-line values.
"""

from __future__ import annotations

import os
from pathlib import Path


def load_dotenv(path: Path) -> list[str]:
    """Load KEY=value pairs from `path` into os.environ for keys not already set.

    Returns the names that were set. A missing file is not an error: an Actions runner has
    its secrets in the environment already and no file at all.
    """
    if not path.is_file():
        return []
    loaded: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if not key or key in os.environ or not value:
            continue
        os.environ[key] = value
        loaded.append(key)
    return loaded


def find_dotenv(*candidates: Path) -> Path | None:
    """First existing .env among the candidate directories."""
    for d in candidates:
        p = d / ".env"
        if p.is_file():
            return p
    return None
