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
            "status_code": status_code,
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


def test_get_job_429_raises_rate_limit_error(mock_backend, client):
    """A 429 on the poll path must surface as RateLimitError (matches submit-time
    behavior) so callers can inspect retry_after / tier / limit uniformly."""
    from hyperapi import RateLimitError
    mock_backend.get("/v1/jobs/j-rl").mock(return_value=httpx.Response(
        429,
        headers={"Retry-After": "60"},
        json={"message": "Rate limit exceeded", "tier": "free", "limit": 1},
    ))

    with pytest.raises(RateLimitError) as ei:
        client.get_job("j-rl")
    assert ei.value.retry_after == 60
    assert ei.value.tier == "free"
    assert ei.value.limit == 1


def test_wait_for_job_backs_off_on_429_then_completes(mock_backend, client, monkeypatch):
    """A 429 *during a poll loop* (free tier: submit spends the 1/60s token, the
    first poll 429s) must NOT abort the wait — the loop honors Retry-After and
    resumes. This is the reported extract_advanced-on-free-tier failure."""
    from hyperapi import RateLimitError  # noqa: F401  (kept for symmetry/readability)
    slept: list[float] = []
    monkeypatch.setattr("hyperapi.client.time.sleep", lambda s: slept.append(s))
    mock_backend.get("/v1/jobs/j-rl2").mock(side_effect=[
        httpx.Response(429, headers={"Retry-After": "2"},
                       json={"message": "Rate limit exceeded", "tier": "free", "limit": 1}),
        _completed_response({"ok": 1}),
    ])
    job = Job(job_id="j-rl2", status="pending", poll_url="/v1/jobs/j-rl2", op="parse")

    result = client.wait_for_job(job)

    assert result == {"ok": 1}          # recovered, did not raise
    # Slept ~Retry-After (2s + up to 0.5s jitter), not the 0s poll interval.
    assert any(2.0 <= s < 3.0 for s in slept), slept


def test_wait_for_job_429_deadline_too_short_fails_fast(mock_backend):
    """If the job's remaining budget can't outlast the rate window, re-raise the
    RateLimitError promptly (with retry_after intact so the caller can resume via
    submit + wait_for_job) rather than sleeping past the deadline or hanging."""
    from hyperapi import RateLimitError
    mock_backend.get("/v1/jobs/j-rl3").mock(return_value=httpx.Response(
        429, headers={"Retry-After": "120"},
        json={"message": "Rate limit exceeded", "tier": "free", "limit": 1},
    ))
    client = HyperAPIClient(
        api_key="hk_test_s", base_url="http://test.local",
        poll_interval=0.0, poll_timeout=0.05,  # window (120s) >> budget
    )
    job = Job(job_id="j-rl3", status="pending", poll_url="/v1/jobs/j-rl3", op="parse")

    with pytest.raises(RateLimitError) as ei:
        client.wait_for_job(job)
    assert ei.value.retry_after == 120   # preserved for a manual resume
    client.close()


def test_wait_for_job_result_null_returns_empty_dict(mock_backend, client):
    """A single-job completed envelope with result=null must return {} (not None)
    so documented result["result"] access can't raise TypeError."""
    mock_backend.get("/v1/jobs/j-nr").mock(return_value=httpx.Response(
        200, json={"status": "completed", "result": None, "request_id": "req-x"},
    ))
    job = Job(job_id="j-nr", status="pending", poll_url="/v1/jobs/j-nr", op="parse")

    assert client.wait_for_job(job) == {}


def test_wait_for_job_missing_result_key_does_not_leak_envelope(mock_backend, client):
    """When a completed envelope has no `result` key, wait_for_job must return {},
    never the raw envelope — which would leak server-side status/timing/usage
    fields to the caller."""
    mock_backend.get("/v1/jobs/j-leak").mock(return_value=httpx.Response(
        200, json={"status": "completed", "request_id": "req-x",
                   "duration_ms": 9, "usage": {"pages": 3}},
    ))
    job = Job(job_id="j-leak", status="pending", poll_url="/v1/jobs/j-leak", op="parse")

    out = client.wait_for_job(job)
    assert out == {}
    assert "usage" not in out and "request_id" not in out


