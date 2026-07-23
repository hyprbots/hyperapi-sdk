"""
Basic AsyncHyperAPIClient tests — import checks, constructor, method shape.
Mirror of test_basic.py focused on the async-specific surface; module-level
pure-utility tests (regex scrubbing, retry-after parsing, content types) are
not duplicated because they exercise code that is shared between both clients
and is already covered by test_basic.py.
"""

import inspect
import pytest

from hyperapi import (
    AsyncHyperAPIClient,
    AuthenticationError,
    HyperAPIClient,
    __version__,
)


# ── Import & version ────────────────────────────────────────────────────


def test_async_client_imports_from_top_level():
    """AsyncHyperAPIClient must be importable from the top-level `hyperapi` package
    so customers don't have to know about submodule layout."""
    assert AsyncHyperAPIClient is not None


def test_version_is_060():
    """v0.6.0 ships the Edit API (detect + fill); the version bump must be visible."""
    assert __version__ == "0.6.0"


# ── Async-ness of methods ───────────────────────────────────────────────


def test_async_public_methods_are_coroutines():
    """Every public op + lifecycle method on AsyncHyperAPIClient must be a
    coroutine function — otherwise `await client.parse(...)` would silently
    return a non-awaitable and the user's event loop would never see the call."""
    for name in (
        "parse", "extract", "classify", "split", "process",
        "submit_parse", "submit_extract", "submit_classify", "submit_split",
        "upload_document", "get_job", "wait_for_job", "wait_for_jobs",
        "aclose",
    ):
        method = getattr(AsyncHyperAPIClient, name)
        assert inspect.iscoroutinefunction(method), f"{name} is not a coroutine"


def test_sync_public_methods_are_not_coroutines():
    """Defensive twin of the above — guards against an accidental copy-paste
    that turns the sync client into an async one (which would silently break
    every existing customer)."""
    for name in (
        "parse", "extract", "classify", "split", "process",
        "submit_parse", "submit_extract", "submit_classify", "submit_split",
        "upload_document", "get_job", "wait_for_job", "wait_for_jobs",
        "close",
    ):
        method = getattr(HyperAPIClient, name)
        assert not inspect.iscoroutinefunction(method), f"{name} unexpectedly async"


# ── Constructor ─────────────────────────────────────────────────────────


def test_async_client_requires_api_key(monkeypatch):
    monkeypatch.delenv("HYPERAPI_KEY", raising=False)
    with pytest.raises(AuthenticationError, match="API key required"):
        AsyncHyperAPIClient()


def test_async_client_accepts_api_key():
    client = AsyncHyperAPIClient(api_key="hk_test_abc")
    assert client.api_key == "hk_test_abc"
    assert client.base_url == "https://apis.hyperbots.com"


def test_async_client_reads_env_var(monkeypatch):
    monkeypatch.setenv("HYPERAPI_KEY", "hk_test_env")
    client = AsyncHyperAPIClient()
    assert client.api_key == "hk_test_env"


def test_async_client_custom_base_url():
    client = AsyncHyperAPIClient(api_key="hk_test_x", base_url="http://localhost:8001/")
    assert client.base_url == "http://localhost:8001"  # trailing slash stripped


def test_async_client_repr_masks_api_key():
    client = AsyncHyperAPIClient(api_key="hk_live_abcdef0123456789xyz")
    text = repr(client)
    assert "hk_live_abcdef" not in text
    assert "hk_***" in text
    assert text.startswith("AsyncHyperAPIClient(")


def test_async_constructor_exposes_poll_knobs():
    sig = inspect.signature(AsyncHyperAPIClient.__init__)
    for kw in (
        "poll_interval",
        "poll_timeout",
        "poll_max_transient_retries",
        "poll_transient_retry_delay",
    ):
        assert kw in sig.parameters


