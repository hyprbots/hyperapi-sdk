"""Async twin of test_contract_upload.py.

Verifies AsyncHyperAPIClient.upload_document() — same presigned-URL flow as the
sync client, same error mapping, same header propagation.
"""

import httpx
import pytest

from hyperapi import AuthenticationError, DocumentUploadError, RateLimitError


PRESIGNED_URL = "https://s3.amazonaws.com/hyperapi-uploads/abc123?signature=mocked"


def _seed_presigned(mock_backend, *, document_key="doc_abc", upload_url=PRESIGNED_URL,
                    status_code=200):
    return mock_backend.post("/v1/documents/upload").mock(
        return_value=httpx.Response(
            status_code,
            json={"document_key": document_key, "upload_url": upload_url, "expires_in": 600},
        ),
    )


async def test_async_upload_pdf_calls_presigned_then_s3(mock_backend, async_client, tiny_pdf):
    api_route = _seed_presigned(mock_backend)
    s3_route = mock_backend.put(PRESIGNED_URL).mock(return_value=httpx.Response(200))

    document_key = await async_client.upload_document(tiny_pdf)

    assert document_key == "doc_abc"
    assert api_route.called
    assert s3_route.called

    api_req = api_route.calls[0].request
    assert api_req.method == "POST"
    assert api_req.headers["X-API-Key"] == "hk_test_unit"
    assert "X-Request-ID" in api_req.headers
    body = api_req.read()
    assert b"doc.pdf" in body
    assert b"application/pdf" in body

    s3_req = s3_route.calls[0].request
    assert s3_req.method == "PUT"
    assert s3_req.headers["x-amz-server-side-encryption"] == "AES256"
    assert s3_req.headers["Content-Type"] == "application/pdf"


async def test_async_upload_does_not_send_x_async(mock_backend, async_client, tiny_pdf):
    route = _seed_presigned(mock_backend)
    mock_backend.put(PRESIGNED_URL).mock(return_value=httpx.Response(200))

    await async_client.upload_document(tiny_pdf)

    assert "X-Async" not in route.calls[0].request.headers


async def test_async_upload_infers_content_type_for_png(mock_backend, async_client, tiny_png):
    _seed_presigned(mock_backend)
    s3_route = mock_backend.put(PRESIGNED_URL).mock(return_value=httpx.Response(204))

    await async_client.upload_document(tiny_png)

    assert s3_route.calls[0].request.headers["Content-Type"] == "image/png"


async def test_async_upload_unauthorized_raises_authentication_error(mock_backend, async_client, tiny_pdf):
    _seed_presigned(mock_backend, status_code=401)

    with pytest.raises(AuthenticationError) as ei:
        await async_client.upload_document(tiny_pdf)
    assert ei.value.status_code == 401
    assert ei.value.request_id


async def test_async_upload_rate_limit_surfaces_typed_error(mock_backend, async_client, tiny_pdf):
    mock_backend.post("/v1/documents/upload").mock(
        return_value=httpx.Response(
            429,
            headers={"Retry-After": "60"},
            json={"message": "Rate limit exceeded", "tier": "free", "limit": 1},
        ),
    )

    with pytest.raises(RateLimitError) as ei:
        await async_client.upload_document(tiny_pdf)
    assert ei.value.retry_after == 60
    assert ei.value.tier == "free"
    assert ei.value.limit == 1
    assert ei.value.window_seconds is None
    assert ei.value.degraded is False


async def test_async_upload_rate_limit_parses_window_seconds_and_degraded(
    mock_backend, async_client, tiny_pdf,
):
    """Bug #86 parity — window_seconds + degraded must parse on the async path too."""
    mock_backend.post("/v1/documents/upload").mock(
        return_value=httpx.Response(
            429,
            headers={"Retry-After": "60"},
            json={
                "message": "Rate limit enforcement temporarily unavailable; retry shortly.",
                "tier": "free",
                "limit": 1,
                "window_seconds": 60,
                "degraded": True,
            },
        ),
    )

    with pytest.raises(RateLimitError) as ei:
        await async_client.upload_document(tiny_pdf)
    assert ei.value.window_seconds == 60
    assert ei.value.degraded is True
    assert ei.value.retry_after == 60


async def test_async_upload_413_raises_document_upload_error(mock_backend, async_client, tiny_pdf):
    mock_backend.post("/v1/documents/upload").mock(
        return_value=httpx.Response(413, json={"message": "File exceeds 50 MB limit"}),
    )

    with pytest.raises(DocumentUploadError) as ei:
        await async_client.upload_document(tiny_pdf)
    assert ei.value.status_code == 413
    assert "50 MB" in str(ei.value)


async def test_async_upload_s3_failure_includes_aes256_hint(mock_backend, async_client, tiny_pdf):
    _seed_presigned(mock_backend)
    mock_backend.put(PRESIGNED_URL).mock(return_value=httpx.Response(403))

    with pytest.raises(DocumentUploadError) as ei:
        await async_client.upload_document(tiny_pdf)
    assert ei.value.status_code == 403
    assert "AES256" in str(ei.value)


async def test_async_upload_missing_file_raises_filenotfound(async_client, tmp_path):
    with pytest.raises(FileNotFoundError):
        await async_client.upload_document(tmp_path / "does_not_exist.pdf")


