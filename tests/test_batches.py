"""Anthropic Message Batches: the same requests at the batch rate, and one ledger row per
request whatever happens to the batch.

The rules being held to here are the repository's, not the vendor's: every call writes a
ledger row before returning including failures, cost comes from returned usage and a dated
price file, and a cap refuses before anything leaves. A batch stresses all three, because
the vendor bills for the whole batch the moment it accepts it and answers hours later, in
a different process.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from boundary.config import BoundaryConfig, CacheConfig, CapsConfig, ProjectCap
from boundary.errors import BatchNotReady, ConfigError, ProviderError, SpendCapExceeded
from boundary.gateway import Gateway
from boundary.ledger.prices import cost_usd, estimate_usd
from boundary.ledger.store import IN_FLIGHT
from boundary.providers.anthropic import AnthropicAdapter
from boundary.types import ChatRequest, Usage

from .conftest import HAIKU, make_gateway

BATCHES_URL = "https://api.anthropic.com/v1/messages/batches"
BATCH_ID = "msgbatch_01abcdef"
STATUS_URL = f"{BATCHES_URL}/{BATCH_ID}"
RESULTS_URL = f"{BATCHES_URL}/{BATCH_ID}/results"


def _requests(n: int, *, max_tokens: int | None = 16) -> list[ChatRequest]:
    return [
        ChatRequest(
            model=HAIKU,
            messages=[{"role": "user", "content": f"Question {i}?"}],
            max_tokens=max_tokens,
        )
        for i in range(n)
    ]


def _submitted(status: str = "in_progress") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": BATCH_ID,
            "type": "message_batch",
            "processing_status": status,
            "request_counts": {"processing": 3, "succeeded": 0, "errored": 0},
            "results_url": None,
        },
    )


def _status(processing: str = "ended", *, results_url: str | None = RESULTS_URL) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": BATCH_ID,
            "type": "message_batch",
            "processing_status": processing,
            "request_counts": {"processing": 0, "succeeded": 2, "errored": 0},
            "results_url": results_url,
        },
    )


def _message(text: str = "B", *, in_tokens: int = 40, out_tokens: int = 3) -> dict[str, Any]:
    return {
        "id": "msg_01",
        "type": "message",
        "role": "assistant",
        "model": "claude-haiku-4-5-20251001",
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": in_tokens, "output_tokens": out_tokens},
    }


def _results(lines: list[dict[str, Any]]) -> httpx.Response:
    body = "\n".join(json.dumps(line) for line in lines)
    return httpx.Response(200, content=body.encode("utf-8"))


def _succeeded(custom_id: str, **kw: Any) -> dict[str, Any]:
    return {"custom_id": custom_id, "result": {"type": "succeeded", "message": _message(**kw)}}


def _errored(custom_id: str) -> dict[str, Any]:
    return {
        "custom_id": custom_id,
        "result": {
            "type": "errored",
            "error": {
                "type": "error",
                "error": {"type": "invalid_request_error", "message": "max_tokens too large"},
            },
        },
    }


def test_submit_sends_one_params_object_per_request_named_by_the_rows_call_uid(
    gw: Gateway,
) -> None:
    """The custom_id is the ledger row's call_uid. That is what lets a result be matched to
    exactly one row, in any process and in any order the vendor chooses to answer."""
    requests = _requests(3)
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(BATCHES_URL).mock(return_value=_submitted())
        handle = gw.batch_submit(requests, purpose="own-run", run_id="panel-1")

    sent = json.loads(route.calls[0].request.content)
    assert [r["custom_id"] for r in sent["requests"]] == list(handle.custom_ids)
    assert {row["call_uid"] for row in gw.ledger.rows()} == set(handle.custom_ids)

    # The params are byte for byte the body a single call would have built.
    ref = gw.resolve(HAIKU)
    single = AnthropicAdapter().build_request(
        ref, requests[0], ref.provider_config, "test-anthropic-key-000000000000"
    )
    assert sent["requests"][0]["params"] == json.loads(single.body)
    assert route.calls[0].request.headers["anthropic-version"] == "2023-06-01"


def test_rows_are_in_flight_at_the_batch_rate_and_carry_the_batch_id(gw: Gateway) -> None:
    requests = _requests(2)
    with respx.mock(assert_all_called=True) as mock:
        mock.post(BATCHES_URL).mock(return_value=_submitted())
        handle = gw.batch_submit(requests, purpose="own-run")

    rows = gw.ledger.rows()
    assert len(rows) == 2
    assert {r["error_type"] for r in rows} == {IN_FLIGHT}, "a submitted batch is still in flight"
    assert {r["batch_id"] for r in rows} == {BATCH_ID}
    assert {r["mode"] for r in rows} == {"standard"}
    assert list(handle.ledger_ids) == [r["id"] for r in rows]

    # The estimate reserves the batch price, not the full one: half, on this price list.
    entry = gw.prices.lookup("anthropic", "claude-haiku-4-5-20251001")
    assert entry is not None and entry.batch_multiplier == 0.5
    ref = gw.resolve(HAIKU)
    single = AnthropicAdapter().build_request(
        ref, requests[0], ref.provider_config, "test-anthropic-key-000000000000"
    )
    at_full_rate = estimate_usd(len(single.body), 16, entry)
    assert rows[0]["cost_usd"] == pytest.approx(at_full_rate * 0.5)
    # And the reservation is what the caps see while the vendor is still working.
    assert gw.ledger.spend_usd(project="ai-release-gate") == pytest.approx(at_full_rate)


def test_a_failed_submit_completes_every_row_rather_than_leaving_it_in_flight(
    gw: Gateway,
) -> None:
    """A batch the vendor refused was never billed, so its rows are failures and not
    estimates. Leaving them in flight would hold spend against the cap for ever."""
    with respx.mock(assert_all_called=True) as mock:
        mock.post(BATCHES_URL).mock(
            return_value=httpx.Response(
                400,
                json={
                    "type": "error",
                    "error": {"type": "invalid_request_error", "message": "bad"},
                },
            )
        )
        with pytest.raises(ProviderError) as excinfo:
            gw.batch_submit(_requests(3), purpose="own-run")

    assert excinfo.value.status == 400
    rows = gw.ledger.rows()
    assert len(rows) == 3, "the rows are written before the submit leaves, so they exist"
    assert {r["error_type"] for r in rows} == {"http_400"}
    assert {r["cost_usd"] for r in rows} == {None}, "a refused batch costs nothing"
    assert {r["batch_id"] for r in rows} == {None}
    assert gw.ledger.spend_usd(project="ai-release-gate") == 0.0


def test_the_cap_is_checked_once_for_the_whole_batch_and_nothing_is_sent(
    repo_config: BoundaryConfig, tmp_path: Path, keys: None
) -> None:
    """Each request is well within the cap; together they are not. A vendor bills for the
    whole batch on acceptance, so the sum is the only number worth checking."""
    caps = CapsConfig(
        version=1,
        portfolio_monthly_usd=1000.0,
        projects={"ai-release-gate": ProjectCap(monthly_usd=0.0005)},
    )
    gw = make_gateway(repo_config, tmp_path, caps=caps)
    try:
        with respx.mock(assert_all_called=False) as mock:
            route = mock.post(BATCHES_URL).mock(return_value=_submitted())
            with pytest.raises(SpendCapExceeded) as excinfo:
                gw.batch_submit(_requests(40, max_tokens=1024), purpose="own-run")
        assert route.call_count == 0, "a refused batch reaches no upstream"
        assert gw.ledger.count() == 0, "and writes no rows, because nothing was attempted"
        assert excinfo.value.cap_usd == 0.0005
    finally:
        gw.close()


def test_results_complete_each_row_at_the_batch_rate(gw: Gateway) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.post(BATCHES_URL).mock(return_value=_submitted())
        handle = gw.batch_submit(_requests(2), purpose="own-run", run_id="panel-1")
        mock.get(STATUS_URL).mock(return_value=_status())
        mock.get(RESULTS_URL).mock(
            return_value=_results([_succeeded(cid) for cid in handle.custom_ids])
        )
        responses = gw.batch_results(handle)

    entry = gw.prices.lookup("anthropic", "claude-haiku-4-5-20251001")
    assert entry is not None
    usage = Usage(input_tokens=40, output_tokens=3)
    expected = cost_usd(usage, entry, batch=True)
    assert expected == pytest.approx((cost_usd(usage, entry) or 0.0) * 0.5)

    assert [r.text for r in responses] == ["B", "B"]
    assert all(r.costed and r.cost_usd == pytest.approx(expected) for r in responses)
    assert all(r.status == 200 and r.mode.value == "standard" for r in responses)

    rows = gw.ledger.rows()
    assert {r["error_type"] for r in rows} == {None}, "no row is left in flight"
    assert {r["http_status"] for r in rows} == {200}
    assert all(r["cost_usd"] == pytest.approx(expected) and r["costed"] for r in rows)
    assert all(r["input_tokens"] == 40 and r["output_tokens"] == 3 for r in rows)
    assert all(r["trace_id"] is None or len(str(r["trace_id"])) == 32 for r in rows)
    assert gw.ledger.uncosted_count() == 0


def test_an_errored_request_is_recorded_beside_the_ones_that_worked(gw: Gateway) -> None:
    """A partial failure is the normal case for a large batch. The successes are costed and
    the failure is not, because there is no usage to cost it from."""
    with respx.mock(assert_all_called=True) as mock:
        mock.post(BATCHES_URL).mock(return_value=_submitted())
        handle = gw.batch_submit(_requests(3), purpose="own-run")
        first, second, third = handle.custom_ids
        mock.get(STATUS_URL).mock(return_value=_status())
        mock.get(RESULTS_URL).mock(
            return_value=_results(
                [
                    _succeeded(first),
                    _errored(second),
                    {"custom_id": third, "result": {"type": "expired"}},
                ]
            )
        )
        responses = gw.batch_results(handle)

    assert [r.status for r in responses] == [200, "errored", "expired"]
    assert responses[0].costed and not responses[1].costed and not responses[2].costed
    rows = {r["call_uid"]: r for r in gw.ledger.rows()}
    assert rows[first]["error_type"] is None
    assert rows[second]["error_type"] == "batch_errored"
    assert rows[third]["error_type"] == "batch_expired"
    assert rows[second]["cost_usd"] is None and rows[third]["cost_usd"] is None
    # Failures are not uncosted successes; the figure that must stay at zero is unaffected.
    assert gw.ledger.uncosted_count() == 0


def test_results_asked_for_too_early_say_not_yet_and_change_nothing(gw: Gateway) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.post(BATCHES_URL).mock(return_value=_submitted())
        handle = gw.batch_submit(_requests(2), purpose="own-run")
        mock.get(STATUS_URL).mock(return_value=_status("in_progress", results_url=None))
        with pytest.raises(BatchNotReady) as excinfo:
            gw.batch_results(handle)

    assert excinfo.value.batch_id == BATCH_ID
    assert excinfo.value.processing_status == "in_progress"
    assert {r["error_type"] for r in gw.ledger.rows()} == {IN_FLIGHT}


def test_waiting_polls_until_the_batch_ends(gw: Gateway) -> None:
    """`wait_s` exists so a short batch can be collected in one go. The clock is the
    gateway's injected sleep, so the test does not actually wait."""
    with respx.mock(assert_all_called=True) as mock:
        mock.post(BATCHES_URL).mock(return_value=_submitted())
        handle = gw.batch_submit(_requests(1), purpose="own-run")
        mock.get(STATUS_URL).mock(side_effect=[_status("in_progress", results_url=None), _status()])
        mock.get(RESULTS_URL).mock(return_value=_results([_succeeded(handle.custom_ids[0])]))
        responses = gw.batch_results(handle, wait_s=60.0, poll_s=1.0)

    assert len(responses) == 1 and responses[0].costed


