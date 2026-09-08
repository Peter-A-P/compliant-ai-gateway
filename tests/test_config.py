"""The checked-in configuration loads, and broken configurations are refused at load."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
import yaml

from boundary.config import (
    BoundaryConfig,
    ProviderKind,
    latest_price_list,
    load_caps,
    load_config,
    load_price_list,
    price_files,
)
from boundary.errors import ConfigError

from .conftest import CONFIG_DIR


def test_repo_config_loads(repo_config: BoundaryConfig) -> None:
    assert repo_config.version == 1
    assert repo_config.providers["anthropic"].kind is ProviderKind.ANTHROPIC
    assert repo_config.providers["anthropic"].api_version == "2023-06-01"
    assert repo_config.providers["google"].api_version == "v1beta"
    assert repo_config.providers["local"].price_zero is True
    assert "fast" in repo_config.routes
    assert repo_config.routes["fast"].provider == "anthropic"


def test_relative_paths_resolve_against_config_file(repo_config: BoundaryConfig) -> None:
    assert repo_config.prices.is_absolute()
    assert repo_config.prices == (CONFIG_DIR / "prices").resolve()
    assert repo_config.caps == (CONFIG_DIR / "caps.yaml").resolve()
    assert repo_config.ledger.path == (CONFIG_DIR / ".." / "boundary.sqlite").resolve()


def test_repo_caps_load_and_default_applies(repo_config: BoundaryConfig) -> None:
    caps = load_caps(repo_config.caps)
    drift = caps.for_project("ai-release-gate")
    assert drift.per_run_usd is not None and drift.per_run_usd <= drift.monthly_usd
    assert caps.for_project("some-new-project") is caps.default
    # The portfolio cap must be able to hold every named project's month at once.
    assert caps.portfolio_monthly_usd >= sum(c.monthly_usd for c in caps.projects.values())


def test_repo_price_lists_load_and_latest_is_by_date(repo_config: BoundaryConfig) -> None:
    files = price_files(repo_config.prices)
    assert files, "at least one dated price file must exist"
    for f in files:
        pl = load_price_list(f)
        assert pl.name == f.stem
    latest = latest_price_list(repo_config.prices)
    assert latest.date == max(load_price_list(f).date for f in files)
    entry = latest.lookup("anthropic", "claude-haiku-4-5-20251001")
    assert entry is not None and entry.input == 1.00 and entry.output == 5.00
    assert latest.lookup("anthropic", "not-a-model") is None


def _write(tmp_path: Path, name: str, data: object) -> Path:
    p = tmp_path / name
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    return p


def _minimal(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "version": 1,
        "providers": {"anthropic": {"kind": "anthropic", "base_url": "https://x.test"}},
        "routes": {"fast": {"provider": "anthropic", "model": "m"}},
        "prices": "prices",
        "caps": "caps.yaml",
    }
    base.update(overrides)
    return base


def test_route_to_unknown_provider_is_refused(tmp_path: Path) -> None:
    p = _write(tmp_path, "b.yaml", _minimal(routes={"fast": {"provider": "nope", "model": "m"}}))
    with pytest.raises(ConfigError, match="unknown provider 'nope'"):
        load_config(p)


def test_alias_containing_slash_is_refused(tmp_path: Path) -> None:
    p = _write(
        tmp_path, "b.yaml", _minimal(routes={"a/b": {"provider": "anthropic", "model": "m"}})
    )
    with pytest.raises(ConfigError, match="contain no '/'"):
        load_config(p)


def test_unknown_key_is_refused(tmp_path: Path) -> None:
    p = _write(tmp_path, "b.yaml", _minimal(retries=3))
    with pytest.raises(ConfigError):
        load_config(p)


def test_routes_may_live_in_a_separate_file(tmp_path: Path) -> None:
    _write(tmp_path, "routes.yaml", {"routes": {"fast": {"provider": "anthropic", "model": "m2"}}})
    p = _write(tmp_path, "b.yaml", _minimal(routes="routes.yaml"))
    cfg = load_config(p)
    assert cfg.routes["fast"].model == "m2"


def test_price_file_name_must_match_date_inside(tmp_path: Path) -> None:
    good = {
        "version": 1,
        "date": dt.date(2026, 9, 7),
        "currency": "USD",
        "source": "test",
        "per_million_tokens": {},
    }
    p = _write(tmp_path, "2026-09-08.yaml", good)
    with pytest.raises(ConfigError, match="must match the file name"):
        load_price_list(p)
    with pytest.raises(ConfigError, match="YYYY-MM-DD"):
        load_price_list(_write(tmp_path, "prices.yaml", good))


def test_price_list_refuses_non_usd_and_negative(tmp_path: Path) -> None:
    bad = {
        "version": 1,
        "date": dt.date(2026, 9, 7),
        "currency": "CAD",
        "source": "test",
        "per_million_tokens": {"a": {"m": {"input": 1, "output": 2}}},
    }
    with pytest.raises(ConfigError):
        load_price_list(_write(tmp_path, "2026-09-07.yaml", bad))
    bad["currency"] = "USD"
    bad["per_million_tokens"] = {"a": {"m": {"input": -1, "output": 2}}}
    with pytest.raises(ConfigError):
        load_price_list(_write(tmp_path, "2026-09-07.yaml", bad))


def test_caps_without_default_refuse_unknown_project(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        "caps.yaml",
        {"version": 1, "portfolio_monthly_usd": 10, "projects": {"x": {"monthly_usd": 1}}},
    )
    caps = load_caps(p)
    with pytest.raises(ConfigError, match="no spend cap for project 'y'"):
        caps.for_project("y")


def test_missing_file_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="cannot read"):
        load_config(tmp_path / "absent.yaml")