async def test_async_upload_unicode_filename(mock_backend, async_client, tmp_path):
    """Non-ASCII filenames must round-trip through httpx async multipart encoding."""
    pdf_bytes = (
        b"%PDF-1.4\n"
        b"1 0 obj <</Type /Catalog /Pages 2 0 R>> endobj\n"
        b"2 0 obj <</Type /Pages /Kids [3 0 R] /Count 1>> endobj\n"
        b"3 0 obj <</Type /Page /Parent 2 0 R /MediaBox [0 0 612 792]>> endobj\n"
        b"xref\n0 4\n0000000000 65535 f\n"
        b"trailer <</Size 4 /Root 1 0 R>>\nstartxref\n%%EOF\n"
    )
    path = tmp_path / "Ènvoîce-2025.pdf"
    path.write_bytes(pdf_bytes)

    api_route = _seed_presigned(mock_backend, document_key="doc_unicode")
    s3_route = mock_backend.put(PRESIGNED_URL).mock(return_value=httpx.Response(200))

    document_key = await async_client.upload_document(path)

    assert document_key == "doc_unicode"
    assert api_route.called
    assert s3_route.called
    api_body = api_route.calls[0].request.read()
    assert "Ènvoîce-2025.pdf".encode() in api_body


async def test_async_upload_zero_byte_file(mock_backend, async_client, tmp_path):
    """Same behaviour as sync: zero-byte files are uploaded; the backend / S3
    policy is the line of defence."""
    path = tmp_path / "empty.txt"
    path.write_bytes(b"")
    assert path.stat().st_size == 0

    _seed_presigned(mock_backend, document_key="doc_empty")
    s3_route = mock_backend.put(PRESIGNED_URL).mock(return_value=httpx.Response(200))

    document_key = await async_client.upload_document(path)

    assert document_key == "doc_empty"
    assert s3_route.called
    assert s3_route.calls[0].request.read() == b""


async def test_async_upload_402_insufficient_credits_raises(mock_backend, async_client, tiny_pdf):
    mock_backend.post("/v1/documents/upload").mock(
        return_value=httpx.Response(
            402, json={"message": "Insufficient credits, top up to continue"},
        ),
    )

    with pytest.raises(DocumentUploadError) as ei:
        await async_client.upload_document(tiny_pdf)
    assert ei.value.status_code == 402
    assert "billing" in str(ei.value).lower() or "credits" in str(ei.value).lower()


async def test_async_upload_415_unsupported_media_type_raises(mock_backend, async_client, tiny_pdf):
    mock_backend.post("/v1/documents/upload").mock(
        return_value=httpx.Response(
            415, json={"message": "Unsupported file type: text/plain"},
        ),
    )

    with pytest.raises(DocumentUploadError) as ei:
        await async_client.upload_document(tiny_pdf)
    assert ei.value.status_code == 415
    assert "Unsupported" in str(ei.value)


async def test_async_upload_403_forbidden_raises_generic_hyperapi_error(
    mock_backend, async_client, tiny_pdf,
):
    from hyperapi import HyperAPIError

    mock_backend.post("/v1/documents/upload").mock(
        return_value=httpx.Response(403, json={"message": "Forbidden by org policy"}),
    )

    with pytest.raises(HyperAPIError) as ei:
        await async_client.upload_document(tiny_pdf)
    assert ei.value.status_code == 403


async def test_async_upload_url_endpoint_502_raises_document_upload_error(
    mock_backend, async_client, tiny_pdf,
):
    mock_backend.post("/v1/documents/upload").mock(
        return_value=httpx.Response(502, json={"message": "Upstream gateway error"}),
    )

    with pytest.raises(DocumentUploadError) as ei:
        await async_client.upload_document(tiny_pdf)
    assert ei.value.status_code == 502
    assert "gateway" in str(ei.value).lower() or "Upstream" in str(ei.value)


async def test_async_upload_presigned_request_error_raises(mock_backend, async_client, tiny_pdf):
    mock_backend.post("/v1/documents/upload").mock(
        side_effect=httpx.RequestError("dns failed"),
    )

    with pytest.raises(DocumentUploadError) as ei:
        await async_client.upload_document(tiny_pdf)
    assert "Failed to get upload URL" in str(ei.value)
    assert ei.value.request_id


async def test_async_upload_s3_put_request_error_raises(mock_backend, async_client, tiny_pdf):
    _seed_presigned(mock_backend)
    mock_backend.put(PRESIGNED_URL).mock(
        side_effect=httpx.RequestError("connection reset"),
    )

    with pytest.raises(DocumentUploadError) as ei:
        await async_client.upload_document(tiny_pdf)
    assert "S3 upload failed" in str(ei.value)
    assert ei.value.request_id


# ── Env-aware error messages parity ─────────────────────────────────────────


async def test_async_upload_401_message_references_base_url(mock_backend, async_client, tiny_pdf):
    """Async parity for the env-aware 401 message — must reference self.base_url
    and never hardcode the prod dashboard URL."""
    _seed_presigned(mock_backend, status_code=401)

    with pytest.raises(AuthenticationError) as ei:
        await async_client.upload_document(tiny_pdf)
    assert "http://test.local" in str(ei.value)
    assert "apis.hyperbots.com" not in str(ei.value)


async def test_async_upload_402_message_references_base_url(mock_backend, async_client, tiny_pdf):
    """Async parity for the env-aware 402 message."""
    mock_backend.post("/v1/documents/upload").mock(
        return_value=httpx.Response(402, json={"message": "Insufficient credits"}),
    )

    with pytest.raises(DocumentUploadError) as ei:
        await async_client.upload_document(tiny_pdf)
    assert "http://test.local" in str(ei.value)
    assert "apis.hyperbots.com" not in str(ei.value)