def test_a_batch_is_collected_by_a_process_that_did_not_submit_it(
    repo_config: BoundaryConfig, tmp_path: Path, keys: None
) -> None:
    """Project 02 submits thousands of requests and comes back hours later. Only the batch
    id survives in between; everything else is rebuilt from the ledger."""
    submitter = make_gateway(repo_config, tmp_path)
    try:
        with respx.mock(assert_all_called=True) as mock:
            mock.post(BATCHES_URL).mock(return_value=_submitted())
            submitted = submitter.batch_submit(_requests(2), purpose="own-run", run_id="panel-1")
    finally:
        submitter.close()

    collector = make_gateway(repo_config, tmp_path)
    try:
        handle = collector.batch_handle(BATCH_ID)
        assert handle.custom_ids == submitted.custom_ids
        assert (handle.provider, handle.purpose, handle.run_id) == (
            "anthropic",
            "own-run",
            "panel-1",
        )
        with respx.mock(assert_all_called=True) as mock:
            mock.get(STATUS_URL).mock(return_value=_status())
            mock.get(RESULTS_URL).mock(
                return_value=_results([_succeeded(cid) for cid in handle.custom_ids])
            )
            responses = collector.batch_results(handle)
        assert all(r.costed for r in responses)
        assert {r["error_type"] for r in collector.ledger.rows()} == {None}
    finally:
        collector.close()


