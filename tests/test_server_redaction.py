"""Redaction in the proxy (0.15, PLAN.md B2.3's proxy half).

A personal request is redacted before it leaves, routed as `internal`, and its answer
rehydrated on the way back, streamed or not. The upstream in these tests echoes what it was
sent, so a correct round trip gives the client back exactly its own text, and anything
personal in the upstream's copy is a leak the test can see.
"""

from __future__ import annotations

import json
import random
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from boundary.config import BoundaryConfig
from boundary.redact.policy import Policy
from boundary.server.redaction import PRESERVE_LINE, StreamRehydrator, redact_request
from boundary.types import ChatRequest

from .test_server import LOCAL_URL, Proxy, _Stream, completion, teams

FOUNDRY_CA = "foundry-canada/gpt-5.6-luna"
FOUNDRY_URL = "https://poc-foundry-cc.services.ai.azure.com/openai/v1/chat/completions"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"

MESSAGE = (
    "Please write to Marie Chaulk at marie.chaulk@example.gov.nl.ca or 709-555-0142 about "
    "file 2024-ATIP-0117. Marie asked for the records on 3 March."
)
SECRETS = ("Chaulk", "marie.chaulk@example.gov.nl.ca", "709-555-0142", "2024-ATIP-0117")


def _sent_user(request: httpx.Request) -> str:
    body = json.loads(request.content)
    return str([m for m in body["messages"] if m["role"] == "user"][-1]["content"])


def _echo(request: httpx.Request) -> httpx.Response:
    return completion(_sent_user(request), model="gpt-5.6-luna")


def _echo_stream(request: httpx.Request) -> httpx.Response:
    """Streams the sent text back three characters at a time, so placeholders arrive split."""
    text = _sent_user(request)
    events = [
        b"data: "
        + json.dumps(
            {
                "id": "s",
                "model": "gpt-5.6-luna",
                "choices": [
                    {"index": 0, "delta": {"content": text[i : i + 3]}, "finish_reason": None}
                ],
            }
        ).encode()
        + b"\n\n"
        for i in range(0, len(text), 3)
    ]
    events.append(
        b'data: {"id":"s","model":"gpt-5.6-luna","choices":[{"index":0,"delta":{},'
        b'"finish_reason":"stop"}]}\n\n'
    )
    events.append(
        b'data: {"id":"s","choices":[],"usage":{"prompt_tokens":50,"completion_tokens":20}}\n\n'
    )
    events.append(b"data: [DONE]\n\n")
    return httpx.Response(
        200, stream=_Stream(events), headers={"content-type": "text/event-stream"}
    )


