"""Contract: the API's error envelope — ``{"error": {"code", "message", "details"?}}``.

Every error the API returns carries a stable machine-readable ``code`` next to
the human ``message``, and optionally a ``details`` object with the numbers
behind a refusal. The SDK must (a) surface ``message`` as the exception text,
(b) attach ``code`` and ``details`` to the exception, and (c) stay tolerant of
other shapes (bare ``{"message": ...}`` / ``{"detail": ...}`` or non-JSON)
from intermediaries.

The 429 case matters most: a rate-window 429 is transient and the poll loops
back off on it, but a 429 with ``code == "page_quota_exceeded"`` is a monthly
quota refusal that no amount of waiting clears — the loops must raise it
immediately instead of retrying.
"""

import httpx
import pytest

from hyperapi import HyperAPIError, Job, RateLimitError
from hyperapi.client import _server_message


def _resp(status, body):
    return httpx.Response(status, json=body)


def _completed_response(result):
    return httpx.Response(
        200,
        json={"status": "completed", "result": result, "request_id": "req-x"},
    )


QUOTA_BODY = {
    "error": {
        "code": "page_quota_exceeded",
        "message": "Monthly page quota exceeded. Upgrade your plan or wait until next billing period.",
        "details": {"pages_used": 98, "pages_limit": 100, "pages_pending": 4},
    }
}

RATE_BODY = {
    "error": {"code": "RATE_LIMITED", "message": "Rate limit exceeded"},
    "tier": "free",
    "limit": 1,
    "window_seconds": 60,
}


# ── envelope parsing ─────────────────────────────────────────────────────────


def test_server_message_prefers_envelope_message():
    assert _server_message(_resp(400, {"error": {"code": "BAD_REQUEST", "message": "bad"}}), "fb") == "bad"


@pytest.mark.parametrize(
    "body,expected",
    [
        ({"message": "flat"}, "flat"),
        ({"detail": "fastapi-style"}, "fastapi-style"),
        ({"detail": {"code": "x", "message": "nested"}}, "nested"),
    ],
)
def test_server_message_tolerates_other_shapes(body, expected):
    assert _server_message(_resp(400, body), "fb") == expected


def test_server_message_falls_back_to_text_then_fallback():
    assert _server_message(httpx.Response(502, text="<html>gateway</html>"), "fb") == "<html>gateway</html>"
    assert _server_message(httpx.Response(502, text=""), "fb") == "fb"


def test_server_message_scrubs_api_keys():
    msg = _server_message(_resp(400, {"error": {"code": "X", "message": "key hk_live_abc123 rejected"}}), "fb")
    assert "hk_live_abc123" not in msg
    assert "hk_***redacted" in msg


# ── code / details on raised exceptions ──────────────────────────────────────


def test_429_quota_exposes_code_and_details(mock_api, client):
    mock_api.get("/v1/jobs/j-q").mock(return_value=_resp(429, QUOTA_BODY))

    with pytest.raises(RateLimitError) as ei:
        client.get_job("j-q")

    err = ei.value
    assert err.code == "page_quota_exceeded"
    assert err.is_quota_exhausted is True
    assert err.details == {"pages_used": 98, "pages_limit": 100, "pages_pending": 4}
    assert "Monthly page quota exceeded" in str(err)


def test_429_rate_window_is_not_quota(mock_api, client):
    mock_api.get("/v1/jobs/j-r").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "3"}, json=RATE_BODY)
    )

    with pytest.raises(RateLimitError) as ei:
        client.get_job("j-r")

    err = ei.value
    assert err.code == "RATE_LIMITED"
    assert err.is_quota_exhausted is False
    assert err.details is None
    assert err.retry_after == 3
    assert err.tier == "free" and err.limit == 1 and err.window_seconds == 60


def test_429_without_envelope_has_no_code(mock_api, client):
    """Older / intermediary 429 bodies carry no envelope; the SDK must not guess."""
    mock_api.get("/v1/jobs/j-n").mock(
        return_value=httpx.Response(429, json={"message": "Rate limit exceeded"})
    )
    with pytest.raises(RateLimitError) as ei:
        client.get_job("j-n")
    assert ei.value.code is None
    assert ei.value.is_quota_exhausted is False


def test_submit_402_carries_code(mock_api, client, tiny_pdf):
    mock_api.post("/v1/documents/upload").mock(
        return_value=_resp(200, {"document_key": "k", "upload_url": "https://s3.local/k", "expires_in": 60})
    )
    mock_api.put("https://s3.local/k").mock(return_value=httpx.Response(200))
    mock_api.post("/v1/parse").mock(
        return_value=_resp(402, {"error": {"code": "insufficient_credits", "message": "Insufficient credits."}})
    )

    with pytest.raises(HyperAPIError) as ei:
        client.submit_parse(tiny_pdf)

    assert ei.value.status_code == 402
    assert ei.value.code == "insufficient_credits"


