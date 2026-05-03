"""Contract: extract() — longer timeout, mode parameter."""

import httpx
import pytest

from hyperapi import ExtractError


EXTRACT_RESPONSE = {
    "request_id": "req_ex_1",
    "result": {"entities": {"vendor": "Acme"}, "line_items": []},
    "ocr_text": "Invoice text…",
}


def _seed(mock_backend):
    mock_backend.post("/v1/documents/upload").mock(
        return_value=httpx.Response(200, json={
            "document_key": "doc_ex", "upload_url": "https://s3.local/ex?sig=x", "expires_in": 600,
        }),
    )
    mock_backend.put("https://s3.local/ex?sig=x").mock(return_value=httpx.Response(200))


def test_extract_sends_mode_default(mock_backend, client, tiny_pdf):
    _seed(mock_backend)
    route = mock_backend.post("/v1/extract").mock(
        return_value=httpx.Response(200, json=EXTRACT_RESPONSE),
    )
    client.extract(tiny_pdf)

    req = route.calls[0].request
    assert req.url.params["mode"] == "default"
    assert req.url.params["ocr_engine"] == "paddle"


def test_extract_custom_mode_propagates(mock_backend, client, tiny_pdf):
    _seed(mock_backend)
    route = mock_backend.post("/v1/extract").mock(
        return_value=httpx.Response(200, json=EXTRACT_RESPONSE),
    )
    client.extract(tiny_pdf, mode="strict")
    assert route.calls[0].request.url.params["mode"] == "strict"


def test_extract_returns_response_envelope(mock_backend, client, tiny_pdf):
    _seed(mock_backend)
    mock_backend.post("/v1/extract").mock(
        return_value=httpx.Response(200, json=EXTRACT_RESPONSE),
    )
    result = client.extract(tiny_pdf)
    assert result["result"]["entities"]["vendor"] == "Acme"


def test_extract_5xx_raises_extract_error(mock_backend, client, tiny_pdf):
    _seed(mock_backend)
    mock_backend.post("/v1/extract").mock(return_value=httpx.Response(500, text="bad"))

    with pytest.raises(ExtractError) as ei:
        client.extract(tiny_pdf)
    assert ei.value.status_code == 500


def test_extract_timeout_surfaces_504(mock_backend, client, tiny_pdf):
    _seed(mock_backend)
    mock_backend.post("/v1/extract").mock(side_effect=httpx.TimeoutException("timeout"))

    with pytest.raises(ExtractError) as ei:
        client.extract(tiny_pdf)
    assert ei.value.status_code == 504
