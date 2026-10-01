"""The hosted proxy's shared state on Postgres and Redis (0.28).

These run against a real Postgres with pgvector and a real Redis, named by
BOUNDARY_TEST_DATABASE_URL (an owner's URL; each test gets a database of its own) and
BOUNDARY_TEST_REDIS_URL. Without them they are skipped, and CI's `hosted` job sets both, so
they are never skipped there. What they check is what a mock could not: grants, triggers,
advisory locks and a Lua script, each doing what the module says in the database itself.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
import uuid
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
import pytest
import respx

from boundary.config import BoundaryConfig, CapsConfig, ProjectCap
from boundary.errors import SpendCapExceeded
from boundary.ledger.store import LedgerRow, utc_now
from boundary.semcache import Vector, unit
from boundary.types import ChatRequest

from .conftest import ANTHROPIC_URL, HAIKU, anthropic_ok, make_gateway

ADMIN = os.environ.get("BOUNDARY_TEST_DATABASE_URL", "")
REDIS = os.environ.get("BOUNDARY_TEST_REDIS_URL", "")
pytestmark = pytest.mark.skipif(not ADMIN, reason="BOUNDARY_TEST_DATABASE_URL is not set")

pg = pytest.importorskip("boundary.pg") if ADMIN else None


def _with_db(url: str, db: str, user: str | None = None, password: str | None = None) -> str:
    parts = urlsplit(url)
    netloc = parts.netloc
    if user is not None:
        host = netloc.split("@", 1)[-1]
        netloc = f"{user}:{password}@{host}"
    return urlunsplit((parts.scheme, netloc, f"/{db}", parts.query, parts.fragment))


class Db:
    def __init__(self, admin: str, app: str) -> None:
        self.admin = admin
        self.app = app


@pytest.fixture
def db() -> Iterator[Db]:
    assert pg is not None
    name = f"bnd_test_{uuid.uuid4().hex[:10]}"
    role = f"app_{uuid.uuid4().hex[:8]}"
    password = secrets.token_hex(12)
    with pg.connect(ADMIN) as conn:
        conn.execute(f"CREATE DATABASE {name}")
        conn.execute(f"CREATE ROLE {role} LOGIN PASSWORD '{password}'")
    admin = _with_db(ADMIN, name)
    pg.init_schema(admin, app_role=role)
    try:
        yield Db(admin, _with_db(ADMIN, name, role, password))
    finally:
        with pg.connect(ADMIN) as conn:
            conn.execute(f"DROP DATABASE {name} WITH (FORCE)")
            conn.execute(f"DROP ROLE {role}")


def _row(project: str = "alpha", cost: float | None = 0.5, **kw: Any) -> LedgerRow:
    return LedgerRow(
        ts_utc=utc_now(),
        boundary_version="0.28.0",
        project=project,
        purpose="t",
        mode="standard",
        provider="anthropic",
        model_requested=HAIKU,
        cost_usd=cost,
        **kw,
    )


# -- grants and triggers ---------------------------------------------------------------------


def test_the_proxys_role_cannot_rewrite_history(db: Db) -> None:
    assert pg is not None
    import psycopg

    ledger = pg.PgLedger(db.app)
    log = pg.PgAuditLog(db.app)
    try:
        row = _row()
        ledger.begin(row)
        log.append_bodies(['{"kind":"x"}'])
        with pg.connect(db.app) as conn:
            for sql in (
                "DELETE FROM ledger",
                "UPDATE audit SET body = 'x'",
                "DELETE FROM audit",
                "TRUNCATE audit",
                "DROP TABLE audit",
                "UPDATE ledger_spend SET cost = 0",
            ):
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    conn.execute(sql)
        with pg.connect(db.admin) as conn:  # the owner is stopped by the trigger
            for sql in ("UPDATE audit SET body = 'x'", "DELETE FROM audit", "TRUNCATE audit"):
                with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
                    conn.execute(sql)
    finally:
        ledger.close()
        log.close()


def test_init_is_idempotent(db: Db) -> None:
    assert pg is not None
    pg.init_schema(db.admin)
    pg.init_schema(db.admin)


# -- the ledger ------------------------------------------------------------------------------


def test_spend_counts_the_estimate_then_the_actual(db: Db) -> None:
    assert pg is not None
    ledger = pg.PgLedger(db.app)
    try:
        month = utc_now()[:7]
        row = _row(cost=0.5)
        ledger.begin(row)
        assert ledger.spend_usd(project="alpha", year_month=month) == pytest.approx(0.5)
        row.cost_usd = 0.125
        row.costed = True
        row.http_status = 200
        ledger.complete(row)
        assert ledger.spend_usd(project="alpha", year_month=month) == pytest.approx(0.125)
        assert ledger.spend_usd(project=None, year_month=month) == pytest.approx(0.125)
        assert ledger.spend_usd(project="alpha") == pytest.approx(0.125)
        (held,) = ledger.rows()
        assert held["call_uid"] == row.call_uid and held["error_type"] is None
        assert ledger.rows_after(row.id or 0) == []
    finally:
        ledger.close()


def test_a_redacted_flagged_row_is_written_as_the_sqlite_ledger_holds_it(db: Db) -> None:
    """0.28: the first hosted load test failed every redacted request, because Postgres
    refuses a Python bool in a BIGINT column where SQLite quietly stores 1."""
    assert pg is not None
    ledger = pg.PgLedger(db.app)
    try:
        row = _row(redacted=True, injection=True, cache_similarity=0.91, cache_source="u")
        ledger.begin(row)
        ledger.complete(row)
        (held,) = ledger.rows()
        assert (held["redacted"], held["injection"], held["cache_similarity"]) == (1, 1, 0.91)
        central = pg.PgCentral(db.app)
        d = row.as_columns()
        d["redacted"] = True
        d["call_uid"] = "other"
        central.ingest([d], source="s", env=None, local_rows=1)
        assert central.rows()[0]["redacted"] == 1
        central.close()
    finally:
        ledger.close()


@pytest.mark.parametrize("processes", [2, 1])
async def test_two_processes_cannot_pass_a_cap_together(
    repo_config: BoundaryConfig, tmp_path: Path, keys: None, db: Db, processes: int
) -> None:
    """Two gateways, each with its own connections, as two workers would have, or one whose
    threads each take one of its pooled connections (0.29): a cap that fits three estimates
    admits three of twelve calls sent at once."""
    assert pg is not None
    from boundary.ledger.prices import estimate_usd
    from boundary.providers.anthropic import AnthropicAdapter
    from boundary.routes import resolve
    from boundary.types import Mode

    req = ChatRequest(model=HAIKU, messages=[{"role": "user", "content": "Q?"}], max_tokens=8)
    ref = resolve(HAIKU, repo_config, Mode.STANDARD)
    built = AnthropicAdapter().build_request(ref, req, ref.provider_config, "k")
    from boundary.config import PriceEntry

    one = estimate_usd(len(built.body), 8, PriceEntry(input=1.0, output=5.0))
    caps = CapsConfig(
        version=1,
        portfolio_monthly_usd=1000.0,
        projects={"ai-release-gate": ProjectCap(monthly_usd=one * 3.5)},
    )
    a = make_gateway(repo_config, tmp_path, caps=caps, ledger=pg.PgLedger(db.app))
    b = (
        make_gateway(repo_config, tmp_path, caps=caps, ledger=pg.PgLedger(db.app))
        if processes == 2
        else a
    )

    async def slow(_request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.05)
        return anthropic_ok(input_tokens=10, output_tokens=2)

    try:
        with respx.mock(assert_all_called=False) as mock:
            route = mock.post(ANTHROPIC_URL).mock(side_effect=slow)
            results = await asyncio.gather(
                *((a if i % 2 else b).achat(req, purpose="dev") for i in range(12)),
                return_exceptions=True,
            )
        assert route.call_count == 3
        assert sum(isinstance(r, SpendCapExceeded) for r in results) == 9
    finally:
        a.close()
        if b is not a:
            b.close()


def test_admission_refuses_as_the_sqlite_ledger_does(db: Db, tmp_path: Path) -> None:
    """0.29: the hosted admission is one call to `ledger_admit`, not Python's check under a
    lock. The same calls against both ledgers are admitted or refused alike, naming the
    same cap and the same spend, and a refusal writes no row."""
    assert pg is not None
    from boundary.ledger.store import Admission, LedgerStore

    month = utc_now()[:7]

    def caps(project: str, estimate: float, run_id: str | None = None) -> Admission:
        return Admission(
            project=project,
            month=month,
            estimate=estimate,
            project_monthly_usd=1.0,
            portfolio_monthly_usd=1.5,
            run_id=run_id,
            per_run_usd=0.4 if run_id else None,
            day=utc_now()[:10],
            project_daily_usd=0.08 if project == "gamma" else None,
        )

    calls = [
        ("alpha", 0.3, "r1"),
        ("alpha", 0.2, "r1"),  # the run's 0.4 would be passed
        ("alpha", 0.6, None),
        ("alpha", 0.2, None),  # alpha's 1.0 would be passed
        ("gamma", 0.05, None),
        ("gamma", 0.05, None),  # gamma's day, 0.08, would be passed (0.30)
        ("beta", 0.5, None),
        ("beta", 0.3, None),  # the gateway's 1.5 would be passed
        ("beta", 0.0, None),
    ]

    def outcomes(ledger: Any) -> list[str]:
        got = []
        for project, estimate, run_id in calls:
            try:
                ledger.admit(
                    _row(project, cost=estimate, run_id=run_id), caps(project, estimate, run_id)
                )
                got.append("admitted")
            except SpendCapExceeded as e:
                got.append(str(e))
        return got

    sqlite = LedgerStore(tmp_path / "l.sqlite")
    hosted = pg.PgLedger(db.app)
    try:
        want = outcomes(sqlite)
        assert outcomes(hosted) == want
        assert [w == "admitted" for w in want] == [
            True, False, True, False, True, False, True, False, True,
        ]  # fmt: skip
        assert "'project alpha run r1'" in want[1] and "'project gamma daily'" in want[5]
        assert "'portfolio monthly'" in want[7]
        assert hosted.count() == sqlite.count() == 5
        assert all(r["error_type"] == "in_flight" for r in hosted.rows())
    finally:
        sqlite.close()
        hosted.close()


# -- the audit chain -------------------------------------------------------------------------


def test_the_chain_seals_the_hosted_ledger_and_imports_verbatim(db: Db, tmp_path: Path) -> None:
    assert pg is not None
    from boundary.audit import AuditLog, verify
    from boundary.audit.appender import AuditAppender
    from boundary.ledger.store import LedgerStore

    # A SQLite chain, as the single-process proxy kept it, imported record for record.
    store = LedgerStore(tmp_path / "l.sqlite")
    for _ in range(3):
        r = _row()
        store.begin(r)
        store.complete(r)
    with AuditLog(tmp_path / "a.sqlite") as src:
        src.seal(store.rows())
        old = src.records()
    ledger = pg.PgLedger(db.app)
    log = pg.PgAuditLog(db.app)
    try:
        ledger.merge_rows(store.rows(), source="import")
        assert log.import_records(old) == 3
        assert log.records() == old, "the same records, so published anchors still hold"
        with pytest.raises(ValueError, match="not empty"):
            log.import_records(old)
        # The sealer carries on from the imported head.
        appender = AuditAppender(log, ledger)
        new = _row()
        ledger.begin(new)
        ledger.complete(new)
        assert appender.after_call() == 1
        records = log.records()
        assert len(records) == 4 and records[3].prev_hash == old[2].record_hash
        assert verify(records, ledger_rows=ledger.rows()).ok
    finally:
        ledger.close()
        log.close()
        store.close()


def test_a_broken_chain_is_not_imported(db: Db) -> None:
    assert pg is not None
    from boundary.audit.chain import Record

    log = pg.PgAuditLog(db.app)
    try:
        with pytest.raises(ValueError, match="does not link"):
            log.import_records([Record(1, "0" * 64, "f" * 64, "{}")])
        assert log.head()[0] == 0
    finally:
        log.close()


# -- the central ledger ----------------------------------------------------------------------


def test_central_ingest_by_call_uid(db: Db) -> None:
    assert pg is not None
    central = pg.PgCentral(db.app)
    try:
        rows = []
        for i in range(4):
            r = _row(project="gate", raw_path="/Users/x/raw.json" if i == 0 else None)
            d = r.as_columns()
            d["error_type"] = None
            rows.append(d)
        first = central.ingest(rows[:3], source="s", env="local", local_rows=4)
        assert first.stats.inserted == 3 and not first.source.complete
        again = central.ingest(rows, source="s", env="local", local_rows=4)
        assert (again.stats.inserted, again.stats.skipped, again.source.held) == (1, 3, 4)
        assert all(r["raw_path"] is None for r in central.rows())
        own = pg.PgLedger(db.app)
        dup = _row(project="gate")
        dup.call_uid = rows[0]["call_uid"]
        own.begin(dup)
        own.close()
        merged = pg.dashboard_rows(db.app)
        assert len(merged) == 4, "one row per call across the two tables"
    finally:
        central.close()


# -- the semantic cache on pgvector ----------------------------------------------------------


class Words:
    def embed(self, texts: Sequence[str]) -> list[Vector]:
        out = []
        for text in texts:
            v = [0.0] * 384
            for w in text.lower().replace("?", " ").split():
                v[int(hashlib.sha256(w.encode()).hexdigest(), 16) % 384] += 1.0
            out.append(unit(v))
        return out


def test_the_pgvector_cache_is_per_team_and_per_scope(db: Db) -> None:
    assert pg is not None
    alpha = pg.PgSemanticCache(db.app, Words(), team="alpha", threshold=0.95, max_entries=2)
    beta = pg.PgSemanticCache(db.app, Words(), team="beta", threshold=0.95)
    q = ChatRequest(model="m", messages=[{"role": "user", "content": "how long to dispute"}])
    other_scope = ChatRequest(
        model="m",
        system="Answer in French.",
        messages=[{"role": "user", "content": "how long to dispute"}],
    )
    assert alpha.lookup(q) is None
    assert alpha.store(q, "60 days", source="uid-1", model="m1", finish_reason="stop") is not None
    hit = alpha.lookup(q)
    assert hit is not None and hit.answer == "60 days" and hit.source == "uid-1"
    assert hit.similarity == pytest.approx(1.0, abs=1e-5)
    assert beta.lookup(q) is None, "never another team's answer"
    assert alpha.lookup(other_scope) is None, "never another scope's"
    alpha.store(other_scope, "x")
    assert alpha.store(q, "y") is None, "full at max_entries"


def test_the_index_finds_every_near_duplicate_in_a_full_cache(db: Db) -> None:
    """0.29: the nearest question is found by an HNSW index, which is approximate. What the
    cache needs of it is that a question close enough to hit is found; checked on 4,000
    random unit vectors, a tenth of them another team's, with 200 queries each at a cosine
    of about 0.95 to one stored vector. The plan must be the index, or this checks the scan."""
    assert pg is not None
    import random

    from boundary.semcache import scope_of

    rng = random.Random(29)

    def rand() -> Vector:
        return unit([rng.gauss(0, 1) for _ in range(384)])

    q = ChatRequest(model="m", messages=[{"role": "user", "content": "q"}])
    scope = scope_of(q)
    stored = [rand() for _ in range(4000)]
    with pg.connect(db.admin) as conn, conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO semcache (team, scope, embedding, answer, source, created_utc) "
            "VALUES (%s, %s, %s::vector, %s, %s, %s)",
            [
                ("other" if i % 10 == 0 else "big", scope, pg._vec(v), f"a{i}", f"s{i}", "t")
                for i, v in enumerate(stored)
            ],
        )
        conn.execute("ANALYZE semcache")
    cache = pg.PgSemanticCache(db.app, Words(), team="big", threshold=0.9)
    targets = [i for i in range(4000) if i % 10][:200]
    found = 0
    for i in targets:
        noise = rand()
        v = unit([0.95 * a + 0.31 * b for a, b in zip(stored[i], noise, strict=True)])
        hit = cache.lookup(q, v)
        found += hit is not None and hit.source == f"s{i}"
    assert found == len(targets)
    with cache._lock:
        plan = cache._conn.execute(
            "EXPLAIN SELECT id FROM semcache WHERE team = %s AND scope = %s "
            "ORDER BY embedding <=> %s::vector LIMIT 1",
            ("big", scope, pg._vec(stored[1])),
        ).fetchall()
    assert "semcache_embedding_hnsw" in " ".join(str(r) for r in plan)


# -- Redis quotas ----------------------------------------------------------------------------


@pytest.mark.skipif(not REDIS, reason="BOUNDARY_TEST_REDIS_URL is not set")
def test_the_quota_is_shared_by_every_worker() -> None:
    assert pg is not None
    prefix = f"test:{uuid.uuid4().hex}:"
    one = pg.RedisQuota(REDIS, prefix=prefix)
    two = pg.RedisQuota(REDIS, prefix=prefix)
    got = [(one if i % 2 else two).take("t", 3) for i in range(5)]
    assert [d.allowed for d in got] == [True, True, True, False, False]
    assert got[3].used == 3 and 0 < got[3].retry_after_s <= 60
    assert one.take("other", 1).allowed, "per team"


async def test_the_proxys_quota_is_the_same_window_without_blocking() -> None:
    """0.29: the proxy asks Redis from the event loop, through the same script and window."""
    assert pg is not None
    prefix = f"test:{uuid.uuid4().hex}:"
    one = pg.RedisQuota(REDIS, prefix=prefix)
    two = pg.RedisQuota(REDIS, prefix=prefix)
    assert one.take("t", 3).allowed
    got = await asyncio.gather(*(two.atake("t", 3) for _ in range(4)))
    assert sorted(d.allowed for d in got) == [False, False, True, True]
    assert not one.take("t", 3).allowed


# -- the proxy, hosted -----------------------------------------------------------------------


@pytest.mark.skipif(not REDIS, reason="BOUNDARY_TEST_REDIS_URL is not set")
async def test_the_hosted_proxy_end_to_end(
    repo_config: BoundaryConfig, tmp_path: Path, keys: None, db: Db
) -> None:
    """One worker's app on Postgres and Redis: a call is a row in Postgres, a marked repeat
    is answered from pgvector and names its source, the sealer's chain verifies and is what
    `/audit/head` reports, the quota is Redis's, and the dashboard reads both tables."""
    assert pg is not None
    pytest.importorskip("fastapi")
    from boundary.audit import verify
    from boundary.audit.appender import AuditAppender
    from boundary.enforce import load_policy
    from boundary.server import create_app
    from boundary.server.app import Hosted
    from boundary.server.teams import TeamsConfig, hash_key

    from .conftest import CONFIG_DIR, OPENWEIGHTS_URL
    from .test_server import KEY, MODEL, completion, teams

    t = teams(requests_per_minute=4)
    cfg = TeamsConfig.model_validate({**t.model_dump(), "ingest_key_sha256": [hash_key("bnd_i")]})
    hosted = Hosted(
        db.app,
        REDIS,
        semantic_threshold=0.95,
        embedder=Words(),
        redis_prefix=f"test:{uuid.uuid4().hex}:",
    )
    app = create_app(
        repo_config,
        cfg,
        ledger_path=tmp_path / "unused.sqlite",
        policy=load_policy(CONFIG_DIR / "policy.yaml"),
        hosted=hosted,
    )
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://p")
    auth = {
        "authorization": f"Bearer {KEY}",
        "x-data-class": "public",
        "x-boundary-cache": "question",
    }
    body = {"model": MODEL, "messages": [{"role": "user", "content": "Say hello."}]}
    try:
        with respx.mock(assert_all_called=False) as mock:
            route = mock.post(OPENWEIGHTS_URL).mock(return_value=completion("Hello"))
            first = await http.post("/v1/chat/completions", json=body, headers=auth)
            second = await http.post("/v1/chat/completions", json=body, headers=auth)
            assert first.status_code == second.status_code == 200
            assert first.headers["x-boundary-cache"] == "miss"
            assert second.headers["x-boundary-cache"] == "hit"
            assert second.headers["x-boundary-cache-source"] == first.headers["x-boundary-call-uid"]
            assert route.call_count == 1
            # The quota is Redis's: four a minute, two used above.
            codes = [
                (await http.post("/v1/chat/completions", json=body, headers=auth)).status_code
                for _ in range(3)
            ]
            assert codes == [200, 200, 429]
        ledger = pg.PgLedger(db.app)
        log = pg.PgAuditLog(db.app)
        try:
            rows = ledger.rows()
            assert len(rows) == 4 and sum(r["cached"] for r in rows) == 3
            AuditAppender(log, ledger)  # the sealer's first pass seals what is there
            head = (await http.get("/audit/head")).json()
            assert head["seq"] == 4 and head["head"] == log.head()[1]
            assert verify(log.records(), ledger_rows=ledger.rows()).ok
        finally:
            ledger.close()
            log.close()
        page = await http.get("/dashboard")
        assert page.status_code == 200 and ">4<" in page.text.replace(",", "")
    finally:
        await http.aclose()
        await app.state.boundary.close()
