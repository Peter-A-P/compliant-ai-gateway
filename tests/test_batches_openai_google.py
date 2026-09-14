"""Goldens for the OpenAI-shaped and Gemini batch endpoints.

Shapes taken from the vendors' own documentation, read 2026-09-14:
    https://developers.openai.com/api/docs/guides/batch
    https://docs.together.ai/docs/batch-inference
    https://ai.google.dev/gemini-api/docs/batch-api

No network. The live exercise of each is a separate step and is recorded in the ledger when
it happens.

The two shapes fail in different places, so they are tested for different things. The
OpenAI one submits in two round trips and names its results by a file id, so what matters is
that the rows are in flight before the *upload* and that a vendor-supplied id cannot become a
path. The Gemini one submits inline and returns its results inside the status response, so
what matters is that a response is never matched to the wrong request.
"""

from __future__ import annotations

import json

import pytest

from boundary.config import ProviderConfig, ProviderKind
from boundary.errors import ProviderError
from boundary.providers.base import BatchItem
from boundary.providers.google_batch import MAX_INLINE_BYTES, GoogleBatchAdapter
from boundary.providers.openai_batch import OpenAICompatBatchAdapter
from boundary.routes import ModelRef
from boundary.types import ChatRequest

OPENAI_BASE = "https://api.openai.com/v1"
TOGETHER_BASE = "https://api.together.xyz/v1"
GOOGLE_BASE = "https://generativelanguage.googleapis.com"


def _openai_provider(base: str = OPENAI_BASE, **kw: object) -> ProviderConfig:
    defaults: dict[str, object] = {
        "kind": ProviderKind.OPENAI_COMPAT,
        "base_url": base,
        "api_key_env": "OPENAI_API_KEY",
        "batches": True,
    }
    defaults.update(kw)
    return ProviderConfig(**defaults)


def _google_provider(**kw: object) -> ProviderConfig:
    defaults: dict[str, object] = {
        "kind": ProviderKind.GOOGLE,
        "base_url": GOOGLE_BASE,
        "api_key_env": "GOOGLE_API_KEY",
        "api_version": "v1beta",
    }
    defaults.update(kw)
    return ProviderConfig(**defaults)


def _items(provider: ProviderConfig, n: int = 2, model: str = "gpt-5-nano") -> list[BatchItem]:
    out: list[BatchItem] = []
    for i in range(n):
        ref = ModelRef(provider="openai", model=model, provider_config=provider)
        req = ChatRequest(
            model=f"openai/{model}",
            messages=[{"role": "user", "content": f"q{i}"}],
            max_tokens=16,
        )
        out.append((f"uid-{i}", ref, req))
    return out


# -- OpenAI-shaped: the input file ------------------------------------------------------


def test_input_file_is_one_whole_request_per_line() -> None:
    a = OpenAICompatBatchAdapter()
    p = _openai_provider()
    payload = a.input_file_bytes(_items(p, 2), p)
    lines = [json.loads(x) for x in payload.splitlines()]
    assert len(lines) == 2
    assert lines[0]["custom_id"] == "uid-0"
    assert lines[0]["method"] == "POST"
    assert lines[0]["url"] == "/v1/chat/completions"
    assert lines[0]["body"]["model"] == "gpt-5-nano"
    assert lines[0]["body"]["messages"] == [{"role": "user", "content": "q0"}]


def test_a_batched_body_is_byte_identical_to_a_single_call_body() -> None:
    """The same prompt costs the same request hash batched or not.

    If these drift, a batched call and a single call to the same prompt stop being
    comparable in the ledger, and the byte-equality argument that pass-through rests on
    would not extend to batches.
    """
    a = OpenAICompatBatchAdapter()
    p = _openai_provider()
    items = _items(p, 1)
    _, ref, req = items[0]
    single = a.build_request(ref, req, p, "k")
    line = json.loads(a.input_file_bytes(items, p).splitlines()[0])
    assert json.loads(single.body) == line["body"]


