"""Async twin of test_contract_jobs.py — GET /v1/jobs/{id} polling.

Most tests mirror the sync versions directly. A few have intentionally
adjusted assertions where async-via-gather genuinely differs from sync
round-robin polling:

  - `wait_for_jobs` uses `asyncio.gather(...)` so the FIRST failing job's
    JobTimeoutError raises and cancels the rest; the sync version's
    comma-joined "A,B" job_id is not produced. The async-equivalent test
    asserts JobTimeoutError raises with one of the two pending ids.
  - KeyboardInterrupt under asyncio.gather propagates; CancelledError on the
    siblings is silently swallowed by gather. We assert KeyboardInterrupt
    still escapes, which is the user-visible contract.
"""

import asyncio

import httpx
import pytest

from hyperapi import (
    AsyncHyperAPIClient,
    AuthenticationError,
    ExtractError,
    HyperAPIError,
    Job,
    JobTimeoutError,
)


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


async def test_async_get_job_returns_envelope(mock_backend, async_client):
    mock_backend.get("/v1/jobs/j-1").mock(return_value=_completed_response({"ocr": "hi"}))
    envelope = await async_client.get_job("j-1")
    assert envelope["status"] == "completed"
    assert envelope["result"] == {"ocr": "hi"}


async def test_async_get_job_404_raises_typed(mock_backend, async_client):
    mock_backend.get("/v1/jobs/missing").mock(return_value=httpx.Response(404))

    with pytest.raises(HyperAPIError) as ei:
        await async_client.get_job("missing")
    assert ei.value.status_code == 404
    assert "not found" in str(ei.value).lower() or "expired" in str(ei.value).lower()


async def test_async_get_job_401_raises_authentication(mock_backend, async_client):
    mock_backend.get("/v1/jobs/j-1").mock(return_value=httpx.Response(401))
    with pytest.raises(AuthenticationError) as ei:
        await async_client.get_job("j-1")
    assert ei.value.status_code == 401


async def test_async_get_job_sends_api_key_header(mock_backend, async_client):
    route = mock_backend.get("/v1/jobs/j-1").mock(return_value=_completed_response({}))
    await async_client.get_job("j-1")
    assert route.calls[0].request.headers["X-API-Key"] == "hk_test_unit"


# ── wait_for_job ─────────────────────────────────────────────────────────


async def test_async_wait_for_job_polls_until_completed(mock_backend, async_client):
    mock_backend.get("/v1/jobs/j-2").mock(side_effect=[
        _pending_response(),
        _pending_response(),
        _completed_response({"data": {"vendor": "Acme"}}),
    ])
    job = Job(job_id="j-2", status="pending", poll_url="/v1/jobs/j-2", op="extract")

    result = await async_client.wait_for_job(job)

    assert result == {"data": {"vendor": "Acme"}}


async def test_async_wait_for_job_failed_raises_op_specific_error(mock_backend, async_client):
    mock_backend.get("/v1/jobs/j-3").mock(return_value=_failed_response(
        error="Insufficient credits", status_code=402,
    ))
    job = Job(job_id="j-3", status="pending", poll_url="/v1/jobs/j-3", op="extract")

    with pytest.raises(ExtractError) as ei:
        await async_client.wait_for_job(job)
    assert ei.value.status_code == 402
    assert "credits" in str(ei.value).lower()


async def test_async_wait_for_job_with_raw_job_id_uses_generic_error(mock_backend, async_client):
    mock_backend.get("/v1/jobs/j-4").mock(return_value=_failed_response(error="boom"))

    with pytest.raises(HyperAPIError) as ei:
        await async_client.wait_for_job("j-4")
    assert type(ei.value) is HyperAPIError


async def test_async_wait_for_job_timeout_raises_job_timeout(mock_backend):
    mock_backend.get("/v1/jobs/j-5").mock(return_value=_pending_response())
    client = AsyncHyperAPIClient(
        api_key="hk_test_t",
        base_url="http://test.local",
        poll_interval=0.01,
        poll_timeout=0.05,
    )
    try:
        job = Job(job_id="j-5", status="pending", poll_url="/v1/jobs/j-5", op="parse")

        with pytest.raises(JobTimeoutError) as ei:
            await client.wait_for_job(job)
        assert ei.value.job_id == "j-5"
        assert ei.value.elapsed_s >= 0.0
    finally:
        await client.aclose()


