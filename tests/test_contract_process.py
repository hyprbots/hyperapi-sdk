"""Contract: process() — uploads once, then calls parse + extract sharing the document_key.

This is the user-visible router-OCR-cache optimization: parse runs OCR, extract
re-uses it via Redis cache. The SDK side just makes the calls in order; we assert
both calls land with the same document_key.
"""

import httpx
import pytest

from hyperapi import ExtractError, ParseError


PARSE_RESPONSE = {"request_id": "p", "result": {"ocr": "Hello world", "pages": 1}}
EXTRACT_RESPONSE = {"request_id": "e", "result": {"entities": {"foo": "bar"}}}


def _seed_upload(mock_backend, key="doc_proc"):
    mock_backend.post("/v1/documents/upload").mock(
        return_value=httpx.Response(200, json={
            "document_key": key, "upload_url": f"https://s3.local/{key}?sig=x", "expires_in": 600,
        }),
    )
    mock_backend.put(f"https://s3.local/{key}?sig=x").mock(return_value=httpx.Response(200))


def test_process_uploads_once_then_calls_parse_and_extract(mock_backend, client, tiny_pdf):
    upload_route = mock_backend.post("/v1/documents/upload").mock(
        return_value=httpx.Response(200, json={
            "document_key": "doc_once", "upload_url": "https://s3.local/once?sig=x",
            "expires_in": 600,
        }),
    )
    s3_route = mock_backend.put("https://s3.local/once?sig=x").mock(
        return_value=httpx.Response(200),
    )
    parse_route = mock_backend.post("/v1/parse").mock(
        return_value=httpx.Response(200, json=PARSE_RESPONSE),
    )
    extract_route = mock_backend.post("/v1/extract").mock(
        return_value=httpx.Response(200, json=EXTRACT_RESPONSE),
    )

    result = client.process(tiny_pdf)

    # Both calls used the same document_key (this is the whole point of process())
    parse_body = parse_route.calls[0].request.read()
    extract_body = extract_route.calls[0].request.read()
    assert b"document_key=doc_once" in parse_body
    assert b"document_key=doc_once" in extract_body

    # Upload happened exactly once
    assert upload_route.call_count == 1
    assert s3_route.call_count == 1

    # Output shape: {ocr, data}
    assert result["ocr"] == "Hello world"
    assert result["data"]["entities"] == {"foo": "bar"}


def test_process_parse_failure_surfaces_parse_error(mock_backend, client, tiny_pdf):
    _seed_upload(mock_backend)
    mock_backend.post("/v1/parse").mock(return_value=httpx.Response(500, text="upstream"))
    mock_backend.post("/v1/extract").mock(return_value=httpx.Response(200, json=EXTRACT_RESPONSE))

    with pytest.raises(ParseError):
        client.process(tiny_pdf)


def test_process_extract_failure_surfaces_extract_error(mock_backend, client, tiny_pdf):
    _seed_upload(mock_backend)
    mock_backend.post("/v1/parse").mock(return_value=httpx.Response(200, json=PARSE_RESPONSE))
    mock_backend.post("/v1/extract").mock(return_value=httpx.Response(500))

    with pytest.raises(ExtractError):
        client.process(tiny_pdf)
