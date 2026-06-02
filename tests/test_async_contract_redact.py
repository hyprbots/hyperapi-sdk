"""Contract: async redact() / deidentify — submit (X-Async:true) + poll."""

import httpx
import pytest

from hyperapi import Job, RedactError


def _seed_presigned(mock_backend, *, key="doc_rd"):
    mock_backend.post("/v1/documents/upload").mock(
        return_value=httpx.Response(200, json={
            "document_key": key,
            "upload_url": f"https://s3.local/{key}?sig=x",
            "expires_in": 600,
        }),
    )
    mock_backend.put(f"https://s3.local/{key}?sig=x").mock(return_value=httpx.Response(200))


def _seed_submit(mock_backend, *, job_id="job_rd_1"):
    return mock_backend.post("/v1/redact").mock(
        return_value=httpx.Response(202, json={
            "job_id": job_id, "status": "pending", "poll_url": f"/v1/jobs/{job_id}",
        }),
    )


def _seed_completed(mock_backend, *, job_id="job_rd_1"):
    return mock_backend.get(f"/v1/jobs/{job_id}").mock(
        return_value=httpx.Response(200, json={
            "status": "completed",
            "result": {
                "images": ["UkVEQUNURUQ="],
                "summary": {"PERSON_NAME": 2},
                "updated_text_block": "Name: xxxx",
                "mode": "redact",
            },
            "request_id": "req-rd",
            "duration_ms": 5000,
        }),
    )


async def test_async_redact_default_uses_async_with_mode_and_logos(mock_backend, async_client, tiny_pdf):
    _seed_presigned(mock_backend)
    submit_route = _seed_submit(mock_backend)
    poll_route = _seed_completed(mock_backend)

    result = await async_client.redact(tiny_pdf)

    assert result["summary"]["PERSON_NAME"] == 2
    req = submit_route.calls[0].request
    assert req.headers["X-Async"] == "true"
    assert req.url.params["mode"] == "redact"
    assert req.url.params["include_logos"] == "false"
    assert poll_route.called


async def test_async_redact_deidentify_and_pii_config(mock_backend, async_client, tiny_pdf):
    _seed_presigned(mock_backend)
    submit_route = _seed_submit(mock_backend)
    _seed_completed(mock_backend)

    await async_client.redact(
        tiny_pdf,
        mode="deidentify",
        include_logos=True,
        pii_config={"mode": "replace", "types": [{"name": "SSN"}]},
    )

    req = submit_route.calls[0].request
    assert req.url.params["mode"] == "deidentify"
    assert req.url.params["include_logos"] == "true"
    body = req.content.decode()
    assert "pii_config" in body and "SSN" in body


async def test_async_submit_redact_returns_job(mock_backend, async_client, tiny_pdf):
    _seed_presigned(mock_backend)
    _seed_submit(mock_backend, job_id="rd_async")

    job = await async_client.submit_redact(tiny_pdf)

    assert isinstance(job, Job)
    assert job.op == "redact"
    assert job.job_id == "rd_async"


async def test_async_redact_failed_job_raises(mock_backend, async_client, tiny_pdf):
    _seed_presigned(mock_backend)
    _seed_submit(mock_backend)
    mock_backend.get("/v1/jobs/job_rd_1").mock(return_value=httpx.Response(
        200, json={"status": "failed", "error": "redact pipeline crashed"},
    ))

    with pytest.raises(RedactError) as ei:
        await async_client.redact(tiny_pdf)
    assert "redact pipeline crashed" in str(ei.value)