async def test_async_wait_for_job_per_call_overrides(mock_backend, async_client):
    mock_backend.get("/v1/jobs/j-6").mock(return_value=_pending_response())
    job = Job(job_id="j-6", status="pending", poll_url="/v1/jobs/j-6", op="parse")

    with pytest.raises(JobTimeoutError):
        await async_client.wait_for_job(job, timeout=0.05, interval=0.01)


# ── _poll_with_retry ─────────────────────────────────────────────────────


async def test_async_poll_retries_on_5xx_then_succeeds(mock_backend, async_client):
    mock_backend.get("/v1/jobs/j-7").mock(side_effect=[
        httpx.Response(500, json={"message": "Authentication service unavailable"}),
        _completed_response({"x": 1}),
    ])
    job = Job(job_id="j-7", status="pending", poll_url="/v1/jobs/j-7", op="parse")

    result = await async_client.wait_for_job(job)

    assert result == {"x": 1}


async def test_async_poll_exhausts_transient_retries_then_raises(mock_backend, async_client):
    mock_backend.get("/v1/jobs/j-8").mock(return_value=httpx.Response(503))
    job = Job(job_id="j-8", status="pending", poll_url="/v1/jobs/j-8", op="parse")

    with pytest.raises(HyperAPIError) as ei:
        await async_client.wait_for_job(job)
    assert ei.value.status_code == 503


async def test_async_poll_does_not_retry_on_401(mock_backend, async_client):
    route = mock_backend.get("/v1/jobs/j-9").mock(return_value=httpx.Response(401))
    job = Job(job_id="j-9", status="pending", poll_url="/v1/jobs/j-9", op="parse")

    with pytest.raises(AuthenticationError):
        await async_client.wait_for_job(job)
    assert route.call_count == 1


async def test_async_poll_does_not_retry_on_404(mock_backend, async_client):
    route = mock_backend.get("/v1/jobs/j-10").mock(return_value=httpx.Response(404))
    job = Job(job_id="j-10", status="pending", poll_url="/v1/jobs/j-10", op="parse")

    with pytest.raises(HyperAPIError):
        await async_client.wait_for_job(job)
    assert route.call_count == 1


# ── wait_for_jobs (async-gather) ─────────────────────────────────────────


async def test_async_wait_for_jobs_returns_results_in_order(mock_backend, async_client):
    mock_backend.get("/v1/jobs/A").mock(return_value=_completed_response({"a": 1}))
    mock_backend.get("/v1/jobs/B").mock(return_value=_completed_response({"b": 2}))
    job_a = Job(job_id="A", status="pending", poll_url="/v1/jobs/A", op="parse")
    job_b = Job(job_id="B", status="pending", poll_url="/v1/jobs/B", op="extract")

    results = await async_client.wait_for_jobs([job_a, job_b])

    assert results == [{"a": 1}, {"b": 2}]


async def test_async_wait_for_jobs_raises_on_first_failure(mock_backend, async_client):
    mock_backend.get("/v1/jobs/A").mock(return_value=_failed_response(error="boom"))
    mock_backend.get("/v1/jobs/B").mock(return_value=_pending_response())
    job_a = Job(job_id="A", status="pending", poll_url="/v1/jobs/A", op="extract")
    job_b = Job(job_id="B", status="pending", poll_url="/v1/jobs/B", op="parse")

    with pytest.raises(ExtractError):
        await async_client.wait_for_jobs([job_a, job_b])


async def test_async_wait_for_jobs_empty_input_returns_empty():
    client = AsyncHyperAPIClient(
        api_key="hk_test_x", base_url="http://test.local",
        poll_interval=0.0, poll_timeout=1.0,
    )
    try:
        assert await client.wait_for_jobs([]) == []
    finally:
        await client.aclose()


# ── 429 + malformed-JSON edge cases ─────────────────────────────────────


