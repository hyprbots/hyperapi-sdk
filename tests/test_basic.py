"""
Basic SDK tests — import checks, version, method shape, exception hierarchy.
These run without network access or API keys.
"""

import inspect
import pytest

from hyperapi import (
    AuthenticationError,
    ClassifyError,
    DocumentUploadError,
    ExtractError,
    HyperAPIClient,
    HyperAPIError,
    Job,
    JobTimeoutError,
    ParseError,
    RateLimitError,
    SplitError,
    __version__,
)
from hyperapi.client import CONTENT_TYPES


# ── Import & version ────────────────────────────────────────────────────


def test_version_is_semver():
    parts = __version__.split(".")
    assert len(parts) == 3
    assert all(p.isdigit() for p in parts)


def test_version_is_010():
    assert __version__ == "0.1.0"


# ── Exception hierarchy ─────────────────────────────────────────────────


def test_all_exceptions_inherit_from_base():
    for cls in (
        AuthenticationError,
        RateLimitError,
        JobTimeoutError,
        ParseError,
        ExtractError,
        ClassifyError,
        SplitError,
        DocumentUploadError,
    ):
        assert issubclass(cls, HyperAPIError)


def test_exception_stores_status_code_and_request_id():
    err = ParseError("bad", status_code=422, request_id="req-1")
    assert err.status_code == 422
    assert err.request_id == "req-1"
    assert str(err) == "bad"


def test_exception_default_status_code_and_request_id():
    err = HyperAPIError("oops")
    assert err.status_code is None
    assert err.request_id is None


def test_rate_limit_error_carries_retry_after_tier_limit():
    err = RateLimitError(retry_after=60, tier="free", limit=1, request_id="r")
    assert err.retry_after == 60
    assert err.tier == "free"
    assert err.limit == 1
    assert err.status_code == 429
    assert err.request_id == "r"


def test_job_timeout_error_carries_job_id_and_elapsed():
    err = JobTimeoutError("timed out", job_id="j-1", elapsed_s=10.5)
    assert err.job_id == "j-1"
    assert err.elapsed_s == 10.5


def test_exception_message_redacts_api_keys():
    """A server response that leaks an API key into the body must not surface
    that key in our exception text."""
    err = ParseError("Invalid token: hk_live_abc123def456ghi789jkl0")
    assert "hk_live_abc123" not in str(err)
    assert "hk_***redacted" in str(err)


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


def test_constructor_exposes_poll_knobs():
    """Polling cadence + timeout are configurable per-client (defaults match the
    platform playground)."""
    sig = inspect.signature(HyperAPIClient.__init__)
    for kw in ("poll_interval", "poll_timeout", "poll_max_transient_retries"):
        assert kw in sig.parameters


def test_client_repr_masks_api_key():
    """Common notebook footgun: print(client) must not leak the key."""
    client = HyperAPIClient(api_key="hk_live_abcdef0123456789xyz")
    text = repr(client)
    assert "hk_live_abcdef" not in text
    assert "hk_***" in text
    assert text.endswith("9xyz', base_url='https://apis.hyperbots.com')")
    client.close()


def test_client_sets_user_agent_header():
    """User-Agent must be present on the underlying httpx.Client so backend
    log analytics + customer firewalls can identify SDK traffic."""
    client = HyperAPIClient(api_key="hk_test_ua")
    assert client._client.headers.get("user-agent") == f"hyperapi-sdk-python/{__version__}"
    client.close()


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
    assert "poll_timeout" in params
    assert "poll_interval" in params


def test_extract_signature():
    params = _get_params(HyperAPIClient.extract)
    assert "file_path" in params
    assert "ocr_engine" in params
    assert "mode" in params
    assert "poll_timeout" in params


def test_classify_signature():
    params = _get_params(HyperAPIClient.classify)
    assert "file_path" in params
    assert "mode" in params


def test_split_signature():
    params = _get_params(HyperAPIClient.split)
    assert "file_path" in params
    assert "mode" in params


def test_process_signature():
    params = _get_params(HyperAPIClient.process)
    assert "file_path" in params
    assert "ocr_engine" in params
    assert "image_path" in params
    assert "poll_timeout" in params


def test_upload_document_signature():
    params = _get_params(HyperAPIClient.upload_document)
    assert "file_path" in params
    assert "content_type" in params


def test_submit_methods_exist_per_op():
    """v0.1.0 brings back the explicit submit-and-poll path (Reducto-style escape hatch)."""
    for op in ("parse", "extract", "classify", "split"):
        method = getattr(HyperAPIClient, f"submit_{op}", None)
        assert callable(method), f"submit_{op} missing"


def test_poll_helpers_exist():
    assert callable(HyperAPIClient.get_job)
    assert callable(HyperAPIClient.wait_for_job)
    assert callable(HyperAPIClient.wait_for_jobs)


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
    assert "X-Async" not in headers  # default: not async (only the async path sets it)
    client.close()


def test_headers_async_mode_sets_x_async_true():
    client = HyperAPIClient(api_key="hk_test_async")
    headers = client._get_headers(async_mode=True)
    assert headers["X-Async"] == "true"
    client.close()


def test_job_dataclass_shape():
    j = Job(job_id="j-1", status="pending", poll_url="/v1/jobs/j-1", op="parse")
    assert j.job_id == "j-1"
    assert j.op == "parse"
    assert j.submitted_at > 0