def test_wait_for_jobs_backs_off_on_429_then_completes(mock_backend, client, monkeypatch):
    """The sync round-robin wait_for_jobs has bespoke 429 handling
    (still_pending.extend(pending[pos:]) + the rate_limited flag). Cover it: A
    completes, B 429s mid-pass then completes — both returned in input order,
    nothing dropped/reordered, and the whole call doesn't abort."""
    slept: list[float] = []
    monkeypatch.setattr("hyperapi.client.time.sleep", lambda s: slept.append(s))
    mock_backend.get("/v1/jobs/A").mock(return_value=_completed_response({"a": 1}))
    mock_backend.get("/v1/jobs/B").mock(side_effect=[
        httpx.Response(429, headers={"Retry-After": "2"},
                       json={"message": "Rate limit exceeded", "tier": "free", "limit": 1}),
        _completed_response({"b": 2}),
    ])
    job_a = Job(job_id="A", status="pending", poll_url="/v1/jobs/A", op="parse")
    job_b = Job(job_id="B", status="pending", poll_url="/v1/jobs/B", op="extract")

    results = client.wait_for_jobs([job_a, job_b])

    assert results == [{"a": 1}, {"b": 2}]              # order preserved, none dropped
    assert any(2.0 <= s < 3.0 for s in slept), slept    # backed off Retry-After, resumed


def test_wait_for_jobs_429_deadline_too_short_fails_fast(mock_backend):
    """Round-robin path: a persistent 429 whose Retry-After exceeds the remaining
    budget re-raises RateLimitError (not JobTimeoutError necessarily), promptly."""
    from hyperapi import RateLimitError
    mock_backend.get("/v1/jobs/A").mock(return_value=httpx.Response(
        429, headers={"Retry-After": "120"},
        json={"message": "Rate limit exceeded", "tier": "free", "limit": 1},
    ))
    client = HyperAPIClient(
        api_key="hk_test_s", base_url="http://test.local",
        poll_interval=0.0, poll_timeout=0.05,
    )
    job_a = Job(job_id="A", status="pending", poll_url="/v1/jobs/A", op="parse")

    with pytest.raises(RateLimitError):
        client.wait_for_jobs([job_a])
    client.close()


def test_get_job_malformed_json_raises_typed(mock_backend, client):
    """A 200 with non-JSON body (misconfigured proxy returning HTML) must surface
    as a typed HyperAPIError, not a raw JSONDecodeError."""
    mock_backend.get("/v1/jobs/j-bad").mock(return_value=httpx.Response(
        200,
        headers={"Content-Type": "text/html"},
        text="<html>oops</html>",
    ))

    with pytest.raises(HyperAPIError) as ei:
        client.get_job("j-bad")
    assert "Malformed" in str(ei.value)
    assert ei.value.status_code == 200


def test_wait_for_job_with_zero_timeout_raises_immediately(mock_backend):
    """Edge case: timeout=0 means 'fail immediately if not done on first poll'.
    Verifies the elapsed_s math no longer treats 0 as falsy via `or` fallback."""
    mock_backend.get("/v1/jobs/j-zero").mock(return_value=httpx.Response(
        200, json={"status": "pending"},
    ))
    client = HyperAPIClient(
        api_key="hk_test_z", base_url="http://test.local",
        poll_interval=0.0, poll_timeout=10.0,  # large constructor budget
    )
    job = Job(job_id="j-zero", status="pending", poll_url="/v1/jobs/j-zero", op="parse")

    with pytest.raises(JobTimeoutError) as ei:
        client.wait_for_job(job, timeout=0)  # per-call OVERRIDE to 0
    # Must reflect the actual wait, not the constructor's 10s — proves the
    # `(timeout or self._poll_timeout)` falsy bug is fixed.
    assert ei.value.elapsed_s < 1.0
    client.close()


