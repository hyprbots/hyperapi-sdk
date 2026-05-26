"""Async twin of test_contract_classify_split.py."""

import httpx
import pytest

from hyperapi import ClassifyError, Job


def _seed(mock_backend, key, op_endpoint, *, job_id, result):
    mock_backend.post("/v1/documents/upload").mock(
        return_value=httpx.Response(200, json={
            "document_key": key,
            "upload_url": f"https://s3.local/{key}?sig=x",
            "expires_in": 600,
        }),
    )
    mock_backend.put(f"https://s3.local/{key}?sig=x").mock(return_value=httpx.Response(200))
    submit = mock_backend.post(op_endpoint).mock(
        return_value=httpx.Response(202, json={
            "job_id": job_id, "status": "pending", "poll_url": f"/v1/jobs/{job_id}",
        }),
    )
    mock_backend.get(f"/v1/jobs/{job_id}").mock(
        return_value=httpx.Response(200, json={
            "status": "completed", "result": result, "request_id": "req-x",
        }),
    )
    return submit


# ── classify ─────────────────────────────────────────────────────────────


async def test_async_classify_submits_with_x_async(mock_backend, async_client, tiny_pdf):
    submit = _seed(
        mock_backend, "doc_cl", "/v1/classify",
        job_id="job_cl_1",
        result={"document_type": "invoice", "confidence": 0.97},
    )

    result = await async_client.classify(tiny_pdf)

    assert result["document_type"] == "invoice"
    req = submit.calls[0].request
    assert req.headers["X-Async"] == "true"
    assert req.url.params["mode"] == "default"
    assert b"document_key=doc_cl" in req.read()


async def test_async_classify_failed_job_raises_classify_error(mock_backend, async_client, tiny_pdf):
    mock_backend.post("/v1/documents/upload").mock(
        return_value=httpx.Response(200, json={
            "document_key": "k", "upload_url": "https://s3.local/k?sig=x", "expires_in": 600,
        }),
    )
    mock_backend.put("https://s3.local/k?sig=x").mock(return_value=httpx.Response(200))
    mock_backend.post("/v1/classify").mock(return_value=httpx.Response(202, json={
        "job_id": "j", "status": "pending", "poll_url": "/v1/jobs/j",
    }))
    mock_backend.get("/v1/jobs/j").mock(return_value=httpx.Response(200, json={
        "status": "failed", "error": "model unavailable", "error_status_code": 503,
    }))

    with pytest.raises(ClassifyError) as ei:
        await async_client.classify(tiny_pdf)
    assert "model unavailable" in str(ei.value)


async def test_async_submit_classify_returns_job(mock_backend, async_client, tiny_pdf):
    _seed(
        mock_backend, "doc_cl2", "/v1/classify",
        job_id="cj_1",
        result={"document_type": "receipt"},
    )

    job = await async_client.submit_classify(tiny_pdf)

    assert isinstance(job, Job)
    assert job.op == "classify"


# ── split ────────────────────────────────────────────────────────────────


async def test_async_split_submits_with_x_async(mock_backend, async_client, tiny_pdf):
    submit = _seed(
        mock_backend, "doc_sp", "/v1/split",
        job_id="job_sp_1",
        result={"segments": [{"start": 1, "end": 3}, {"start": 4, "end": 5}]},
    )

    result = await async_client.split(tiny_pdf)

    assert len(result["segments"]) == 2
    assert submit.calls[0].request.url.params["mode"] == "default"


async def test_async_split_429_raises_rate_limit_error(mock_backend, async_client, tiny_pdf):
    mock_backend.post("/v1/documents/upload").mock(
        return_value=httpx.Response(200, json={
            "document_key": "k", "upload_url": "https://s3.local/k?sig=x", "expires_in": 600,
        }),
    )
    mock_backend.put("https://s3.local/k?sig=x").mock(return_value=httpx.Response(200))
    mock_backend.post("/v1/split").mock(return_value=httpx.Response(
        429,
        headers={"Retry-After": "60"},
        json={"message": "Rate limit exceeded", "tier": "free", "limit": 1},
    ))

    from hyperapi import RateLimitError
    with pytest.raises(RateLimitError) as ei:
        await async_client.split(tiny_pdf)
    assert ei.value.retry_after == 60
    assert ei.value.tier == "free"
    assert ei.value.limit == 1


async def test_async_submit_split_returns_job(mock_backend, async_client, tiny_pdf):
    _seed(
        mock_backend, "doc_sp2", "/v1/split",
        job_id="sj_1",
        result={"segments": []},
    )

    job = await async_client.submit_split(tiny_pdf)

    assert isinstance(job, Job)
    assert job.op == "split"