def test_generic_4xx_carries_code_and_details(mock_api, client):
    mock_api.get("/v1/jobs/j-g").mock(
        return_value=_resp(409, {"error": {"code": "duplicate_request", "message": "already processed", "details": {"x": 1}}})
    )
    with pytest.raises(HyperAPIError) as ei:
        client.get_job("j-g")
    assert ei.value.code == "duplicate_request"
    assert ei.value.details == {"x": 1}


# ── poll loops: quota is not retried, rate window still is ──────────────────


def test_wait_for_job_does_not_retry_quota_429(mock_api, client, monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr("hyperapi.client.time.sleep", lambda s: slept.append(s))
    route = mock_api.get("/v1/jobs/j-q2").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "1"}, json=QUOTA_BODY),
            _completed_response({"never": "reached"}),
        ]
    )
    job = Job(job_id="j-q2", status="pending", poll_url="/v1/jobs/j-q2", op="parse")

    with pytest.raises(RateLimitError) as ei:
        client.wait_for_job(job)

    assert ei.value.is_quota_exhausted
    assert route.call_count == 1          # raised on the first poll, no retry
    assert not any(s >= 1.0 for s in slept)


def test_wait_for_job_still_backs_off_on_rate_window_429(mock_api, client, monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr("hyperapi.client.time.sleep", lambda s: slept.append(s))
    mock_api.get("/v1/jobs/j-r2").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "2"}, json=RATE_BODY),
            _completed_response({"ok": 1}),
        ]
    )
    job = Job(job_id="j-r2", status="pending", poll_url="/v1/jobs/j-r2", op="parse")

    assert client.wait_for_job(job) == {"ok": 1}
    assert any(2.0 <= s < 3.0 for s in slept), slept


def test_wait_for_jobs_does_not_retry_quota_429(mock_api, client, monkeypatch):
    monkeypatch.setattr("hyperapi.client.time.sleep", lambda s: None)
    mock_api.get("/v1/jobs/j-a").mock(return_value=_completed_response({"a": 1}))
    route_b = mock_api.get("/v1/jobs/j-b").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "1"}, json=QUOTA_BODY)
    )
    jobs = [
        Job(job_id="j-a", status="pending", poll_url="/v1/jobs/j-a", op="parse"),
        Job(job_id="j-b", status="pending", poll_url="/v1/jobs/j-b", op="parse"),
    ]

    with pytest.raises(RateLimitError) as ei:
        client.wait_for_jobs(jobs)

    assert ei.value.is_quota_exhausted
    assert route_b.call_count == 1


# ── async twins ──────────────────────────────────────────────────────────────


async def test_async_429_quota_exposes_code_and_details(mock_api, async_client):
    mock_api.get("/v1/jobs/j-q").mock(return_value=_resp(429, QUOTA_BODY))

    with pytest.raises(RateLimitError) as ei:
        await async_client.get_job("j-q")

    assert ei.value.code == "page_quota_exceeded"
    assert ei.value.is_quota_exhausted
    assert ei.value.details["pages_pending"] == 4


async def test_async_wait_for_job_does_not_retry_quota_429(mock_api, async_client, monkeypatch):
    slept: list[float] = []

    async def fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr("hyperapi.async_client.asyncio.sleep", fake_sleep)
    route = mock_api.get("/v1/jobs/j-q3").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "1"}, json=QUOTA_BODY),
            _completed_response({"never": "reached"}),
        ]
    )
    job = Job(job_id="j-q3", status="pending", poll_url="/v1/jobs/j-q3", op="parse")

    with pytest.raises(RateLimitError) as ei:
        await async_client.wait_for_job(job)

    assert ei.value.is_quota_exhausted
    assert route.call_count == 1
    assert not any(s >= 1.0 for s in slept)


async def test_async_wait_for_job_still_backs_off_on_rate_window_429(mock_api, async_client, monkeypatch):
    slept: list[float] = []

    async def fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr("hyperapi.async_client.asyncio.sleep", fake_sleep)
    mock_api.get("/v1/jobs/j-r3").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "2"}, json=RATE_BODY),
            _completed_response({"ok": 1}),
        ]
    )
    job = Job(job_id="j-r3", status="pending", poll_url="/v1/jobs/j-r3", op="parse")

    assert await async_client.wait_for_job(job) == {"ok": 1}
    assert any(2.0 <= s < 3.0 for s in slept), slept
