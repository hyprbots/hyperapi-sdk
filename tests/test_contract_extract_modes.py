"""Contract: Basic ``category`` routing + Advanced ``extract_advanced()``.

Basic ``extract()`` carries ``category`` as a query param on ``/v1/extract``
(default ``financial``); ``extract_advanced()`` submits to ``/v1/extract-omni``.
"""

import httpx


def _seed_presigned(mock_backend, *, key="doc_em"):
    mock_backend.post("/v1/documents/upload").mock(
        return_value=httpx.Response(200, json={
            "document_key": key,
            "upload_url": f"https://s3.local/{key}?sig=x",
            "expires_in": 600,
        }),
    )
    mock_backend.put(f"https://s3.local/{key}?sig=x").mock(return_value=httpx.Response(200))


def _seed_completed(mock_backend, *, job_id, path):
    submit = mock_backend.post(path).mock(
        return_value=httpx.Response(202, json={
            "job_id": job_id, "status": "pending", "poll_url": f"/v1/jobs/{job_id}",
        }),
    )
    poll = mock_backend.get(f"/v1/jobs/{job_id}").mock(
        return_value=httpx.Response(200, json={
            "status": "completed",
            "result": {"entities": {"vendor": "Acme"}, "line_items": [], "ocr_text": "x"},
            "request_id": "req-x", "duration_ms": 1000,
        }),
    )
    return submit, poll


def test_basic_extract_defaults_to_financial(mock_backend, client, tiny_pdf):
    _seed_presigned(mock_backend)
    submit, _ = _seed_completed(mock_backend, job_id="j_fin", path="/v1/extract")

    client.extract(tiny_pdf)

    req = submit.calls[0].request
    assert req.url.path == "/v1/extract"
    assert req.url.params["category"] == "financial"


def test_basic_extract_non_financial_sets_category(mock_backend, client, tiny_pdf):
    _seed_presigned(mock_backend)
    submit, _ = _seed_completed(mock_backend, job_id="j_nf", path="/v1/extract")

    client.extract(tiny_pdf, category="non_financial")

    req = submit.calls[0].request
    assert req.url.path == "/v1/extract"
    assert req.url.params["category"] == "non_financial"


def test_advanced_extract_hits_omni_endpoint(mock_backend, client, tiny_pdf):
    _seed_presigned(mock_backend)
    submit, poll = _seed_completed(mock_backend, job_id="j_adv", path="/v1/extract-omni")

    result = client.extract_advanced(tiny_pdf)

    assert result["entities"]["vendor"] == "Acme"
    req = submit.calls[0].request
    assert req.url.path == "/v1/extract-omni"
    assert "category" not in req.url.params      # Advanced auto-detects — no category
    assert poll.called


def test_submit_extract_advanced_returns_job_no_polling(mock_backend, client, tiny_pdf):
    _seed_presigned(mock_backend)
    submit, poll = _seed_completed(mock_backend, job_id="j_adv2", path="/v1/extract-omni")

    job = client.submit_extract_advanced(tiny_pdf)

    assert job.job_id == "j_adv2"
    assert submit.calls[0].request.url.path == "/v1/extract-omni"
    assert not poll.called                       # submit_* does not poll


# ── async mirrors (the async client is a hand-maintained parallel impl) ────────
async def test_async_basic_extract_defaults_to_financial(mock_backend, async_client, tiny_pdf):
    _seed_presigned(mock_backend)
    submit, _ = _seed_completed(mock_backend, job_id="aj_fin", path="/v1/extract")

    await async_client.extract(tiny_pdf)

    req = submit.calls[0].request
    assert req.url.path == "/v1/extract"
    assert req.url.params["category"] == "financial"


async def test_async_basic_extract_non_financial_sets_category(mock_backend, async_client, tiny_pdf):
    _seed_presigned(mock_backend)
    submit, _ = _seed_completed(mock_backend, job_id="aj_nf", path="/v1/extract")

    await async_client.extract(tiny_pdf, category="non_financial")

    assert submit.calls[0].request.url.params["category"] == "non_financial"


