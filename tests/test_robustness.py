"""Robustness contract for 0.7.1 — both clients, one behaviour.

Each section pins one hardening item: ids escaped in URL paths, picklable
exceptions, stricter base_url, typed errors for malformed replies, transient
retry for batch polls, the poll retry policy, and safe page downloads/uploads.
"""

from __future__ import annotations

import inspect
import pickle
import time

import httpx
import pytest

import hyperapi.exceptions as exc_mod
from hyperapi import (
    AsyncHyperAPIClient,
    DocumentUploadError,
    ExtractError,
    HyperAPIClient,
    HyperAPIError,
    JobTimeoutError,
    ParseError,
)
from hyperapi.client import Job


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


# ── B10: base_url edge cases ────────────────────────────────────────────────


@pytest.mark.parametrize("cls", [HyperAPIClient, AsyncHyperAPIClient])
@pytest.mark.parametrize("bad", ["https://example.com?", "https://example.com#",
                                 "https://example.com/?", "https://example.com/api#"])
def test_base_url_with_empty_query_or_fragment_is_refused(cls, bad):
    with pytest.raises(ValueError, match="query string"):
        cls(api_key=KEY, base_url=bad)


@pytest.mark.parametrize("cls", [HyperAPIClient, AsyncHyperAPIClient])
def test_base_url_accepts_httpx_url(cls):
    c = cls(api_key=KEY, base_url=httpx.URL("https://example.com/"))
    assert c.base_url == "https://example.com"


@pytest.mark.parametrize("cls", [HyperAPIClient, AsyncHyperAPIClient])
def test_base_url_httpx_url_is_still_validated(cls):
    with pytest.raises(ValueError, match="Invalid base_url"):
        cls(api_key=KEY, base_url=httpx.URL("ftp://example.com"))


# ── B11: malformed server replies become SDK errors ─────────────────────────

UPLOAD_OK = {"document_key": "k", "upload_url": "https://s3.local/k?sig=x"}


def _seed_upload(mock_api, body=None, status=200):
    mock_api.post("/v1/documents/upload").mock(
        return_value=httpx.Response(status, json=UPLOAD_OK if body is None else body))
    mock_api.put("https://s3.local/k?sig=x").mock(return_value=httpx.Response(200))


HTML_200 = httpx.Response(200, text="<html>proxy</html>", headers={"Content-Type": "text/html"})

SUBMIT_BAD_REPLIES = [
    pytest.param(HTML_200, "not JSON", id="html-200"),
    pytest.param(httpx.Response(202, json={"status": "pending"}), "'job_id'", id="202-no-job-id"),
    pytest.param(httpx.Response(202, json=["job"]), "JSON object", id="202-list"),
    pytest.param(httpx.Response(202, json={"job_id": None}), "'job_id'", id="202-null-job-id"),
]


@pytest.mark.parametrize(("reply", "match"), SUBMIT_BAD_REPLIES)
def test_submit_malformed_reply_raises_op_error(mock_api, client, tiny_pdf, reply, match):
    _seed_upload(mock_api)
    mock_api.post("/v1/parse").mock(return_value=reply)
    with pytest.raises(ParseError, match=match) as ei:
        client.submit_parse(tiny_pdf)
    assert ei.value.status_code == reply.status_code
    assert ei.value.request_id


@pytest.mark.parametrize(("reply", "match"), SUBMIT_BAD_REPLIES)
async def test_async_submit_malformed_reply_raises_op_error(
    mock_api, async_client, tiny_pdf, reply, match
):
    _seed_upload(mock_api)
    mock_api.post("/v1/extract").mock(return_value=reply)
    with pytest.raises(ExtractError, match=match) as ei:
        await async_client.submit_extract(tiny_pdf)
    assert ei.value.status_code == reply.status_code


def test_process_submit_malformed_reply_raises(mock_api, client, tiny_pdf):
    """process() submits with an existing document_key (_submit_via_doc_key)."""
    _seed_upload(mock_api)
    mock_api.post("/v1/parse").mock(return_value=httpx.Response(202, json={}))
    with pytest.raises(ParseError, match="'job_id'"):
        client.process(tiny_pdf)


async def test_async_edit_fill_malformed_reply_raises(mock_api, async_client):
    """edit_fill submits a JSON body (_submit_json)."""
    from hyperapi import EditError

    mock_api.post("/v1/edit/fill").mock(return_value=HTML_200)
    with pytest.raises(EditError, match="not JSON"):
        await async_client.submit_edit_fill("d1", values={"0": "x"})