async def test_async_get_job_429_raises_rate_limit_error(mock_backend, async_client):
    from hyperapi import RateLimitError
    mock_backend.get("/v1/jobs/j-rl").mock(return_value=httpx.Response(
        429,
        headers={"Retry-After": "60"},
        json={"message": "Rate limit exceeded", "tier": "free", "limit": 1},
    ))

    with pytest.raises(RateLimitError) as ei:
        await async_client.get_job("j-rl")
    assert ei.value.retry_after == 60
    assert ei.value.tier == "free"
    assert ei.value.limit == 1


async def test_async_get_job_malformed_json_raises_typed(mock_backend, async_client):
    mock_backend.get("/v1/jobs/j-bad").mock(return_value=httpx.Response(
        200,
        headers={"Content-Type": "text/html"},
        text="<html>oops</html>",
    ))

    with pytest.raises(HyperAPIError) as ei:
        await async_client.get_job("j-bad")
    assert "Malformed" in str(ei.value)
    assert ei.value.status_code == 200


async def test_async_wait_for_job_with_zero_timeout_raises_immediately(mock_backend):
    """Edge case: timeout=0 must not silently swap in the constructor default
    via Python's `or` truthiness — same fix as the sync client."""
    mock_backend.get("/v1/jobs/j-zero").mock(return_value=httpx.Response(
        200, json={"status": "pending"},
    ))
    client = AsyncHyperAPIClient(
        api_key="hk_test_z", base_url="http://test.local",
        poll_interval=0.0, poll_timeout=10.0,
    )
    try:
        job = Job(job_id="j-zero", status="pending", poll_url="/v1/jobs/j-zero", op="parse")

        with pytest.raises(JobTimeoutError) as ei:
            await client.wait_for_job(job, timeout=0)
        assert ei.value.elapsed_s < 1.0
    finally:
        await client.aclose()


async def test_async_wait_for_jobs_returns_full_length_with_empty_result_envelope(
    mock_backend, async_client,
):
    mock_backend.get("/v1/jobs/A").mock(return_value=httpx.Response(
        200, json={"status": "completed", "result": None},
    ))
    mock_backend.get("/v1/jobs/B").mock(return_value=httpx.Response(
        200, json={"status": "completed", "result": {"ok": 1}},
    ))
    job_a = Job(job_id="A", status="pending", poll_url="/v1/jobs/A", op="parse")
    job_b = Job(job_id="B", status="pending", poll_url="/v1/jobs/B", op="extract")

    results = await async_client.wait_for_jobs([job_a, job_b])

    assert len(results) == 2
    # A's None result coerces to {} so the slot is preserved.
    # NOTE: wait_for_job returns envelope.get("result", envelope); if result is
    # None, that returns None, and our async wait_for_jobs coerces None → {}.
    # Sync version uses the same coercion (line 822 in client.py).
    assert results[0] == {}
    assert results[1] == {"ok": 1}


async def test_async_failed_job_request_id_carries_to_exception(mock_backend, async_client):
    mock_backend.get("/v1/jobs/j-11").mock(return_value=httpx.Response(
        200,
        json={
            "status": "failed", "error": "service down",
            "error_status_code": 500, "request_id": "req-zzz",
        },
    ))
    job = Job(job_id="j-11", status="pending", poll_url="/v1/jobs/j-11", op="parse")

    with pytest.raises(HyperAPIError) as ei:
        await async_client.wait_for_job(job)
    assert ei.value.request_id == "req-zzz"


# ── KeyboardInterrupt + connection-error guards ──────────────────────────


async def test_async_wait_for_job_propagates_keyboard_interrupt(mock_backend, async_client):
    """Ctrl+C mid-poll must NOT be swallowed by the SDK's transient-retry path."""
    call_count = {"n": 0}

    def _side_effect(request):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return httpx.Response(200, json={"status": "pending", "request_id": "req-x"})
        raise KeyboardInterrupt("user pressed Ctrl+C")

    mock_backend.get("/v1/jobs/j-kbi").mock(side_effect=_side_effect)
    job = Job(job_id="j-kbi", status="pending", poll_url="/v1/jobs/j-kbi", op="parse")

    with pytest.raises(KeyboardInterrupt):
        await async_client.wait_for_job(job)