@pytest.fixture
def upstream(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("AZURE_FOUNDRY_CANADA_API_KEY", "test-foundry-key-000000000000")
    with respx.mock(assert_all_called=False) as router:
        yield router


@pytest.fixture
async def proxy(
    repo_config: BoundaryConfig, tmp_path: Path, keys: None, upstream: Any
) -> AsyncIterator[Proxy]:
    p = Proxy(repo_config, tmp_path, teams())
    yield p
    await p.close()


def body(**kw: Any) -> dict[str, Any]:
    b: dict[str, Any] = {
        "model": FOUNDRY_CA,
        "messages": [{"role": "user", "content": MESSAGE}],
        "max_tokens": 200,
    }
    b.update(kw)
    return b


async def test_a_personal_request_leaves_redacted_and_comes_back_whole(
    proxy: Proxy, upstream: respx.MockRouter
) -> None:
    route = upstream.post(FOUNDRY_URL).mock(side_effect=_echo)
    r = await proxy.post(body())  # no X-Data-Class: personal
    assert r.status_code == 200, r.text
    sent = route.calls.last.request
    sent_body = json.loads(sent.content)
    wire_text = sent.content.decode()
    for value in SECRETS:
        assert value not in wire_text, f"{value!r} left the boundary"
    assert "<" in _sent_user(sent) and "_1>" in _sent_user(sent)
    system = [m for m in sent_body["messages"] if m["role"] == "system"]
    assert system and PRESERVE_LINE in system[0]["content"]

    assert r.json()["choices"][0]["message"]["content"] == MESSAGE
    assert r.headers["x-boundary-redacted"] == "true"
    assert int(r.headers["x-boundary-placeholders"]) >= 4
    assert r.headers["x-boundary-unresolved"] == "0"
    (row,) = proxy.rows()
    assert row["data_class"] == "personal" and row["redacted"] == 1
    assert row["provider"] == "foundry-canada" and row["http_status"] == 200


async def test_a_streamed_answer_is_rehydrated_across_split_placeholders(
    proxy: Proxy, upstream: respx.MockRouter
) -> None:
    upstream.post(FOUNDRY_URL).mock(side_effect=_echo_stream)
    r = await proxy.post(body(stream=True))
    assert r.status_code == 200, r.text
    assert r.headers["x-boundary-stream"] == "native"
    text = "".join(
        c["delta"].get("content") or ""
        for line in r.text.split("\n\n")
        if line.startswith("data: {")
        for c in json.loads(line[6:]).get("choices", [])
    )
    assert text == MESSAGE
    (row,) = proxy.rows()
    assert row["redacted"] == 1 and row["ttft_ms"] is not None


async def test_the_same_person_is_one_placeholder_across_turns(
    proxy: Proxy, upstream: respx.MockRouter
) -> None:
    route = upstream.post(FOUNDRY_URL).mock(side_effect=_echo)
    messages = [
        {"role": "user", "content": "Who is Marie Chaulk?"},
        {"role": "assistant", "content": "Marie Chaulk is the applicant."},
        {"role": "user", "content": "Write to Marie Chaulk."},
    ]
    r = await proxy.post(body(messages=messages))
    assert r.status_code == 200, r.text
    sent = [m["content"] for m in json.loads(route.calls.last.request.content)["messages"]]
    tokens = [set(re.findall(r"<[A-Z_]+_\d+(?:\.\d+)?>", t)) for t in sent if "<" in t]
    assert len(tokens) >= 3 and set.intersection(*tokens), "one person, one placeholder"


async def test_public_data_is_sent_as_written(proxy: Proxy, upstream: respx.MockRouter) -> None:
    route = upstream.post(FOUNDRY_URL).mock(side_effect=_echo)
    r = await proxy.post(body(), headers={"X-Data-Class": "public"})
    assert r.status_code == 200
    assert r.headers["x-boundary-redacted"] == "false"
    wire_text = route.calls.last.request.content.decode()
    assert "Chaulk" in wire_text and PRESERVE_LINE not in wire_text
    assert proxy.rows()[0]["redacted"] is None


async def test_redacted_personal_data_still_cannot_reach_a_provider_declaring_no_residency(
    proxy: Proxy, upstream: respx.MockRouter
) -> None:
    route = upstream.post(ANTHROPIC_URL).mock(return_value=completion())
    r = await proxy.post(body(model="fast"))
    assert r.status_code == 403
    assert "judged as internal" in r.json()["error"]["message"]
    assert route.call_count == 0
    (row,) = proxy.rows()
    assert row["error_type"] == "policy_refused" and row["redacted"] == 1


async def test_sensitive_data_is_not_redacted_and_stays_local(
    proxy: Proxy, upstream: respx.MockRouter
) -> None:
    upstream.post(LOCAL_URL).mock(return_value=completion(model="llama3.2:3b"))
    r = await proxy.post(body(model="local/llama3.2:3b"), headers={"X-Data-Class": "sensitive"})
    assert r.status_code == 200
    assert r.headers["x-boundary-redacted"] == "false"
    r = await proxy.post(body(), headers={"X-Data-Class": "sensitive"})
    assert r.status_code == 403


async def test_the_guard_refuses_rather_than_sends(
    proxy: Proxy, upstream: respx.MockRouter
) -> None:
    """A source text carrying a placeholder the policy is about to mint for a real value is
    ambiguous on the way back (0.5.3), so the guard refuses the whole request."""
    route = upstream.post(FOUNDRY_URL).mock(side_effect=_echo)
    r = await proxy.post(
        body(messages=[{"role": "user", "content": "Reply to <EMAIL_1> and to bob@example.com."}])
    )
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["type"] == "redaction_refused"
    assert err["findings"] == {"placeholder:EMAIL": 1}
    assert "bob@example.com" not in r.text
    assert route.call_count == 0
    # On the record since 0.16: a compliance refusal belongs in the ledger, with no content.
    (row,) = proxy.rows()
    assert row["id"] == err["ledger_id"]
    assert row["error_type"] == "redaction_refused" and row["data_class"] == "personal"
    assert row["request_sha256"] is None and row["cost_usd"] == 0.0 and row["redacted"] is None
    assert row["http_status"] is None and row["provider"] == "foundry-canada"


# -- the stream rehydrator -----------------------------------------------------------------


def _policy_and_forms() -> tuple[Policy, list[str]]:
    red = redact_request(
        ChatRequest(model="m", messages=[{"role": "user", "content": MESSAGE}], system="S")
    )
    minted = sorted(red.policy.vault)
    forms: list[str] = []
    for p in minted:
        inner = p.strip("<>")
        forms += [p, p.lower(), inner, f"< {inner} >", f"[{inner}]", f"<{inner.replace('_', ' ')}>"]
    forms += ["<NAME_LIKE_999>", "<", ">", "a < b", "x_1", "E_", "2.", "Mr."]
    return red.policy, forms


@pytest.mark.parametrize("seed", range(60))
def test_the_stream_rehydrator_equals_rehydrating_the_whole(seed: int) -> None:
    """Random texts of placeholders in the forms models write, words and stray brackets,
    cut at random points: what comes out, joined, is exactly the whole text rehydrated."""
    policy, forms = _policy_and_forms()
    rng = random.Random(seed)
    words = ["the", "file", "Dear", ",", ".", " ", "\n", "and", "records", "-", "1", "_"]
    text = "".join(rng.choice(forms if rng.random() < 0.4 else words) for _ in range(60))
    cuts = sorted(rng.sample(range(1, len(text)), k=min(len(text) - 1, rng.randint(1, 25))))
    pieces = [text[a:b] for a, b in zip([0, *cuts], [*cuts, len(text)], strict=True)]
    rh = StreamRehydrator(policy)
    out = "".join(rh.feed(p) for p in pieces) + rh.flush()
    assert out == policy.rehydrate(text)


def test_an_unclosed_bracket_is_not_held_forever() -> None:
    policy, _ = _policy_and_forms()
    rh = StreamRehydrator(policy)
    released = rh.feed("if a < b then" + " x" * 50)
    assert released.startswith("if a < b then"), "a long-open < is ordinary text"
