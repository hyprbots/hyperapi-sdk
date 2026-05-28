"""Async twin of test_contract_parse.py.

Verifies AsyncHyperAPIClient.parse() — submit + poll + result handling.
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


async def test_async_parse_submits_with_x_async_then_polls(mock_backend, async_client, tiny_pdf):
    _seed_presigned(mock_backend)
    submit_route = _seed_submit(mock_backend)
    poll_route = _seed_completed(mock_backend, ocr="Invoice #12345")

    result = await async_client.parse(tiny_pdf)

    assert result["ocr"] == "Invoice #12345"
    submit_req = submit_route.calls[0].request
    assert submit_req.headers["X-Async"] == "true"
    assert submit_req.url.params["ocr_engine"] == "paddle"
    assert b"document_key=doc_parse" in submit_req.read()
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


async def test_async_parse_default_sends_include_boxes_false(mock_backend, async_client, tiny_pdf):
    _seed_presigned(mock_backend)
    submit_route = _seed_submit(mock_backend)
    _seed_completed(mock_backend)

    await async_client.parse(tiny_pdf)

    assert submit_route.calls[0].request.url.params["include_boxes"] == "false"


async def test_async_parse_include_boxes_propagates_and_returns_boxes(
    mock_backend, async_client, tiny_pdf
):
    _seed_presigned(mock_backend)
    submit_route = _seed_submit(mock_backend)
    _seed_completed_with_boxes(mock_backend)

    result = await async_client.parse(tiny_pdf, include_boxes=True)

    assert submit_route.calls[0].request.url.params["include_boxes"] == "true"
    box = result["pages"][0]["boxes"][0]
    assert box["text"] == "Invoice #4471"
    assert box["bbox"] == [120, 340, 410, 372]
    assert box["confidence"] == 0.987


async def test_async_parse_with_doc_intent_engine(mock_backend, async_client, tiny_pdf):
    _seed_presigned(mock_backend)
    submit_route = _seed_submit(mock_backend)
    _seed_completed(mock_backend)

    await async_client.parse(tiny_pdf, ocr_engine="doc-intent")

    assert submit_route.calls[0].request.url.params["ocr_engine"] == "doc-intent"


async def test_async_parse_image_path_alias_still_works(mock_backend, async_client, tiny_png):
    _seed_presigned(mock_backend)
    _seed_submit(mock_backend)
    _seed_completed(mock_backend, ocr="image text")

    result = await async_client.parse(image_path=tiny_png)

    assert result["ocr"] == "image text"


async def test_async_parse_use_presigned_false_sends_multipart(mock_backend, async_client, tiny_pdf):
    submit_route = _seed_submit(mock_backend)
    _seed_completed(mock_backend)

    await async_client.parse(tiny_pdf, use_presigned=False)

    req = submit_route.calls[0].request
    assert "multipart/form-data" in req.headers["content-type"]
    body = req.read()
    assert b"doc.pdf" in body
    assert b"application/pdf" in body
    assert req.headers["X-Async"] == "true"


async def test_async_parse_submit_401_raises_authentication_error(mock_backend, async_client, tiny_pdf):
    _seed_presigned(mock_backend)
    mock_backend.post("/v1/parse").mock(return_value=httpx.Response(401))

    with pytest.raises(AuthenticationError) as ei:
        await async_client.parse(tiny_pdf)
    assert ei.value.status_code == 401


async def test_async_parse_submit_500_raises_parse_error(mock_backend, async_client, tiny_pdf):
    _seed_presigned(mock_backend)
    mock_backend.post("/v1/parse").mock(
        return_value=httpx.Response(500, json={"message": "service down"}),
    )

    with pytest.raises(ParseError) as ei:
        await async_client.parse(tiny_pdf)
    assert ei.value.status_code == 500


async def test_async_parse_failed_job_raises_parse_error(mock_backend, async_client, tiny_pdf):
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
        await async_client.parse(tiny_pdf)
    assert "OCR pipeline failed" in str(ei.value)


async def test_async_submit_parse_returns_job_no_polling(mock_backend, async_client, tiny_pdf):
    _seed_presigned(mock_backend)
    submit_route = _seed_submit(mock_backend, job_id="my_job_id")
    poll_route = mock_backend.get("/v1/jobs/my_job_id")

    job = await async_client.submit_parse(tiny_pdf)

    assert isinstance(job, Job)
    assert job.job_id == "my_job_id"
    assert job.op == "parse"
    assert job.status == "pending"
    assert submit_route.called
    assert not poll_route.called


async def test_async_parse_per_call_poll_overrides_propagate(mock_backend, async_client, tiny_pdf):
    _seed_presigned(mock_backend)
    _seed_submit(mock_backend)
    mock_backend.get("/v1/jobs/job_parse_1").mock(
        return_value=httpx.Response(200, json={"status": "pending"}),
    )

    from hyperapi import JobTimeoutError
    with pytest.raises(JobTimeoutError):
        await async_client.parse(tiny_pdf, poll_timeout=0.05, poll_interval=0.01)


async def test_async_parse_each_call_has_unique_request_id(mock_backend, async_client, tiny_pdf):
    _seed_presigned(mock_backend)
    submit_route = _seed_submit(mock_backend)
    _seed_completed(mock_backend)

    await async_client.parse(tiny_pdf)
    await async_client.parse(tiny_pdf)

    ids = {c.request.headers["X-Request-ID"] for c in submit_route.calls}
    assert len(ids) == 2
