"""Contract: extract() — submit (X-Async:true) + poll until completed.

Extract is the original motivation for v0.1.0's submit+poll: production
extracts regularly take 60-180 s and would 504 at CloudFront's 30 s edge
under direct sync. With submit+poll, each HTTP call stays sub-second.
"""

import httpx
import pytest

from hyperapi import ExtractError, Job, JobTimeoutError


def _seed_presigned(mock_backend, *, key="doc_ex"):
    mock_backend.post("/v1/documents/upload").mock(
        return_value=httpx.Response(200, json={
            "document_key": key,
            "upload_url": f"https://s3.local/{key}?sig=x",
            "expires_in": 600,
        }),
    )
    mock_backend.put(f"https://s3.local/{key}?sig=x").mock(return_value=httpx.Response(200))


def _seed_submit(mock_backend, *, job_id="job_ex_1"):
    return mock_backend.post("/v1/extract").mock(
        return_value=httpx.Response(202, json={
            "job_id": job_id, "status": "pending", "poll_url": f"/v1/jobs/{job_id}",
        }),
    )


def _seed_completed(mock_backend, *, job_id="job_ex_1", entities=None):
    return mock_backend.get(f"/v1/jobs/{job_id}").mock(
        return_value=httpx.Response(200, json={
            "status": "completed",
            "result": {
                "entities": entities or {"vendor": "Acme"},
                "line_items": [],
                "ocr_text": "Invoice text...",
            },
            "request_id": "req-x",
            "duration_ms": 76000,
        }),
    )


def test_extract_default_path_uses_async(mock_backend, client, tiny_pdf):
    _seed_presigned(mock_backend)
    submit_route = _seed_submit(mock_backend)
    poll_route = _seed_completed(mock_backend)

    result = client.extract(tiny_pdf)

    assert result["entities"]["vendor"] == "Acme"
    submit_req = submit_route.calls[0].request
    assert submit_req.headers["X-Async"] == "true"
    assert submit_req.url.params["ocr_engine"] == "paddle"
    assert submit_req.url.params["mode"] == "default"
    assert poll_route.called


def test_extract_custom_mode_propagates(mock_backend, client, tiny_pdf):
    _seed_presigned(mock_backend)
    submit_route = _seed_submit(mock_backend)
    _seed_completed(mock_backend)

    client.extract(tiny_pdf, mode="strict")

    assert submit_route.calls[0].request.url.params["mode"] == "strict"


def test_extract_pending_then_completed_polls_loop(mock_backend, client, tiny_pdf):
    """Slow extract — pending → pending → completed."""
    _seed_presigned(mock_backend)
    _seed_submit(mock_backend)
    mock_backend.get("/v1/jobs/job_ex_1").mock(side_effect=[
        httpx.Response(200, json={"status": "pending"}),
        httpx.Response(200, json={"status": "pending"}),
        httpx.Response(200, json={
            "status": "completed",
            "result": {"entities": {"foo": "bar"}},
        }),
    ])

    result = client.extract(tiny_pdf)

    assert result["entities"] == {"foo": "bar"}


def test_extract_failed_job_raises_extract_error(mock_backend, client, tiny_pdf):
    """A `failed` job envelope on extract surfaces as ExtractError, not generic."""
    _seed_presigned(mock_backend)
    _seed_submit(mock_backend)
    mock_backend.get("/v1/jobs/job_ex_1").mock(return_value=httpx.Response(
        200, json={
            "status": "failed",
            "error": "extract pipeline crashed",
            "error_status_code": 500,
        },
    ))

    with pytest.raises(ExtractError) as ei:
        client.extract(tiny_pdf)
    assert "extract pipeline crashed" in str(ei.value)


def test_extract_submit_5xx_raises_extract_error(mock_backend, client, tiny_pdf):
    _seed_presigned(mock_backend)
    mock_backend.post("/v1/extract").mock(return_value=httpx.Response(500))

    with pytest.raises(ExtractError) as ei:
        client.extract(tiny_pdf)
    assert ei.value.status_code == 500


def test_extract_submit_timeout_raises_504(mock_backend, client, tiny_pdf):
    _seed_presigned(mock_backend)
    mock_backend.post("/v1/extract").mock(side_effect=httpx.TimeoutException("timeout"))

    with pytest.raises(ExtractError) as ei:
        client.extract(tiny_pdf)
    assert ei.value.status_code == 504


def test_submit_extract_returns_job_no_polling(mock_backend, client, tiny_pdf):
    _seed_presigned(mock_backend)
    submit_route = _seed_submit(mock_backend, job_id="custom_id")
    poll_route = mock_backend.get("/v1/jobs/custom_id")

    job = client.submit_extract(tiny_pdf)

    assert isinstance(job, Job)
    assert job.op == "extract"
    assert job.job_id == "custom_id"
    assert submit_route.called
    assert not poll_route.called


def test_extract_submit_402_insufficient_credits_raises(mock_backend, client, tiny_pdf):
    """A 402 on the per-op submit POST surfaces as the per-op error class
    (ExtractError here) with the billing-URL hint baked into the message."""
    _seed_presigned(mock_backend)
    mock_backend.post("/v1/extract").mock(
        return_value=httpx.Response(402, json={"message": "Insufficient credits"}),
    )

    with pytest.raises(ExtractError) as ei:
        client.extract(tiny_pdf)
    assert ei.value.status_code == 402
    assert "billing" in str(ei.value).lower() or "credits" in str(ei.value).lower()


def test_extract_submit_408_service_deadline_raises(mock_backend, client, tiny_pdf):
    """408 from the upstream service translates into the per-op error class."""
    _seed_presigned(mock_backend)
    mock_backend.post("/v1/extract").mock(
        return_value=httpx.Response(408, json={"message": "Service deadline exceeded"}),
    )

    with pytest.raises(ExtractError) as ei:
        client.extract(tiny_pdf)
    assert ei.value.status_code == 408


def test_extract_submit_403_forbidden_raises_generic_hyperapi_error(mock_backend, client, tiny_pdf):
    """A 403 on submit raises the generic HyperAPIError — it's a policy/IAM problem."""
    from hyperapi import HyperAPIError

    _seed_presigned(mock_backend)
    mock_backend.post("/v1/extract").mock(
        return_value=httpx.Response(403, json={"message": "Forbidden by org policy"}),
    )

    with pytest.raises(HyperAPIError) as ei:
        client.extract(tiny_pdf)
    assert ei.value.status_code == 403


def test_extract_submit_request_error_raises_extract_error(mock_backend, client, tiny_pdf):
    """A bare httpx.RequestError on the submit POST (DNS / connect-reset)
    must be caught and surface as the per-op error class with the request_id
    we generated on the way out."""
    _seed_presigned(mock_backend)
    mock_backend.post("/v1/extract").mock(
        side_effect=httpx.RequestError("dns failed"),
    )

    with pytest.raises(ExtractError) as ei:
        client.extract(tiny_pdf)
    assert "Request failed" in str(ei.value)
    assert ei.value.request_id


def test_extract_per_call_timeout_raises_job_timeout(mock_backend, client, tiny_pdf):
    """Per-call poll_timeout overrides constructor — useful for fast-fail."""
    _seed_presigned(mock_backend)
    _seed_submit(mock_backend)
    mock_backend.get("/v1/jobs/job_ex_1").mock(
        return_value=httpx.Response(200, json={"status": "pending"}),
    )

    with pytest.raises(JobTimeoutError):
        client.extract(tiny_pdf, poll_timeout=0.05, poll_interval=0.01)
