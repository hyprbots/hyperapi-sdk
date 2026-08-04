"""Contract: redact() / deidentify — submit (X-Async:true) + poll until completed.

Redact runs the same submit+poll path as extract (multi-page redaction can take
tens of seconds). mode + include_logos ride as query params; pii_config (when
given) rides in the form body alongside document_key.
"""

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


def test_redact_default_uses_async_with_mode_and_logos(mock_backend, client, tiny_pdf):
    _seed_presigned(mock_backend)
    submit_route = _seed_submit(mock_backend)
    poll_route = _seed_completed(mock_backend)

    result = client.redact(tiny_pdf)

    assert result["summary"]["PERSON_NAME"] == 2
    req = submit_route.calls[0].request
    assert req.headers["X-Async"] == "true"
    assert req.url.params["mode"] == "redact"
    assert req.url.params["include_logos"] == "false"
    assert poll_route.called


def test_redact_deidentify_mode_and_pii_config_in_body(mock_backend, client, tiny_pdf):
    _seed_presigned(mock_backend)
    submit_route = _seed_submit(mock_backend)
    _seed_completed(mock_backend)

    client.redact(
        tiny_pdf,
        mode="deidentify",
        include_logos=True,
        pii_config={"mode": "extend", "types": [{"name": "SSN"}]},
    )

    req = submit_route.calls[0].request
    assert req.url.params["mode"] == "deidentify"
    assert req.url.params["include_logos"] == "true"
    # pii_config rides in the form body (not the URL).
    body = req.content.decode()
    assert "pii_config" in body
    assert "SSN" in body


def test_submit_redact_returns_job_no_polling(mock_backend, client, tiny_pdf):
    _seed_presigned(mock_backend)
    submit_route = _seed_submit(mock_backend, job_id="rd_custom")
    poll_route = mock_backend.get("/v1/jobs/rd_custom")

    job = client.submit_redact(tiny_pdf)

    assert isinstance(job, Job)
    assert job.op == "redact"
    assert job.job_id == "rd_custom"
    assert submit_route.called
    assert not poll_route.called


def test_redact_failed_job_raises_redact_error(mock_backend, client, tiny_pdf):
    _seed_presigned(mock_backend)
    _seed_submit(mock_backend)
    mock_backend.get("/v1/jobs/job_rd_1").mock(return_value=httpx.Response(
        200, json={"status": "failed", "error": "redact pipeline crashed", "status_code": 500},
    ))

    with pytest.raises(RedactError) as ei:
        client.redact(tiny_pdf)
    assert "redact pipeline crashed" in str(ei.value)


def test_redact_submit_5xx_raises_redact_error(mock_backend, client, tiny_pdf):
    _seed_presigned(mock_backend)
    mock_backend.post("/v1/redact").mock(return_value=httpx.Response(500))

    with pytest.raises(RedactError) as ei:
        client.redact(tiny_pdf)
    assert ei.value.status_code == 500


def test_redact_use_presigned_false_falls_back_to_presigned_with_pii_config(mock_backend, client, tiny_pdf):
    """use_presigned=False is unsupported by the API; the SDK warns and falls
    back to the presigned flow, still carrying the extra pii_config form field
    alongside the presigned document_key."""
    _seed_presigned(mock_backend)
    submit_route = _seed_submit(mock_backend)
    _seed_completed(mock_backend)

    with pytest.warns(DeprecationWarning):
        client.redact(
            tiny_pdf,
            use_presigned=False,
            pii_config={"mode": "extend", "types": [{"name": "SSN"}]},
        )

    req = submit_route.calls[0].request
    # No multipart upload — the submit carries the presigned document_key instead.
    assert "multipart/form-data" not in req.headers.get("content-type", "")
    assert req.url.params["mode"] == "redact"
    body = req.content.decode("utf-8", errors="ignore")
    assert "document_key=doc_rd" in body           # presigned document_key present
    assert "pii_config" in body and "SSN" in body  # extra form field still present
