"""Async twin of test_contract_batch.py — POST/GET /v1/batch.

Hermetic — respx (`mock_api`) intercepts every /v1/batch call; no network.
Focused on the async surface, especially the webhook_url/metadata body fields.
"""

from __future__ import annotations

import json

import httpx
import pytest

from hyperapi import HyperAPIError


async def test_create_batch_posts_body_and_returns(async_client, mock_api):
    route = mock_api.post("/v1/batch").mock(
        return_value=httpx.Response(
            202, json={"batch_id": "b1", "status": "queued", "total_items": 2}
        )
    )
    out = await async_client.create_batch(endpoint="/v1/classify", document_keys=["k1", "k2"])
    assert out == {"batch_id": "b1", "status": "queued", "total_items": 2}
    payload = json.loads(route.calls.last.request.content)
    assert payload["endpoint"] == "/v1/classify"
    assert payload["document_keys"] == ["k1", "k2"]


async def test_create_batch_sends_webhook_url_and_metadata_when_set(async_client, mock_api):
    route = mock_api.post("/v1/batch").mock(
        return_value=httpx.Response(
            202, json={"batch_id": "b1", "status": "queued", "total_items": 1}
        )
    )
    await async_client.create_batch(
        endpoint="/v1/classify",
        document_keys=["k1"],
        webhook_url="https://hooks.example.com/batch",
        metadata={"run": "nightly", "team": "ap"},
    )
    payload = json.loads(route.calls.last.request.content)
    assert payload["webhook_url"] == "https://hooks.example.com/batch"
    assert payload["metadata"] == {"run": "nightly", "team": "ap"}


async def test_create_batch_omits_webhook_url_and_metadata_when_unset(async_client, mock_api):
    route = mock_api.post("/v1/batch").mock(
        return_value=httpx.Response(
            202, json={"batch_id": "b1", "status": "queued", "total_items": 1}
        )
    )
    await async_client.create_batch(endpoint="/v1/classify", document_keys=["k1"])
    payload = json.loads(route.calls.last.request.content)
    assert "webhook_url" not in payload
    assert "metadata" not in payload


async def test_get_batch_404_raises(async_client, mock_api):
    mock_api.get("/v1/batch/missing").mock(
        return_value=httpx.Response(404, json={"detail": "Batch not found"})
    )
    with pytest.raises(HyperAPIError):
        await async_client.get_batch("missing")


async def test_create_batch_402_object_body_is_readable(async_client, mock_api):
    from tests.test_contract_batch import QUOTA_402, _assert_readable_quota_error

    mock_api.post("/v1/batch").mock(return_value=httpx.Response(402, json=QUOTA_402))
    with pytest.raises(HyperAPIError) as ei:
        await async_client.create_batch(endpoint="/v1/parse", document_keys=["k1"])
    _assert_readable_quota_error(ei.value)


async def test_create_batch_404_is_not_reported_as_batch_not_found(async_client, mock_api):
    mock_api.post("/v1/batch").mock(
        return_value=httpx.Response(404, json={"error": {"code": "NOT_FOUND", "message": "Document not found: k1"}})
    )
    with pytest.raises(HyperAPIError) as ei:
        await async_client.create_batch(endpoint="/v1/parse", document_keys=["k1"])
    assert "Batch not found" not in str(ei.value)
    assert "Document not found: k1" in str(ei.value)


async def test_id_addressed_404_is_still_batch_not_found(async_client, mock_api):
    mock_api.get("/v1/batch/missing").mock(
        return_value=httpx.Response(404, json={"error": {"code": "NOT_FOUND", "message": "Batch not found"}})
    )
    with pytest.raises(HyperAPIError, match="Batch not found") as ei:
        await async_client.get_batch("missing")
    assert ei.value.code == "NOT_FOUND"
