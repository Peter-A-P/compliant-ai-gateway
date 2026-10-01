"""The hosted demo (0.30): a key anybody may use, and the limits that make that safe.

A day's budget for a team, checked in the same admission step as the month's; a model
list; a ceiling on the answer asked for; and the page that publishes the key, which refuses
to publish one for a team without every limit or a key that is not the team's.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from boundary.config import BoundaryConfig
from boundary.enforce import load_policy
from boundary.errors import ConfigError, SpendCapExceeded
from boundary.ledger.store import Admission, LedgerRow, LedgerStore, next_day, utc_now
from boundary.server import create_app
from boundary.server.app import Demo
from boundary.server.teams import Team, TeamsConfig, hash_key

from .conftest import CONFIG_DIR, OPENWEIGHTS_URL
from .test_server import KEY, MODEL, NOW, OTHER, PUBLIC, _no_sleep, completion

DEMO = "bnd_test-key-for-the-public-demo-000000"


def demo_teams(**overrides: Any) -> TeamsConfig:
    public: dict[str, Any] = {
        "key_sha256": [hash_key(DEMO)],
        "monthly_usd": 5.0,
        "daily_usd": 0.25,
        "requests_per_minute": 10,
        "models": [MODEL],
        "max_tokens": 300,
    }
    public.update(overrides)
    return TeamsConfig(
        version=1,
        gateway_monthly_usd=10.0,
        teams={
            "public": Team(**public),
            "alpha": Team(key_sha256=[hash_key(KEY)], monthly_usd=4.0, requests_per_minute=100),
        },
    )


def _row(project: str, cost: float, ts: str | None = None) -> LedgerRow:
    return LedgerRow(
        ts_utc=ts or utc_now(),
        boundary_version="0.30.0",
        project=project,
        purpose="t",
        mode="standard",
        provider="openweights",
        model_requested=MODEL,
        cost_usd=cost,
        costed=True,
    )


# -- the day's budget in the ledger ----------------------------------------------------------


def test_a_days_budget_counts_only_that_day(tmp_path: Path) -> None:
    store = LedgerStore(tmp_path / "l.sqlite")
    try:
        today = utc_now()[:10]
        yesterday = (dt.date.fromisoformat(today) - dt.timedelta(days=1)).isoformat()
        store.begin(_row("public", 3.0, ts=f"{yesterday}T23:59:59.999Z"))
        store.begin(_row("public", 0.2))
        store.begin(_row("other", 0.2))
        assert store.spend_usd(project="public", day=today) == pytest.approx(0.2)
        assert store.spend_usd(project="public", day=yesterday) == pytest.approx(3.0)
        caps = Admission(
            project="public",
            month=today[:7],
            estimate=0.04,
            project_monthly_usd=100.0,
            portfolio_monthly_usd=100.0,
            day=today,
            project_daily_usd=0.25,
        )
        store.admit(_row("public", 0.04), caps)
        with pytest.raises(SpendCapExceeded) as e:
            store.admit(_row("public", 0.04), caps)
        assert e.value.scope == "project public daily" and e.value.cap_usd == 0.25
        assert next_day("2026-12-31") == "2027-01-01"
    finally:
        store.close()


def test_a_day_above_the_month_is_refused() -> None:
    with pytest.raises(ValueError, match="above monthly_usd"):
        Team(key_sha256=[hash_key(DEMO)], monthly_usd=1.0, daily_usd=2.0, requests_per_minute=1)


# -- the proxy ---------------------------------------------------------------------------------


class App:
    def __init__(self, app: Any, ledger: Path) -> None:
        self.app = app
        self.ledger = ledger
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://p")

    async def post(self, payload: dict[str, Any], key: str = DEMO) -> httpx.Response:
        return await self.http.post(
            "/v1/chat/completions",
            json=payload,
            headers={"authorization": f"Bearer {key}", **PUBLIC},
        )


def ask(**kw: Any) -> dict[str, Any]:
    b: dict[str, Any] = {"model": MODEL, "messages": [{"role": "user", "content": "Hello?"}]}
    b.update(kw)
    return b


@pytest.fixture
async def app(repo_config: BoundaryConfig, tmp_path: Path, keys: None) -> AsyncIterator[App]:
    a = create_app(
        repo_config,
        demo_teams(),
        ledger_path=tmp_path / "proxy.sqlite",
        policy=load_policy(CONFIG_DIR / "policy.yaml"),
        central_path=tmp_path / "central.sqlite",
        demo=Demo(key=DEMO, team="public", base_url="https://gateway.example"),
        asleep=_no_sleep,
        wall=lambda: NOW,
    )
    wrapped = App(a, tmp_path / "proxy.sqlite")
    yield wrapped
    await wrapped.http.aclose()
    await a.state.boundary.close()


@pytest.fixture
def upstream() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        yield router


async def test_a_spent_day_is_a_429_that_says_when_it_resets(
    app: App, upstream: respx.MockRouter
) -> None:
    route = upstream.post(OPENWEIGHTS_URL).mock(return_value=completion())
    store = LedgerStore(app.ledger)
    store.begin(_row("public", 0.2499))
    store.close()
    r = await app.post(ask())
    assert r.status_code == 429
    err = r.json()["error"]
    assert err["code"] == "team_daily_budget" and err["limit_usd"] == 0.25
    # NOW is 2026-09-25 12:00 UTC: the day resets at midnight, twelve hours on.
    assert err["resets_at"] == "2026-09-26T00:00:00Z" and r.headers["retry-after"] == "43200"
    assert route.call_count == 0, "nothing was sent"


async def test_only_the_teams_models_and_at_most_its_answer_length(
    app: App, upstream: respx.MockRouter
) -> None:
    route = upstream.post(OPENWEIGHTS_URL).mock(return_value=completion())
    other = await app.post(ask(model="anthropic/claude-haiku-4-5-20251001"))
    assert other.status_code == 403 and other.json()["error"]["code"] == "model_not_allowed"
    long = await app.post(ask(max_tokens=301))
    assert long.status_code == 400 and long.json()["error"]["code"] == "max_tokens_too_large"
    assert route.call_count == 0
    ok = await app.post(ask())
    assert ok.status_code == 200
    sent = json.loads(route.calls[0].request.content)
    assert sent["max_tokens"] == 300, "a request that asks for no length gets the team's"
    fine = await app.post(ask(max_tokens=50))
    assert (
        fine.status_code == 200 and json.loads(route.calls[1].request.content)["max_tokens"] == 50
    )
    # Another team has none of these limits.
    free = await app.post(ask(max_tokens=2000), key=KEY)
    assert free.status_code == 200


async def test_the_page_publishes_the_key_and_whats_left_today(app: App) -> None:
    store = LedgerStore(app.ledger)
    store.begin(_row("public", 0.05, ts="2026-09-25T09:00:00.000Z"))  # NOW's day
    store.close()
    r = await app.http.get("/demo")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    page = r.text
    assert DEMO in page and "https://gateway.example/v1/chat/completions" in page
    assert "US$0.20</b><span>left today of US$0.25" in page
    assert KEY not in page and OTHER not in page
    assert "/demo" in (await app.http.get("/dashboard")).text


def test_the_page_is_never_built_for_an_unbounded_team_or_a_wrong_key(
    repo_config: BoundaryConfig, tmp_path: Path
) -> None:
    policy = load_policy(CONFIG_DIR / "policy.yaml")

    def build(teams: TeamsConfig, demo: Demo) -> None:
        create_app(repo_config, teams, ledger_path=tmp_path / "l.sqlite", policy=policy, demo=demo)

    with pytest.raises(ConfigError, match="not one of team 'public'"):
        build(demo_teams(), Demo(key=KEY, team="public"))
    with pytest.raises(ConfigError, match="not in the teams file"):
        build(demo_teams(), Demo(key=DEMO, team="nobody"))
    for unset in ("daily_usd", "models", "max_tokens"):
        with pytest.raises(ConfigError, match=unset):
            build(demo_teams(**{unset: None}), Demo(key=DEMO, team="public"))


async def test_no_demo_no_page(repo_config: BoundaryConfig, tmp_path: Path) -> None:
    a = create_app(
        repo_config,
        demo_teams(),
        ledger_path=tmp_path / "l.sqlite",
        policy=load_policy(CONFIG_DIR / "policy.yaml"),
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=a), base_url="http://p") as c:
        assert (await c.get("/demo")).status_code == 404
    await a.state.boundary.close()