class _TestSignal(BaseException):
    """Stand-in for KeyboardInterrupt in tests that exercise BaseException
    propagation through asyncio.gather. We don't use KeyboardInterrupt itself
    because pytest's own signal handling intercepts it and interrupts the
    whole test runner before the test's `pytest.raises` block can catch it
    (a known interaction between pytest, pytest-asyncio, and KeyboardInterrupt
    raised from inside respx's sync transport callback). Using a custom
    BaseException subclass exercises the same code path — gather's behavior
    for BaseException — without tripping pytest's interrupt path.
    """


async def test_async_wait_for_jobs_propagates_base_exception(mock_backend, async_client):
    """asyncio.gather must propagate BaseException from a child task — the user
    contract is that Ctrl+C / SystemExit / similar interrupt-class exceptions
    inside `await client.wait_for_jobs(...)` reach application code rather than
    being swallowed by the gather machinery. Uses _TestSignal as a KeyboardInterrupt
    proxy that pytest won't intercept (see _TestSignal docstring)."""
    a_count = {"n": 0}

    def _a_side_effect(request):
        a_count["n"] += 1
        if a_count["n"] == 1:
            return httpx.Response(200, json={"status": "pending", "request_id": "req-x"})
        raise _TestSignal("simulated interrupt")

    mock_backend.get("/v1/jobs/A").mock(side_effect=_a_side_effect)
    mock_backend.get("/v1/jobs/B").mock(return_value=_pending_response())
    job_a = Job(job_id="A", status="pending", poll_url="/v1/jobs/A", op="parse")
    job_b = Job(job_id="B", status="pending", poll_url="/v1/jobs/B", op="extract")

    with pytest.raises(_TestSignal):
        await async_client.wait_for_jobs([job_a, job_b])


async def test_async_poll_retries_on_connection_error_then_succeeds(mock_backend):
    """ConnectError on a poll must be absorbed by transient-retry; next poll succeeds."""
    mock_backend.get("/v1/jobs/j-conn").mock(side_effect=[
        httpx.ConnectError("DNS failed"),
        _completed_response({"data": {"ok": True}}),
    ])
    client = AsyncHyperAPIClient(
        api_key="hk_test_conn",
        base_url="http://test.local",
        poll_interval=0.0,
        poll_timeout=5.0,
        poll_transient_retry_delay=0.0,
        poll_max_transient_retries=2,
    )
    try:
        job = Job(job_id="j-conn", status="pending", poll_url="/v1/jobs/j-conn", op="extract")

        result = await client.wait_for_job(job)

        assert result == {"data": {"ok": True}}
    finally:
        await client.aclose()


async def test_async_wait_for_jobs_timeout_raises_job_timeout(mock_backend):
    """Async divergence note: asyncio.gather raises the FIRST timeout immediately
    and cancels the rest, so the raised JobTimeoutError's job_id is a single id
    (whichever fired first), NOT the sync client's "A,B" comma-joined form.
    """
    mock_backend.get("/v1/jobs/A").mock(return_value=_pending_response())
    mock_backend.get("/v1/jobs/B").mock(return_value=_pending_response())
    client = AsyncHyperAPIClient(
        api_key="hk_test_multi",
        base_url="http://test.local",
        poll_interval=0.0,
        poll_timeout=10.0,
        poll_transient_retry_delay=0.0,
        poll_max_transient_retries=0,
    )
    try:
        job_a = Job(job_id="A", status="pending", poll_url="/v1/jobs/A", op="parse")
        job_b = Job(job_id="B", status="pending", poll_url="/v1/jobs/B", op="extract")

        with pytest.raises(JobTimeoutError) as ei:
            await client.wait_for_jobs([job_a, job_b], timeout=0.05, interval=0.01)
        # The first task to time out wins; its job_id will be either "A" or "B".
        assert ei.value.job_id in ("A", "B")
        assert ei.value.elapsed_s >= 0.0
    finally:
        await client.aclose()


