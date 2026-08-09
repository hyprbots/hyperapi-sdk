"""Async twin of test_contract_extract.py.

Extract is the original motivation for submit+poll — production extracts run
60-180s, so async support matters most here. Same coverage as the sync suite.
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


async def test_async_extract_default_path_uses_async(mock_backend, async_client, tiny_pdf):
    _seed_presigned(mock_backend)
    submit_route = _seed_submit(mock_backend)
    poll_route = _seed_completed(mock_backend)

    result = await async_client.extract(tiny_pdf)

    assert result["entities"]["vendor"] == "Acme"
    submit_req = submit_route.calls[0].request
    assert submit_req.headers["X-Async"] == "true"
    assert "ocr_engine" not in submit_req.url.params
    # Basic extract carries only `category`; `mode` was removed (parity with sync).
    assert "mode" not in submit_req.url.params
    # Omitted so the router's request > org > platform precedence can reach
    # the org tier. Sending it unconditionally is what made an org's
    # defaults.extract_category unreachable from the SDK.
    assert "category" not in submit_req.url.params
    assert poll_route.called


async def test_async_extract_rejects_mode_kwarg(async_client, tiny_pdf):
    # Parity with sync: `mode` is gone from Basic extract; Advanced is
    # extract_advanced(). The silent-misroute footgun is unrepresentable.
    with pytest.raises(TypeError):
        await async_client.extract(tiny_pdf, mode="advanced")


async def test_async_extract_pending_then_completed_polls_loop(mock_backend, async_client, tiny_pdf):
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

    result = await async_client.extract(tiny_pdf)

    assert result["entities"] == {"foo": "bar"}


async def test_async_extract_failed_job_raises_extract_error(mock_backend, async_client, tiny_pdf):
    _seed_presigned(mock_backend)
    _seed_submit(mock_backend)
    mock_backend.get("/v1/jobs/job_ex_1").mock(return_value=httpx.Response(
        200, json={
            "status": "failed",
            "error": "extract pipeline crashed",
            "status_code": 500,
        },
    ))

    with pytest.raises(ExtractError) as ei:
        await async_client.extract(tiny_pdf)
    assert "extract pipeline crashed" in str(ei.value)


async def test_async_extract_submit_5xx_raises_extract_error(mock_backend, async_client, tiny_pdf):
    _seed_presigned(mock_backend)
    mock_backend.post("/v1/extract").mock(return_value=httpx.Response(500))

    with pytest.raises(ExtractError) as ei:
        await async_client.extract(tiny_pdf)
    assert ei.value.status_code == 500


async def test_async_extract_submit_timeout_raises_504(mock_backend, async_client, tiny_pdf):
    _seed_presigned(mock_backend)
    mock_backend.post("/v1/extract").mock(side_effect=httpx.TimeoutException("timeout"))

    with pytest.raises(ExtractError) as ei:
        await async_client.extract(tiny_pdf)
    assert ei.value.status_code == 504


async def test_async_submit_extract_returns_job_no_polling(mock_backend, async_client, tiny_pdf):
    _seed_presigned(mock_backend)
    submit_route = _seed_submit(mock_backend, job_id="custom_id")
    poll_route = mock_backend.get("/v1/jobs/custom_id")

    job = await async_client.submit_extract(tiny_pdf)

    assert isinstance(job, Job)
    assert job.op == "extract"
    assert job.job_id == "custom_id"
    assert submit_route.called
    assert not poll_route.called


async def test_async_extract_submit_402_insufficient_credits_raises(
    mock_backend, async_client, tiny_pdf,
):
    _seed_presigned(mock_backend)
    mock_backend.post("/v1/extract").mock(
        return_value=httpx.Response(402, json={"message": "Insufficient credits"}),
    )

    with pytest.raises(ExtractError) as ei:
        await async_client.extract(tiny_pdf)
    assert ei.value.status_code == 402
    assert "billing" in str(ei.value).lower() or "credits" in str(ei.value).lower()


async def test_async_extract_submit_408_service_deadline_raises(
    mock_backend, async_client, tiny_pdf,
):
    _seed_presigned(mock_backend)
    mock_backend.post("/v1/extract").mock(
        return_value=httpx.Response(408, json={"message": "Service deadline exceeded"}),
    )

    with pytest.raises(ExtractError) as ei:
        await async_client.extract(tiny_pdf)
    assert ei.value.status_code == 408


async def test_async_extract_submit_403_forbidden_raises_generic_hyperapi_error(
    mock_backend, async_client, tiny_pdf,
):
    from hyperapi import HyperAPIError

    _seed_presigned(mock_backend)
    mock_backend.post("/v1/extract").mock(
        return_value=httpx.Response(403, json={"message": "Forbidden by org policy"}),
    )

    with pytest.raises(HyperAPIError) as ei:
        await async_client.extract(tiny_pdf)
    assert ei.value.status_code == 403


async def test_async_extract_submit_request_error_raises_extract_error(
    mock_backend, async_client, tiny_pdf,
):
    _seed_presigned(mock_backend)
    mock_backend.post("/v1/extract").mock(
        side_effect=httpx.RequestError("dns failed"),
    )

    with pytest.raises(ExtractError) as ei:
        await async_client.extract(tiny_pdf)
    assert "Request failed" in str(ei.value)
    assert ei.value.request_id


async def test_async_extract_per_call_timeout_raises_job_timeout(
    mock_backend, async_client, tiny_pdf,
):
    _seed_presigned(mock_backend)
    _seed_submit(mock_backend)
    mock_backend.get("/v1/jobs/job_ex_1").mock(
        return_value=httpx.Response(200, json={"status": "pending"}),
    )

    with pytest.raises(JobTimeoutError):
        await async_client.extract(tiny_pdf, poll_timeout=0.05, poll_interval=0.01)
