"""Contract: parse() — request shape, response parsing, error mapping."""

import httpx
import pytest

from hyperapi import AuthenticationError, ParseError


PARSE_RESPONSE = {
    "request_id": "req_parse_1",
    "result": {"ocr": "Invoice #12345\nTotal: $1,234.00", "pages": 1},
}


def _seed_presigned(mock_backend):
    mock_backend.post("/v1/documents/upload").mock(
        return_value=httpx.Response(200, json={
            "document_key": "doc_parse",
            "upload_url": "https://s3.local/parse?sig=x",
            "expires_in": 600,
        }),
    )
    mock_backend.put("https://s3.local/parse?sig=x").mock(return_value=httpx.Response(200))


def test_parse_default_uses_presigned_path(mock_backend, client, tiny_pdf):
    _seed_presigned(mock_backend)
    parse_route = mock_backend.post("/v1/parse").mock(
        return_value=httpx.Response(200, json=PARSE_RESPONSE),
    )

    result = client.parse(tiny_pdf)

    assert result == PARSE_RESPONSE
    assert parse_route.called
    req = parse_route.calls[0].request
    assert req.method == "POST"
    assert req.headers["X-API-Key"] == "hk_test_unit"
    assert b"document_key=doc_parse" in req.read()
    assert req.url.params["ocr_engine"] == "paddle"


def test_parse_with_doc_intent_engine_propagates_param(mock_backend, client, tiny_pdf):
    _seed_presigned(mock_backend)
    route = mock_backend.post("/v1/parse").mock(
        return_value=httpx.Response(200, json=PARSE_RESPONSE),
    )

    client.parse(tiny_pdf, ocr_engine="doc-intent")

    assert route.calls[0].request.url.params["ocr_engine"] == "doc-intent"


def test_parse_use_presigned_false_sends_multipart(mock_backend, client, tiny_pdf):
    route = mock_backend.post("/v1/parse").mock(
        return_value=httpx.Response(200, json=PARSE_RESPONSE),
    )

    client.parse(tiny_pdf, use_presigned=False)

    req = route.calls[0].request
    assert "multipart/form-data" in req.headers["content-type"]
    body = req.read()
    assert b"doc.pdf" in body
    assert b"application/pdf" in body


def test_parse_image_path_alias_still_works(mock_backend, client, tiny_png):
    _seed_presigned(mock_backend)
    mock_backend.post("/v1/parse").mock(
        return_value=httpx.Response(200, json=PARSE_RESPONSE),
    )
    result = client.parse(image_path=tiny_png)
    assert result["result"]["pages"] == 1


def test_parse_401_raises_authentication_error(mock_backend, client, tiny_pdf):
    _seed_presigned(mock_backend)
    mock_backend.post("/v1/parse").mock(return_value=httpx.Response(401))

    with pytest.raises(AuthenticationError) as ei:
        client.parse(tiny_pdf)
    assert ei.value.status_code == 401


@pytest.mark.parametrize("status,attr", [(402, "Insufficient credits"), (429, "Rate limit")])
def test_parse_402_429_raises_parse_error_with_status(mock_backend, client, tiny_pdf, status, attr):
    _seed_presigned(mock_backend)
    mock_backend.post("/v1/parse").mock(return_value=httpx.Response(status))

    with pytest.raises(ParseError) as ei:
        client.parse(tiny_pdf)
    assert ei.value.status_code == status


def test_parse_5xx_raises_parse_error(mock_backend, client, tiny_pdf):
    _seed_presigned(mock_backend)
    mock_backend.post("/v1/parse").mock(return_value=httpx.Response(503, text="upstream down"))

    with pytest.raises(ParseError) as ei:
        client.parse(tiny_pdf)
    assert ei.value.status_code == 503


def test_parse_request_timeout_raises_parse_error_504(mock_backend, client, tiny_pdf):
    _seed_presigned(mock_backend)
    mock_backend.post("/v1/parse").mock(side_effect=httpx.TimeoutException("timed out"))

    with pytest.raises(ParseError) as ei:
        client.parse(tiny_pdf)
    assert ei.value.status_code == 504


def test_parse_each_call_has_unique_request_id(mock_backend, client, tiny_pdf):
    _seed_presigned(mock_backend)
    route = mock_backend.post("/v1/parse").mock(
        return_value=httpx.Response(200, json=PARSE_RESPONSE),
    )
    client.parse(tiny_pdf)
    client.parse(tiny_pdf)

    ids = {c.request.headers["X-Request-ID"] for c in route.calls}
    assert len(ids) == 2  # two distinct UUIDs
