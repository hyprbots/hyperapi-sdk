"""Contract: process() uploads once, submits parse and extract concurrently,
shares a document key, and polls both operations with ``wait_for_jobs``.
"""

import httpx
import pytest

from hyperapi import ExtractError, ParseError


def _seed_upload(mock_backend, key="doc_proc"):
    mock_backend.post("/v1/documents/upload").mock(
        return_value=httpx.Response(200, json={
            "document_key": key,
            "upload_url": f"https://s3.local/{key}?sig=x",
            "expires_in": 600,
        }),
    )
    mock_backend.put(f"https://s3.local/{key}?sig=x").mock(return_value=httpx.Response(200))


def test_process_uploads_once_then_submits_both_legs_async(mock_backend, client, tiny_pdf):
    upload_route = mock_backend.post("/v1/documents/upload").mock(
        return_value=httpx.Response(200, json={
            "document_key": "doc_once",
            "upload_url": "https://s3.local/once?sig=x",
            "expires_in": 600,
        }),
    )
    s3_route = mock_backend.put("https://s3.local/once?sig=x").mock(
        return_value=httpx.Response(200),
    )
    parse_submit = mock_backend.post("/v1/parse").mock(
        return_value=httpx.Response(202, json={
            "job_id": "parse_J", "status": "pending", "poll_url": "/v1/jobs/parse_J",
        }),
    )
    extract_submit = mock_backend.post("/v1/extract").mock(
        return_value=httpx.Response(202, json={
            "job_id": "extract_J", "status": "pending", "poll_url": "/v1/jobs/extract_J",
        }),
    )
    # Production double-nests the parse result: the GET /v1/jobs envelope's
    # top-level `result` is the inference envelope, which itself nests the parse
    # payload under `result`. wait_for_jobs unwraps one level (leaving the
    # inference envelope), so the OCR text lives at result["result"]["ocr"].
    mock_backend.get("/v1/jobs/parse_J").mock(return_value=httpx.Response(
        200, json={
            "status": "completed",
            "result": {"result": {"ocr": "Hello world"}, "metadata": {"cached": False}},
        },
    ))
    mock_backend.get("/v1/jobs/extract_J").mock(return_value=httpx.Response(
        200, json={"status": "completed", "result": {"entities": {"foo": "bar"}}},
    ))

    result = client.process(tiny_pdf)

    # Both legs submitted with the SAME document_key
    parse_body = parse_submit.calls[0].request.read()
    extract_body = extract_submit.calls[0].request.read()
    assert b"document_key=doc_once" in parse_body
    assert b"document_key=doc_once" in extract_body

    # Both legs sent X-Async: true (so neither blocks under CloudFront)
    assert parse_submit.calls[0].request.headers["X-Async"] == "true"
    assert extract_submit.calls[0].request.headers["X-Async"] == "true"

    # Upload happened exactly once
    assert upload_route.call_count == 1
    assert s3_route.call_count == 1

    # Output shape: {ocr, data}
    assert result["ocr"] == "Hello world"
    assert result["data"] == {"entities": {"foo": "bar"}}


def test_process_parse_failure_surfaces_parse_error(mock_backend, client, tiny_pdf):
    _seed_upload(mock_backend)
    mock_backend.post("/v1/parse").mock(return_value=httpx.Response(202, json={
        "job_id": "p", "status": "pending", "poll_url": "/v1/jobs/p",
    }))
    mock_backend.post("/v1/extract").mock(return_value=httpx.Response(202, json={
        "job_id": "e", "status": "pending", "poll_url": "/v1/jobs/e",
    }))
    mock_backend.get("/v1/jobs/p").mock(return_value=httpx.Response(
        200, json={"status": "failed", "error": "ocr failed", "status_code": 500},
    ))
    mock_backend.get("/v1/jobs/e").mock(return_value=httpx.Response(
        200, json={"status": "pending"},
    ))

    with pytest.raises(ParseError):
        client.process(tiny_pdf)


def test_process_extract_failure_surfaces_extract_error(mock_backend, client, tiny_pdf):
    _seed_upload(mock_backend)
    mock_backend.post("/v1/parse").mock(return_value=httpx.Response(202, json={
        "job_id": "p", "status": "pending", "poll_url": "/v1/jobs/p",
    }))
    mock_backend.post("/v1/extract").mock(return_value=httpx.Response(202, json={
        "job_id": "e", "status": "pending", "poll_url": "/v1/jobs/e",
    }))
    mock_backend.get("/v1/jobs/p").mock(return_value=httpx.Response(
        200, json={"status": "completed", "result": {"ocr": "ok"}},
    ))
    mock_backend.get("/v1/jobs/e").mock(return_value=httpx.Response(
        200, json={"status": "failed", "error": "extract crashed"},
    ))

    with pytest.raises(ExtractError):
        client.process(tiny_pdf)


# ── Coverage: _submit_via_doc_key error paths (used only by process()) ──────


def test_process_submit_timeout_raises_504(mock_backend, client, tiny_pdf):
    """An httpx.TimeoutException on the parse-leg submit (which goes through
    _submit_via_doc_key) translates into the per-op error class with status_code=504."""
    _seed_upload(mock_backend)
    mock_backend.post("/v1/parse").mock(side_effect=httpx.TimeoutException("timeout"))

    with pytest.raises(ParseError) as ei:
        client.process(tiny_pdf)
    assert ei.value.status_code == 504
    assert ei.value.request_id


def test_process_submit_request_error_raises_parse_error(mock_backend, client, tiny_pdf):
    """A bare httpx.RequestError on the parse-leg submit surfaces as ParseError."""
    _seed_upload(mock_backend)
    mock_backend.post("/v1/parse").mock(side_effect=httpx.RequestError("connection reset"))

    with pytest.raises(ParseError) as ei:
        client.process(tiny_pdf)
    assert "Request failed" in str(ei.value)
    assert ei.value.request_id


def test_process_submit_unexpected_status_raises(mock_backend, client, tiny_pdf):
    """A 500 from the parse-leg submit (no well-known mapping, just an upstream
    crash) falls through to the generic non-(200,202) branch and surfaces as
    ParseError carrying the server message + status_code."""
    _seed_upload(mock_backend)
    mock_backend.post("/v1/parse").mock(
        return_value=httpx.Response(500, json={"message": "parse pipeline crashed"}),
    )

    with pytest.raises(ParseError) as ei:
        client.process(tiny_pdf)
    assert ei.value.status_code == 500
    assert "parse pipeline crashed" in str(ei.value)