def test_upload_is_multipart_with_purpose_batch_and_carries_no_key_when_redacted() -> None:
    a = OpenAICompatBatchAdapter()
    p = _openai_provider()
    built = a.build_batch_upload(_items(p, 1), p, "sk-secret-123")
    assert built.method == "POST"
    assert built.url == f"{OPENAI_BASE}/files"
    assert built.headers["content-type"].startswith("multipart/form-data; boundary=")
    assert b'name="purpose"' in built.body
    assert b"batch" in built.body
    assert b'filename="batch.jsonl"' in built.body
    assert "sk-secret-123" not in json.dumps(built.redacted_headers())


def test_upload_bytes_are_deterministic() -> None:
    """Two builds of the same items produce the same bytes, so a retry resends the same file."""
    a = OpenAICompatBatchAdapter()
    p = _openai_provider()
    items = _items(p, 3)
    assert a.build_batch_upload(items, p, "k").body == a.build_batch_upload(items, p, "k").body


def test_create_names_the_uploaded_file() -> None:
    a = OpenAICompatBatchAdapter()
    p = _openai_provider()
    built = a.build_batch_create("file-abc123", p, "k")
    assert built.url == f"{OPENAI_BASE}/batches"
    body = json.loads(built.body)
    assert body == {
        "input_file_id": "file-abc123",
        "endpoint": "/v1/chat/completions",
        "completion_window": "24h",
    }


def test_build_batch_submit_refuses_rather_than_guessing() -> None:
    """This shape uploads first. Calling the inline entry point means the gateway is wrong."""
    a = OpenAICompatBatchAdapter()
    p = _openai_provider()
    with pytest.raises(ProviderError, match="uploads its requests as a file first"):
        a.build_batch_submit(_items(p, 1), p, "k")


# -- OpenAI-shaped: status and results --------------------------------------------------


def test_status_reports_the_output_file_id_and_whether_it_ended() -> None:
    a = OpenAICompatBatchAdapter()
    running = json.dumps({"id": "batch_1", "status": "in_progress"}).encode()
    assert not a.parse_batch_status(200, {}, running).ended

    done = json.dumps(
        {
            "id": "batch_1",
            "status": "completed",
            "output_file_id": "file-out-1",
            "request_counts": {"total": 2, "completed": 2, "failed": 0},
        }
    ).encode()
    p = a.parse_batch_status(200, {}, done)
    assert p.ended
    assert p.results_url == "file-out-1"
    assert p.counts["completed"] == 2


def test_a_failed_batch_ends_without_an_output_file() -> None:
    a = OpenAICompatBatchAdapter()
    body = json.dumps({"id": "batch_1", "status": "failed"}).encode()
    p = a.parse_batch_status(200, {}, body)
    assert p.ended and p.results_url is None


@pytest.mark.parametrize(
    "bad_id",
    [
        "../../secrets",
        "https://evil.invalid/steal",
        "file/../..",
        "a b",
        "",
    ],
)
def test_a_file_id_that_is_not_a_plain_id_is_refused_before_the_key_is_sent(bad_id: str) -> None:
    """The id becomes part of a path on a request that carries the API key."""
    a = OpenAICompatBatchAdapter()
    p = _openai_provider()
    with pytest.raises(ProviderError, match="not a plain id"):
        a.build_batch_results(bad_id, p, "k")


def test_results_url_is_built_on_the_configured_host() -> None:
    a = OpenAICompatBatchAdapter()
    built = a.build_batch_results("file-out-1", _openai_provider(), "k")
    assert built.method == "GET"
    assert built.url == f"{OPENAI_BASE}/files/file-out-1/content"


def test_results_parse_successes_errors_and_non_2xx_lines() -> None:
    a = OpenAICompatBatchAdapter()
    ok = {
        "id": "b1",
        "custom_id": "uid-0",
        "response": {
            "status_code": 200,
            "body": {
                "model": "gpt-5-nano",
                "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2},
            },
        },
        "error": None,
    }
    failed = {
        "id": "b2",
        "custom_id": "uid-1",
        "response": None,
        "error": {"code": "rate_limit", "message": "slow down"},
    }
    http_err = {
        "id": "b3",
        "custom_id": "uid-2",
        "response": {"status_code": 400, "body": {}},
        "error": None,
    }
    body = ("\n".join(json.dumps(x) for x in (ok, failed, http_err))).encode()

    out = {r.custom_id: r for r in a.parse_batch_results(200, {}, body)}
    assert out["uid-0"].succeeded
    assert out["uid-0"].parsed is not None
    assert out["uid-0"].parsed.text == "hi"
    assert out["uid-0"].parsed.usage.input_tokens == 5
    assert not out["uid-1"].succeeded
    assert "slow down" in (out["uid-1"].error or "")
    assert not out["uid-2"].succeeded