def test_wait_for_jobs_returns_full_length_with_empty_result_envelope(mock_backend, client):
    """If a completed envelope's `result` is None/missing, the returned list must
    still have the same length as input — never silently drop entries."""
    mock_backend.get("/v1/jobs/A").mock(return_value=httpx.Response(
        200, json={"status": "completed", "result": None},
    ))
    mock_backend.get("/v1/jobs/B").mock(return_value=httpx.Response(
        200, json={"status": "completed", "result": {"ok": 1}},
    ))
    job_a = Job(job_id="A", status="pending", poll_url="/v1/jobs/A", op="parse")
    job_b = Job(job_id="B", status="pending", poll_url="/v1/jobs/B", op="extract")

    results = client.wait_for_jobs([job_a, job_b])

    # CRITICAL invariant: len(results) == len(input). The None-result for A
    # becomes {} so we don't silently drop the slot.
    assert len(results) == 2
    assert results[0] == {}
    assert results[1] == {"ok": 1}


def test_failed_job_request_id_carries_to_exception(mock_backend, client):
    """A failed job envelope's `request_id` field should land on the raised exception."""
    mock_backend.get("/v1/jobs/j-11").mock(return_value=httpx.Response(
        200,
        json={
            "status": "failed", "error": "service down",
            "status_code": 500, "request_id": "req-zzz",
        },
    ))
    job = Job(job_id="j-11", status="pending", poll_url="/v1/jobs/j-11", op="parse")

    with pytest.raises(HyperAPIError) as ei:
        client.wait_for_job(job)
    assert ei.value.request_id == "req-zzz"


# ── Tier-3 polish: KeyboardInterrupt + ConnectError guards ───────────────


def test_wait_for_job_propagates_keyboard_interrupt(mock_backend, client):
    """Ctrl+C mid-poll must NOT be swallowed by the SDK's transient-retry path.
    The user expects to interrupt cleanly — KeyboardInterrupt is BaseException,
    not Exception, but defending against `except Exception:` regressions is
    cheap insurance."""
    call_count = {"n": 0}

    def _side_effect(request):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return httpx.Response(200, json={"status": "pending", "request_id": "req-x"})
        raise KeyboardInterrupt("user pressed Ctrl+C")

    mock_backend.get("/v1/jobs/j-kbi").mock(side_effect=_side_effect)
    job = Job(job_id="j-kbi", status="pending", poll_url="/v1/jobs/j-kbi", op="parse")

    with pytest.raises(KeyboardInterrupt):
        client.wait_for_job(job)


def test_wait_for_jobs_propagates_keyboard_interrupt(mock_backend, client):
    """Same guarantee for the multi-job round-robin polling path."""
    a_count = {"n": 0}

    def _a_side_effect(request):
        a_count["n"] += 1
        if a_count["n"] == 1:
            return httpx.Response(200, json={"status": "pending", "request_id": "req-x"})
        raise KeyboardInterrupt("user pressed Ctrl+C")

    mock_backend.get("/v1/jobs/A").mock(side_effect=_a_side_effect)
    mock_backend.get("/v1/jobs/B").mock(return_value=_pending_response())
    job_a = Job(job_id="A", status="pending", poll_url="/v1/jobs/A", op="parse")
    job_b = Job(job_id="B", status="pending", poll_url="/v1/jobs/B", op="extract")

    with pytest.raises(KeyboardInterrupt):
        client.wait_for_jobs([job_a, job_b])


