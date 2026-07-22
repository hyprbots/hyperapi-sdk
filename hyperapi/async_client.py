"""HyperAPI Async Client.

Async-native twin of :class:`hyperapi.client.HyperAPIClient`. The HTTP contract
is identical (submit + poll, same envelopes, same error mapping); the only
difference is that every public method is a coroutine and the polling loop
yields to the event loop via ``asyncio.sleep`` instead of blocking the thread
with ``time.sleep``.

Use this client when your code already runs inside an event loop:

* FastAPI / Starlette / aiohttp request handlers
* LLM agent loops that interleave HyperAPI with other ``await`` calls
* Discord / Slack / Telegram bots
* High-concurrency processors that fan out with ``asyncio.gather``

For batch scripts, Airflow tasks, Celery workers, and Jupyter notebooks the
sync :class:`hyperapi.client.HyperAPIClient` is the right choice — async adds
boilerplate without benefit in those contexts.

Quickstart
==========

::

    import asyncio
    from hyperapi import AsyncHyperAPIClient

    async def main():
        async with AsyncHyperAPIClient(api_key="hk_live_…") as client:
            result = await client.extract("invoice.pdf")
            print(result["data"])

    asyncio.run(main())

Inside FastAPI::

    from fastapi import FastAPI, UploadFile
    from hyperapi import AsyncHyperAPIClient

    app = FastAPI()
    client = AsyncHyperAPIClient()  # reads HYPERAPI_KEY from env

    @app.post("/extract")
    async def extract(file: UploadFile):
        # Save UploadFile to a tmp path, then:
        return await client.extract(tmp_path)

    @app.on_event("shutdown")
    async def shutdown():
        await client.aclose()
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
import uuid
import warnings
from pathlib import Path
from typing import Any
from collections.abc import Iterable

import httpx

# Reuse the sync client's pure helpers + constants + Job + op→error registry.
# Importing rather than duplicating keeps the two clients behaviorally aligned
# and means bug fixes in error parsing / scrubbing land in both.
from .client import (
    CONTENT_TYPES,
    ExtractCategory,
    Job,
    ParseMode,
    _DEFAULT_POLL_INTERVAL_S,
    _DEFAULT_POLL_MAX_TRANSIENT_RETRIES,
    _DEFAULT_POLL_TIMEOUT_S,
    _DEFAULT_POLL_TRANSIENT_RETRY_DELAY_S,
    _DEFAULT_TIMEOUT,
    _OP_TO_ERROR,
    _parse_ocr_text,
    _rate_limit_error_from,
    _request_id_of,
    _server_message,
)
from .exceptions import (
    AuthenticationError,
    DocumentUploadError,
    HyperAPIError,
    JobTimeoutError,
    RateLimitError,
    _strip_api_key,
)


logger = logging.getLogger("hyperapi")


class AsyncHyperAPIClient:
    """Async client for the HyperAPI document intelligence platform.

    Mirrors :class:`hyperapi.client.HyperAPIClient` one-to-one — same
    constructor signature, same method names, same return shapes, same typed
    exceptions. Pick this client when your runtime is async (FastAPI, agent
    loops, asyncio-based services); otherwise use the sync client.

    Lifecycle
    ---------
    The underlying :class:`httpx.AsyncClient` holds a connection pool that
    must be released. Use as an async context manager::

        async with AsyncHyperAPIClient() as client:
            ...

    Or close manually::

        client = AsyncHyperAPIClient()
        try:
            ...
        finally:
            await client.aclose()

    Concurrency
    -----------
    A single ``AsyncHyperAPIClient`` instance can be shared across many
    concurrent ``asyncio.gather`` operations — ``httpx.AsyncClient`` is
    coroutine-safe. Construct one client per application, not per request.
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = _DEFAULT_TIMEOUT,
        *,
        poll_interval: float = _DEFAULT_POLL_INTERVAL_S,
        poll_timeout: float = _DEFAULT_POLL_TIMEOUT_S,
        poll_max_transient_retries: int = _DEFAULT_POLL_MAX_TRANSIENT_RETRIES,
        poll_transient_retry_delay: float = _DEFAULT_POLL_TRANSIENT_RETRY_DELAY_S,
    ):
        """Initialize an async HyperAPI client.

        Args mirror :class:`hyperapi.client.HyperAPIClient` exactly — see
        that class for full descriptions.
        """
        self.api_key = api_key or os.environ.get("HYPERAPI_KEY")
        if not self.api_key:
            raise AuthenticationError(
                "API key required. Pass api_key or set HYPERAPI_KEY environment variable."
            )

        self.base_url = (
            base_url
            or os.environ.get("HYPERAPI_URL")
            or "https://apis.hyperbots.com"
        ).rstrip("/")
        self.timeout = timeout
        self._poll_interval = poll_interval
        self._poll_timeout = poll_timeout
        self._poll_max_transient_retries = poll_max_transient_retries
        self._poll_transient_retry_delay = poll_transient_retry_delay

        from . import __version__  # local import to avoid circular at module load
        py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        # Note the "-async" suffix in the User-Agent so backend analytics can
        # distinguish sync vs async client usage without sniffing other signals.
        self._user_agent = (
            f"hyperapi-sdk-python/{__version__}-async "
            f"(httpx/{httpx.__version__}; Python/{py_ver})"
        )
        self._client = httpx.AsyncClient(
            timeout=timeout,
            headers={"User-Agent": self._user_agent},
        )

    # ── Repr ─────────────────────────────────────────────────────────────

    def __repr__(self) -> str:
        tail = self.api_key[-4:] if self.api_key else ""
        return f"AsyncHyperAPIClient(api_key='hk_***{tail}', base_url='{self.base_url}')"

    # ── Headers ──────────────────────────────────────────────────────────

    def _get_headers(self, *, async_mode: bool = False) -> dict[str, str]:
        """Per-request headers. Generates a fresh X-Request-ID per call.

        ``async_mode`` here refers to the HTTP-contract flag (``X-Async: true``)
        that tells the server to return a job handle, not Python's async/await.
        """
        headers = {
            "X-API-Key": self.api_key,
            "X-Request-ID": str(uuid.uuid4()),
        }
        if async_mode:
            headers["X-Async"] = "true"
        return headers

    # ── Path helpers ─────────────────────────────────────────────────────

    def _resolve_path(
        self,
        file_path: str | Path | None,
        image_path: str | Path | None = None,
    ) -> Path:
        """Resolve and validate file path."""
        raw = file_path or image_path
        if raw is None:
            raise ValueError("file_path is required")
        path = Path(raw)
        if not path.exists():
            raise FileNotFoundError(f"File not found: {path}")
        return path

    # ── Upload ───────────────────────────────────────────────────────────

    async def upload_document(
        self,
        file_path: str | Path,
        content_type: str | None = None,
    ) -> str:
        """Upload a document via the presigned-URL flow and return its document_key.

        Two awaits under the hood: a presigned-URL fetch and the direct upload
        PUT. The returned ``document_key`` is reusable across subsequent
        inference calls without re-uploading.

        Raises:
            FileNotFoundError: If the file does not exist.
            AuthenticationError: 401 from the server.
            DocumentUploadError: Any other failure on the presigned fetch or S3 PUT.
        """
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"File not found: {path}")

        if content_type is None:
            content_type = CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")

        headers = self._get_headers()
        request_id = headers["X-Request-ID"]

        # Send the file size so the server can pre-flight the size limit and
        # reject oversized files with a 413 here, before the S3 PUT, instead of
        # failing later at the PUT. Best-effort: skipped if stat() fails.
        body: dict[str, object] = {"filename": path.name, "content_type": content_type}
        try:
            body["content_length"] = path.stat().st_size
        except OSError:
            pass

        # Step 1 — get presigned upload URL (goes through Kong + auth)
        try:
            resp = await self._client.post(
                f"{self.base_url}/v1/documents/upload",
                json=body,
                headers=headers,
            )
        except httpx.RequestError as e:
            raise DocumentUploadError(
                f"Failed to get upload URL: {e}", request_id=request_id
            ) from e

        self._raise_for_known_status(
            resp,
            request_id=request_id,
            error_cls=DocumentUploadError,
            op_name="upload",
        )
        if resp.status_code != 200:
            raise DocumentUploadError(
                _server_message(resp, "Upload URL request failed"),
                status_code=resp.status_code,
                request_id=_request_id_of(resp, request_id),
            )

        upload_data = resp.json()
        document_key = upload_data["document_key"]
        upload_url = upload_data["upload_url"]

        # Step 2 — PUT bytes directly to S3 (bypasses Kong; S3 validates the presigned URL)
        try:
            with open(path, "rb") as f:
                file_bytes = f.read()
            s3_resp = await self._client.put(
                upload_url,
                content=file_bytes,
                headers={
                    "Content-Type": content_type,
                    "x-amz-server-side-encryption": "AES256",
                },
            )
        except httpx.RequestError as e:
            raise DocumentUploadError(f"S3 upload failed: {e}", request_id=request_id) from e

        if s3_resp.status_code not in (200, 204):
            raise DocumentUploadError(
                f"S3 PUT failed (status {s3_resp.status_code}). "
                "Ensure x-amz-server-side-encryption: AES256 is present.",
                status_code=s3_resp.status_code,
                request_id=request_id,
            )

        logger.info(
            "document_uploaded",
            extra={"request_id": request_id, "document_key": document_key},
        )
        return document_key

    # ── HTTP error classification ────────────────────────────────────────

    def _raise_for_known_status(
        self,
        resp: httpx.Response,
        *,
        request_id: str | None,
        error_cls: type,
        op_name: str,
    ) -> None:
        """Raise the appropriate typed exception for well-known HTTP statuses.

        Returns silently for 200/202 (success) and any other status that the
        caller wants to handle explicitly. Mirrors the sync client's mapping
        byte-for-byte.
        """
        sc = resp.status_code
        rid = _request_id_of(resp, request_id)

        if sc == 401:
            # Same env-aware behavior as the sync client: reference self.base_url
            # so customers on staging / on-prem aren't pointed at the prod dashboard.
            raise AuthenticationError(
                f"Invalid API key for {self.base_url}. "
                "Check your HyperAPI dashboard for the correct key.",
                status_code=401,
                request_id=rid,
            )
        if sc == 402:
            raise error_cls(
                f"Insufficient credits for {self.base_url}. "
                "Add credits via your HyperAPI dashboard.",
                status_code=402,
                request_id=rid,
            )
        if sc == 429:
            raise _rate_limit_error_from(resp, rid)
        if sc == 413:
            raise DocumentUploadError(
                _server_message(resp, "File exceeds upload size limit"),
                status_code=413,
                request_id=rid,
            )
        if sc == 415:
            raise DocumentUploadError(
                _server_message(resp, "Unsupported file type"),
                status_code=415,
                request_id=rid,
            )
        if sc == 408:
            raise error_cls(
                _server_message(resp, "Service deadline exceeded"),
                status_code=408,
                request_id=rid,
            )
        if sc == 403:
            raise HyperAPIError(
                _server_message(resp, "Forbidden"),
                status_code=403,
                request_id=rid,
            )

    # ── Async submission (X-Async: true) ─────────────────────────────────

    async def _submit_via_path(
        self,
        endpoint: str,
        op_name: str,
        file_path: Path,
        *,
        params: dict[str, Any],
        use_presigned: bool,
        data: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Job:
        """Submit a pipeline op asynchronously. Returns a Job handle.

        ``data`` carries extra form fields beyond ``document_key`` (e.g. redact's
        ``pii_config``), sent in the form body alongside ``document_key``.

        The op always uploads via the presigned-S3 flow: the API has no
        direct-upload path and only accepts a ``document_key``. ``use_presigned``
        is retained for backward compatibility; ``use_presigned=False`` emits a
        ``DeprecationWarning`` and falls back to the presigned flow.
        """
        request_timeout = timeout or self.timeout
        error_cls = _OP_TO_ERROR[op_name]
        content_type = CONTENT_TYPES.get(
            file_path.suffix.lower(), "application/octet-stream"
        )
        headers = self._get_headers(async_mode=True)
        request_id = headers["X-Request-ID"]
        extra_form = data or {}

        if not use_presigned:
            warnings.warn(
                "use_presigned=False is no longer supported: the API has no "
                "direct-upload path and only accepts a presigned document_key. "
                "Falling back to the presigned upload flow.",
                DeprecationWarning,
                stacklevel=2,
            )

        try:
            document_key = await self.upload_document(file_path, content_type=content_type)
            response = await self._client.post(
                f"{self.base_url}{endpoint}",
                data={"document_key": document_key, **extra_form},
                params=params,
                headers=headers,
                timeout=request_timeout,
            )
        except httpx.TimeoutException as e:
            raise error_cls(
                "Request timed out",
                status_code=504,
                request_id=request_id,
            ) from e
        except httpx.RequestError as e:
            raise error_cls(f"Request failed: {e}", request_id=request_id) from e

        self._raise_for_known_status(
            response, request_id=request_id, error_cls=error_cls, op_name=op_name
        )
        if response.status_code not in (200, 202):
            raise error_cls(
                _server_message(response, f"{op_name} submission failed"),
                status_code=response.status_code,
                request_id=_request_id_of(response, request_id),
            )

        envelope = response.json()
        return self._job_from_envelope(envelope, op=op_name)

    async def _submit_via_doc_key(
        self,
        endpoint: str,
        op_name: str,
        document_key: str,
        *,
        params: dict[str, Any],
        timeout: float | None = None,
    ) -> Job:
        """Submit a pipeline op asynchronously when the document_key is already
        in hand (used by ``process()`` to share one upload between parse + extract)."""
        request_timeout = timeout or self.timeout
        error_cls = _OP_TO_ERROR[op_name]
        headers = self._get_headers(async_mode=True)
        request_id = headers["X-Request-ID"]

        try:
            response = await self._client.post(
                f"{self.base_url}{endpoint}",
                data={"document_key": document_key},
                params=params,
                headers=headers,
                timeout=request_timeout,
            )
        except httpx.TimeoutException as e:
            raise error_cls(
                "Request timed out", status_code=504, request_id=request_id
            ) from e
        except httpx.RequestError as e:
            raise error_cls(f"Request failed: {e}", request_id=request_id) from e

        self._raise_for_known_status(
            response, request_id=request_id, error_cls=error_cls, op_name=op_name
        )
        if response.status_code not in (200, 202):
            raise error_cls(
                _server_message(response, f"{op_name} submission failed"),
                status_code=response.status_code,
                request_id=_request_id_of(response, request_id),
            )

        return self._job_from_envelope(response.json(), op=op_name)

    @staticmethod
    def _job_from_envelope(envelope: dict, *, op: str) -> Job:
        return Job(
            job_id=envelope["job_id"],
            status=envelope.get("status", "pending"),
            poll_url=envelope.get("poll_url", f"/v1/jobs/{envelope['job_id']}"),
            op=op,
        )

    # ── Polling ──────────────────────────────────────────────────────────

    async def get_job(self, job_id: str) -> dict:
        """Single-shot async poll. No retry, no waiting. Raises on 4xx; returns
        the raw envelope on 200 (``{status, result?, error?, ...}``)."""
        headers = self._get_headers()
        request_id = headers["X-Request-ID"]

        try:
            resp = await self._client.get(
                f"{self.base_url}/v1/jobs/{job_id}", headers=headers
            )
        except httpx.RequestError as e:
            raise HyperAPIError(
                f"Job poll failed: {e}", request_id=request_id
            ) from e

        rid = _request_id_of(resp, request_id)
        if resp.status_code == 401:
            raise AuthenticationError(
                "Invalid API key.", status_code=401, request_id=rid
            )
        if resp.status_code == 404:
            raise HyperAPIError(
                "Job not found or expired.",
                status_code=404,
                request_id=rid,
            )
        if resp.status_code == 429:
            # Kong's hyperapi-auth plugin rate-limits GET /v1/jobs/{id} too — surface
            # as RateLimitError so callers (and `_poll_with_retry`'s don't-retry list)
            # see it consistently with submit-time 429s.
            raise _rate_limit_error_from(resp, rid)
        if resp.status_code >= 400:
            raise HyperAPIError(
                _server_message(resp, f"Poll failed (HTTP {resp.status_code})"),
                status_code=resp.status_code,
                request_id=rid,
            )
        try:
            return resp.json()
        except (json.JSONDecodeError, ValueError) as e:
            raise HyperAPIError(
                f"Malformed job-status response: {e}",
                status_code=200,
                request_id=rid,
            ) from e

    # ── Jobs management ──────────────────────────────────────────────────

    async def list_recent_jobs(
        self,
        *,
        limit: int = 20,
        source: str | None = None,
    ) -> list[dict]:
        """List the org's recent jobs (summary rows, newest first).

        Mirrors :meth:`HyperAPIClient.list_recent_jobs` — same params
        (``limit`` valid range [1, 100], out-of-range falls back to the
        server default 20; ``source`` filter ``"api"`` | ``"playground"``),
        same summary-row shape, same exceptions. Fetch a full result
        envelope for one job with :py:meth:`get_job`.
        """
        headers = self._get_headers()
        request_id = headers["X-Request-ID"]
        params: dict[str, Any] = {"limit": limit}
        if source is not None:
            params["source"] = source

        try:
            resp = await self._client.get(
                f"{self.base_url}/v1/jobs/recent", params=params, headers=headers
            )
        except httpx.RequestError as e:
            raise HyperAPIError(
                f"Recent-jobs request failed: {e}", request_id=request_id
            ) from e

        rid = _request_id_of(resp, request_id)
        if resp.status_code == 401:
            raise AuthenticationError(
                "Invalid API key.", status_code=401, request_id=rid
            )
        if resp.status_code == 429:
            raise _rate_limit_error_from(resp, rid)
        if resp.status_code >= 400:
            raise HyperAPIError(
                _server_message(resp, f"Recent-jobs request failed (HTTP {resp.status_code})"),
                status_code=resp.status_code,
                request_id=rid,
            )
        try:
            jobs = resp.json()["jobs"]
            if not isinstance(jobs, list):
                raise TypeError(f"'jobs' is {type(jobs).__name__}, expected list")
            return jobs
        except (json.JSONDecodeError, ValueError, KeyError, TypeError) as e:
            raise HyperAPIError(
                f"Malformed recent-jobs response: {e}",
                status_code=200,
                request_id=rid,
            ) from e

    async def delete_job(self, job_id: str) -> None:
        """Cancel a job (``DELETE /v1/jobs/{job_id}``).

        Mirrors :meth:`HyperAPIClient.delete_job` — the server semantics are
        *cancel*, not erase: in-flight jobs are stopped (status becomes
        ``"cancelled"``), terminal jobs keep their result. Idempotent —
        missing, expired, and already-terminal job ids all return success.

        Raises:
            AuthenticationError: 401 (bad key).
            HyperAPIError: 404 (job belongs to a different org), or any other
                ≥400. A missing/expired job is *not* a 404 here — DELETE is
                idempotent and returns 204 (success) for ids that no longer
                exist; the only 404 is a cross-org id.
        """
        headers = self._get_headers()
        request_id = headers["X-Request-ID"]

        try:
            resp = await self._client.delete(
                f"{self.base_url}/v1/jobs/{job_id}", headers=headers
            )
        except httpx.RequestError as e:
            raise HyperAPIError(
                f"Job cancel failed: {e}", request_id=request_id
            ) from e

        rid = _request_id_of(resp, request_id)
        if resp.status_code in (200, 204):
            return
        if resp.status_code == 401:
            raise AuthenticationError(
                "Invalid API key.", status_code=401, request_id=rid
            )
        if resp.status_code == 404:
            # On DELETE, a 404 means the job belongs to a different org
            # (opaque enumeration guard). Missing/expired ids short-circuit to
            # 204 above, so they never reach here.
            raise HyperAPIError(
                "Job not found.",
                status_code=404,
                request_id=rid,
            )
        if resp.status_code == 429:
            raise _rate_limit_error_from(resp, rid)
        raise HyperAPIError(
            _server_message(resp, f"Job cancel failed (HTTP {resp.status_code})"),
            status_code=resp.status_code,
            request_id=rid,
        )

    async def _poll_with_retry(self, job_id: str) -> dict:
        """Poll one time, retrying on transient failures (5xx, connection errors).

        Fast-fails on 401 (bad key) and 404 (job missing). Bounds the retry
        budget so a single poll attempt can't dominate poll_timeout.
        """
        last_err: Exception | None = None
        for attempt in range(self._poll_max_transient_retries + 1):
            try:
                envelope = await self.get_job(job_id)
                return envelope
            except (AuthenticationError, RateLimitError):
                # Don't retry — caller's key/credit problem.
                raise
            except HyperAPIError as e:
                if e.status_code == 404:
                    # Job genuinely doesn't exist — no point retrying.
                    raise
                last_err = e
                if attempt < self._poll_max_transient_retries:
                    logger.info(
                        "poll_transient_retry",
                        extra={
                            "job_id": job_id,
                            "attempt": attempt + 1,
                            "delay_s": self._poll_transient_retry_delay,
                            "error": str(e)[:200],
                        },
                    )
                    await asyncio.sleep(self._poll_transient_retry_delay)
                    continue
                raise
        if last_err:  # pragma: no cover
            raise last_err
        raise HyperAPIError("Poll exhausted retries with no response")  # pragma: no cover

    async def wait_for_job(
        self,
        job: Job | str,
        *,
        timeout: float | None = None,
        interval: float | None = None,
    ) -> dict:
        """Poll a job until it completes, fails, or the timeout elapses.

        Mirrors :meth:`HyperAPIClient.wait_for_job` semantics — same return
        shape, same exceptions — but yields to the event loop between polls
        via ``await asyncio.sleep`` instead of blocking the thread.
        """
        if isinstance(job, Job):
            job_id = job.job_id
            op = job.op
            start = job.submitted_at
        else:
            job_id = job
            op = "unknown"
            start = time.monotonic()

        loop_start = time.monotonic()
        budget = timeout if timeout is not None else self._poll_timeout
        deadline = loop_start + budget
        wait_s = interval if interval is not None else self._poll_interval

        while True:
            envelope = await self._poll_with_retry(job_id)
            status = envelope.get("status")
            if status == "completed":
                logger.info(
                    "job_completed",
                    extra={
                        "job_id": job_id,
                        "op": op,
                        "duration_ms": envelope.get("duration_ms"),
                    },
                )
                return envelope.get("result", envelope)
            if status == "failed":
                raise self._exception_for_failed_job(op, envelope, job_id)

            if time.monotonic() >= deadline:
                raise JobTimeoutError(
                    (
                        f"Job {job_id} did not complete within {budget}s. "
                        "Pass a larger poll_timeout or use submit_<op>+wait_for_job "
                        "to manage polling yourself."
                    ),
                    job_id=job_id,
                    elapsed_s=time.monotonic() - start,
                )

            remaining = deadline - time.monotonic()
            await asyncio.sleep(min(wait_s, max(0.0, remaining)))

    async def wait_for_jobs(
        self,
        jobs: Iterable[Job],
        *,
        timeout: float | None = None,
        interval: float | None = None,
    ) -> list[dict]:
        """Poll multiple jobs concurrently and return results in input order.

        Uses ``asyncio.gather`` for true concurrent polling — each job runs on
        its own task in the event loop. This is the natural async fan-out and
        is strictly more concurrent than the sync client's round-robin loop.

        Behavior matches the sync client otherwise: shared timeout budget per
        job, first failure raises (others are cancelled), returns ``[]`` for
        empty input.

        Raises:
            JobTimeoutError: If any job exceeds ``timeout``.
            HyperAPIError (or subclass): If any job ends with status=failed.
        """
        jobs_list = list(jobs)
        if not jobs_list:
            return []

        # Each task waits up to `budget`; gather raises on the first failure
        # and cancels the rest, matching the sync semantics ("first failure
        # wins; pending jobs abandoned by the SDK").
        budget = timeout if timeout is not None else self._poll_timeout

        coros = [
            self.wait_for_job(j, timeout=budget, interval=interval)
            for j in jobs_list
        ]
        results = await asyncio.gather(*coros)

        # `wait_for_job` returns the envelope's `result` field (or the full
        # envelope if there's no `result`). Coerce non-dicts to {} so positional
        # ordering is preserved exactly like the sync version.
        normalized: list[dict] = []
        for res in results:
            normalized.append(res if isinstance(res, dict) else {})
        return normalized

    def _exception_for_failed_job(
        self, op: str, envelope: dict, job_id: str
    ) -> HyperAPIError:
        """Build the right typed exception for a ``status: failed`` envelope.

        Same registry as the sync client — per-op subclass when known
        (ParseError, ExtractError, …), HyperAPIError otherwise.
        """
        raw = _strip_api_key(envelope.get("error")) or "Job failed"
        status_code = envelope.get("error_status_code")
        message = f"(HTTP {status_code}) {raw}" if status_code else raw
        cls = _OP_TO_ERROR.get(op, HyperAPIError)
        return cls(
            message,
            status_code=status_code,
            request_id=envelope.get("request_id"),
        )

    # ── Public submit_<op> methods ───────────────────────────────────────

    async def submit_parse(
        self,
        file_path: str | Path | None = None,
        *,
        image_path: str | Path | None = None,
        mode: ParseMode = "fast",
        include_boxes: bool = False,
        include_image: bool = False,
        use_presigned: bool = True,
    ) -> Job:
        """Submit a parse job asynchronously and return immediately.

        ``mode="advanced"`` runs layout-aware structured OCR: each
        ``result["result"]["pages"]`` entry gains a ``structured`` dict with ``html``,
        ``markdown``, and ``regions``. Only meaningful on the default OCR
        engine; noticeably slower than ``"fast"`` but well within the default
        ``poll_timeout``.

        ``include_boxes=True`` adds per-segment bounding boxes to each entry of
        ``result["result"]["pages"]`` (standard OCR engine only; the layout-aware engine
        returns an empty list).

        ``include_image=True`` adds a presigned ``image_url`` + ``dimensions``
        to each ``result["result"]["pages"]`` entry for the deskew-corrected page image.
        """
        path = self._resolve_path(file_path, image_path)
        return await self._submit_via_path(
            "/v1/parse", "parse", path,
            params={
                "mode": mode,
                "include_boxes": include_boxes,
                "include_image": include_image,
            },
            use_presigned=use_presigned,
        )

    async def submit_extract(
        self,
        file_path: str | Path,
        *,
        category: ExtractCategory = "financial",
        parse_mode: ParseMode = "fast",
        use_presigned: bool = True,
    ) -> Job:
        """Submit a Basic extract job asynchronously and return immediately.

        ``category`` selects the Basic extractor: ``"financial"`` (default —
        the two-leg IDP adapter for invoices/receipts) or ``"non_financial"``
        (a generic single-pass extractor). For auto-detecting Advanced
        extraction, use :py:meth:`submit_extract_advanced`.

        ``parse_mode`` selects the Stage-1 OCR engine, independent of
        ``category``: ``"fast"`` (default, fast text extraction) or
        ``"advanced"`` (advanced layout-aware parsing for dense tables/forms —
        higher accuracy, slower, costs more; available on paid tiers).
        """
        path = self._resolve_path(file_path)
        return await self._submit_via_path(
            "/v1/extract", "extract", path,
            params={"category": category, "parse_mode": parse_mode},
            use_presigned=use_presigned,
        )

    async def submit_extract_advanced(
        self,
        file_path: str | Path,
        *,
        parse_mode: ParseMode = "fast",
        use_presigned: bool = True,
    ) -> Job:
        """Submit an Advanced extract job asynchronously and return immediately.

        Advanced extraction auto-detects the document type (no ``category``
        needed): invoice-family documents route to the two-leg IDP adapter,
        everything else to schema-less grounded extraction. Returns the same
        envelope shape as :py:meth:`submit_extract`.

        ``parse_mode`` selects the Stage-1 OCR engine (``"fast"``/``"advanced"``,
        same meaning as :py:meth:`submit_extract`).
        """
        path = self._resolve_path(file_path)
        # op_name="extract" is deliberate error-taxonomy reuse (same ExtractError,
        # same response envelope) — NOT a typo. There is no "extract-omni" key in
        # _OP_TO_ERROR; do not "fix" this to one (it would KeyError).
        return await self._submit_via_path(
            "/v1/extract-omni", "extract", path,
            params={"parse_mode": parse_mode},
            use_presigned=use_presigned,
        )

    async def submit_classify(
        self,
        file_path: str | Path,
        *,
        mode: str = "default",
        options: dict | None = None,
        use_presigned: bool = True,
    ) -> Job:
        """Submit a classify job asynchronously and return immediately.

        ``options`` is an optional dict of classifier knobs, sent as a JSON
        form field and validated server-side. Accepted keys:

        - ``mode`` (``"fast"`` | ``"balanced"`` | ``"thorough"``) — the
          pipeline depth, distinct from this method's ``mode=`` task selector.
        - ``active_classes`` — explicit list of class labels to consider.
        - ``active_classes_type`` (``"default"`` | ``"minimal"``, default
          ``"default"``) — picks the built-in label set (default = full set,
          minimal = reduced set).
        - ``custom_llm_classes`` — additional caller-defined class labels.
        - ``system_instruction`` — extra prompt guidance.
        - ``domain_options`` (str, optional) — pipe-separated list of allowed
          first-stage domain labels.
        - ``non_domain_label`` (str, optional, effective default
          ``"non_financial"``) — label used when the document falls outside
          the configured domain.
        - ``non_domain_exit_label`` (str, optional, effective default
          ``"other_non_financial"``) — terminal label for out-of-domain exits.
        - ``confusion_neighbors`` (dict of ``{label: [neighbor_label, ...]}``,
          optional) — disambiguation hints between easily-confused classes.
        - Token/page budgets: ``start_token_budget`` (default 3200, range
          256–8192), ``end_token_budget`` (default 1600, range 256–8192),
          ``max_start_pages`` (default 4, range 1–20), ``max_end_pages``
          (default 2, range 0–10).
        """
        path = self._resolve_path(file_path)
        data = {"options": json.dumps(options)} if options is not None else None
        return await self._submit_via_path(
            "/v1/classify", "classify", path,
            params={"mode": mode},
            data=data,
            use_presigned=use_presigned,
        )

    async def submit_split(
        self,
        file_path: str | Path,
        *,
        mode: str = "default",
        options: dict | None = None,
        use_presigned: bool = True,
    ) -> Job:
        """Submit a split job asynchronously and return immediately.

        ``options`` is an optional dict of splitter knobs, sent as a JSON
        form field and validated server-side: ``use_thinking``,
        ``segment_classes``, ``extend_segment_classes``, and
        ``custom_domain_guidelines`` — a nested object whose ``domain_name``
        sub-field is **required** when ``custom_domain_guidelines`` is given;
        the remaining sub-fields (``identifier_patterns``, ``binding_patterns``,
        ``heuristics``, ``examples``) are optional.
        """
        path = self._resolve_path(file_path)
        data = {"options": json.dumps(options)} if options is not None else None
        return await self._submit_via_path(
            "/v1/split", "split", path,
            params={"mode": mode},
            data=data,
            use_presigned=use_presigned,
        )

    async def submit_redact(
        self,
        file_path: str | Path,
        *,
        mode: str = "redact",
        pii_config: dict | None = None,
        include_logos: bool = False,
        use_presigned: bool = True,
    ) -> Job:
        """Submit a redact/deidentify job asynchronously and return immediately.

        ``mode="redact"`` applies black boxes; ``mode="deidentify"`` overlays
        synthetic values. ``pii_config`` (``{"mode": "extend"|"replace",
        "types": [...]}``) customizes the detected PII types. Built-in types
        include PERSON_NAME, COMPANY_NAME, EMAIL, ADDRESS, WEBSITE, PHONE,
        and CREDENTIALS (passwords, API keys, tokens, secrets) — all with
        synthetic replacements, so each works in both modes.
        """
        path = self._resolve_path(file_path)
        data = {"pii_config": json.dumps(pii_config)} if pii_config is not None else None
        return await self._submit_via_path(
            "/v1/redact", "redact", path,
            params={"mode": mode, "include_logos": include_logos},
            data=data,
            use_presigned=use_presigned,
        )

    # ── Batch API (async, deferred) ──────────────────────────────────────

    _BATCH_TERMINAL = frozenset(
        {"completed", "completed_with_errors", "failed", "canceled"}
    )

    async def _batch_request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict | None = None,
        params: dict | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> dict:
        """Shared request+error handling for the /v1/batch endpoints."""
        headers = self._get_headers()
        request_id = headers["X-Request-ID"]
        if extra_headers:
            headers.update(extra_headers)
        try:
            resp = await self._client.request(
                method,
                f"{self.base_url}{path}",
                headers=headers,
                json=json_body,
                params=params,
            )
        except httpx.RequestError as e:
            raise HyperAPIError(f"Batch request failed: {e}", request_id=request_id) from e

        rid = _request_id_of(resp, request_id)
        if resp.status_code == 401:
            raise AuthenticationError("Invalid API key.", status_code=401, request_id=rid)
        if resp.status_code == 404:
            raise HyperAPIError("Batch not found.", status_code=404, request_id=rid)
        if resp.status_code == 429:
            raise _rate_limit_error_from(resp, rid)
        if resp.status_code >= 400:
            raise HyperAPIError(
                _server_message(resp, f"Batch request failed (HTTP {resp.status_code})"),
                status_code=resp.status_code,
                request_id=rid,
            )
        try:
            return resp.json()
        except (json.JSONDecodeError, ValueError) as e:
            raise HyperAPIError(
                f"Malformed batch response: {e}", status_code=resp.status_code, request_id=rid
            ) from e

    async def create_batch(
        self,
        *,
        endpoint: str,
        document_keys: list[str],
        options: dict | None = None,
        parse_mode: str | None = None,
        webhook_url: str | None = None,
        metadata: dict | None = None,
        estimated_pages_per_doc: int = 1,
        idempotency_key: str | None = None,
    ) -> dict:
        """Submit a batch of already-uploaded documents for async processing.

        Returns ``{batch_id, status, total_items}``; poll :meth:`get_batch` (or
        :meth:`wait_for_batch`) for per-document results. Endpoint is one of
        ``/v1/parse``, ``/v1/extract``, ``/v1/classify``, ``/v1/split``, ``/v1/redact``.
        A repeated ``idempotency_key`` returns the existing batch. ``webhook_url``
        registers a completion callback; ``metadata`` attaches caller-supplied
        key/value tags echoed back on reads and in the webhook payload.
        ``parse_mode`` (``/v1/parse`` only) selects ``fast`` (default) or
        ``advanced`` (advanced layout-aware, paid plans only — 403 otherwise);
        advanced batch runs on the lowest-priority lane (24h SLA).
        """
        body: dict = {
            "endpoint": endpoint,
            "document_keys": document_keys,
            "options": options or {},
            "estimated_pages_per_doc": estimated_pages_per_doc,
        }
        if parse_mode is not None:
            body["parse_mode"] = parse_mode
        if webhook_url is not None:
            body["webhook_url"] = webhook_url
        if metadata is not None:
            body["metadata"] = metadata
        extra = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        return await self._batch_request("POST", "/v1/batch", json_body=body, extra_headers=extra)

    async def create_batch_from_files(
        self,
        *,
        endpoint: str,
        file_paths: list[str | Path],
        **kwargs,
    ) -> dict:
        """Upload each file concurrently (presigned flow) then :meth:`create_batch`."""
        document_keys = await asyncio.gather(
            *(self.upload_document(p) for p in file_paths)
        )
        return await self.create_batch(
            endpoint=endpoint, document_keys=list(document_keys), **kwargs
        )

    async def get_batch(self, batch_id: str, *, limit: int = 200, offset: int = 0) -> dict:
        """Batch status: ``{status, counts, items:[{doc_index, status, ...}]}``."""
        return await self._batch_request(
            "GET", f"/v1/batch/{batch_id}", params={"limit": limit, "offset": offset}
        )

    async def list_batches(self, *, limit: int = 50, offset: int = 0) -> dict:
        """List the org's batches, newest first."""
        return await self._batch_request(
            "GET", "/v1/batch", params={"limit": limit, "offset": offset}
        )

    async def cancel_batch(self, batch_id: str) -> dict:
        """Cancel a batch — stops queued items; in-flight items finish."""
        return await self._batch_request("DELETE", f"/v1/batch/{batch_id}")

    async def wait_for_batch(
        self,
        batch_id: str,
        *,
        timeout: float = 3600.0,
        poll_interval: float = 5.0,
    ) -> dict:
        """Poll :meth:`get_batch` until the batch reaches a terminal state.

        Raises :class:`JobTimeoutError` if ``timeout`` elapses (the batch keeps
        running server-side).
        """
        start = time.monotonic()
        deadline = start + timeout
        while True:
            status = await self.get_batch(batch_id)
            if status.get("status") in self._BATCH_TERMINAL:
                return status
            if time.monotonic() >= deadline:
                raise JobTimeoutError(
                    f"Batch {batch_id} did not complete within {timeout}s.",
                    job_id=batch_id,
                    elapsed_s=time.monotonic() - start,
                )
            await asyncio.sleep(min(poll_interval, max(0.0, deadline - time.monotonic())))

    # ── Public convenience methods (submit + wait) ───────────────────────

    async def parse(
        self,
        file_path: str | Path | None = None,
        *,
        image_path: str | Path | None = None,
        mode: ParseMode = "fast",
        include_boxes: bool = False,
        include_image: bool = False,
        use_presigned: bool = True,
        poll_timeout: float | None = None,
        poll_interval: float | None = None,
    ) -> dict:
        """Parse a document using OCR. Submits asynchronously and polls until done.

        ``mode="advanced"`` runs layout-aware structured OCR: each
        ``result["result"]["pages"]`` entry gains a ``structured`` dict with ``html``,
        ``markdown``, and ``regions``. Only meaningful on the default OCR
        engine; noticeably slower than ``"fast"``.

        ``include_boxes=True`` adds per-segment bounding boxes to each entry of
        ``result["result"]["pages"]`` (standard OCR engine only; the layout-aware engine
        returns an empty list).

        ``include_image=True`` adds a presigned ``image_url`` + ``dimensions``
        to each ``result["result"]["pages"]`` entry for the deskew-corrected page image.
        """
        job = await self.submit_parse(
            file_path=file_path,
            image_path=image_path,
            mode=mode,
            include_boxes=include_boxes,
            include_image=include_image,
            use_presigned=use_presigned,
        )
        return await self.wait_for_job(job, timeout=poll_timeout, interval=poll_interval)

    async def extract(
        self,
        file_path: str | Path,
        *,
        category: ExtractCategory = "financial",
        parse_mode: ParseMode = "fast",
        use_presigned: bool = True,
        poll_timeout: float | None = None,
        poll_interval: float | None = None,
    ) -> dict:
        """Extract structured data (entities + line items) from a document.

        This is the Basic extractor. ``category="financial"`` (default) runs the
        two-leg IDP adapter (invoices/receipts); ``"non_financial"`` runs a
        generic single-pass extractor. For auto-detecting Advanced extraction,
        use :py:meth:`extract_advanced`.
        """
        job = await self.submit_extract(
            file_path,
            category=category,
            parse_mode=parse_mode,
            use_presigned=use_presigned,
        )
        return await self.wait_for_job(job, timeout=poll_timeout, interval=poll_interval)

    async def extract_advanced(
        self,
        file_path: str | Path,
        *,
        parse_mode: ParseMode = "fast",
        use_presigned: bool = True,
        poll_timeout: float | None = None,
        poll_interval: float | None = None,
    ) -> dict:
        """Advanced extraction — auto-detects the document type, no ``category``.

        Invoice-family documents route to the two-leg IDP adapter; everything
        else to schema-less grounded extraction. Submits asynchronously and
        polls until done. Returns the same envelope shape as :py:meth:`extract`.
        """
        job = await self.submit_extract_advanced(
            file_path,
            parse_mode=parse_mode,
            use_presigned=use_presigned,
        )
        return await self.wait_for_job(job, timeout=poll_timeout, interval=poll_interval)

    async def classify(
        self,
        file_path: str | Path,
        *,
        mode: str = "default",
        options: dict | None = None,
        use_presigned: bool = True,
        poll_timeout: float | None = None,
        poll_interval: float | None = None,
    ) -> dict:
        """Classify a document type (invoice, contract, receipt, etc.).

        ``options`` customizes the classifier (see :py:meth:`submit_classify`
        for the full key list): pipeline depth via ``options["mode"]``
        (``"fast"`` | ``"balanced"`` | ``"thorough"`` — distinct from the
        ``mode=`` task selector), ``active_classes``, ``active_classes_type``,
        ``custom_llm_classes``, ``system_instruction``, ``domain_options``,
        ``non_domain_label``, ``non_domain_exit_label``, ``confusion_neighbors``,
        and the token/page budgets (``start_token_budget``, ``end_token_budget``,
        ``max_start_pages``, ``max_end_pages``).
        """
        job = await self.submit_classify(
            file_path,
            mode=mode,
            options=options,
            use_presigned=use_presigned,
        )
        return await self.wait_for_job(job, timeout=poll_timeout, interval=poll_interval)

    async def split(
        self,
        file_path: str | Path,
        *,
        mode: str = "default",
        options: dict | None = None,
        use_presigned: bool = True,
        poll_timeout: float | None = None,
        poll_interval: float | None = None,
    ) -> dict:
        """Split a multi-document PDF into individual document segments.

        ``options`` customizes the splitter (see :py:meth:`submit_split`):
        ``use_thinking``, ``segment_classes``, ``extend_segment_classes``,
        and ``custom_domain_guidelines`` (its ``domain_name`` sub-field is
        required when the object is supplied).
        """
        job = await self.submit_split(
            file_path,
            mode=mode,
            options=options,
            use_presigned=use_presigned,
        )
        return await self.wait_for_job(job, timeout=poll_timeout, interval=poll_interval)

    async def redact(
        self,
        file_path: str | Path,
        *,
        mode: str = "redact",
        pii_config: dict | None = None,
        include_logos: bool = False,
        use_presigned: bool = True,
        poll_timeout: float | None = None,
        poll_interval: float | None = None,
    ) -> dict:
        """Redact or deidentify PII in a document. Submits async + polls until done.

        ``mode="redact"`` masks PII with black boxes; ``mode="deidentify"``
        overlays synthetic replacements. ``include_logos=True`` also masks logos.
        ``pii_config`` customizes the detected PII type set. Returns the response
        envelope with ``result`` (``images``, ``updated_text_block``, ``summary``).
        """
        job = await self.submit_redact(
            file_path,
            mode=mode,
            pii_config=pii_config,
            include_logos=include_logos,
            use_presigned=use_presigned,
        )
        return await self.wait_for_job(job, timeout=poll_timeout, interval=poll_interval)

    async def process(
        self,
        file_path: str | Path | None = None,
        *,
        image_path: str | Path | None = None,
        poll_timeout: float | None = None,
        poll_interval: float | None = None,
    ) -> dict:
        """Parse and extract in one call.

        Uploads the document once, then submits the parse and extract legs
        back-to-back. The platform runs the two legs concurrently on the
        backend; the SDK polls both jobs concurrently via ``asyncio.gather``.
        Total wall-clock ≈ max(parse, extract) + small polling overhead.

        Returns:
            ``{"ocr": <parse text>, "data": <extract structured fields>}``
        """
        path = self._resolve_path(file_path, image_path)
        document_key = await self.upload_document(path)
        # The two submissions could in principle be gathered too, but keeping
        # them sequential matches the sync client's behavior and the backend
        # router serializes them anyway through the shared document_key.
        parse_job = await self._submit_via_doc_key(
            "/v1/parse", "parse", document_key,
            params={},
        )
        extract_job = await self._submit_via_doc_key(
            "/v1/extract", "extract", document_key,
            params={},
        )
        results = await self.wait_for_jobs(
            [parse_job, extract_job],
            timeout=poll_timeout,
            interval=poll_interval,
        )
        parse_result, extract_result = results
        return {
            "ocr": _parse_ocr_text(parse_result),
            "data": extract_result if isinstance(extract_result, dict) else {},
        }

    # ── Lifecycle ────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        """Close the underlying HTTP client and release its connection pool."""
        await self._client.aclose()

    async def __aenter__(self) -> AsyncHyperAPIClient:
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.aclose()