def test_async_client_user_agent_has_async_suffix():
    """The async client's User-Agent gets a '-async' suffix so backend log
    analytics can distinguish sync vs async client traffic without inspecting
    other signals."""
    client = AsyncHyperAPIClient(api_key="hk_test_ua")
    ua = client._client.headers.get("user-agent")
    assert ua.startswith(f"hyperapi-sdk-python/{__version__}-async")
    assert "httpx/" in ua
    assert "Python/" in ua


# ── Async context manager ───────────────────────────────────────────────


async def test_async_context_manager_closes_client():
    """`async with` must call aclose() on exit so the connection pool is released."""
    client = AsyncHyperAPIClient(api_key="hk_test_ctx")
    async with client as c:
        assert c is client
        assert c.api_key == "hk_test_ctx"
    # After exit, the httpx.AsyncClient is closed — its `is_closed` flag flips True.
    assert client._client.is_closed


async def test_async_aclose_is_idempotent():
    """Calling aclose() twice must not raise — important for shutdown paths
    that may try to close in multiple places (signal handlers + finally blocks)."""
    client = AsyncHyperAPIClient(api_key="hk_test_x")
    await client.aclose()
    # Second close must not raise (httpx.AsyncClient.aclose is itself idempotent).
    await client.aclose()


# ── Method signatures (parity with sync) ────────────────────────────────


def _get_params(method):
    sig = inspect.signature(method)
    return set(sig.parameters.keys()) - {"self"}


def test_async_parse_signature_matches_sync():
    assert _get_params(AsyncHyperAPIClient.parse) == _get_params(HyperAPIClient.parse)


def test_async_extract_signature_matches_sync():
    assert _get_params(AsyncHyperAPIClient.extract) == _get_params(HyperAPIClient.extract)


def test_async_classify_signature_matches_sync():
    assert _get_params(AsyncHyperAPIClient.classify) == _get_params(HyperAPIClient.classify)


def test_async_split_signature_matches_sync():
    assert _get_params(AsyncHyperAPIClient.split) == _get_params(HyperAPIClient.split)


def test_async_process_signature_matches_sync():
    assert _get_params(AsyncHyperAPIClient.process) == _get_params(HyperAPIClient.process)


def test_async_upload_document_signature_matches_sync():
    assert (
        _get_params(AsyncHyperAPIClient.upload_document)
        == _get_params(HyperAPIClient.upload_document)
    )


def test_async_submit_methods_exist_per_op():
    """The fire-and-forget submit_* methods are first-class on the async client too."""
    for op in ("parse", "extract", "classify", "split"):
        method = getattr(AsyncHyperAPIClient, f"submit_{op}", None)
        assert callable(method), f"submit_{op} missing on AsyncHyperAPIClient"
        assert inspect.iscoroutinefunction(method)


def test_async_poll_helpers_exist():
    for name in ("get_job", "wait_for_job", "wait_for_jobs"):
        method = getattr(AsyncHyperAPIClient, name)
        assert inspect.iscoroutinefunction(method)


# ── Path resolution ─────────────────────────────────────────────────────


def test_async_resolve_path_requires_file():
    client = AsyncHyperAPIClient(api_key="hk_test_x")
    with pytest.raises(ValueError, match="file_path is required"):
        client._resolve_path(None)


def test_async_resolve_path_file_not_found():
    client = AsyncHyperAPIClient(api_key="hk_test_x")
    with pytest.raises(FileNotFoundError):
        client._resolve_path("/nonexistent/file.pdf")


# ── Headers ─────────────────────────────────────────────────────────────


def test_async_headers_contain_api_key_and_request_id():
    client = AsyncHyperAPIClient(api_key="hk_test_hdr")
    headers = client._get_headers()
    assert headers["X-API-Key"] == "hk_test_hdr"
    assert "X-Request-ID" in headers
    assert "X-Async" not in headers


def test_async_headers_async_mode_sets_x_async_true():
    """`async_mode` here is the HTTP-contract flag, not Python async/await."""
    client = AsyncHyperAPIClient(api_key="hk_test_async")
    headers = client._get_headers(async_mode=True)
    assert headers["X-Async"] == "true"
