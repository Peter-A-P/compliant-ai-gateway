"""The adversarial suite for the data policy: every way a call could reach a provider.

PLAN.md B10 asks for "residency violations zero on the adversarial suite, with correct
refusals". This builds the suite from the configuration itself, so it covers every provider
entry and every alias the operator actually has, and drives each case through a real
`Gateway` against an in-process upstream that counts what reaches it:

- **Targets**: every provider entry by its explicit `provider/model` form, and every alias,
  which is how a call arrives at a provider it did not name.
- **Classes**: the four declared classes, no declaration at all, and five malformed ones
  (`PERSONAL`, ` personal`, `Personal`, `secret`, the empty string).
- **Entry points**: `chat` in standard and pass-through mode, `chat_stream`, `batch_submit`
  and `raw`, because a policy checked on one door and not another is not a policy.

The expected answer comes from an **oracle written separately** from `boundary.enforce`: it
reads the policy file as plain YAML and applies the rules with set arithmetic, sharing no
code with the pydantic model or `decide`. It is written by the same hand, which is the
honest limit of an independent check inside one repository.

A **violation** is a case the oracle refuses that put even one request on the wire. A
**false refusal** is a case the oracle allows that the policy refused. An **unaudited
refusal** is a policy refusal with no `policy_refused` ledger row for it.

`run_proxy` (0.13) is the same suite through the proxy, over HTTP, which is where Part B's
B10 asks for it: the class arrives as an `X-Data-Class` header rather than an argument, and
the proxy has rules of its own at the door (absent or blank means `personal`, case and
surrounding space are forgiven, any other word is a 400). The oracle models those rules in
`door`, again sharing no code with `boundary.server`, and the two proxy entry points are a
plain call and a stream. Pass-through, batches and `raw` are not proxy entry points.
"""

from __future__ import annotations

import datetime as dt
import os
import tempfile
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from boundary._mock import MockUpstream, open_caps
from boundary.config import BoundaryConfig
from boundary.enforce import load_policy
from boundary.errors import BoundaryError, PolicyRefused
from boundary.gateway import POLICY_REFUSED, Gateway
from boundary.redact.evaluate import Rate
from boundary.transport import Transport
from boundary.types import ChatRequest, DataClass, Mode

MALFORMED = ("PERSONAL", " personal", "Personal", "secret", "")
CLASSES: tuple[str | None, ...] = (None, *(c.value for c in DataClass), *MALFORMED)
ENTRY_POINTS = ("chat", "chat-passthrough", "chat_stream", "batch_submit", "raw")
_RANK = {"single-region": 0, "geo": 1, "global": 2}


PROXY_ENTRY_POINTS = ("proxy-chat", "proxy-stream")
# Header values: absent, blank, the vocabulary, and forms the proxy forgives or refuses.
PROXY_CLASSES: tuple[str | None, ...] = (
    None,
    "",
    "   ",
    *(c.value for c in DataClass),
    *MALFORMED[:4],
    "PUBLIC",
    "pii",
)
_INVALID = "<invalid>"


def door(header: str | None) -> str | None:
    """What the proxy's documented rules make of an `X-Data-Class` header: None stands for
    the class the proxy substitutes for no claim, which is `personal`, and `_INVALID` for a
    word it refuses with a 400."""
    if header is None or not header.strip():
        return "personal"
    word = header.strip().lower()
    return word if word in {c.value for c in DataClass} else _INVALID


def oracle(
    raw_policy: dict[str, Any],
    data_class: str | None,
    residency: str | None,
    region: str | None,
    provider: str,
) -> bool:
    """Whether the policy file, read as plain data, allows this call."""
    if data_class is not None and data_class not in {c.value for c in DataClass}:
        return False
    effective = data_class if data_class is not None else raw_policy.get("undeclared", "personal")
    rule = (raw_policy.get("classes") or {}).get(effective)
    if rule is None:
        return False
    limit = rule.get("max_residency")
    if limit is not None and (residency not in _RANK or _RANK[residency] > _RANK[limit]):
        return False
    regions = rule.get("regions")
    if regions is not None and (region or "").casefold() not in {r.casefold() for r in regions}:
        return False
    providers = rule.get("providers")
    return not (providers is not None and provider not in providers)


@dataclass(frozen=True, slots=True)
class Target:
    model: str  # what the caller passes: provider/model or an alias
    provider: str
    region: str | None
    residency: str | None
    alias: bool


@dataclass
class Outcome:
    target: Target
    data_class: str | None
    entry: str
    expected_allowed: bool
    sent: bool
    refused_by_policy: bool
    refused_otherwise: str  # the error type when something else stopped the call
    audited_rows: int


