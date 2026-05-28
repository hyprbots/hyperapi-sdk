"""Contract: parse() — submit (X-Async:true) + poll until completed.

Covers:
  - Default convenience method submits with X-Async: true and polls under the hood
  - Per-call ocr_engine and use_presigned overrides propagate to the submit POST
  - submit_parse() returns a Job and does NOT poll
  - Error mapping on submit (401 → AuthenticationError, 5xx → ParseError)
"""

import httpx
import pytest

from hyperapi import AuthenticationError, Job, ParseError


def _seed_presigned(mock_backend, *, key="doc_parse"):
    mock_backend.post("/v1/documents/upload").mock(
        return_value=httpx.Response(200, json={
            "document_key": key,
            "upload_url": f"https://s3.local/{key}?sig=x",
            "expires_in": 600,
        }),
    )
    mock_backend.put(f"https://s3.local/{key}?sig=x").mock(return_value=httpx.Response(200))


def _seed_submit(mock_backend, *, job_id="job_parse_1"):
    return mock_backend.post("/v1/parse").mock(
        return_value=httpx.Response(202, json={
            "job_id": job_id,
            "status": "pending",
            "poll_url": f"/v1/jobs/{job_id}",
        }),
    )


def _seed_completed(mock_backend, *, job_id="job_parse_1", ocr="Hello"):
    return mock_backend.get(f"/v1/jobs/{job_id}").mock(
        return_value=httpx.Response(200, json={
            "status": "completed",
            "result": {"ocr": ocr, "pages": 1},
            "request_id": "req-x",
            "duration_ms": 1234,
        }),
    )


def test_parse_submits_with_x_async_then_polls(mock_backend, client, tiny_pdf):
    _seed_presigned(mock_backend)
    submit_route = _seed_submit(mock_backend)
    poll_route = _seed_completed(mock_backend, ocr="Invoice #12345")

    result = client.parse(tiny_pdf)

    assert result["ocr"] == "Invoice #12345"
    # Submit happened with X-Async: true
    submit_req = submit_route.calls[0].request
    assert submit_req.headers["X-Async"] == "true"
    assert submit_req.url.params["ocr_engine"] == "paddle"
    assert b"document_key=doc_parse" in submit_req.read()
    # Poll happened
    assert poll_route.called


def _seed_completed_with_boxes(mock_backend, *, job_id="job_parse_1"):
    boxes = [{"text": "Invoice #4471", "bbox": [120, 340, 410, 372], "confidence": 0.987}]
    return mock_backend.get(f"/v1/jobs/{job_id}").mock(
        return_value=httpx.Response(200, json={
            "status": "completed",
            "result": {
                "ocr": "Invoice #4471",
                "pages": [{"page_number": 1, "text": "Invoice #4471", "boxes": boxes}],
            },
            "request_id": "req-x",
            "duration_ms": 1234,
        }),
    )


def _seed_completed_with_image(mock_backend, *, job_id="job_parse_1"):
    image_url = "https://s3.example.com/deskewed/org/hash/0.webp?sig=xxx"
    boxes = [{"text": "Invoice #4471", "bbox": [120, 340, 410, 372], "confidence": 0.987}]
    return mock_backend.get(f"/v1/jobs/{job_id}").mock(
        return_value=httpx.Response(200, json={
            "status": "completed",
            "result": {
                "ocr": "Invoice #4471",
                "pages": [{
                    "page_number": 1,
                    "text": "Invoice #4471",
                    "image_url": image_url,
                    "dimensions": {"width": 824, "height": 1066},
                    "boxes": boxes,
                }],
            },
            "request_id": "req-x",
            "duration_ms": 1234,
        }),
    )


def test_parse_default_sends_include_boxes_false(mock_backend, client, tiny_pdf):
    _seed_presigned(mock_backend)
    submit_route = _seed_submit(mock_backend)
    _seed_completed(mock_backend)

    client.parse(tiny_pdf)

    assert submit_route.calls[0].request.url.params["include_boxes"] == "false"


def test_parse_default_sends_include_image_false(mock_backend, client, tiny_pdf):
    _seed_presigned(mock_backend)
    submit_route = _seed_submit(mock_backend)
    _seed_completed(mock_backend)

    client.parse(tiny_pdf)

    assert submit_route.calls[0].request.url.params["include_image"] == "false"


def test_parse_include_image_propagates_and_returns_image_url(mock_backend, client, tiny_pdf):
    _seed_presigned(mock_backend)
    submit_route = _seed_submit(mock_backend)
    _seed_completed_with_image(mock_backend)

    result = client.parse(tiny_pdf, include_image=True)

    assert submit_route.calls[0].request.url.params["include_image"] == "true"
    page = result["pages"][0]
    assert "s3.example.com" in page["image_url"]
    assert page["dimensions"] == {"width": 824, "height": 1066}


def test_submit_parse_include_image_propagates(mock_backend, client, tiny_pdf):
    _seed_presigned(mock_backend)
    submit_route = _seed_submit(mock_backend)

    client.submit_parse(tiny_pdf, include_image=True)

    assert submit_route.calls[0].request.url.params["include_image"] == "true"