def test_a_request_the_results_never_mention_is_completed_as_missing(gw: Gateway) -> None:
    """A batch that has ended will not mention it later either, so the row is closed rather
    than left in flight for ever."""
    with respx.mock(assert_all_called=True) as mock:
        mock.post(BATCHES_URL).mock(return_value=_submitted())
        handle = gw.batch_submit(_requests(2), purpose="own-run")
        mock.get(STATUS_URL).mock(return_value=_status())
        mock.get(RESULTS_URL).mock(return_value=_results([_succeeded(handle.custom_ids[0])]))
        responses = gw.batch_results(handle)

    assert [r.status for r in responses] == [200, "missing"]
    rows = {r["call_uid"]: r for r in gw.ledger.rows()}
    assert rows[handle.custom_ids[1]]["error_type"] == "batch_missing"
    assert not any(r["error_type"] == IN_FLIGHT for r in gw.ledger.rows())


def test_a_results_url_on_another_host_is_refused_before_the_key_is_sent(gw: Gateway) -> None:
    """The results URL comes from the vendor and the request that fetches it carries the API
    key. A URL pointing elsewhere would hand the key to elsewhere."""
    with respx.mock(assert_all_called=True) as mock:
        mock.post(BATCHES_URL).mock(return_value=_submitted())
        handle = gw.batch_submit(_requests(1), purpose="own-run")
        mock.get(STATUS_URL).mock(return_value=_status(results_url="https://example.invalid/steal"))
        with pytest.raises(ProviderError, match="not on the configured host"):
            gw.batch_results(handle)