def test_poll_retries_on_connection_error_then_succeeds(mock_backend):
    """A bare httpx.ConnectError on a poll (DNS hiccup, brief network blip)
    must be absorbed by the transient-retry path and the next poll succeed.
    Mirrors the 5xx-retry guarantee but for the connection-error branch in
    `get_job` -> `_poll_with_retry`."""
    mock_backend.get("/v1/jobs/j-conn").mock(side_effect=[
        httpx.ConnectError("DNS failed"),
        _completed_response({"data": {"ok": True}}),
    ])
    client = HyperAPIClient(
        api_key="hk_test_conn",
        base_url="http://test.local",
        poll_interval=0.0,
        poll_timeout=5.0,
        # Tiny delay so the retry path doesn't sleep meaningfully in tests.
        poll_transient_retry_delay=0.0,
        poll_max_transient_retries=2,
    )
    job = Job(job_id="j-conn", status="pending", poll_url="/v1/jobs/j-conn", op="extract")

    result = client.wait_for_job(job)

    assert result == {"data": {"ok": True}}
    client.close()


def test_wait_for_jobs_timeout_lists_pending_ids(mock_backend):
    """All jobs stay pending past the deadline → JobTimeoutError with comma-joined
    pending ids and elapsed_s populated. Drives L831-832 in `wait_for_jobs`."""
    mock_backend.get("/v1/jobs/A").mock(return_value=_pending_response())
    mock_backend.get("/v1/jobs/B").mock(return_value=_pending_response())
    client = HyperAPIClient(
        api_key="hk_test_multi",
        base_url="http://test.local",
        poll_interval=0.0,
        poll_timeout=10.0,
        poll_transient_retry_delay=0.0,
        poll_max_transient_retries=0,
    )
    job_a = Job(job_id="A", status="pending", poll_url="/v1/jobs/A", op="parse")
    job_b = Job(job_id="B", status="pending", poll_url="/v1/jobs/B", op="extract")

    with pytest.raises(JobTimeoutError) as ei:
        client.wait_for_jobs([job_a, job_b], timeout=0.05, interval=0.01)
    # Both ids must appear, comma-joined.
    assert ei.value.job_id == "A,B"
    assert ei.value.elapsed_s >= 0.0
    client.close()


def test_failed_job_message_includes_http_status(mock_backend, client):
    """str(e) on a failed-job exception must include the HTTP status — support
    tickets quote the message verbatim, so it should be self-describing."""
    mock_backend.get("/v1/jobs/j-msg").mock(return_value=httpx.Response(
        200,
        json={
            "status": "failed",
            "error": "extract pipeline crashed",
            "status_code": 500,
        },
    ))
    job = Job(job_id="j-msg", status="pending", poll_url="/v1/jobs/j-msg", op="extract")

    with pytest.raises(ExtractError) as ei:
        client.wait_for_job(job)
    assert "(HTTP 500)" in str(ei.value)
    assert "extract pipeline crashed" in str(ei.value)
    assert ei.value.status_code == 500


# ── list_recent_jobs ─────────────────────────────────────────────────────


_RECENT_ROWS = [
    {
        "job_id": "j-r1", "request_id": "req-1", "status": "completed",
        "task": "extract", "endpoint": "/v1/extract", "pages": 3,
        "filename": "invoice.pdf", "source": "api", "is_test": False,
        "created_at": "2026-06-10T10:00:00Z", "completed_at": "2026-06-10T10:00:09Z",
        "error": None,
    },
    {
        "job_id": "j-r2", "request_id": "req-2", "status": "cancelled",
        "task": "parse", "endpoint": "/v1/parse", "pages": 1,
        "filename": "scan.png", "source": "playground", "is_test": True,
        "created_at": "2026-06-10T09:00:00Z", "completed_at": None,
        "error": None,
    },
]


def test_list_recent_jobs_defaults(mock_backend, client):
    route = mock_backend.get("/v1/jobs/recent").mock(
        return_value=httpx.Response(200, json={"jobs": _RECENT_ROWS}),
    )

    jobs = client.list_recent_jobs()

    assert [j["job_id"] for j in jobs] == ["j-r1", "j-r2"]
    req = route.calls[0].request
    assert req.headers["X-API-Key"] == "hk_test_unit"
    assert req.url.params["limit"] == "20"
    assert "source" not in req.url.params


