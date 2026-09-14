"""The two-round-trip submit, end to end through the Gateway.

An OpenAI-shaped batch is created by naming a file that was uploaded first, so submitting
takes two requests instead of one. The repository's rule is that every call writes a ledger
row before returning, and the prompts leave the process during the *upload*, so the rows have
to exist before that request and not before the create that follows it.

These are the gateway-level behaviours; the request and response shapes themselves are in
tests/test_batches_openai_google.py.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import respx

from boundary.config import BoundaryConfig
from boundary.errors import ProviderError
from boundary.gateway import Gateway
from boundary.ledger.store import IN_FLIGHT
from boundary.types import ChatRequest

from .conftest import make_gateway

FILES_URL = "https://api.openai.com/v1/files"
BATCHES_URL = "https://api.openai.com/v1/batches"
BATCH_ID = "batch_abc123"
STATUS_URL = f"{BATCHES_URL}/{BATCH_ID}"
OUTPUT_FILE = "file-out-1"
RESULTS_URL = f"{FILES_URL}/{OUTPUT_FILE}/content"

MODEL = "openai/gpt-5-nano"


@pytest.fixture
def ogw(repo_config: BoundaryConfig, tmp_path: Path, keys: None) -> Iterator[Gateway]:
    g = make_gateway(repo_config, tmp_path, project="model-selection-tenth-cost")
    yield g
    g.close()


def _requests(n: int) -> list[ChatRequest]:
    return [
        ChatRequest(model=MODEL, messages=[{"role": "user", "content": f"q{i}"}], max_tokens=16)
        for i in range(n)
    ]


def _uploaded() -> httpx.Response:
    return httpx.Response(200, json={"id": "file-in-1", "object": "file", "purpose": "batch"})


def _created() -> httpx.Response:
    return httpx.Response(200, json={"id": BATCH_ID, "object": "batch", "status": "validating"})


def _completed() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": BATCH_ID,
            "status": "completed",
            "output_file_id": OUTPUT_FILE,
            "request_counts": {"total": 2, "completed": 2, "failed": 0},
        },
    )


def _results(custom_ids: list[str]) -> httpx.Response:
    lines = []
    for i, cid in enumerate(custom_ids):
        lines.append(
            json.dumps(
                {
                    "id": f"resp-{i}",
                    "custom_id": cid,
                    "response": {
                        "status_code": 200,
                        "body": {
                            "model": "gpt-5-nano",
                            "choices": [{"message": {"content": f"a{i}"}, "finish_reason": "stop"}],
                            "usage": {"prompt_tokens": 7, "completion_tokens": 3},
                        },
                    },
                    "error": None,
                }
            )
        )
    return httpx.Response(200, text="\n".join(lines) + "\n")


def test_submit_uploads_then_creates_and_every_row_is_in_flight(ogw: Gateway) -> None:
    with respx.mock(assert_all_called=True) as mock:
        up = mock.post(FILES_URL).mock(return_value=_uploaded())
        cr = mock.post(BATCHES_URL).mock(return_value=_created())
        handle = ogw.batch_submit(_requests(2), purpose="own-run", run_id="r1")

    assert up.call_count == 1
    assert cr.call_count == 1
    # The create names the file the upload returned.
    assert json.loads(cr.calls[0].request.content)["input_file_id"] == "file-in-1"
    # The upload carried the prompts, as multipart.
    assert up.calls[0].request.headers["content-type"].startswith("multipart/form-data")
    assert b"q0" in up.calls[0].request.content

    assert handle.batch_id == BATCH_ID
    rows = list(ogw.ledger.rows_for_batch(BATCH_ID))
    assert len(rows) == 2
    assert all(r["error_type"] == IN_FLIGHT for r in rows)
    assert {r["call_uid"] for r in rows} == set(handle.custom_ids)


def test_a_failed_upload_completes_every_row_at_no_cost_and_never_creates(
    ogw: Gateway,
) -> None:
    """The upload is not billed, so its failure is a failure with no cost, not an in-flight row.

    The create must not happen either: there is no file to name.
    """
    with respx.mock(assert_all_called=False) as mock:
        up = mock.post(FILES_URL).mock(return_value=httpx.Response(413, json={"error": "big"}))
        cr = mock.post(BATCHES_URL)
        with pytest.raises(ProviderError):
            ogw.batch_submit(_requests(2), purpose="own-run", run_id="r1")

    assert up.call_count >= 1
    assert cr.call_count == 0, "nothing may be created when the file never landed"

    rows = list(ogw.ledger.rows())
    assert len(rows) == 2
    for r in rows:
        assert r["error_type"] == "batch_upload"
        assert r["cost_usd"] is None
        assert r["costed"] == 0


def test_the_whole_round_trip_completes_rows_from_returned_usage(ogw: Gateway) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.post(FILES_URL).mock(return_value=_uploaded())
        mock.post(BATCHES_URL).mock(return_value=_created())
        handle = ogw.batch_submit(_requests(2), purpose="own-run", run_id="r1")
        mock.get(STATUS_URL).mock(return_value=_completed())
        # Returned in the opposite order on purpose: mapping is by custom_id.
        mock.get(RESULTS_URL).mock(return_value=_results(list(reversed(handle.custom_ids))))
        responses = ogw.batch_results(handle)

    assert len(responses) == 2
    rows = {r["call_uid"]: r for r in ogw.ledger.rows_for_batch(BATCH_ID)}
    assert len(rows) == 2
    for r in rows.values():
        assert r["error_type"] is None
        assert r["input_tokens"] == 7
        assert r["output_tokens"] == 3
        assert r["costed"] == 1
        assert r["cost_usd"] is not None and r["cost_usd"] > 0


def test_the_batch_rate_is_what_prices_the_rows(ogw: Gateway) -> None:
    """gpt-5-nano carries batch_multiplier 0.5, so a batched row costs half a single call."""
    with respx.mock(assert_all_called=True) as mock:
        mock.post(FILES_URL).mock(return_value=_uploaded())
        mock.post(BATCHES_URL).mock(return_value=_created())
        handle = ogw.batch_submit(_requests(1), purpose="own-run", run_id="r1")
        mock.get(STATUS_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": BATCH_ID,
                    "status": "completed",
                    "output_file_id": OUTPUT_FILE,
                    "request_counts": {"total": 1, "completed": 1, "failed": 0},
                },
            )
        )
        mock.get(RESULTS_URL).mock(return_value=_results(list(handle.custom_ids)))
        ogw.batch_results(handle)

    row = next(iter(ogw.ledger.rows_for_batch(BATCH_ID)))
    entry = ogw.prices.lookup("openai", "gpt-5-nano")
    assert entry is not None and entry.batch_multiplier == 0.5
    full = (7 * entry.input + 3 * entry.output) / 1_000_000
    assert row["cost_usd"] == pytest.approx(full * 0.5)
