"""Azure AI Document Intelligence (1.6): golden request, the batch of one end to end through
the gateway, its per-page costing, and the refusals."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import httpx
import pytest
import respx

from boundary.config import BoundaryConfig
from boundary.errors import ConfigError
from boundary.providers.docintel import AzureDocIntelAdapter, split_model
from boundary.routes import resolve
from boundary.types import ChatRequest, Mode

from .conftest import make_gateway

BASE = "https://legible-docintel.cognitiveservices.azure.com"
MODEL = "azure-docintel/custom/legible-fara-amendment-s0"
ANALYZE = (
    f"{BASE}/documentintelligence/documentModels/legible-fara-amendment-s0:analyze"
    "?api-version=2024-11-30&stringIndexType=unicodeCodePoint"
)
RESULT = (
    f"{BASE}/documentintelligence/documentModels/legible-fara-amendment-s0/analyzeResults/"
    "r-1?api-version=2024-11-30"
)
PDF = base64.b64encode(b"%PDF-1.4 two pages").decode()


def _request(pages: int = 2, **kw: object) -> ChatRequest:
    base: dict[str, object] = {
        "model": MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {"type": "base64", "media_type": "application/pdf", "data": PDF},
                    }
                ],
            }
        ],
        "extra": {"pages": pages},
    }
    base.update(kw)
    return ChatRequest(**base)  # type: ignore[arg-type]


def _done(pages: int = 2) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "status": "succeeded",
            "analyzeResult": {
                "modelId": "legible-fara-amendment-s0",
                "pages": [{"pageNumber": i + 1, "words": []} for i in range(pages)],
                "documents": [
                    {"fields": {"registrant_name": {"type": "string", "content": "Acme"}}}
                ],
            },
        },
    )


def test_the_golden_analyze_request(repo_config: BoundaryConfig) -> None:
    ref = resolve(MODEL, repo_config, Mode.STANDARD)
    built = AzureDocIntelAdapter().build_request(ref, _request(), ref.provider_config, "the-key")
    assert built.method == "POST" and built.url == ANALYZE
    assert json.loads(built.body) == {"base64Source": PDF}
    assert built.headers["ocp-apim-subscription-key"] == "the-key"
    assert built.redacted_headers()["ocp-apim-subscription-key"] == "<redacted>"


def test_a_model_names_its_billing_tier() -> None:
    assert split_model("custom/x") == ("custom", "x")
    assert split_model("prebuilt/prebuilt-receipt") == ("prebuilt", "prebuilt-receipt")
    with pytest.raises(ValueError, match="tier"):
        split_model("prebuilt-receipt")
    with pytest.raises(ValueError, match="tier"):
        split_model("gold/x")


def test_what_the_service_cannot_carry_is_refused_not_dropped(
    repo_config: BoundaryConfig,
) -> None:
    ref = resolve(MODEL, repo_config, Mode.STANDARD)
    adapter = AzureDocIntelAdapter()
    for bad in (
        _request(system="be careful"),
        _request(temperature=0.0),
        _request(messages=[{"role": "user", "content": "text, not a document"}]),
    ):
        with pytest.raises(ValueError):
            adapter.build_request(ref, bad, ref.provider_config, "k")
    with pytest.raises(ValueError, match="pages"):
        adapter.estimate_usage(_request(pages=0))


def test_a_document_goes_through_the_batch_path_and_is_billed_by_the_page(
    repo_config: BoundaryConfig, tmp_path: Path, keys: None
) -> None:
    gw = make_gateway(repo_config, tmp_path, project="legible")
    try:
        with respx.mock(assert_all_called=True) as mock:
            mock.post(ANALYZE).mock(
                return_value=httpx.Response(202, headers={"Operation-Location": RESULT})
            )
            handle = gw.batch_submit([_request(pages=2)], purpose="arm-f", data_class="public")
            assert handle.batch_id == RESULT
            # In flight at the estimate: two pages at US$30 a thousand.
            row = gw.ledger.rows()[0]
            assert row["cost_usd"] == pytest.approx(0.06)
            mock.get(RESULT).mock(return_value=_done(pages=2))
            (response,) = gw.batch_results(gw.batch_handle(handle.batch_id))
        assert response.ok
        result = json.loads(response.text or "")
        assert result["documents"][0]["fields"]["registrant_name"]["content"] == "Acme"
        assert response.usage.input_tokens == 2
        assert response.cost_usd == pytest.approx(2 * 30.0 / 1000)
        row = gw.ledger.rows()[0]
        assert row["costed"] == 1 and row["data_class"] == "public"
    finally:
        gw.close()


def test_a_failed_analysis_completes_its_row_as_a_failure(
    repo_config: BoundaryConfig, tmp_path: Path, keys: None
) -> None:
    gw = make_gateway(repo_config, tmp_path, project="legible")
    try:
        with respx.mock() as mock:
            mock.post(ANALYZE).mock(
                return_value=httpx.Response(202, headers={"Operation-Location": RESULT})
            )
            handle = gw.batch_submit([_request()], purpose="arm-f", data_class="public")
            mock.get(RESULT).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "status": "failed",
                        "error": {"code": "InvalidContent", "message": "corrupt"},
                    },
                )
            )
            (response,) = gw.batch_results(handle)
        assert not response.ok
        assert gw.ledger.rows()[0]["error_type"] == "batch_errored"
    finally:
        gw.close()


def test_a_running_analysis_is_not_ready(
    repo_config: BoundaryConfig, tmp_path: Path, keys: None
) -> None:
    from boundary.errors import BatchNotReady

    gw = make_gateway(repo_config, tmp_path, project="legible")
    try:
        with respx.mock() as mock:
            mock.post(ANALYZE).mock(
                return_value=httpx.Response(202, headers={"Operation-Location": RESULT})
            )
            handle = gw.batch_submit([_request()], purpose="arm-f", data_class="public")
            mock.get(RESULT).mock(return_value=httpx.Response(200, json={"status": "running"}))
            with pytest.raises(BatchNotReady):
                gw.batch_results(handle)
    finally:
        gw.close()


def test_the_single_call_path_refuses_an_asynchronous_service(
    repo_config: BoundaryConfig, tmp_path: Path, keys: None
) -> None:
    gw = make_gateway(repo_config, tmp_path, project="legible")
    try:
        with respx.mock(assert_all_called=False) as mock:
            route = mock.post(ANALYZE)
            with pytest.raises(ConfigError, match="asynchronously"):
                gw.chat(_request(), purpose="arm-f", data_class="public")
            assert not route.called
        assert gw.ledger.rows() == []
    finally:
        gw.close()


def test_two_documents_are_two_batches_not_one(
    repo_config: BoundaryConfig, tmp_path: Path, keys: None
) -> None:
    gw = make_gateway(repo_config, tmp_path, project="legible")
    try:
        with respx.mock(assert_all_called=False), pytest.raises(ValueError, match="one document"):
            gw.batch_submit([_request(), _request()], purpose="arm-f", data_class="public")
    finally:
        gw.close()


def test_the_key_goes_only_to_the_configured_host(repo_config: BoundaryConfig) -> None:
    ref = resolve(MODEL, repo_config, Mode.STANDARD)
    with pytest.raises(ValueError, match="is not on"):
        AzureDocIntelAdapter().build_batch_status(
            "https://elsewhere.example/analyzeResults/1", ref.provider_config, "k"
        )