@pytest.mark.parametrize("body", [{"document_key": "k"}, {"upload_url": "https://s3.local/k"}, ["k"]])
def test_upload_url_reply_missing_keys(mock_api, client, tiny_pdf, body):
    _seed_upload(mock_api, body=body)
    with pytest.raises(DocumentUploadError, match="Malformed upload-URL response") as ei:
        client.upload_document(tiny_pdf)
    assert ei.value.status_code == 200


async def test_async_upload_url_reply_not_json(mock_api, async_client, tiny_pdf):
    mock_api.post("/v1/documents/upload").mock(return_value=HTML_200)
    with pytest.raises(DocumentUploadError, match="not JSON"):
        await async_client.upload_document(tiny_pdf)


def test_poll_list_body_raises_typed(mock_api, client):
    mock_api.get("/v1/jobs/j1").mock(return_value=httpx.Response(200, json=["completed"]))
    with pytest.raises(HyperAPIError, match="JSON object"):
        client.get_job("j1")
    with pytest.raises(HyperAPIError, match="JSON object"):
        client.wait_for_job("j1")


async def test_async_poll_list_body_raises_typed(mock_api, async_client):
    mock_api.get("/v1/jobs/j1").mock(return_value=httpx.Response(200, json="completed"))
    with pytest.raises(HyperAPIError, match="JSON object"):
        await async_client.wait_for_job("j1")


def test_failed_job_with_dict_error_raises_op_error(mock_api, client):
    mock_api.get("/v1/jobs/j1").mock(return_value=httpx.Response(200, json={
        "status": "failed", "error": {"code": "ocr_failed", "message": "bad page hk_live_SECRET"}}))
    job = Job(job_id="j1", status="pending", poll_url="/v1/jobs/j1", op="parse")
    with pytest.raises(ParseError) as ei:
        client.wait_for_job(job)
    assert "ocr_failed" in str(ei.value)
    assert "SECRET" not in str(ei.value)


async def test_async_failed_job_with_dict_error_raises_op_error(mock_api, async_client):
    mock_api.get("/v1/jobs/j1").mock(return_value=httpx.Response(200, json={
        "status": "failed", "error": {"code": "ocr_failed"}}))
    job = Job(job_id="j1", status="pending", poll_url="/v1/jobs/j1", op="extract")
    with pytest.raises(ExtractError, match="ocr_failed"):
        await async_client.wait_for_job(job)


def test_strip_api_key_accepts_non_strings():
    assert exc_mod._strip_api_key(None) is None
    assert exc_mod._strip_api_key({"k": "hk_test_abc"}) == "{'k': 'hk_***redacted'}"
    assert exc_mod._strip_api_key(42) == "42"


def test_batch_create_without_batch_id_raises(mock_api, client):
    mock_api.post("/v1/batch").mock(return_value=httpx.Response(202, json={"status": "queued"}))
    with pytest.raises(HyperAPIError, match="'batch_id'"):
        client.create_batch(endpoint="/v1/parse", document_keys=["k"])


async def test_async_batch_poll_list_body_raises(mock_api, async_client):
    mock_api.get("/v1/batch/b1").mock(return_value=httpx.Response(200, json=[]))
    with pytest.raises(HyperAPIError, match="JSON object"):
        await async_client.get_batch("b1")
    mock_api.post("/v1/batch").mock(return_value=HTML_200)
    with pytest.raises(HyperAPIError, match="not JSON"):
        await async_client.create_batch(endpoint="/v1/parse", document_keys=["k"])


# ── B12: wait_for_batch rides out a transient failure ───────────────────────

BATCH_DONE = {"batch_id": "b1", "status": "completed", "counts": {"total": 1}, "items": []}


@pytest.mark.parametrize("first", [
    pytest.param(httpx.Response(502), id="502"),
    pytest.param(httpx.ConnectError("reset"), id="network"),
])
def test_wait_for_batch_retries_transient_failure(mock_api, client, first):
    route = mock_api.get("/v1/batch/b1").mock(
        side_effect=[first, httpx.Response(200, json=BATCH_DONE)])
    client._poll_transient_retry_delay = 0.0
    assert client.wait_for_batch("b1", poll_interval=0)["status"] == "completed"
    assert route.call_count == 2


@pytest.mark.parametrize("first", [
    pytest.param(httpx.Response(502), id="502"),
    pytest.param(httpx.ConnectError("reset"), id="network"),
])
async def test_async_wait_for_batch_retries_transient_failure(mock_api, async_client, first):
    route = mock_api.get("/v1/batch/b1").mock(
        side_effect=[first, httpx.Response(200, json=BATCH_DONE)])
    async_client._poll_transient_retry_delay = 0.0
    result = await async_client.wait_for_batch("b1", poll_interval=0)
    assert result["status"] == "completed"
    assert route.call_count == 2


