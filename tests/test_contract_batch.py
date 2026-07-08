"""Contract tests for the Batch API SDK methods (sync).

Hermetic — respx (`mock_backend`) intercepts every /v1/batch call; no network.
"""

from __future__ import annotations

import json

import httpx
import pytest

from hyperapi import HyperAPIError


def test_create_batch_posts_body_and_returns(client, mock_backend):
    route = mock_backend.post("/v1/batch").mock(
        return_value=httpx.Response(
            202, json={"batch_id": "b1", "status": "queued", "total_items": 2}
        )
    )
    out = client.create_batch(endpoint="/v1/classify", document_keys=["k1", "k2"])
    assert out == {"batch_id": "b1", "status": "queued", "total_items": 2}
    assert route.called
    payload = json.loads(route.calls.last.request.content)
    assert payload["endpoint"] == "/v1/classify"
    assert payload["document_keys"] == ["k1", "k2"]
    assert payload["options"] == {}


def test_create_batch_sends_webhook_url_and_metadata_when_set(client, mock_backend):
    route = mock_backend.post("/v1/batch").mock(
        return_value=httpx.Response(
            202, json={"batch_id": "b1", "status": "queued", "total_items": 1}
        )
    )
    client.create_batch(
        endpoint="/v1/classify",
        document_keys=["k1"],
        webhook_url="https://hooks.example.com/batch",
        metadata={"run": "nightly", "team": "ap"},
    )
    payload = json.loads(route.calls.last.request.content)
    assert payload["webhook_url"] == "https://hooks.example.com/batch"
    assert payload["metadata"] == {"run": "nightly", "team": "ap"}


def test_create_batch_omits_webhook_url_and_metadata_when_unset(client, mock_backend):
    route = mock_backend.post("/v1/batch").mock(
        return_value=httpx.Response(
            202, json={"batch_id": "b1", "status": "queued", "total_items": 1}
        )
    )
    client.create_batch(endpoint="/v1/classify", document_keys=["k1"])
    payload = json.loads(route.calls.last.request.content)
    assert "webhook_url" not in payload
    assert "metadata" not in payload


def test_create_batch_forwards_idempotency_key_header(client, mock_backend):
    route = mock_backend.post("/v1/batch").mock(
        return_value=httpx.Response(
            202, json={"batch_id": "b1", "status": "queued", "total_items": 1}
        )
    )
    client.create_batch(endpoint="/v1/parse", document_keys=["k"], idempotency_key="idem-1")
    assert route.calls.last.request.headers.get("Idempotency-Key") == "idem-1"


def test_get_batch(client, mock_backend):
    mock_backend.get("/v1/batch/b1").mock(
        return_value=httpx.Response(
            200,
            json={"batch_id": "b1", "status": "processing", "counts": {"total": 2}, "items": []},
        )
    )
    assert client.get_batch("b1")["status"] == "processing"


def test_list_batches(client, mock_backend):
    mock_backend.get("/v1/batch").mock(
        return_value=httpx.Response(200, json={"batches": [], "limit": 50, "offset": 0})
    )
    assert "batches" in client.list_batches()


def test_cancel_batch(client, mock_backend):
    mock_backend.delete("/v1/batch/b1").mock(
        return_value=httpx.Response(
            200,
            json={"batch_id": "b1", "status": "canceled", "counts": {"total": 1}, "items": []},
        )
    )
    assert client.cancel_batch("b1")["status"] == "canceled"


def test_get_batch_404_raises(client, mock_backend):
    mock_backend.get("/v1/batch/missing").mock(
        return_value=httpx.Response(404, json={"detail": "Batch not found"})
    )
    with pytest.raises(HyperAPIError):
        client.get_batch("missing")


def test_wait_for_batch_polls_until_terminal(client, mock_backend):
    mock_backend.get("/v1/batch/b1").mock(
        side_effect=[
            httpx.Response(
                200,
                json={"batch_id": "b1", "status": "processing", "counts": {"total": 1}, "items": []},
            ),
            httpx.Response(
                200,
                json={
                    "batch_id": "b1",
                    "status": "completed",
                    "counts": {"total": 1, "done": 1},
                    "items": [],
                },
            ),
        ]
    )
    out = client.wait_for_batch("b1", timeout=5.0, poll_interval=0.0)
    assert out["status"] == "completed"