def test_a_provider_without_batch_support_is_refused_by_name(gw: Gateway) -> None:
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(BATCHES_URL)
        with pytest.raises(ConfigError, match="no batch support"):
            gw.batch_submit(
                [
                    ChatRequest(
                        model="openai/gpt-5-nano", messages=[{"role": "user", "content": "x"}]
                    )
                ],
                purpose="own-run",
            )
    assert route.call_count == 0
    assert gw.ledger.count() == 0


def test_one_batch_goes_to_one_provider(gw: Gateway) -> None:
    mixed = [
        ChatRequest(model=HAIKU, messages=[{"role": "user", "content": "a"}], max_tokens=8),
        ChatRequest(
            model="openai/gpt-5-nano", messages=[{"role": "user", "content": "b"}], max_tokens=8
        ),
    ]
    with pytest.raises(ConfigError, match="one batch goes to one provider"):
        gw.batch_submit(mixed, purpose="own-run")
    assert gw.ledger.count() == 0


def test_an_empty_batch_is_refused(gw: Gateway) -> None:
    with pytest.raises(ValueError, match="at least one request"):
        gw.batch_submit([], purpose="own-run")


def test_max_tokens_is_filled_from_the_defaults_like_any_standard_call(gw: Gateway) -> None:
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(BATCHES_URL).mock(return_value=_submitted())
        gw.batch_submit(_requests(1, max_tokens=None), purpose="own-run")
    sent = json.loads(route.calls[0].request.content)
    assert sent["requests"][0]["params"]["max_tokens"] == gw.config.defaults.max_tokens


def test_the_development_cache_never_answers_a_batch(
    repo_config: BoundaryConfig, tmp_path: Path, keys: None
) -> None:
    """A cache hit inside a batch would make a row say a request was billed when it was
    not, so the cache is not consulted at all."""
    cached_config = repo_config.model_copy(
        update={"cache": CacheConfig(enabled=True, path=tmp_path / "cache")}
    )
    gw = make_gateway(cached_config, tmp_path)
    try:
        with respx.mock(assert_all_called=True) as mock:
            route = mock.post(BATCHES_URL).mock(return_value=_submitted())
            gw.batch_submit(_requests(1), purpose="own-run")
            gw.batch_submit(_requests(1), purpose="own-run")
        assert route.call_count == 2, "the identical second batch still went upstream"
        assert gw.ledger.count() == 2
        assert not any(r["cached"] for r in gw.ledger.rows())
    finally:
        gw.close()


def test_a_results_file_that_cannot_be_read_completes_nothing(gw: Gateway) -> None:
    """Half an understood results file would complete some rows and silently leave others
    in flight, which is the one state the ledger must never be left in by accident."""
    with respx.mock(assert_all_called=True) as mock:
        mock.post(BATCHES_URL).mock(return_value=_submitted())
        handle = gw.batch_submit(_requests(2), purpose="own-run")
        mock.get(STATUS_URL).mock(return_value=_status())
        mock.get(RESULTS_URL).mock(return_value=httpx.Response(200, content=b"{not json}\n"))
        with pytest.raises(ProviderError, match="not JSON"):
            gw.batch_results(handle)
    assert {r["error_type"] for r in gw.ledger.rows()} == {IN_FLIGHT}
