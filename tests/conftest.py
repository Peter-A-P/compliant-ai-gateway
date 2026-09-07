from __future__ import annotations

from pathlib import Path

import pytest

from boundary.config import BoundaryConfig, load_config

REPO = Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO / "config"


@pytest.fixture(scope="session")
def repo_config() -> BoundaryConfig:
    """The configuration checked into this repository."""
    return load_config(CONFIG_DIR / "boundary.yaml")
