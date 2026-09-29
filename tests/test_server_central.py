"""The ingest endpoint and the dashboard (0.27)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from boundary.config import BoundaryConfig
from boundary.enforce import load_policy
from boundary.server import create_app
from boundary.server.teams import TeamsConfig, hash_key

from .conftest import CONFIG_DIR
from .test_central import _rows
from .test_server import KEY, NOW, _no_sleep, teams

INGEST = "bnd_test-ingest-key-00000000000000000000000"


@pytest.fixture
async def http(
    repo_config: BoundaryConfig, tmp_path: Path, keys: None
) -> AsyncIterator[httpx.AsyncClient]:
    t = teams()
    cfg = TeamsConfig.model_validate({**t.model_dump(), "ingest_key_sha256": [hash_key(INGEST)]})
    app = create_app(
        repo_config,
        cfg,
        ledger_path=tmp_path / "proxy.sqlite",
        policy=load_policy(CONFIG_DIR / "policy.yaml"),
        central_path=tmp_path / "central.sqlite",
        asleep=_no_sleep,
        wall=lambda: NOW,
    )
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://p")
    yield client
    await client.aclose()
    await app.state.boundary.close()


async def test_only_an_ingest_key_may_push(http: httpx.AsyncClient, tmp_path: Path) -> None:
    body = {"source": "s", "env": "local", "local_rows": 2, "rows": _rows(tmp_path, 2)}
    for key in ("", KEY, "bnd_nonsense"):
        r = await http.post(
            "/v1/ledger/ingest", json=body, headers={"authorization": f"Bearer {key}"}
        )
        assert r.status_code == 401, key
    r = await http.post(
        "/v1/ledger/ingest", json=body, headers={"authorization": f"Bearer {INGEST}"}
    )
    assert r.status_code == 200
    assert r.json()["inserted"] == 2 and r.json()["held"] == 2


async def test_a_malformed_push_is_a_400(http: httpx.AsyncClient) -> None:
    auth = {"authorization": f"Bearer {INGEST}"}
    for body in ({}, {"source": "s", "local_rows": 1, "rows": "x"}, {"source": "s", "rows": []}):
        assert (await http.post("/v1/ledger/ingest", json=body, headers=auth)).status_code == 400


async def test_the_dashboard_shows_pushed_rows_and_no_content(
    http: httpx.AsyncClient, tmp_path: Path
) -> None:
    rows = _rows(tmp_path, 3, project="release-gate")
    await http.post(
        "/v1/ledger/ingest",
        json={"source": "local:gate.sqlite", "env": "local", "local_rows": 4, "rows": rows},
        headers={"authorization": f"Bearer {INGEST}"},
    )
    page = await http.get("/dashboard")
    assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
    assert "release-gate" in page.text and "local:gate.sqlite" in page.text
    assert "1 missing" in page.text, "the source said 4 and sent 3"
    assert "/Users/somebody" not in page.text
    assert rows[0]["request_sha256"] is None or rows[0]["request_sha256"] not in page.text
    assert (await http.get("/")).headers["location"] == "/dashboard"


async def test_no_dashboard_without_a_central_ledger(
    repo_config: BoundaryConfig, tmp_path: Path, keys: None
) -> None:
    app = create_app(
        repo_config,
        teams(),
        ledger_path=tmp_path / "proxy.sqlite",
        policy=load_policy(CONFIG_DIR / "policy.yaml"),
        asleep=_no_sleep,
    )
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://p")
    try:
        assert (await client.get("/dashboard")).status_code == 404
        assert (await client.post("/v1/ledger/ingest", json={})).status_code == 404
    finally:
        await client.aclose()
        await app.state.boundary.close()
