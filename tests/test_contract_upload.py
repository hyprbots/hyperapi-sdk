"""Contract: upload_document() — presigned URL flow (POST /v1/documents/upload → S3 PUT).

upload_document remains direct sync — sub-second by definition, no value in
async-ifying. These tests verify request shape, header propagation, and error
mapping.
"""

import httpx
import pytest

from hyperapi import AuthenticationError, DocumentUploadError, RateLimitError


PRESIGNED_URL = "https://storage.example.test/uploads/abc123?signature=mocked"


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
    # Old server (pre-bug-#86) doesn't surface these — ensure we don't crash.
    assert ei.value.window_seconds is None
    assert ei.value.degraded is False


def test_upload_rate_limit_parses_window_seconds_and_degraded(mock_backend, client, tiny_pdf):
    """Bug #86 (b): server's 429 envelope now includes window_seconds; (a):
    free-tier fail-closed sets degraded=true. SDK must parse both."""
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
        client.upload_document(tiny_pdf)
    assert ei.value.window_seconds == 60
    assert ei.value.degraded is True
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


# ── Tier-3 polish: filename + zero-byte regression guards ───────────────────


def test_upload_unicode_filename(mock_backend, client, tmp_path):
    """Non-ASCII filenames must round-trip through httpx multipart encoding
    without raising. Customers in EU/LATAM regularly upload `Ènvoîce-2025.pdf`-
    style filenames; UnicodeEncodeError on the multipart boundary would break
    them silently."""
    # Reuse the same minimal PDF bytes the tiny_pdf fixture writes.
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

    document_key = client.upload_document(path)

    assert document_key == "doc_unicode"
    assert api_route.called
    assert s3_route.called
    # The non-ASCII filename must reach the presigned-URL request body so the
    # backend can persist the original name on the document record.
    api_body = api_route.calls[0].request.read()
    assert "Ènvoîce-2025.pdf".encode() in api_body


def test_upload_zero_byte_file(mock_backend, client, tmp_path):
    """Lock in the SDK's current behaviour for 0-byte files: it does NOT
    validate file size client-side — it requests a presigned URL and PUTs an
    empty body to S3, returning the document_key. The backend (or S3 bucket
    policy) is the line of defence against empty uploads. If we ever decide to
    fail-fast in the SDK, this test will flip to assert DocumentUploadError."""
    path = tmp_path / "empty.txt"
    path.write_bytes(b"")
    assert path.stat().st_size == 0

    _seed_presigned(mock_backend, document_key="doc_empty")
    s3_route = mock_backend.put(PRESIGNED_URL).mock(return_value=httpx.Response(200))

    document_key = client.upload_document(path)

    # Current behaviour: the SDK uploads zero bytes and returns the key.
    assert document_key == "doc_empty"
    assert s3_route.called
    assert s3_route.calls[0].request.read() == b""


# ── Coverage: 402 / 415 / 403 / 5xx-on-presigned + network errors ───────────


def test_upload_402_insufficient_credits_raises(mock_backend, client, tiny_pdf):
    """A 402 on the presigned-URL fetch must surface as DocumentUploadError
    (the registered error_cls for the upload op) with the billing-URL message."""
    mock_backend.post("/v1/documents/upload").mock(
        return_value=httpx.Response(
            402, json={"message": "Insufficient credits, top up to continue"},
        ),
    )

    with pytest.raises(DocumentUploadError) as ei:
        client.upload_document(tiny_pdf)
    assert ei.value.status_code == 402
    assert "billing" in str(ei.value).lower() or "credits" in str(ei.value).lower()


def test_upload_415_unsupported_media_type_raises(mock_backend, client, tiny_pdf):
    """The server rejects an unsupported MIME with 415 — surface as DocumentUploadError."""
    mock_backend.post("/v1/documents/upload").mock(
        return_value=httpx.Response(
            415, json={"message": "Unsupported file type: text/plain"},
        ),
    )

    with pytest.raises(DocumentUploadError) as ei:
        client.upload_document(tiny_pdf)
    assert ei.value.status_code == 415
    assert "Unsupported" in str(ei.value)