def test_list_recent_jobs_limit_and_source_params(mock_backend, client):
    route = mock_backend.get("/v1/jobs/recent").mock(
        return_value=httpx.Response(200, json={"jobs": []}),
    )

    client.list_recent_jobs(limit=5, source="playground")

    req = route.calls[0].request
    assert req.url.params["limit"] == "5"
    assert req.url.params["source"] == "playground"


def test_list_recent_jobs_401_raises_authentication(mock_backend, client):
    mock_backend.get("/v1/jobs/recent").mock(return_value=httpx.Response(401))

    with pytest.raises(AuthenticationError) as ei:
        client.list_recent_jobs()
    assert ei.value.status_code == 401


def test_list_recent_jobs_malformed_envelope_raises_typed(mock_backend, client):
    """A 200 without the {"jobs": [...]} envelope (broken proxy) must surface
    as a typed HyperAPIError, not a raw KeyError."""
    mock_backend.get("/v1/jobs/recent").mock(
        return_value=httpx.Response(200, json={"unexpected": True}),
    )

    with pytest.raises(HyperAPIError) as ei:
        client.list_recent_jobs()
    assert "Malformed" in str(ei.value)


def test_list_recent_jobs_non_list_jobs_raises_typed(mock_backend, client):
    """{"jobs": <non-list>} must also be rejected — the -> list[dict] contract
    holds even against a proxy that mangles only the value."""
    mock_backend.get("/v1/jobs/recent").mock(
        return_value=httpx.Response(200, json={"jobs": {"x": 1}}),
    )

    with pytest.raises(HyperAPIError) as ei:
        client.list_recent_jobs()
    assert "Malformed" in str(ei.value)


def test_list_recent_jobs_429_raises_rate_limit_error(mock_backend, client):
    """The new endpoint must ride the same 429 ladder as get_job/submits."""
    from hyperapi import RateLimitError
    mock_backend.get("/v1/jobs/recent").mock(return_value=httpx.Response(
        429,
        headers={"Retry-After": "30"},
        json={"message": "Rate limit exceeded", "tier": "free", "limit": 1},
    ))

    with pytest.raises(RateLimitError) as ei:
        client.list_recent_jobs()
    assert ei.value.retry_after == 30
    assert ei.value.tier == "free"


# ── delete_job (cancel semantics) ────────────────────────────────────────


def test_delete_job_204_returns_none(mock_backend, client):
    route = mock_backend.delete("/v1/jobs/j-del").mock(return_value=httpx.Response(204))

    assert client.delete_job("j-del") is None
    req = route.calls[0].request
    assert req.method == "DELETE"
    assert req.headers["X-API-Key"] == "hk_test_unit"


def test_delete_job_idempotent_204_for_unknown_id(mock_backend, client):
    """The server returns 204 even for missing/expired/already-terminal jobs —
    the SDK must treat that as success, not raise."""
    mock_backend.delete("/v1/jobs/never-existed").mock(return_value=httpx.Response(204))

    client.delete_job("never-existed")  # no raise


def test_delete_job_404_raises_typed(mock_backend, client):
    """Opaque 404 (cross-org isolation) — same wording as get_job's 404."""
    mock_backend.delete("/v1/jobs/other-org").mock(return_value=httpx.Response(404))

    with pytest.raises(HyperAPIError) as ei:
        client.delete_job("other-org")
    assert ei.value.status_code == 404
    assert "not found" in str(ei.value).lower() or "expired" in str(ei.value).lower()


def test_delete_job_401_raises_authentication(mock_backend, client):
    mock_backend.delete("/v1/jobs/j-del").mock(return_value=httpx.Response(401))

    with pytest.raises(AuthenticationError):
        client.delete_job("j-del")
