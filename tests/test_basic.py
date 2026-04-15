"""
Basic SDK tests — import checks, method signatures, exception hierarchy.
These run without network access or API keys.
"""

import inspect
import pytest

from hyperapi import (
    HyperAPIClient,
    HyperAPIError,
    AuthenticationError,
    ParseError,
    ExtractError,
    ClassifyError,
    SplitError,
    DocumentUploadError,
    __version__,
)
from hyperapi.client import CONTENT_TYPES, OCREngine


# ── Import & version ────────────────────────────────────────────────────

def test_version_is_semver():
    parts = __version__.split(".")
    assert len(parts) == 3
    assert all(p.isdigit() for p in parts)


def test_version_matches_020():
    assert __version__ == "0.2.0"


# ── Exception hierarchy ─────────────────────────────────────────────────

def test_all_exceptions_inherit_from_base():
    for cls in (AuthenticationError, ParseError, ExtractError, ClassifyError, SplitError, DocumentUploadError):
        assert issubclass(cls, HyperAPIError)


def test_exception_stores_status_code():
    err = ParseError("bad", status_code=422)
    assert err.status_code == 422
    assert str(err) == "bad"


def test_exception_default_status_code():
    err = HyperAPIError("oops")
    assert err.status_code is None


# ── Client constructor ──────────────────────────────────────────────────

def test_client_requires_api_key(monkeypatch):
    monkeypatch.delenv("HYPERAPI_KEY", raising=False)
    with pytest.raises(AuthenticationError, match="API key required"):
        HyperAPIClient()


def test_client_accepts_api_key():
    client = HyperAPIClient(api_key="hk_test_abc")
    assert client.api_key == "hk_test_abc"
    assert client.base_url == "https://apis.hyperbots.com"
    client.close()


def test_client_reads_env_var(monkeypatch):
    monkeypatch.setenv("HYPERAPI_KEY", "hk_test_env")
    client = HyperAPIClient()
    assert client.api_key == "hk_test_env"
    client.close()


def test_client_custom_base_url():
    client = HyperAPIClient(api_key="hk_test_x", base_url="http://localhost:8001/")
    assert client.base_url == "http://localhost:8001"  # trailing slash stripped
    client.close()


def test_client_context_manager():
    with HyperAPIClient(api_key="hk_test_ctx") as client:
        assert client.api_key == "hk_test_ctx"


# ── Method signatures ───────────────────────────────────────────────────

def _get_params(method):
    """Return set of parameter names (excluding self)."""
    sig = inspect.signature(method)
    return set(sig.parameters.keys()) - {"self"}


def test_parse_signature():
    params = _get_params(HyperAPIClient.parse)
    assert "file_path" in params
    assert "ocr_engine" in params
    assert "use_presigned" in params
    assert "image_path" in params  # deprecated alias
    assert "async_mode" not in params  # stripped in v0.2.0


def test_extract_signature():
    params = _get_params(HyperAPIClient.extract)
    assert "file_path" in params
    assert "ocr_engine" in params
    assert "mode" in params
    assert "async_mode" not in params


def test_classify_signature():
    params = _get_params(HyperAPIClient.classify)
    assert "file_path" in params
    assert "mode" in params
    assert "async_mode" not in params


def test_split_signature():
    params = _get_params(HyperAPIClient.split)
    assert "file_path" in params
    assert "mode" in params
    assert "async_mode" not in params


def test_process_signature():
    params = _get_params(HyperAPIClient.process)
    assert "file_path" in params
    assert "ocr_engine" in params
    assert "image_path" in params


def test_upload_document_signature():
    params = _get_params(HyperAPIClient.upload_document)
    assert "file_path" in params
    assert "content_type" in params


def test_no_poll_job_method():
    """poll_job was stripped from v0.2.0 — ensure it doesn't exist."""
    assert not hasattr(HyperAPIClient, "poll_job")


# ── Supported content types ─────────────────────────────────────────────

def test_content_types_include_common_formats():
    assert CONTENT_TYPES[".pdf"] == "application/pdf"
    assert CONTENT_TYPES[".png"] == "image/png"
    assert CONTENT_TYPES[".jpg"] == "image/jpeg"
    assert CONTENT_TYPES[".jpeg"] == "image/jpeg"
    assert CONTENT_TYPES[".tiff"] == "image/tiff"
    assert CONTENT_TYPES[".webp"] == "image/webp"


# ── Path resolution ─────────────────────────────────────────────────────

def test_resolve_path_requires_file():
    client = HyperAPIClient(api_key="hk_test_x")
    with pytest.raises(ValueError, match="file_path is required"):
        client._resolve_path(None)
    client.close()


def test_resolve_path_file_not_found():
    client = HyperAPIClient(api_key="hk_test_x")
    with pytest.raises(FileNotFoundError):
        client._resolve_path("/nonexistent/file.pdf")
    client.close()


# ── Headers ─────────────────────────────────────────────────────────────

def test_headers_contain_api_key_and_request_id():
    client = HyperAPIClient(api_key="hk_test_hdr")
    headers = client._get_headers()
    assert headers["X-API-Key"] == "hk_test_hdr"
    assert "X-Request-ID" in headers
    assert "X-Async" not in headers  # async removed in v0.2.0
    client.close()
