"""Contract: `force_refresh` reaches the wire as `?force_refresh=true`.

The API stores results for an identical (document + task + params) for
24 hours, so re-running an unchanged document returns the STORED result. `force_refresh=True` is the escape hatch; without it a caller retrying
after a bad result gets the same bad result back for a day.

Two properties matter and both are pinned here:
  * when True  → the param is on the query string, for every cacheable op;
  * when False → it is ABSENT, not `false`. An ordinary call must be
    byte-identical to what it was before this parameter existed, so that adding
    it cannot perturb caching, billing, or the request signature for anyone who
    does not opt in.
"""

import httpx
import pytest


def _seed_presigned(mock_api, *, key="doc_fr"):
    mock_api.post("/v1/documents/upload").mock(
        return_value=httpx.Response(200, json={
            "document_key": key,
            "upload_url": f"https://s3.local/{key}?sig=x",
            "expires_in": 600,
        }),
    )
    mock_api.put(f"https://s3.local/{key}?sig=x").mock(return_value=httpx.Response(200))


def _seed_job(mock_api, endpoint, *, job_id):
    submit = mock_api.post(endpoint).mock(
        return_value=httpx.Response(202, json={
            "job_id": job_id, "status": "pending", "poll_url": f"/v1/jobs/{job_id}",
        }),
    )
    mock_api.get(f"/v1/jobs/{job_id}").mock(
        return_value=httpx.Response(200, json={
            "status": "completed",
            "result": {"ok": True},
            "cached": False,
        }),
    )
    return submit


OPS = [
    ("redact", "/v1/redact"),
    ("classify", "/v1/classify"),
    ("split", "/v1/split"),
    ("extract", "/v1/extract"),
    ("parse", "/v1/parse"),
]


@pytest.mark.parametrize("method_name,endpoint", OPS)
def test_force_refresh_true_is_sent(client, mock_api, tmp_path, method_name, endpoint):
    _seed_presigned(mock_api)
    submit = _seed_job(mock_api, endpoint, job_id=f"job_{method_name}")
    doc = tmp_path / "a.pdf"
    doc.write_bytes(b"%PDF-1.4 x")

    getattr(client, method_name)(doc, force_refresh=True)

    assert submit.called
    assert "force_refresh=true" in str(submit.calls.last.request.url)


@pytest.mark.parametrize("method_name,endpoint", OPS)
def test_default_omits_it_entirely(client, mock_api, tmp_path, method_name, endpoint):
    """Absent, not `force_refresh=false` — an opt-out must not change the request."""
    _seed_presigned(mock_api)
    submit = _seed_job(mock_api, endpoint, job_id=f"job_{method_name}_d")
    doc = tmp_path / "a.pdf"
    doc.write_bytes(b"%PDF-1.4 x")

    getattr(client, method_name)(doc)

    assert submit.called
    assert "force_refresh" not in str(submit.calls.last.request.url)


def test_submit_variants_accept_it_too(client, mock_api, tmp_path):
    """The fire-and-forget submit_* path needs the same lever as the blocking one."""
    _seed_presigned(mock_api)
    submit = _seed_job(mock_api, "/v1/redact", job_id="job_sub")
    doc = tmp_path / "a.pdf"
    doc.write_bytes(b"%PDF-1.4 x")

    client.submit_redact(doc, force_refresh=True)

    assert "force_refresh=true" in str(submit.calls.last.request.url)


def test_submit_parse_forwards_force_refresh(client, mock_api, tmp_path):
    """`parse()` polls to completion, but callers who submit and poll manually
    go through `submit_parse` — the bypass has to be reachable from both.

    Parse is the odd one out: its results are cached as OCR output, so what it
    bypasses is the stored OCR output. Same flag, same 24-hour retention.
    """
    _seed_presigned(mock_api)
    submit = _seed_job(mock_api, "/v1/parse", job_id="job_submit_parse")
    doc = tmp_path / "a.pdf"
    doc.write_bytes(b"%PDF-1.4 x")

    client.submit_parse(doc, force_refresh=True)

    assert submit.called
    assert "force_refresh=true" in str(submit.calls.last.request.url)


def test_extract_advanced_forwards_force_refresh(client, mock_api, tmp_path):
    """`/v1/extract-omni` accepts the same bypass; both the blocking and the
    submit_ variant must be able to send it, and omit it by default."""
    _seed_presigned(mock_api)
    submit = _seed_job(mock_api, "/v1/extract-omni", job_id="job_omni")
    doc = tmp_path / "a.pdf"
    doc.write_bytes(b"%PDF-1.4 x")

    client.extract_advanced(doc)
    assert "force_refresh" not in str(submit.calls.last.request.url)

    client.extract_advanced(doc, force_refresh=True)
    assert "force_refresh=true" in str(submit.calls.last.request.url)

    client.submit_extract_advanced(doc, force_refresh=True)
    assert "force_refresh=true" in str(submit.calls.last.request.url)


async def test_async_extract_advanced_forwards_force_refresh(async_client, mock_api, tmp_path):
    _seed_presigned(mock_api)
    submit = _seed_job(mock_api, "/v1/extract-omni", job_id="job_omni_a")
    doc = tmp_path / "a.pdf"
    doc.write_bytes(b"%PDF-1.4 x")

    await async_client.submit_extract_advanced(doc, force_refresh=True)
    assert "force_refresh=true" in str(submit.calls.last.request.url)
