"""Contract: classify() and split() — share the _call_endpoint code path."""

import httpx
import pytest

from hyperapi import ClassifyError, SplitError


CLASSIFY_RESPONSE = {"request_id": "req_cl", "result": {"document_type": "invoice", "confidence": 0.97}}
SPLIT_RESPONSE = {"request_id": "req_sp", "result": {"segments": [{"start": 1, "end": 3}, {"start": 4, "end": 5}]}}


def _seed(mock_backend, key):
    mock_backend.post("/v1/documents/upload").mock(
        return_value=httpx.Response(200, json={
            "document_key": key, "upload_url": f"https://s3.local/{key}?sig=x", "expires_in": 600,
        }),
    )
    mock_backend.put(f"https://s3.local/{key}?sig=x").mock(return_value=httpx.Response(200))


def test_classify_request_shape(mock_backend, client, tiny_pdf):
    _seed(mock_backend, "doc_cl")
    route = mock_backend.post("/v1/classify").mock(
        return_value=httpx.Response(200, json=CLASSIFY_RESPONSE),
    )
    result = client.classify(tiny_pdf)

    assert result["result"]["document_type"] == "invoice"
    req = route.calls[0].request
    assert req.url.params["ocr_engine"] == "paddle"
    assert req.url.params["mode"] == "default"
    assert b"document_key=doc_cl" in req.read()


def test_classify_5xx_raises(mock_backend, client, tiny_pdf):
    _seed(mock_backend, "doc_cl")
    mock_backend.post("/v1/classify").mock(return_value=httpx.Response(500))
    with pytest.raises(ClassifyError):
        client.classify(tiny_pdf)


def test_split_request_shape(mock_backend, client, tiny_pdf):
    _seed(mock_backend, "doc_sp")
    route = mock_backend.post("/v1/split").mock(
        return_value=httpx.Response(200, json=SPLIT_RESPONSE),
    )
    result = client.split(tiny_pdf)

    assert len(result["result"]["segments"]) == 2
    assert route.calls[0].request.url.params["mode"] == "default"


def test_split_429_raises_with_status(mock_backend, client, tiny_pdf):
    _seed(mock_backend, "doc_sp")
    mock_backend.post("/v1/split").mock(return_value=httpx.Response(429))
    with pytest.raises(SplitError) as ei:
        client.split(tiny_pdf)
    assert ei.value.status_code == 429