def test_wait_for_batch_does_not_retry_4xx(mock_api, client):
    route = mock_api.get("/v1/batch/b1").mock(return_value=httpx.Response(400))
    with pytest.raises(HyperAPIError) as ei:
        client.wait_for_batch("b1", poll_interval=0)
    assert ei.value.status_code == 400
    assert route.call_count == 1


# ── B13: poll retry policy — 4xx fail fast, deadline respected ──────────────

JOB_DONE = {"status": "completed", "result": {"ok": True}}


@pytest.mark.parametrize("status", [400, 403, 409, 422])
def test_poll_4xx_is_not_retried(mock_api, client, status):
    route = mock_api.get("/v1/jobs/j1").mock(return_value=httpx.Response(status))
    with pytest.raises(HyperAPIError) as ei:
        client.wait_for_job("j1")
    assert ei.value.status_code == status
    assert route.call_count == 1


@pytest.mark.parametrize("status", [400, 403, 422])
async def test_async_poll_4xx_is_not_retried(mock_api, async_client, status):
    route = mock_api.get("/v1/jobs/j1").mock(return_value=httpx.Response(status))
    with pytest.raises(HyperAPIError) as ei:
        await async_client.wait_for_job("j1")
    assert ei.value.status_code == status
    assert route.call_count == 1


def test_poll_malformed_200_is_not_retried(mock_api, client):
    route = mock_api.get("/v1/jobs/j1").mock(return_value=HTML_200)
    with pytest.raises(HyperAPIError, match="Malformed"):
        client.wait_for_job("j1")
    assert route.call_count == 1


def _slow_retry_client(cls):
    return cls(api_key=KEY, base_url=TEST_BASE_URL, poll_interval=0.0,
               poll_max_transient_retries=3, poll_transient_retry_delay=0.5)


def test_poll_503_past_deadline_raises_job_timeout_quickly(mock_api):
    mock_api.get("/v1/jobs/j1").mock(return_value=httpx.Response(503))
    c = _slow_retry_client(HyperAPIClient)
    t0 = time.monotonic()
    with pytest.raises(JobTimeoutError) as ei:
        c.wait_for_job("j1", timeout=0.2)
    elapsed = time.monotonic() - t0
    c.close()
    assert elapsed < 0.6  # was 1.5 s (3 retries x 0.5 s) and a plain HyperAPIError
    assert ei.value.job_id == "j1"
    assert "HTTP 503" in str(ei.value)


async def test_async_poll_503_past_deadline_raises_job_timeout_quickly(mock_api):
    mock_api.get("/v1/jobs/j1").mock(return_value=httpx.Response(503))
    c = _slow_retry_client(AsyncHyperAPIClient)
    t0 = time.monotonic()
    with pytest.raises(JobTimeoutError):
        await c.wait_for_job("j1", timeout=0.2)
    elapsed = time.monotonic() - t0
    await c.aclose()
    assert elapsed < 0.6


def test_wait_for_batch_503_past_deadline_raises_job_timeout(mock_api):
    mock_api.get("/v1/batch/b1").mock(return_value=httpx.Response(503))
    c = _slow_retry_client(HyperAPIClient)
    t0 = time.monotonic()
    with pytest.raises(JobTimeoutError) as ei:
        c.wait_for_batch("b1", timeout=0.2, poll_interval=0)
    c.close()
    assert time.monotonic() - t0 < 0.6
    assert ei.value.job_id == "b1"


def test_wait_for_jobs_503_past_deadline_raises_job_timeout(mock_api):
    mock_api.get("/v1/jobs/A").mock(return_value=httpx.Response(503))
    c = _slow_retry_client(HyperAPIClient)
    job = Job(job_id="A", status="pending", poll_url="/v1/jobs/A", op="parse")
    t0 = time.monotonic()
    with pytest.raises(JobTimeoutError):
        c.wait_for_jobs([job], timeout=0.2)
    c.close()
    assert time.monotonic() - t0 < 0.6


def test_poll_503_then_200_succeeds(mock_api, client):
    route = mock_api.get("/v1/jobs/j1").mock(
        side_effect=[httpx.Response(503), httpx.Response(200, json=JOB_DONE)])
    client._poll_transient_retry_delay = 0.0
    assert client.wait_for_job("j1") == {"ok": True}
    assert route.call_count == 2


async def test_async_poll_503_then_200_succeeds(mock_api, async_client):
    route = mock_api.get("/v1/jobs/j1").mock(
        side_effect=[httpx.Response(503), httpx.Response(200, json=JOB_DONE)])
    async_client._poll_transient_retry_delay = 0.0
    assert await async_client.wait_for_job("j1") == {"ok": True}
    assert route.call_count == 2
