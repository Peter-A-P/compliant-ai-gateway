from __future__ import annotations

import importlib.util
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from boundary.config import BoundaryConfig, load_config
from boundary.gateway import Gateway

REPO = Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO / "config"

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
OPENAI_URL = "https://api.openai.com/v1/chat/completions"
OPENWEIGHTS_URL = "https://api.together.xyz/v1/chat/completions"
BEDROCK_URL = "https://bedrock-runtime.ca-central-1.amazonaws.com/anthropic/v1/messages"

HAIKU = "anthropic/claude-haiku-4-5-20251001"

# The proxy's tests need the `server` extra. Left uncollected without it, so a checkout
# that only wants the library can run the suite; CI installs the extra and checks that the
# import works, so the proxy is never skipped there.
collect_ignore = [] if importlib.util.find_spec("fastapi") else ["test_server.py"]


@pytest.fixture(scope="session")
def repo_config() -> BoundaryConfig:
    """The configuration checked into this repository."""
    return load_config(CONFIG_DIR / "boundary.yaml")


@pytest.fixture
def keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dummy keys in the environment. Tests never touch the real .env."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key-000000000000")
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key-000000000000")
    monkeypatch.setenv("GOOGLE_API_KEY", "test-google-key-000000000000")
    monkeypatch.setenv("OPENWEIGHTS_API_KEY", "test-openweights-key-000000000000")
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "test-bedrock-key-000000000000")


async def _no_sleep(_: float) -> None:
    return None


def make_gateway(
    config: BoundaryConfig, tmp_path: Path, *, raw_store: bool = True, **kwargs: Any
) -> Gateway:
    return Gateway(
        config,
        project=kwargs.pop("project", "ai-release-gate"),
        ledger_path=tmp_path / "ledger.sqlite",
        raw_store=(tmp_path / "raw") if raw_store else None,
        sleep=lambda _s: None,
        asleep=_no_sleep,
        **kwargs,
    )


@pytest.fixture
def gw(repo_config: BoundaryConfig, tmp_path: Path, keys: None) -> Iterator[Gateway]:
    g = make_gateway(repo_config, tmp_path)
    yield g
    g.close()


def anthropic_ok(
    *, text: str = "B", input_tokens: int = 40, output_tokens: int = 3, model: str | None = None
) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "msg_01",
            "type": "message",
            "role": "assistant",
            "model": model or "claude-haiku-4-5-20251001",
            "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
        },
        headers={"request-id": "req_abc", "anthropic-ratelimit-requests-remaining": "49"},
    )


def openai_ok(
    *, text: str = "B", prompt: int = 40, completion: int = 3, cached: int = 0
) -> httpx.Response:
    usage: dict[str, Any] = {"prompt_tokens": prompt, "completion_tokens": completion}
    if cached:
        usage["prompt_tokens_details"] = {"cached_tokens": cached}
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-1",
            "object": "chat.completion",
            "model": "gpt-x-2026-01-01",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": "stop",
                }
            ],
            "usage": usage,
        },
        headers={"x-request-id": "req_xyz"},
    )
