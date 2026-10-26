"""Contract: get_limits() — GET /v1/limits, sync and async.

Limits vary by plan, so the SDK exposes the API's per-task ceilings as plain
JSON for callers to pre-flight against (and to feature-detect optional modes
such as redact ``output="urls"`` via ``redact.urls_max_pages``).
"""

import httpx
import pytest

from hyperapi import AuthenticationError, HyperAPIError, RateLimitError


_LIMITS = {
    "max_upload_bytes": 52428800,
    "parse_fast": {"max_bytes": 52428800, "max_pages": None},
    "parse_advanced": {"max_bytes": 52428800, "max_pages": 60},
    "extract_fast": {"max_bytes": 52428800, "max_pages": None},
    "classify": {"max_bytes": 52428800, "max_pages": None},
    "split": {"max_bytes": 52428800, "max_pages": None},
    "redact": {"max_bytes": 52428800, "max_pages": 36, "urls_max_pages": 60},
}


# ── sync ─────────────────────────────────────────────────────────────────────


def test_get_limits_returns_json_body(mock_api, client):
    route = mock_api.get("/v1/limits").mock(
        return_value=httpx.Response(200, json=_LIMITS)
    )

    limits = client.get_limits()

    assert limits == _LIMITS
    assert route.calls[0].request.headers["X-API-Key"].startswith("hk_test_")
    # Feature detection pattern documented on submit_redact.
    assert limits["redact"].get("urls_max_pages") == 60


def test_get_limits_absent_optional_blocks_stay_absent(mock_api, client):
    """`bulk_parse` / `extract_advanced` are omitted when not offered; the SDK
    must not invent them (absent means "not offered", never "unlimited")."""
    mock_api.get("/v1/limits").mock(return_value=httpx.Response(200, json=_LIMITS))

    limits = client.get_limits()

    assert "bulk_parse" not in limits
    assert "extract_advanced" not in limits


def test_get_limits_401_raises_authentication(mock_api, client):
    mock_api.get("/v1/limits").mock(return_value=httpx.Response(401))
    with pytest.raises(AuthenticationError):
        client.get_limits()


def test_get_limits_429_raises_rate_limit(mock_api, client):
    mock_api.get("/v1/limits").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "7"})
    )
    with pytest.raises(RateLimitError) as ei:
        client.get_limits()
    assert ei.value.retry_after == 7


def test_get_limits_non_object_body_raises_typed(mock_api, client):
    mock_api.get("/v1/limits").mock(return_value=httpx.Response(200, json=[1, 2]))
    with pytest.raises(HyperAPIError, match="Malformed limits response"):
        client.get_limits()


# ── async ────────────────────────────────────────────────────────────────────


async def test_async_get_limits_returns_json_body(mock_api, async_client):
    mock_api.get("/v1/limits").mock(return_value=httpx.Response(200, json=_LIMITS))

    limits = await async_client.get_limits()

    assert limits == _LIMITS


async def test_async_get_limits_401_raises_authentication(mock_api, async_client):
    mock_api.get("/v1/limits").mock(return_value=httpx.Response(401))
    with pytest.raises(AuthenticationError):
        await async_client.get_limits()


async def test_async_get_limits_429_raises_rate_limit(mock_api, async_client):
    mock_api.get("/v1/limits").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "7"})
    )
    with pytest.raises(RateLimitError) as ei:
        await async_client.get_limits()
    assert ei.value.retry_after == 7