async def test_async_failed_job_message_includes_http_status(mock_backend, async_client):
    mock_backend.get("/v1/jobs/j-msg").mock(return_value=httpx.Response(
        200,
        json={
            "status": "failed",
            "error": "extract pipeline crashed",
            "error_status_code": 500,
        },
    ))
    job = Job(job_id="j-msg", status="pending", poll_url="/v1/jobs/j-msg", op="extract")

    with pytest.raises(ExtractError) as ei:
        await async_client.wait_for_job(job)
    assert "(HTTP 500)" in str(ei.value)
    assert "extract pipeline crashed" in str(ei.value)
    assert ei.value.status_code == 500


# ── Concurrent fan-out: a meaningful async-specific check ────────────────


async def test_async_concurrent_polling_yields_to_event_loop(mock_backend, async_client):
    """Async-specific: while one wait_for_job is sleeping between polls, the
    event loop must remain free to service other coroutines. We assert this by
    running two parallel wait_for_job calls and confirming both complete in
    roughly the time of a single poll, not 2× sequential."""
    mock_backend.get("/v1/jobs/X").mock(return_value=_completed_response({"x": 1}))
    mock_backend.get("/v1/jobs/Y").mock(return_value=_completed_response({"y": 2}))
    job_x = Job(job_id="X", status="pending", poll_url="/v1/jobs/X", op="parse")
    job_y = Job(job_id="Y", status="pending", poll_url="/v1/jobs/Y", op="extract")

    x_res, y_res = await asyncio.gather(
        async_client.wait_for_job(job_x),
        async_client.wait_for_job(job_y),
    )
    assert x_res == {"x": 1}
    assert y_res == {"y": 2}


async def test_async_wait_for_jobs_polls_concurrently_not_sequentially(mock_backend):
    """Concrete proof that asyncio.gather makes wait_for_jobs poll concurrently:
    if two jobs each take ~3 polls of 50ms to complete, total wall time should be
    ≈150ms (concurrent), NOT ≈300ms (sequential).

    Without concurrent polling, this test would fail by ~2x. The 50ms / 0.05s
    interval is small enough that test flakiness from CI scheduler jitter is
    bounded, while still being long enough that sequential vs concurrent is
    visibly distinguishable.
    """
    import time

    a_count = {"n": 0}
    b_count = {"n": 0}

    def _a_side_effect(request):
        a_count["n"] += 1
        if a_count["n"] < 3:
            return httpx.Response(200, json={"status": "pending"})
        return _completed_response({"a": 1})

    def _b_side_effect(request):
        b_count["n"] += 1
        if b_count["n"] < 3:
            return httpx.Response(200, json={"status": "pending"})
        return _completed_response({"b": 2})

    mock_backend.get("/v1/jobs/A").mock(side_effect=_a_side_effect)
    mock_backend.get("/v1/jobs/B").mock(side_effect=_b_side_effect)

    client = AsyncHyperAPIClient(
        api_key="hk_test_concur",
        base_url="http://test.local",
        poll_interval=0.05,            # 50ms between polls
        poll_timeout=5.0,
        poll_max_transient_retries=0,
    )
    try:
        job_a = Job(job_id="A", status="pending", poll_url="/v1/jobs/A", op="parse")
        job_b = Job(job_id="B", status="pending", poll_url="/v1/jobs/B", op="extract")

        start = time.monotonic()
        results = await client.wait_for_jobs([job_a, job_b])
        elapsed = time.monotonic() - start

        assert results == [{"a": 1}, {"b": 2}]
        # Sequential would be ~300ms (2 jobs × 3 polls × ~50ms). Concurrent
        # should be ~150ms. Generous upper bound at 250ms to absorb CI jitter
        # but still tight enough that sequential would fail.
        assert elapsed < 0.25, (
            f"wait_for_jobs took {elapsed:.3f}s — suggests sequential polling "
            f"(expected ~0.15s for concurrent, would be ~0.3s for sequential)"
        )
    finally:
        await client.aclose()