def test_upload_403_forbidden_raises_generic_hyperapi_error(mock_backend, client, tiny_pdf):
    """A 403 on any path raises the generic HyperAPIError — it's a policy/IAM problem,
    not credentials (401) and not credits (402)."""
    from hyperapi import HyperAPIError

    mock_backend.post("/v1/documents/upload").mock(
        return_value=httpx.Response(403, json={"message": "Forbidden by org policy"}),
    )

    with pytest.raises(HyperAPIError) as ei:
        client.upload_document(tiny_pdf)
    assert ei.value.status_code == 403


def test_upload_url_endpoint_502_raises_document_upload_error(mock_backend, client, tiny_pdf):
    """If the presigned-URL endpoint returns a non-200 status that isn't in the
    well-known map (e.g., a 502 from a flaky upstream), the generic non-200 fallback
    must surface as DocumentUploadError with the server message."""
    mock_backend.post("/v1/documents/upload").mock(
        return_value=httpx.Response(502, json={"message": "Upstream gateway error"}),
    )

    with pytest.raises(DocumentUploadError) as ei:
        client.upload_document(tiny_pdf)
    assert ei.value.status_code == 502
    assert "gateway" in str(ei.value).lower() or "Upstream" in str(ei.value)


def test_upload_presigned_request_error_raises(mock_backend, client, tiny_pdf):
    """A bare httpx.RequestError on the presigned-URL POST (DNS / connect-reset)
    must be caught and re-raised as DocumentUploadError carrying the original
    request_id from the headers we generated."""
    mock_backend.post("/v1/documents/upload").mock(
        side_effect=httpx.RequestError("dns failed"),
    )

    with pytest.raises(DocumentUploadError) as ei:
        client.upload_document(tiny_pdf)
    assert "Failed to get upload URL" in str(ei.value)
    assert ei.value.request_id  # the SDK-generated request id propagates


def test_upload_s3_put_request_error_raises(mock_backend, client, tiny_pdf):
    """A network error on the S3 PUT itself (after we got the presigned URL)
    must surface as DocumentUploadError with an S3-specific message."""
    _seed_presigned(mock_backend)
    mock_backend.put(PRESIGNED_URL).mock(
        side_effect=httpx.RequestError("connection reset"),
    )

    with pytest.raises(DocumentUploadError) as ei:
        client.upload_document(tiny_pdf)
    assert "S3 upload failed" in str(ei.value)
    assert ei.value.request_id


# ── Env-aware error messages: 401/402 reference self.base_url ───────────────


def test_upload_401_message_references_base_url(mock_backend, client, tiny_pdf):
    """A 401 message must include the client's base_url so customers on
    staging/on-prem aren't told to "go to the prod dashboard". Prevents a
    regression of the hardcoded https://apis.hyperbots.com/dashboard string
    that used to ship in the error text."""
    _seed_presigned(mock_backend, status_code=401)

    with pytest.raises(AuthenticationError) as ei:
        client.upload_document(tiny_pdf)
    # Fixture's client uses base_url="http://test.local"
    assert "http://test.local" in str(ei.value), (
        f"401 message should reference client.base_url, got: {ei.value}"
    )
    # The old hardcoded prod URL must NOT appear.
    assert "apis.hyperbots.com" not in str(ei.value), (
        f"401 message must not hardcode prod dashboard URL, got: {ei.value}"
    )


def test_upload_402_message_references_base_url(mock_backend, client, tiny_pdf):
    """Same env-awareness for the 402-insufficient-credits message."""
    mock_backend.post("/v1/documents/upload").mock(
        return_value=httpx.Response(402, json={"message": "Insufficient credits"}),
    )

    with pytest.raises(DocumentUploadError) as ei:
        client.upload_document(tiny_pdf)
    assert "http://test.local" in str(ei.value), (
        f"402 message should reference client.base_url, got: {ei.value}"
    )
    assert "apis.hyperbots.com" not in str(ei.value), (
        f"402 message must not hardcode prod dashboard URL, got: {ei.value}"
    )