@dataclass
class PolicyEvalResults:
    boundary_version: str
    ran_utc: str
    policy: str
    outcomes: list[Outcome] = field(default_factory=list)

    @property
    def forbidden(self) -> list[Outcome]:
        return [o for o in self.outcomes if not o.expected_allowed]

    @property
    def violations(self) -> Rate:
        f = self.forbidden
        return Rate(sum(1 for o in f if o.sent), len(f))

    @property
    def policy_refusals(self) -> list[Outcome]:
        return [o for o in self.outcomes if o.refused_by_policy]

    @property
    def false_refusals(self) -> Rate:
        allowed = [o for o in self.outcomes if o.expected_allowed]
        return Rate(sum(1 for o in allowed if o.refused_by_policy), len(allowed))

    @property
    def audited(self) -> Rate:
        r = self.policy_refusals
        return Rate(sum(1 for o in r if o.audited_rows > 0), len(r))

    def table(self) -> str:
        f = self.forbidden
        stopped = Counter(
            "policy" if o.refused_by_policy else (o.refused_otherwise or "SENT") for o in f
        )
        by_entry = Counter(o.entry for o in f if o.refused_by_policy)
        lines = [
            f"boundary {self.boundary_version}, policy {self.policy}: {len(self.outcomes)} "
            f"cases, {len(f)} of which the policy forbids",
            "",
            f"violations (forbidden and sent)   {self.violations}   "
            f"({self.violations.hits} of {self.violations.total})",
            f"false refusals (allowed, refused) {self.false_refusals}",
            f"policy refusals audited           {self.audited}",
            "",
            "forbidden cases, by what stopped them: "
            + ", ".join(f"{k} {v}" for k, v in stopped.most_common()),
            "policy refusals by entry point: "
            + ", ".join(f"{k} {v}" for k, v in sorted(by_entry.items())),
            "",
            "Residency here is the operator's declaration. A clean run means every call went "
            "to an endpoint whose declared limit fits its class, not that a vendor kept to it.",
        ]
        return "\n".join(lines)

    def readme_row(self) -> str:
        return (
            f"| {self.violations.hits} of {self.violations.total} forbidden cases sent, "
            f"{self.violations} | {self.false_refusals.hits} of {self.false_refusals.total} "
            f"allowed cases refused | {self.audited.hits} of {self.audited.total} refusals on "
            f"the ledger |"
        )


def targets(config: BoundaryConfig, models: dict[str, str]) -> list[Target]:
    out: list[Target] = []
    for name, pc in sorted(config.providers.items()):
        model = models.get(name, f"{name}/policy-eval-model")
        residency = pc.residency.value if pc.residency else None
        out.append(Target(model, name, pc.region, residency, alias=False))
    for alias, route in sorted(config.routes.items()):
        pc = config.providers[route.provider]
        residency = pc.residency.value if pc.residency else None
        out.append(Target(alias, route.provider, route.region or pc.region, residency, True))
    return out


@contextmanager
def _dummy_credentials(config: BoundaryConfig) -> Iterator[None]:
    """Every key the configuration names, set to a dummy for the length of the run, so that
    a case is stopped by the policy or reaches the mock, never by a missing key."""
    names = {pc.api_key_env for pc in config.providers.values() if pc.api_key_env}
    names.add("GOOGLE_VERTEX_ACCESS_TOKEN")
    saved = {n: os.environ.get(n) for n in names}
    for n in names:
        os.environ[n] = "policy-eval-dummy"
    try:
        yield
    finally:
        for n, v in saved.items():
            if v is None:
                os.environ.pop(n, None)
            else:
                os.environ[n] = v


def _call(gw: Gateway, entry: str, target: Target, data_class: str | None) -> None:
    req = ChatRequest(
        model=target.model, messages=[{"role": "user", "content": "policy eval"}], max_tokens=8
    )
    if entry == "chat":
        gw.chat(req, purpose="policy-eval", data_class=data_class)
    elif entry == "chat-passthrough":
        gw.chat(req, purpose="policy-eval", mode=Mode.PASSTHROUGH, data_class=data_class)
    elif entry == "chat_stream":
        gw.chat_stream(req, purpose="policy-eval", data_class=data_class)
    elif entry == "batch_submit":
        gw.batch_submit([req, req], purpose="policy-eval", data_class=data_class)
    elif entry == "raw":
        gw.raw(
            target.provider,
            "POST",
            "v1/policy-eval",
            {"x": 1},
            purpose="policy-eval",
            data_class=data_class,
        )


