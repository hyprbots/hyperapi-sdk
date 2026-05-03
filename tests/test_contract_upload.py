"""Contract: upload_document() — presigned URL flow (POST /v1/documents/upload → S3 PUT).

upload_document remains direct sync — sub-second by definition, no value in
async-ifying. These tests verify request shape, header propagation, and error
mapping.
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


def test_upload_pdf_calls_presigned_then_s3(mock_backend, client, tiny_pdf):
    api_route = _seed_presigned(mock_backend)
    s3_route = mock_backend.put(PRESIGNED_URL).mock(return_value=httpx.Response(200))

    document_key = client.upload_document(tiny_pdf)

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

    # S3 PUT must include AES256 (otherwise the bucket policy rejects)
    s3_req = s3_route.calls[0].request
    assert s3_req.method == "PUT"
    assert s3_req.headers["x-amz-server-side-encryption"] == "AES256"
    assert s3_req.headers["Content-Type"] == "application/pdf"


def test_upload_does_not_send_x_async(mock_backend, client, tiny_pdf):
    """upload_document is intentionally synchronous; X-Async should NOT be set."""
    route = _seed_presigned(mock_backend)
    mock_backend.put(PRESIGNED_URL).mock(return_value=httpx.Response(200))

    client.upload_document(tiny_pdf)

    assert "X-Async" not in route.calls[0].request.headers


def test_upload_infers_content_type_for_png(mock_backend, client, tiny_png):
    _seed_presigned(mock_backend)
    s3_route = mock_backend.put(PRESIGNED_URL).mock(return_value=httpx.Response(204))

    client.upload_document(tiny_png)

    assert s3_route.calls[0].request.headers["Content-Type"] == "image/png"


def test_upload_unauthorized_raises_authentication_error(mock_backend, client, tiny_pdf):
    _seed_presigned(mock_backend, status_code=401)

    with pytest.raises(AuthenticationError) as ei:
        client.upload_document(tiny_pdf)
    assert ei.value.status_code == 401
    assert ei.value.request_id  # must propagate


def test_upload_rate_limit_surfaces_typed_error(mock_backend, client, tiny_pdf):
    """A 429 on upload becomes RateLimitError with retry_after parsed from header."""
    mock_backend.post("/v1/documents/upload").mock(
        return_value=httpx.Response(
            429,
            headers={"Retry-After": "60"},
            json={"message": "Rate limit exceeded", "tier": "free", "limit": 1},
        ),
    )

    with pytest.raises(RateLimitError) as ei:
        client.upload_document(tiny_pdf)
    assert ei.value.retry_after == 60
    assert ei.value.tier == "free"
    assert ei.value.limit == 1


def test_upload_413_raises_document_upload_error(mock_backend, client, tiny_pdf):
    mock_backend.post("/v1/documents/upload").mock(
        return_value=httpx.Response(413, json={"message": "File exceeds 50 MB limit"}),
    )

    with pytest.raises(DocumentUploadError) as ei:
        client.upload_document(tiny_pdf)
    assert ei.value.status_code == 413
    assert "50 MB" in str(ei.value)


def test_upload_s3_failure_includes_aes256_hint(mock_backend, client, tiny_pdf):
    _seed_presigned(mock_backend)
    mock_backend.put(PRESIGNED_URL).mock(return_value=httpx.Response(403))

    with pytest.raises(DocumentUploadError) as ei:
        client.upload_document(tiny_pdf)
    assert ei.value.status_code == 403
    assert "AES256" in str(ei.value)


def test_upload_missing_file_raises_filenotfound(client, tmp_path):
    with pytest.raises(FileNotFoundError):
        client.upload_document(tmp_path / "does_not_exist.pdf")
