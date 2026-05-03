"""Contract: upload_document() — presigned URL flow (POST /v1/documents/upload → S3 PUT)."""

import httpx
import pytest

from hyperapi import AuthenticationError, DocumentUploadError


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

    # API request shape
    api_req = api_route.calls[0].request
    assert api_req.method == "POST"
    assert api_req.headers["X-API-Key"] == "hk_test_unit"
    assert "X-Request-ID" in api_req.headers
    body = api_req.read()
    assert b"doc.pdf" in body
    assert b"application/pdf" in body

    # S3 PUT must include AES256 header (otherwise S3 rejects per backend policy)
    s3_req = s3_route.calls[0].request
    assert s3_req.method == "PUT"
    assert s3_req.headers["x-amz-server-side-encryption"] == "AES256"
    assert s3_req.headers["Content-Type"] == "application/pdf"


def test_upload_infers_content_type_for_png(mock_backend, client, tiny_png):
    _seed_presigned(mock_backend)
    s3_route = mock_backend.put(PRESIGNED_URL).mock(return_value=httpx.Response(204))

    client.upload_document(tiny_png)

    s3_req = s3_route.calls[0].request
    assert s3_req.headers["Content-Type"] == "image/png"


def test_upload_explicit_content_type_overrides_inference(mock_backend, client, tiny_pdf):
    _seed_presigned(mock_backend)
    s3_route = mock_backend.put(PRESIGNED_URL).mock(return_value=httpx.Response(200))

    client.upload_document(tiny_pdf, content_type="application/x-custom")

    assert s3_route.calls[0].request.headers["Content-Type"] == "application/x-custom"


def test_upload_unauthorized_raises_authentication_error(mock_backend, client, tiny_pdf):
    _seed_presigned(mock_backend, status_code=401)

    with pytest.raises(AuthenticationError) as ei:
        client.upload_document(tiny_pdf)
    assert ei.value.status_code == 401


def test_upload_backend_error_raises_document_upload_error(mock_backend, client, tiny_pdf):
    mock_backend.post("/v1/documents/upload").mock(
        return_value=httpx.Response(500, text="boom"),
    )

    with pytest.raises(DocumentUploadError) as ei:
        client.upload_document(tiny_pdf)
    assert ei.value.status_code == 500


def test_upload_s3_failure_raises_document_upload_error(mock_backend, client, tiny_pdf):
    _seed_presigned(mock_backend)
    mock_backend.put(PRESIGNED_URL).mock(return_value=httpx.Response(403))

    with pytest.raises(DocumentUploadError) as ei:
        client.upload_document(tiny_pdf)
    assert ei.value.status_code == 403
    # The AES256 hint should be in the message — it's the most common cause.
    assert "AES256" in str(ei.value)


def test_upload_missing_file_raises_filenotfound(client, tmp_path):
    with pytest.raises(FileNotFoundError):
        client.upload_document(tmp_path / "does_not_exist.pdf")
