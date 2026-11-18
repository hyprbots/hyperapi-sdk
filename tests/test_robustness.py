"""Robustness contract for 0.7.1 — both clients, one behaviour.

Each section pins one hardening item: ids escaped in URL paths, picklable
exceptions, stricter base_url, typed errors for malformed replies, transient
retry for batch polls, the poll retry policy, and safe page downloads/uploads.
"""

from __future__ import annotations

import inspect
import pickle

import httpx
import pytest

import hyperapi.exceptions as exc_mod


TEST_BASE_URL = "http://test.local"
KEY = "hk_test_unit"
EVIL_ID = "../limits?x="
EVIL_RAW_PATH_JOB = b"/v1/jobs/..%2Flimits%3Fx%3D"
EVIL_RAW_PATH_BATCH = b"/v1/batch/..%2Flimits%3Fx%3D"


def _pending():
    return httpx.Response(200, json={"status": "pending"})


# ── B8: ids are one escaped path segment ────────────────────────────────────


def test_get_job_escapes_id(mock_api, client):
    route = mock_api.route(method="GET").mock(return_value=_pending())
    client.get_job(EVIL_ID)
    assert route.calls.last.request.url.raw_path == EVIL_RAW_PATH_JOB


async def test_async_get_job_escapes_id(mock_api, async_client):
    route = mock_api.route(method="GET").mock(return_value=_pending())
    await async_client.get_job(EVIL_ID)
    assert route.calls.last.request.url.raw_path == EVIL_RAW_PATH_JOB


def test_delete_job_and_batch_paths_escape_id(mock_api, client):
    route = mock_api.route().mock(return_value=httpx.Response(200, json={"status": "completed"}))
    client.delete_job(EVIL_ID)
    assert route.calls.last.request.url.raw_path == EVIL_RAW_PATH_JOB
    client.get_batch(EVIL_ID)
    assert route.calls.last.request.url.raw_path.split(b"?")[0] == EVIL_RAW_PATH_BATCH
    client.cancel_batch(EVIL_ID)
    assert route.calls.last.request.url.raw_path == EVIL_RAW_PATH_BATCH
    client.wait_for_batch(EVIL_ID)
    assert route.calls.last.request.url.raw_path.split(b"?")[0] == EVIL_RAW_PATH_BATCH
    client.wait_for_job(EVIL_ID)
    assert route.calls.last.request.url.raw_path == EVIL_RAW_PATH_JOB


async def test_async_delete_job_and_batch_paths_escape_id(mock_api, async_client):
    route = mock_api.route().mock(return_value=httpx.Response(200, json={"status": "completed"}))
    await async_client.delete_job(EVIL_ID)
    assert route.calls.last.request.url.raw_path == EVIL_RAW_PATH_JOB
    await async_client.get_batch(EVIL_ID)
    assert route.calls.last.request.url.raw_path.split(b"?")[0] == EVIL_RAW_PATH_BATCH
    await async_client.cancel_batch(EVIL_ID)
    assert route.calls.last.request.url.raw_path == EVIL_RAW_PATH_BATCH
    await async_client.wait_for_batch(EVIL_ID)
    assert route.calls.last.request.url.raw_path.split(b"?")[0] == EVIL_RAW_PATH_BATCH
    await async_client.wait_for_job(EVIL_ID)
    assert route.calls.last.request.url.raw_path == EVIL_RAW_PATH_JOB


@pytest.mark.parametrize("bad", ["", "   ", None])
def test_empty_ids_are_refused_before_any_request(mock_api, client, bad):
    route = mock_api.route().mock(return_value=_pending())
    with pytest.raises(ValueError, match="job_id"):
        client.get_job(bad)
    with pytest.raises(ValueError, match="batch_id"):
        client.get_batch(bad)
    assert route.call_count == 0


@pytest.mark.parametrize("bad", ["", None])
async def test_async_empty_ids_are_refused_before_any_request(mock_api, async_client, bad):
    route = mock_api.route().mock(return_value=_pending())
    with pytest.raises(ValueError, match="job_id"):
        await async_client.delete_job(bad)
    with pytest.raises(ValueError, match="batch_id"):
        await async_client.cancel_batch(bad)
    assert route.call_count == 0


def test_submit_default_poll_url_is_escaped(mock_api, client, tiny_pdf):
    mock_api.post("/v1/documents/upload").mock(return_value=httpx.Response(
        200, json={"document_key": "k", "upload_url": "https://s3.local/k?sig=x"}))
    mock_api.put("https://s3.local/k?sig=x").mock(return_value=httpx.Response(200))
    mock_api.post("/v1/parse").mock(return_value=httpx.Response(202, json={"job_id": "a/b"}))
    job = client.submit_parse(tiny_pdf)
    assert job.poll_url == "/v1/jobs/a%2Fb"


# ── B9: every exception survives pickle ─────────────────────────────────────

def _exception_samples():
    common = {"request_id": "req-1"}
    yield exc_mod.HyperAPIError("boom", 500, "req-1", code="X", details={"a": 1})
    yield exc_mod.RateLimitError(
        "slow down", retry_after=7, tier="free", limit=1, window_seconds=60,
        degraded=True, code="page_quota_exceeded", details={"pages_used": 5}, **common,
    )
    yield exc_mod.JobTimeoutError("late", job_id="j1", elapsed_s=1.5, **common)
    yield exc_mod.JobCancelledError("gone", job_id="j2", **common)
    for name in ("AuthenticationError", "ParseError", "ExtractError", "ClassifyError",
                 "SplitError", "RedactError", "EditError", "DocumentUploadError"):
        yield getattr(exc_mod, name)("msg", 418, "req-1", code="c", details={"d": 2})


def test_every_exception_class_is_covered():
    classes = {
        obj for _, obj in inspect.getmembers(exc_mod, inspect.isclass)
        if issubclass(obj, exc_mod.HyperAPIError)
    }
    assert classes == {type(e) for e in _exception_samples()}


@pytest.mark.parametrize("err", list(_exception_samples()), ids=lambda e: type(e).__name__)
def test_exception_pickle_round_trip(err):
    clone = pickle.loads(pickle.dumps(err))
    assert type(clone) is type(err)
    assert clone.__dict__ == err.__dict__
    assert str(clone) == str(err)
    assert clone.args == err.args
