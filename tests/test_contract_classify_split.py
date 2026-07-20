"""Contract: classify() and split() — same submit+poll path as parse/extract.

These ops use the shared `_submit_via_path` + `wait_for_job` plumbing, so the
tests focus on per-op specifics: endpoint, error class, mode parameter.
"""

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


def test_classify_submits_with_x_async(mock_backend, client, tiny_pdf):
    submit = _seed(
        mock_backend, "doc_cl", "/v1/classify",
        job_id="job_cl_1",
        result={"document_type": "invoice", "confidence": 0.97},
    )

    result = client.classify(tiny_pdf)

    assert result["document_type"] == "invoice"
    req = submit.calls[0].request
    assert req.headers["X-Async"] == "true"
    assert req.url.params["mode"] == "default"
    assert b"document_key=doc_cl" in req.read()


def test_classify_failed_job_raises_classify_error(mock_backend, client, tiny_pdf):
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
        client.classify(tiny_pdf)
    assert "model unavailable" in str(ei.value)


def test_submit_classify_returns_job(mock_backend, client, tiny_pdf):
    _seed(
        mock_backend, "doc_cl2", "/v1/classify",
        job_id="cj_1",
        result={"document_type": "receipt"},
    )

    job = client.submit_classify(tiny_pdf)

    assert isinstance(job, Job)
    assert job.op == "classify"


# ── split ────────────────────────────────────────────────────────────────


def test_split_submits_with_x_async(mock_backend, client, tiny_pdf):
    submit = _seed(
        mock_backend, "doc_sp", "/v1/split",
        job_id="job_sp_1",
        result={"segments": [{"start": 1, "end": 3}, {"start": 4, "end": 5}]},
    )

    result = client.split(tiny_pdf)

    assert len(result["segments"]) == 2
    assert submit.calls[0].request.url.params["mode"] == "default"


def test_split_429_raises_rate_limit_error(mock_backend, client, tiny_pdf):
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
        client.split(tiny_pdf)
    assert ei.value.retry_after == 60
    assert ei.value.tier == "free"
    assert ei.value.limit == 1


def test_submit_split_returns_job(mock_backend, client, tiny_pdf):
    _seed(
        mock_backend, "doc_sp2", "/v1/split",
        job_id="sj_1",
        result={"segments": []},
    )

    job = client.submit_split(tiny_pdf)

    assert isinstance(job, Job)
    assert job.op == "split"


# ── options (classify + split) ───────────────────────────────────────────


def test_classify_default_omits_options_from_body(mock_backend, client, tiny_pdf):
    submit = _seed(
        mock_backend, "doc_cl3", "/v1/classify",
        job_id="cj_2",
        result={"document_type": "invoice"},
    )

    client.classify(tiny_pdf)

    body = submit.calls[0].request.content.decode()
    assert "options" not in body


def test_classify_options_ride_in_form_body(mock_backend, client, tiny_pdf):
    submit = _seed(
        mock_backend, "doc_cl4", "/v1/classify",
        job_id="cj_3",
        result={"document_type": "invoice"},
    )

    client.classify(
        tiny_pdf,
        options={"mode": "thorough", "active_classes": ["invoice", "purchase_order"]},
    )

    req = submit.calls[0].request
    # The task-selector query param stays "default" — options["mode"] is the
    # separate service-level knob and must NOT leak into the URL.
    assert req.url.params["mode"] == "default"
    body = req.content.decode()
    assert "options" in body
    assert "thorough" in body
    assert "purchase_order" in body


def test_split_options_ride_in_form_body(mock_backend, client, tiny_pdf):
    submit = _seed(
        mock_backend, "doc_sp3", "/v1/split",
        job_id="sj_2",
        result={"segments": []},
    )

    client.split(tiny_pdf, options={"use_thinking": False})

    body = submit.calls[0].request.content.decode()
    assert "options" in body
    assert "use_thinking" in body


def test_split_use_presigned_false_falls_back_to_presigned_with_options(mock_backend, client, tiny_pdf):
    """use_presigned=False is unsupported by the API; the SDK warns and falls
    back to the presigned flow, still carrying the extra options form field
    alongside the presigned document_key."""
    submit = _seed(
        mock_backend, "doc_sp4", "/v1/split",
        job_id="sj_3",
        result={"segments": []},
    )

    with pytest.warns(DeprecationWarning):
        client.split(tiny_pdf, use_presigned=False, options={"use_thinking": False})

    req = submit.calls[0].request
    # No multipart upload — the submit carries the presigned document_key instead.
    assert "multipart/form-data" not in req.headers.get("content-type", "")
    body = req.content.decode("utf-8", errors="ignore")
    assert "document_key=doc_sp4" in body                 # presigned document_key present
    assert "options" in body and "use_thinking" in body   # extra form field still present


def test_classify_invalid_options_400_raises_classify_error(mock_backend, client, tiny_pdf):
    """The router rejects a malformed options payload with 400 + detail — the
    SDK surfaces it as ClassifyError without any client-side validation."""
    mock_backend.post("/v1/documents/upload").mock(
        return_value=httpx.Response(200, json={
            "document_key": "k", "upload_url": "https://s3.local/k?sig=x", "expires_in": 600,
        }),
    )
    mock_backend.put("https://s3.local/k?sig=x").mock(return_value=httpx.Response(200))
    mock_backend.post("/v1/classify").mock(return_value=httpx.Response(
        400, json={"detail": "Invalid options JSON: not an object"},
    ))

    with pytest.raises(ClassifyError) as ei:
        client.classify(tiny_pdf, options={"mode": "thorough"})
    assert ei.value.status_code == 400
    assert "Invalid options" in str(ei.value)
