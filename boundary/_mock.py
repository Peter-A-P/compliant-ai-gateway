"""An in-process upstream for the measurements. No network, no keys, no money.

Shared by `boundary bench` (the library's own overhead and completeness) and
`boundary experiment` (the Rule C evidence in docs/rejected.md) so that both measure the
same code path against the same request corpus and the same instant responses. Private to
the library: nothing here is part of the frozen interface.
"""

from __future__ import annotations

import random
import string
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx

from boundary.config import BoundaryConfig, CapsConfig, ProjectCap
from boundary.gateway import Gateway
from boundary.transport import Transport
from boundary.types import ChatRequest

MODEL = "anthropic/claude-haiku-4-5-20251001"


def ok_body(input_tokens: int = 40, output_tokens: int = 3) -> dict[str, Any]:
    return {
        "id": "msg_bench",
        "type": "message",
        "role": "assistant",
        "model": "claude-haiku-4-5-20251001",
        "content": [{"type": "text", "text": "B"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
    }


class MockUpstream:
    """An httpx transport that answers according to a script, and counts what it sees."""

    def __init__(self, script: Callable[[int], httpx.Response | Exception] | None = None) -> None:
        self.calls = 0
        self._script = script or (lambda _i: httpx.Response(200, json=ok_body()))

    def handler(self, request: httpx.Request) -> httpx.Response:
        i = self.calls
        self.calls += 1
        out = self._script(i)
        if isinstance(out, Exception):
            raise out
        return out

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))


def open_caps(project: str, *, monthly_usd: float = 1000.0) -> CapsConfig:
    return CapsConfig(
        version=1,
        portfolio_monthly_usd=max(monthly_usd, 1000.0),
        projects={project: ProjectCap(monthly_usd=monthly_usd, per_run_usd=monthly_usd)},
    )


@contextmanager
def mock_gateway(
    config: BoundaryConfig,
    work: Path,
    upstream: MockUpstream,
    *,
    caps: CapsConfig | None = None,
    transport: Transport | None = None,
    project: str = "bench",
    env: str = "mock",
) -> Iterator[Gateway]:
    gw = Gateway(
        config,
        project=project,
        ledger_path=work / f"{project}.sqlite",
        raw_store=work / "raw",
        transport=transport or Transport(config.defaults.timeouts, sync_client=upstream.client()),
        caps=caps or open_caps(project),
        env=env,
        sleep=lambda _s: None,
    )
    try:
        yield gw
    finally:
        gw.close()


def sample_request(i: int, rng: random.Random) -> ChatRequest:
    """One request from the corpus: varied lengths, systems, stops and vendor extras, so a
    measurement is not made against one shape of body."""
    words = " ".join(
        "".join(rng.choices(string.ascii_lowercase, k=rng.randint(2, 9)))
        for _ in range(rng.randint(3, 40))
    )
    extra: dict[str, Any] = {}
    if rng.random() < 0.3:
        extra["metadata"] = {"user_id": f"u{rng.randint(1, 999)}"}
    return ChatRequest(
        model=MODEL,
        messages=[{"role": "user", "content": f"{i}: {words}"}],
        system="Answer with the letter only." if rng.random() < 0.5 else None,
        max_tokens=rng.choice([1, 8, 64]),
        temperature=rng.choice([None, 0.0, 0.7]),
        stop=["\n"] if rng.random() < 0.2 else None,
        extra=extra,
    )