def run(config: BoundaryConfig, policy_path: Path, *, models: dict[str, str]) -> PolicyEvalResults:
    from boundary import __version__

    policy = load_policy(policy_path)
    raw_policy = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
    results = PolicyEvalResults(
        boundary_version=__version__,
        ran_utc=dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        policy=policy_path.name,
    )
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp, _dummy_credentials(config):
        work = Path(tmp)
        n = 0
        for target in targets(config, models):
            for data_class in CLASSES:
                for entry in ENTRY_POINTS:
                    n += 1
                    upstream = MockUpstream()
                    gw = Gateway(
                        config,
                        project="policy-eval",
                        ledger_path=work / f"{n}.sqlite",
                        raw_store=work / "raw",
                        transport=Transport(
                            config.defaults.timeouts, sync_client=upstream.client()
                        ),
                        caps=open_caps("policy-eval"),
                        env="policy-eval",
                        sleep=lambda _s: None,
                        policy=policy,
                    )
                    refused, other = False, ""
                    try:
                        _call(gw, entry, target, data_class)
                    except PolicyRefused:
                        refused = True
                    except (BoundaryError, ValueError) as e:
                        other = type(e).__name__
                    except Exception as e:
                        # Anything else that stopped a call after it was sent (a mock answer an
                        # adapter cannot parse) is recorded by name; what counts here is
                        # whether the upstream saw a request, and the mock says that.
                        other = type(e).__name__
                    finally:
                        rows = [
                            r for r in gw.ledger.rows() if r.get("error_type") == POLICY_REFUSED
                        ]
                        gw.close()
                    results.outcomes.append(
                        Outcome(
                            target=target,
                            data_class=data_class,
                            entry=entry,
                            expected_allowed=oracle(
                                raw_policy,
                                data_class,
                                target.residency,
                                target.region,
                                target.provider,
                            ),
                            sent=upstream.calls > 0,
                            refused_by_policy=refused,
                            refused_otherwise=other,
                            audited_rows=len(rows),
                        )
                    )
    return results


def run_proxy(
    config: BoundaryConfig, policy_path: Path, *, models: dict[str, str]
) -> PolicyEvalResults:
    """The suite through `boundary.server` over HTTP, against a counting mock upstream.
    Needs the `server` extra."""
    import asyncio

    import httpx

    from boundary import __version__
    from boundary.server import create_app
    from boundary.server.teams import Team, TeamsConfig, hash_key

    policy = load_policy(policy_path)
    raw_policy = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
    results = PolicyEvalResults(
        boundary_version=__version__,
        ran_utc=dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        policy=f"{policy_path.name} through the proxy",
    )
    key = "bnd_policy-eval-key"
    team = TeamsConfig(
        version=1,
        gateway_monthly_usd=1000.0,
        teams={
            "policy-eval": Team(
                key_sha256=[hash_key(key)], monthly_usd=1000.0, requests_per_minute=10**9
            )
        },
    )

    async def drive(work: Path) -> None:
        upstream = MockUpstream()
        app = create_app(
            config,
            team,
            ledger_path=work / "proxy.sqlite",
            env="policy-eval",
            policy=policy,
            transport=Transport(
                config.defaults.timeouts,
                async_client=httpx.AsyncClient(transport=httpx.MockTransport(upstream.handler)),
            ),
            sleep=lambda _s: None,
        )
        state = app.state.boundary
        ledger = state.gateways["policy-eval"].ledger
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://policy-eval"
        ) as client:
            for target in targets(config, models):
                for header in PROXY_CLASSES:
                    for entry in PROXY_ENTRY_POINTS:
                        before_calls = upstream.calls
                        before_rows = len(ledger.rows())
                        headers = {"authorization": f"Bearer {key}"}
                        if header is not None:
                            headers["x-data-class"] = header
                        body: dict[str, Any] = {
                            "model": target.model,
                            "messages": [{"role": "user", "content": "policy eval"}],
                            "max_tokens": 8,
                            "stream": entry == "proxy-stream",
                        }
                        r = await client.post("/v1/chat/completions", json=body, headers=headers)
                        error = r.json().get("error", {}) if r.status_code != 200 else {}
                        refused = r.status_code == 403 and error.get("type") == POLICY_REFUSED
                        new_rows = ledger.rows()[before_rows:]
                        judged = door(header)
                        results.outcomes.append(
                            Outcome(
                                target=target,
                                data_class=header,
                                entry=entry,
                                expected_allowed=judged != _INVALID
                                and oracle(
                                    raw_policy,
                                    judged,
                                    target.residency,
                                    target.region,
                                    target.provider,
                                ),
                                sent=upstream.calls > before_calls,
                                refused_by_policy=refused,
                                refused_otherwise=""
                                if refused or r.status_code == 200
                                else f"http_{r.status_code}",
                                audited_rows=sum(
                                    1 for row in new_rows if row["error_type"] == POLICY_REFUSED
                                ),
                            )
                        )
        await state.close()

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp, _dummy_credentials(config):
        asyncio.run(drive(Path(tmp)))
    return results


README_START = "<!-- policy:start -->"
README_END = "<!-- policy:end -->"


PROXY_README_START = "<!-- policy-proxy:start -->"
PROXY_README_END = "<!-- policy-proxy:end -->"


def write_readme(readme: Path, row: str, *, proxy: bool = False) -> None:
    start_mark, end_mark = (
        (PROXY_README_START, PROXY_README_END) if proxy else (README_START, README_END)
    )
    text = readme.read_text(encoding="utf-8")
    start = text.index(start_mark)
    end = text.index(end_mark)
    readme.write_text(
        text[: start + len(start_mark)] + "\n" + row + "\n" + text[end:], encoding="utf-8"
    )


__all__ = [
    "CLASSES",
    "ENTRY_POINTS",
    "MALFORMED",
    "PROXY_CLASSES",
    "PROXY_ENTRY_POINTS",
    "Outcome",
    "PolicyEvalResults",
    "Target",
    "door",
    "oracle",
    "run",
    "run_proxy",
    "targets",
    "write_readme",
]