def test_parse_include_boxes_propagates_and_returns_boxes(mock_backend, client, tiny_pdf):
    _seed_presigned(mock_backend)
    submit_route = _seed_submit(mock_backend)
    _seed_completed_with_boxes(mock_backend)

    result = client.parse(tiny_pdf, include_boxes=True)

    # Flag reached the submit POST as a query param.
    assert submit_route.calls[0].request.url.params["include_boxes"] == "true"
    # Boxes flow through the dict-passthrough result untouched.
    box = result["pages"][0]["boxes"][0]
    assert box["text"] == "Invoice #4471"
    assert box["bbox"] == [120, 340, 410, 372]
    assert box["confidence"] == 0.987


def test_submit_parse_include_boxes_propagates(mock_backend, client, tiny_pdf):
    _seed_presigned(mock_backend)
    submit_route = _seed_submit(mock_backend)

    client.submit_parse(tiny_pdf, include_boxes=True)

    assert submit_route.calls[0].request.url.params["include_boxes"] == "true"


def test_parse_with_doc_intent_engine(mock_backend, client, tiny_pdf):
    _seed_presigned(mock_backend)
    submit_route = _seed_submit(mock_backend)
    _seed_completed(mock_backend)

    client.parse(tiny_pdf, ocr_engine="doc-intent")

    assert submit_route.calls[0].request.url.params["ocr_engine"] == "doc-intent"


def test_parse_image_path_alias_still_works(mock_backend, client, tiny_png):
    _seed_presigned(mock_backend)
    _seed_submit(mock_backend)
    _seed_completed(mock_backend, ocr="image text")

    result = client.parse(image_path=tiny_png)

    assert result["ocr"] == "image text"


def test_parse_use_presigned_false_sends_multipart(mock_backend, client, tiny_pdf):
    submit_route = _seed_submit(mock_backend)
    _seed_completed(mock_backend)

    client.parse(tiny_pdf, use_presigned=False)

    req = submit_route.calls[0].request
    assert "multipart/form-data" in req.headers["content-type"]
    body = req.read()
    assert b"doc.pdf" in body
    assert b"application/pdf" in body
    assert req.headers["X-Async"] == "true"


def test_parse_submit_401_raises_authentication_error(mock_backend, client, tiny_pdf):
    _seed_presigned(mock_backend)
    mock_backend.post("/v1/parse").mock(return_value=httpx.Response(401))

    with pytest.raises(AuthenticationError) as ei:
        client.parse(tiny_pdf)
    assert ei.value.status_code == 401


def test_parse_submit_500_raises_parse_error(mock_backend, client, tiny_pdf):
    _seed_presigned(mock_backend)
    mock_backend.post("/v1/parse").mock(
        return_value=httpx.Response(500, json={"message": "service down"}),
    )

    with pytest.raises(ParseError) as ei:
        client.parse(tiny_pdf)
    assert ei.value.status_code == 500


def test_parse_failed_job_raises_parse_error(mock_backend, client, tiny_pdf):
    _seed_presigned(mock_backend)
    _seed_submit(mock_backend)
    mock_backend.get("/v1/jobs/job_parse_1").mock(
        return_value=httpx.Response(200, json={
            "status": "failed",
            "error": "OCR pipeline failed",
            "error_status_code": 500,
            "request_id": "req-x",
        }),
    )

    with pytest.raises(ParseError) as ei:
        client.parse(tiny_pdf)
    assert "OCR pipeline failed" in str(ei.value)


def test_submit_parse_returns_job_no_polling(mock_backend, client, tiny_pdf):
    """submit_parse should return immediately with a Job; never call /v1/jobs/."""
    _seed_presigned(mock_backend)
    submit_route = _seed_submit(mock_backend, job_id="my_job_id")
    poll_route = mock_backend.get("/v1/jobs/my_job_id")

    job = client.submit_parse(tiny_pdf)

    assert isinstance(job, Job)
    assert job.job_id == "my_job_id"
    assert job.op == "parse"
    assert job.status == "pending"
    assert submit_route.called
    assert not poll_route.called  # NO polling happened


def test_parse_per_call_poll_overrides_propagate(mock_backend, client, tiny_pdf):
    """Per-call poll_timeout should override the constructor's value."""
    _seed_presigned(mock_backend)
    _seed_submit(mock_backend)
    mock_backend.get("/v1/jobs/job_parse_1").mock(
        return_value=httpx.Response(200, json={"status": "pending"}),
    )

    # Use a tiny per-call timeout to force fast JobTimeoutError
    from hyperapi import JobTimeoutError
    with pytest.raises(JobTimeoutError):
        client.parse(tiny_pdf, poll_timeout=0.05, poll_interval=0.01)


def test_parse_each_call_has_unique_request_id(mock_backend, client, tiny_pdf):
    _seed_presigned(mock_backend)
    submit_route = _seed_submit(mock_backend)
    _seed_completed(mock_backend)

    client.parse(tiny_pdf)
    client.parse(tiny_pdf)

    ids = {c.request.headers["X-Request-ID"] for c in submit_route.calls}
    assert len(ids) == 2  # two distinct UUIDs
