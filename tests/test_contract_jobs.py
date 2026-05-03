"""Contract: GET /v1/jobs/{id} polling — the heart of v0.1.0 submit+poll.

Covers: poll loop, status transitions, transient retry on 5xx, fast-fail on
401/404, JobTimeoutError, request_id propagation, and per-call overrides.
"""

import httpx
import pytest

from hyperapi import (
    AuthenticationError,
    ExtractError,
    HyperAPIError,
    Job,
    JobTimeoutError,
)
from hyperapi import HyperAPIClient


def _completed_response(result):
    return httpx.Response(
        200,
        json={"status": "completed", "result": result, "request_id": "req-x", "duration_ms": 100},
    )


def _pending_response():
    return httpx.Response(200, json={"status": "pending", "request_id": "req-x"})


def _failed_response(error="Insufficient credits", status_code=402):
    return httpx.Response(
        200,
        json={
            "status": "failed",
            "error": error,
            "error_status_code": status_code,
            "request_id": "req-x",
        },
    )


# ── get_job ──────────────────────────────────────────────────────────────


def test_get_job_returns_envelope(mock_backend, client):
    mock_backend.get("/v1/jobs/j-1").mock(return_value=_completed_response({"ocr": "hi"}))
    envelope = client.get_job("j-1")
    assert envelope["status"] == "completed"
    assert envelope["result"] == {"ocr": "hi"}


def test_get_job_404_raises_typed(mock_backend, client):
    mock_backend.get("/v1/jobs/missing").mock(return_value=httpx.Response(404))

    with pytest.raises(HyperAPIError) as ei:
        client.get_job("missing")
    assert ei.value.status_code == 404
    assert "not found" in str(ei.value).lower() or "expired" in str(ei.value).lower()


def test_get_job_401_raises_authentication(mock_backend, client):
    mock_backend.get("/v1/jobs/j-1").mock(return_value=httpx.Response(401))
    with pytest.raises(AuthenticationError) as ei:
        client.get_job("j-1")
    assert ei.value.status_code == 401


def test_get_job_sends_api_key_header(mock_backend, client):
    route = mock_backend.get("/v1/jobs/j-1").mock(return_value=_completed_response({}))
    client.get_job("j-1")
    assert route.calls[0].request.headers["X-API-Key"] == "hk_test_unit"


# ── wait_for_job ─────────────────────────────────────────────────────────


def test_wait_for_job_polls_until_completed(mock_backend, client):
    """Pending → pending → completed should resolve to the result envelope."""
    mock_backend.get("/v1/jobs/j-2").mock(side_effect=[
        _pending_response(),
        _pending_response(),
        _completed_response({"data": {"vendor": "Acme"}}),
    ])
    job = Job(job_id="j-2", status="pending", poll_url="/v1/jobs/j-2", op="extract")

    result = client.wait_for_job(job)

    assert result == {"data": {"vendor": "Acme"}}


def test_wait_for_job_failed_raises_op_specific_error(mock_backend, client):
    """A `failed` envelope on an extract job must surface as ExtractError."""
    mock_backend.get("/v1/jobs/j-3").mock(return_value=_failed_response(
        error="Insufficient credits", status_code=402,
    ))
    job = Job(job_id="j-3", status="pending", poll_url="/v1/jobs/j-3", op="extract")

    with pytest.raises(ExtractError) as ei:
        client.wait_for_job(job)
    assert ei.value.status_code == 402
    assert "credits" in str(ei.value).lower()


def test_wait_for_job_with_raw_job_id_uses_generic_error(mock_backend, client):
    """If the caller passes a raw job_id (not a Job), failures fall back to HyperAPIError."""
    mock_backend.get("/v1/jobs/j-4").mock(return_value=_failed_response(error="boom"))

    with pytest.raises(HyperAPIError) as ei:
        client.wait_for_job("j-4")
    # Not the per-op subclass
    assert type(ei.value) is HyperAPIError


def test_wait_for_job_timeout_raises_job_timeout(mock_backend):
    """Always pending — should raise JobTimeoutError once the budget elapses."""
    mock_backend.get("/v1/jobs/j-5").mock(return_value=_pending_response())
    client = HyperAPIClient(
        api_key="hk_test_t",
        base_url="http://test.local",
        poll_interval=0.01,
        poll_timeout=0.05,
    )
    job = Job(job_id="j-5", status="pending", poll_url="/v1/jobs/j-5", op="parse")

    with pytest.raises(JobTimeoutError) as ei:
        client.wait_for_job(job)
    assert ei.value.job_id == "j-5"
    assert ei.value.elapsed_s >= 0.0
    client.close()


