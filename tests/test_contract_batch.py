"""Contract tests for the Batch API SDK methods (sync).

Hermetic — respx (`mock_api`) intercepts every /v1/batch call; no network.
"""

from __future__ import annotations

import json

import httpx
import pytest

from hyperapi import HyperAPIError


def test_create_batch_posts_body_and_returns(client, mock_api):
    route = mock_api.post("/v1/batch").mock(
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


def test_create_batch_sends_webhook_url_and_metadata_when_set(client, mock_api):
    route = mock_api.post("/v1/batch").mock(
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


def test_create_batch_omits_webhook_url_and_metadata_when_unset(client, mock_api):
    route = mock_api.post("/v1/batch").mock(
        return_value=httpx.Response(
            202, json={"batch_id": "b1", "status": "queued", "total_items": 1}
        )
    )
    client.create_batch(endpoint="/v1/classify", document_keys=["k1"])
    payload = json.loads(route.calls.last.request.content)
    assert "webhook_url" not in payload
    assert "metadata" not in payload


def test_create_batch_forwards_idempotency_key_header(client, mock_api):
    route = mock_api.post("/v1/batch").mock(
        return_value=httpx.Response(
            202, json={"batch_id": "b1", "status": "queued", "total_items": 1}
        )
    )
    client.create_batch(endpoint="/v1/parse", document_keys=["k"], idempotency_key="idem-1")
    assert route.calls.last.request.headers.get("Idempotency-Key") == "idem-1"


def test_get_batch(client, mock_api):
    mock_api.get("/v1/batch/b1").mock(
        return_value=httpx.Response(
            200,
            json={"batch_id": "b1", "status": "processing", "counts": {"total": 2}, "items": []},
        )
    )
    assert client.get_batch("b1")["status"] == "processing"


def test_list_batches(client, mock_api):
    mock_api.get("/v1/batch").mock(
        return_value=httpx.Response(200, json={"batches": [], "limit": 50, "offset": 0})
    )
    assert "batches" in client.list_batches()


def test_cancel_batch(client, mock_api):
    mock_api.delete("/v1/batch/b1").mock(
        return_value=httpx.Response(
            200,
            json={"batch_id": "b1", "status": "canceled", "counts": {"total": 1}, "items": []},
        )
    )
    assert client.cancel_batch("b1")["status"] == "canceled"


def test_get_batch_404_raises(client, mock_api):
    mock_api.get("/v1/batch/missing").mock(
        return_value=httpx.Response(404, json={"detail": "Batch not found"})
    )
    with pytest.raises(HyperAPIError):
        client.get_batch("missing")


def test_wait_for_batch_polls_until_terminal(client, mock_api):
    mock_api.get("/v1/batch/b1").mock(
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


def test_wait_for_batch_backs_off_on_429_then_completes(client, mock_api, monkeypatch):
    """wait_for_batch shares the poll-loop 429-backoff contract: a 429 mid-wait
    sleeps Retry-After and resumes instead of aborting the (potentially hours-long)
    batch wait."""
    slept: list[float] = []
    monkeypatch.setattr("hyperapi.client.time.sleep", lambda s: slept.append(s))
    mock_api.get("/v1/batch/b2").mock(side_effect=[
        httpx.Response(429, headers={"Retry-After": "2"},
                       json={"message": "Rate limit exceeded", "tier": "free", "limit": 1}),
        httpx.Response(200, json={"batch_id": "b2", "status": "completed",
                                  "counts": {"total": 1, "done": 1}, "items": []}),
    ])

    out = client.wait_for_batch("b2", timeout=5.0, poll_interval=0.0)

    assert out["status"] == "completed"
    assert any(2.0 <= s < 3.0 for s in slept), slept   # Retry-After + jitter


# ── error bodies ─────────────────────────────────────────────────────────────

# The quota refusal on POST /v1/batch: the human-text slot of the envelope
# holds an object, not a string.
QUOTA_402 = {
    "error": {
        "code": "PAYMENT_REQUIRED",
        "message": {
            "error": "quota_exceeded",
            "list_price_cents": 120,
            "list_price_micros": 1200000,
        },
    }
}


def _assert_readable_quota_error(exc):
    text = str(exc)
    assert "quota_exceeded" in text
    assert "{" not in text and "'" not in text      # no Python dict repr
    assert exc.status_code == 402
    assert exc.code == "PAYMENT_REQUIRED"
    assert exc.details == QUOTA_402["error"]["message"]


def test_create_batch_402_object_body_is_readable(client, mock_api):
    mock_api.post("/v1/batch").mock(return_value=httpx.Response(402, json=QUOTA_402))
    with pytest.raises(HyperAPIError) as ei:
        client.create_batch(endpoint="/v1/parse", document_keys=["k1"])
    _assert_readable_quota_error(ei.value)


def test_batch_error_object_without_text_is_summarised(client, mock_api):
    """No message/detail/error string inside: fall back to key=value pairs."""
    mock_api.post("/v1/batch").mock(
        return_value=httpx.Response(
            402, json={"detail": {"list_price_cents": 120, "reason_code": 7, "extra": None}}
        )
    )
    with pytest.raises(HyperAPIError) as ei:
        client.create_batch(endpoint="/v1/parse", document_keys=["k1"])
    assert str(ei.value).startswith("list_price_cents=120, reason_code=7")
    assert "{" not in str(ei.value)
    assert ei.value.details == {"list_price_cents": 120, "reason_code": 7, "extra": None}


def test_batch_error_carries_code_and_details(client, mock_api):
    mock_api.post("/v1/batch").mock(
        return_value=httpx.Response(
            400,
            json={"error": {"code": "invalid_endpoint", "message": "Unsupported endpoint", "details": {"endpoint": "/v1/x"}}},
        )
    )
    with pytest.raises(HyperAPIError, match="Unsupported endpoint") as ei:
        client.create_batch(endpoint="/v1/x", document_keys=["k1"])
    assert ei.value.code == "invalid_endpoint"
    assert ei.value.details == {"endpoint": "/v1/x"}


def test_create_batch_404_is_not_reported_as_batch_not_found(client, mock_api):
    """POST /v1/batch addresses no batch, so a 404 there means something else."""
    mock_api.post("/v1/batch").mock(
        return_value=httpx.Response(404, json={"error": {"code": "NOT_FOUND", "message": "Document not found: k1"}})
    )
    with pytest.raises(HyperAPIError) as ei:
        client.create_batch(endpoint="/v1/parse", document_keys=["k1"])
    assert ei.value.status_code == 404
    assert "Batch not found" not in str(ei.value)
    assert "Document not found: k1" in str(ei.value)
    assert ei.value.code == "NOT_FOUND"


def test_id_addressed_404_is_still_batch_not_found(client, mock_api):
    mock_api.get("/v1/batch/missing").mock(
        return_value=httpx.Response(404, json={"error": {"code": "NOT_FOUND", "message": "Batch not found"}})
    )
    mock_api.delete("/v1/batch/missing").mock(
        return_value=httpx.Response(404, json={"error": {"code": "NOT_FOUND", "message": "Batch not found"}})
    )
    for call in (client.get_batch, client.cancel_batch):
        with pytest.raises(HyperAPIError, match="Batch not found") as ei:
            call("missing")
        assert ei.value.status_code == 404
        assert ei.value.code == "NOT_FOUND"


def test_create_batch_accepts_the_advanced_extract_endpoint(client, mock_api):
    route = mock_api.post("/v1/batch").mock(
        return_value=httpx.Response(
            202, json={"batch_id": "b2", "status": "queued", "total_items": 1}
        )
    )
    client.create_batch(endpoint="/v1/extract-omni", document_keys=["k1"])
    assert json.loads(route.calls.last.request.content)["endpoint"] == "/v1/extract-omni"