async def test_async_advanced_extract_hits_omni_endpoint(mock_backend, async_client, tiny_pdf):
    _seed_presigned(mock_backend)
    submit, poll = _seed_completed(mock_backend, job_id="aj_adv", path="/v1/extract-omni")

    result = await async_client.extract_advanced(tiny_pdf)

    assert result["entities"]["vendor"] == "Acme"
    req = submit.calls[0].request
    assert req.url.path == "/v1/extract-omni"
    assert "category" not in req.url.params
    assert poll.called


async def test_async_submit_extract_advanced_returns_job_no_polling(mock_backend, async_client, tiny_pdf):
    _seed_presigned(mock_backend)
    submit, poll = _seed_completed(mock_backend, job_id="aj_adv2", path="/v1/extract-omni")

    job = await async_client.submit_extract_advanced(tiny_pdf)

    assert job.job_id == "aj_adv2"
    assert submit.calls[0].request.url.path == "/v1/extract-omni"
    assert not poll.called


# ── parse_mode: Stage-1 OCR engine, fast (default, Paddle) vs advanced (Chandra) ──
# Orthogonal to category and to the Basic/Advanced product split — rides as a
# query param on both /v1/extract and /v1/extract-omni.
def test_basic_extract_defaults_to_fast_parse_mode(mock_backend, client, tiny_pdf):
    _seed_presigned(mock_backend)
    submit, _ = _seed_completed(mock_backend, job_id="j_pm_fast", path="/v1/extract")

    client.extract(tiny_pdf)

    assert submit.calls[0].request.url.params["parse_mode"] == "fast"


def test_basic_extract_advanced_parse_mode_sets_param(mock_backend, client, tiny_pdf):
    _seed_presigned(mock_backend)
    submit, _ = _seed_completed(mock_backend, job_id="j_pm_adv", path="/v1/extract")

    client.extract(tiny_pdf, parse_mode="advanced")

    req = submit.calls[0].request
    assert req.url.params["parse_mode"] == "advanced"
    assert req.url.params["category"] == "financial"      # orthogonal to category


def test_advanced_extract_carries_parse_mode(mock_backend, client, tiny_pdf):
    _seed_presigned(mock_backend)
    submit, _ = _seed_completed(mock_backend, job_id="j_pm_omni", path="/v1/extract-omni")

    client.extract_advanced(tiny_pdf, parse_mode="advanced")

    req = submit.calls[0].request
    assert req.url.path == "/v1/extract-omni"
    assert req.url.params["parse_mode"] == "advanced"


async def test_async_basic_extract_defaults_to_fast_parse_mode(mock_backend, async_client, tiny_pdf):
    _seed_presigned(mock_backend)
    submit, _ = _seed_completed(mock_backend, job_id="aj_pm_fast", path="/v1/extract")

    await async_client.extract(tiny_pdf)

    assert submit.calls[0].request.url.params["parse_mode"] == "fast"


async def test_async_basic_extract_advanced_parse_mode_sets_param(mock_backend, async_client, tiny_pdf):
    _seed_presigned(mock_backend)
    submit, _ = _seed_completed(mock_backend, job_id="aj_pm_adv", path="/v1/extract")

    await async_client.extract(tiny_pdf, parse_mode="advanced")

    assert submit.calls[0].request.url.params["parse_mode"] == "advanced"


async def test_async_advanced_extract_carries_parse_mode(mock_backend, async_client, tiny_pdf):
    _seed_presigned(mock_backend)
    submit, _ = _seed_completed(mock_backend, job_id="aj_pm_omni", path="/v1/extract-omni")

    await async_client.extract_advanced(tiny_pdf, parse_mode="advanced")

    assert submit.calls[0].request.url.params["parse_mode"] == "advanced"