def test_wait_for_job_per_call_overrides(mock_backend, client):
    """Per-call timeout/interval kwargs override the constructor defaults."""
    mock_backend.get("/v1/jobs/j-6").mock(return_value=_pending_response())
    job = Job(job_id="j-6", status="pending", poll_url="/v1/jobs/j-6", op="parse")

    with pytest.raises(JobTimeoutError):
        client.wait_for_job(job, timeout=0.05, interval=0.01)


# ── _poll_with_retry ─────────────────────────────────────────────────────


def test_poll_retries_on_5xx_then_succeeds(mock_backend, client):
    """A 500 followed by a successful poll completes the job — transient flake
    recovery is mandatory because Kong's auth path has residual Sentinel flake."""
    mock_backend.get("/v1/jobs/j-7").mock(side_effect=[
        httpx.Response(500, json={"message": "Authentication service unavailable"}),
        _completed_response({"x": 1}),
    ])
    job = Job(job_id="j-7", status="pending", poll_url="/v1/jobs/j-7", op="parse")

    result = client.wait_for_job(job)

    assert result == {"x": 1}


def test_poll_exhausts_transient_retries_then_raises(mock_backend, client):
    """If 5xx persists past poll_max_transient_retries, the SDK gives up."""
    mock_backend.get("/v1/jobs/j-8").mock(return_value=httpx.Response(503))
    job = Job(job_id="j-8", status="pending", poll_url="/v1/jobs/j-8", op="parse")

    with pytest.raises(HyperAPIError) as ei:
        client.wait_for_job(job)
    assert ei.value.status_code == 503


def test_poll_does_not_retry_on_401(mock_backend, client):
    """401 means the key is bad — retrying just generates noise."""
    route = mock_backend.get("/v1/jobs/j-9").mock(return_value=httpx.Response(401))
    job = Job(job_id="j-9", status="pending", poll_url="/v1/jobs/j-9", op="parse")

    with pytest.raises(AuthenticationError):
        client.wait_for_job(job)
    # Single call — no retry
    assert route.call_count == 1


def test_poll_does_not_retry_on_404(mock_backend, client):
    """404 means the job genuinely doesn't exist (org mismatch or expired)."""
    route = mock_backend.get("/v1/jobs/j-10").mock(return_value=httpx.Response(404))
    job = Job(job_id="j-10", status="pending", poll_url="/v1/jobs/j-10", op="parse")

    with pytest.raises(HyperAPIError):
        client.wait_for_job(job)
    assert route.call_count == 1


# ── wait_for_jobs (round-robin) ──────────────────────────────────────────


def test_wait_for_jobs_returns_results_in_order(mock_backend, client):
    mock_backend.get("/v1/jobs/A").mock(return_value=_completed_response({"a": 1}))
    mock_backend.get("/v1/jobs/B").mock(return_value=_completed_response({"b": 2}))
    job_a = Job(job_id="A", status="pending", poll_url="/v1/jobs/A", op="parse")
    job_b = Job(job_id="B", status="pending", poll_url="/v1/jobs/B", op="extract")

    results = client.wait_for_jobs([job_a, job_b])

    assert results == [{"a": 1}, {"b": 2}]


def test_wait_for_jobs_raises_on_first_failure(mock_backend, client):
    """If one job in the batch fails, the SDK surfaces it (other in-flight jobs
    keep running on the server but are abandoned by the SDK)."""
    mock_backend.get("/v1/jobs/A").mock(return_value=_failed_response(error="boom"))
    mock_backend.get("/v1/jobs/B").mock(return_value=_pending_response())
    job_a = Job(job_id="A", status="pending", poll_url="/v1/jobs/A", op="extract")
    job_b = Job(job_id="B", status="pending", poll_url="/v1/jobs/B", op="parse")

    with pytest.raises(ExtractError):
        client.wait_for_jobs([job_a, job_b])


def test_wait_for_jobs_empty_input_returns_empty():
    client = HyperAPIClient(
        api_key="hk_test_x", base_url="http://test.local",
        poll_interval=0.0, poll_timeout=1.0,
    )
    assert client.wait_for_jobs([]) == []
    client.close()


# ── request_id propagation ───────────────────────────────────────────────


def test_failed_job_request_id_carries_to_exception(mock_backend, client):
    """A failed job envelope's `request_id` field should land on the raised exception."""
    mock_backend.get("/v1/jobs/j-11").mock(return_value=httpx.Response(
        200,
        json={
            "status": "failed", "error": "service down",
            "error_status_code": 500, "request_id": "req-zzz",
        },
    ))
    job = Job(job_id="j-11", status="pending", poll_url="/v1/jobs/j-11", op="parse")

    with pytest.raises(HyperAPIError) as ei:
        client.wait_for_job(job)
    assert ei.value.request_id == "req-zzz"