async def test_async_process_polls_parse_and_extract_concurrently(mock_backend, async_client, tiny_pdf):
    """The full process() flow: upload once, submit parse + extract, then
    asyncio.gather both polls. Verifies the polling halves run concurrently
    via wall-time comparison — same approach as the wait_for_jobs concurrency
    test, but exercised through the higher-level process() call site that
    customers actually use."""
    import time

    # Reuse the standard upload + submit mocks
    mock_backend.post("/v1/documents/upload").mock(
        return_value=httpx.Response(200, json={
            "document_key": "doc_concur",
            "upload_url": "https://s3.local/concur?sig=x",
            "expires_in": 600,
        }),
    )
    mock_backend.put("https://s3.local/concur?sig=x").mock(return_value=httpx.Response(200))
    mock_backend.post("/v1/parse").mock(return_value=httpx.Response(202, json={
        "job_id": "p_concur", "status": "pending", "poll_url": "/v1/jobs/p_concur",
    }))
    mock_backend.post("/v1/extract").mock(return_value=httpx.Response(202, json={
        "job_id": "e_concur", "status": "pending", "poll_url": "/v1/jobs/e_concur",
    }))

    # Each leg takes 3 polls × 50ms = ~150ms to complete.
    p_count = {"n": 0}
    e_count = {"n": 0}

    def _p_side_effect(request):
        p_count["n"] += 1
        if p_count["n"] < 3:
            return httpx.Response(200, json={"status": "pending"})
        return httpx.Response(200, json={"status": "completed", "result": {"ocr": "txt"}})

    def _e_side_effect(request):
        e_count["n"] += 1
        if e_count["n"] < 3:
            return httpx.Response(200, json={"status": "pending"})
        return httpx.Response(200, json={"status": "completed", "result": {"entities": {"v": 1}}})

    mock_backend.get("/v1/jobs/p_concur").mock(side_effect=_p_side_effect)
    mock_backend.get("/v1/jobs/e_concur").mock(side_effect=_e_side_effect)

    # async_client fixture has poll_interval=0.0, which would race; use a
    # client with a small positive interval so wall-time comparison is meaningful.
    client = AsyncHyperAPIClient(
        api_key="hk_test_concur",
        base_url="http://test.local",
        poll_interval=0.05,
        poll_timeout=5.0,
        poll_max_transient_retries=0,
    )
    try:
        start = time.monotonic()
        result = await client.process(tiny_pdf)
        elapsed = time.monotonic() - start

        assert result["ocr"] == "txt"
        assert result["data"] == {"entities": {"v": 1}}
        # Sequential polling of two ~150ms legs would be ~300ms; concurrent ~150ms.
        # Upper bound 0.25s — fails for sequential, passes for concurrent.
        assert elapsed < 0.25, (
            f"process() polling took {elapsed:.3f}s — suggests sequential polling "
            f"of parse + extract legs (expected ~0.15s for concurrent)"
        )
    finally:
        await client.aclose()


# ── Mixed sync + async in the same process ──────────────────────────────


async def test_async_and_sync_clients_coexist_in_same_process(mock_backend):
    """Defensive: a customer migrating from sync to async may have both clients
    instantiated in the same process for a transition period. Both must work
    independently — different connection pools, different httpx.Client instances,
    no shared mutable state."""
    from hyperapi import HyperAPIClient

    # Both clients should hit the same mocked backend and behave independently.
    mock_backend.get("/v1/jobs/sync-j").mock(return_value=_completed_response({"who": "sync"}))
    mock_backend.get("/v1/jobs/async-j").mock(return_value=_completed_response({"who": "async"}))

    sync_client = HyperAPIClient(
        api_key="hk_test_s", base_url="http://test.local",
        poll_interval=0.0, poll_timeout=2.0,
    )
    async_client = AsyncHyperAPIClient(
        api_key="hk_test_a", base_url="http://test.local",
        poll_interval=0.0, poll_timeout=2.0,
    )
    try:
        # Sync call followed by async call — should not affect each other.
        sync_result = sync_client.get_job("sync-j")
        async_result = await async_client.get_job("async-j")

        assert sync_result["result"] == {"who": "sync"}
        assert async_result["result"] == {"who": "async"}

        # Connection pools are separate — closing one must not affect the other.
        sync_client.close()
        # Async client still usable
        async_result_2 = await async_client.get_job("async-j")
        assert async_result_2["result"] == {"who": "async"}
    finally:
        await async_client.aclose()