def test_results_are_matched_by_custom_id_not_by_order() -> None:
    a = OpenAICompatBatchAdapter()
    rows = [
        {
            "custom_id": "uid-1",
            "response": {
                "status_code": 200,
                "body": {"choices": [{"message": {"content": "second"}}]},
            },
            "error": None,
        },
        {
            "custom_id": "uid-0",
            "response": {
                "status_code": 200,
                "body": {"choices": [{"message": {"content": "first"}}]},
            },
            "error": None,
        },
    ]
    body = ("\n".join(json.dumps(x) for x in rows)).encode()
    out = {r.custom_id: r for r in a.parse_batch_results(200, {}, body)}
    assert out["uid-0"].parsed is not None and out["uid-0"].parsed.text == "first"
    assert out["uid-1"].parsed is not None and out["uid-1"].parsed.text == "second"


def test_together_uses_the_same_shape_on_its_own_host() -> None:
    a = OpenAICompatBatchAdapter()
    p = _openai_provider(base=TOGETHER_BASE, api_key_env="OPENWEIGHTS_API_KEY")
    assert a.build_batch_upload(_items(p, 1), p, "k").url == f"{TOGETHER_BASE}/files"
    assert a.build_batch_create("file-1", p, "k").url == f"{TOGETHER_BASE}/batches"
    assert a.build_batch_results("file-2", p, "k").url == f"{TOGETHER_BASE}/files/file-2/content"


# -- Gemini -----------------------------------------------------------------------------


def _g_items(
    provider: ProviderConfig, n: int = 2, model: str = "gemini-3.8-flash"
) -> list[BatchItem]:
    out: list[BatchItem] = []
    for i in range(n):
        ref = ModelRef(provider="google", model=model, provider_config=provider)
        req = ChatRequest(
            model=f"google/{model}",
            messages=[{"role": "user", "content": f"q{i}"}],
            max_tokens=16,
        )
        out.append((f"uid-{i}", ref, req))
    return out


def test_gemini_submits_inline_with_the_model_in_the_url_and_a_key_per_request() -> None:
    a = GoogleBatchAdapter()
    p = _google_provider()
    built = a.build_batch_submit(_g_items(p, 2), p, "gk")
    assert built.url == f"{GOOGLE_BASE}/v1beta/models/gemini-3.8-flash:batchGenerateContent"
    assert built.headers["x-goog-api-key"] == "gk"
    body = json.loads(built.body)
    reqs = body["batch"]["input_config"]["requests"]["requests"]
    assert len(reqs) == 2
    assert reqs[0]["metadata"]["key"] == "uid-0"
    assert reqs[0]["request"]["contents"][0]["parts"][0]["text"] == "q0"


def test_a_gemini_batched_body_is_identical_to_a_single_call_body() -> None:
    a = GoogleBatchAdapter()
    p = _google_provider()
    items = _g_items(p, 1)
    _, ref, req = items[0]
    single = json.loads(a.build_request(ref, req, p, "k").body)
    batched = json.loads(a.build_batch_submit(items, p, "k").body)
    inner = batched["batch"]["input_config"]["requests"]["requests"][0]["request"]
    assert inner == single


def test_a_mixed_model_gemini_batch_is_refused() -> None:
    """The model is in the URL, so a mixed batch would silently go to one of them."""
    a = GoogleBatchAdapter()
    p = _google_provider()
    items = _g_items(p, 1, "gemini-3.8-flash") + _g_items(p, 1, "gemini-3.5-flash-lite")
    with pytest.raises(ProviderError, match="same model"):
        a.build_batch_submit(items, p, "k")


def test_an_oversized_inline_gemini_batch_is_refused_by_name() -> None:
    a = GoogleBatchAdapter()
    p = _google_provider()
    ref = ModelRef(provider="google", model="gemini-3.8-flash", provider_config=p)
    huge = ChatRequest(
        model="google/gemini-3.8-flash",
        messages=[{"role": "user", "content": "x" * (MAX_INLINE_BYTES // 2)}],
        max_tokens=8,
    )
    with pytest.raises(ProviderError, match="capped at"):
        a.build_batch_submit([("a", ref, huge), ("b", ref, huge)], p, "k")


def test_gemini_status_only_offers_results_when_it_actually_succeeded() -> None:
    a = GoogleBatchAdapter()
    running = json.dumps({"name": "batches/1", "state": "JOB_STATE_RUNNING"}).encode()
    assert not a.parse_batch_status(200, {}, running).ended

    failed = json.dumps({"name": "batches/1", "state": "JOB_STATE_FAILED"}).encode()
    pf = a.parse_batch_status(200, {}, failed)
    assert pf.ended and pf.results_url is None

    ok = json.dumps({"name": "batches/1", "state": "JOB_STATE_SUCCEEDED"}).encode()
    po = a.parse_batch_status(200, {}, ok)
    assert po.ended and po.results_url == "batches/1"


def test_gemini_results_use_the_key_when_the_vendor_returns_it() -> None:
    a = GoogleBatchAdapter()
    body = json.dumps(
        {
            "name": "batches/1",
            "state": "JOB_STATE_SUCCEEDED",
            "response": {
                "inlinedResponses": [
                    {
                        "metadata": {"key": "uid-1"},
                        "response": {
                            "candidates": [
                                {"content": {"parts": [{"text": "second"}]}, "finishReason": "STOP"}
                            ],
                            "usageMetadata": {"promptTokenCount": 4, "candidatesTokenCount": 1},
                        },
                    },
                    {
                        "metadata": {"key": "uid-0"},
                        "response": {
                            "candidates": [
                                {"content": {"parts": [{"text": "first"}]}, "finishReason": "STOP"}
                            ],
                            "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 1},
                        },
                    },
                ]
            },
        }
    ).encode()
    out = {r.custom_id: r for r in a.parse_batch_results(200, {}, body)}
    # Returned out of order on purpose: the key decides, not the position.
    assert out["uid-0"].parsed is not None and out["uid-0"].parsed.text == "first"
    assert out["uid-1"].parsed is not None and out["uid-1"].parsed.text == "second"
    assert out["uid-0"].parsed.usage.input_tokens == 3


def test_gemini_results_fall_back_to_position_when_no_key_comes_back() -> None:
    """Documented shape does not promise the key on the response, so position is the fallback."""
    a = GoogleBatchAdapter()
    body = json.dumps(
        {
            "name": "batches/1",
            "state": "JOB_STATE_SUCCEEDED",
            "response": {
                "inlinedResponses": [
                    {"response": {"candidates": [{"content": {"parts": [{"text": "a"}]}}]}},
                    {"response": {"candidates": [{"content": {"parts": [{"text": "b"}]}}]}},
                ]
            },
        }
    ).encode()
    out = a.parse_batch_results(200, {}, body)
    assert [r.custom_id for r in out] == ["#0", "#1"]


def test_a_gemini_batch_with_no_responses_is_refused_rather_than_completed_empty() -> None:
    a = GoogleBatchAdapter()
    body = json.dumps({"name": "batches/1", "state": "JOB_STATE_FAILED"}).encode()
    with pytest.raises(ProviderError, match="no inlined"):
        a.parse_batch_results(200, {}, body)


@pytest.mark.parametrize("bad", ["../secrets", "https://evil.invalid/x", "batches/1/2", "nope/1"])
def test_a_gemini_batch_name_that_is_not_batches_id_is_refused(bad: str) -> None:
    a = GoogleBatchAdapter()
    with pytest.raises(ProviderError, match="not 'batches/<id>'"):
        a.build_batch_status(bad, _google_provider(), "k")
