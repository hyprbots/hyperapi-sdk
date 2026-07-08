"""HyperAPI Client.

Architecture
============
Every long-running pipeline op (`parse`, `extract`, `classify`, `split`,
`process`) goes through **submit + poll** under the hood:

    1. POST /v1/<op>           with X-Async: true  →  202 {job_id, poll_url}
    2. GET  /v1/jobs/{job_id}  (every poll_interval) →  {status, result?, error?}

This keeps every individual HTTP request short (sub-second), so the SDK is
**timeout-immune** at any edge — works behind CloudFront's 30 s
`origin_response_timeout`, Cloudflare's 100 s, a raw ALB, or no edge at all.
The convenience methods (``parse(...)``, ``extract(...)``, ...) hide the
``job_id`` from callers; advanced users who want fire-and-forget or custom
progress UIs can call ``submit_<op>(...)`` and ``wait_for_job(...)`` directly.

Pipeline (server-side, transparent to callers):

    Stage 1 — OCR (always runs)
        Upload → page images → OCR text (full_text + page_texts)

    Stage 2 — Task-specific processing (skipped for parse)
        parse:      OCR text only (no Stage 2)
        extract:    OCR output → structured extraction
        classify:   OCR output → classification
        split:      OCR output → document splitting
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from collections.abc import Iterable

import httpx

from .exceptions import (
    AuthenticationError,
    ClassifyError,
    DocumentUploadError,
    ExtractError,
    HyperAPIError,
    JobTimeoutError,
    ParseError,
    RateLimitError,
    RedactError,
    SplitError,
    _strip_api_key,
)


CONTENT_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".tiff": "image/tiff",
    ".tif": "image/tiff",
    ".pdf": "application/pdf",
}

ParseMode = Literal["fast", "advanced"]
# Basic extract document family. ``financial`` (default) routes to the two-leg
# IDP adapter (invoices, receipts); ``non_financial`` routes to the generic
# single-pass extractor. Advanced extraction (auto-detect) is a separate method.
ExtractCategory = Literal["financial", "non_financial"]

# Per-HTTP-call defaults — single submit / single poll, NOT total job time.
_DEFAULT_TIMEOUT = 120.0

# Polling defaults — match the playground (hyperapi/src/app/(dashboard)/dashboard/playground/page.tsx)
# so operational characteristics are identical to what the team has already validated in production.
_DEFAULT_POLL_INTERVAL_S = 3.0           # POLL_INTERVAL_MS = 3000 in the playground
_DEFAULT_POLL_TIMEOUT_S = 1800.0         # HARD_TIMEOUT_MS = 1_800_000
_DEFAULT_POLL_MAX_TRANSIENT_RETRIES = 3
_DEFAULT_POLL_TRANSIENT_RETRY_DELAY_S = 0.5


logger = logging.getLogger("hyperapi")


# Ops the SDK can submit asynchronously. Used as the per-op error class registry.
_OP_TO_ERROR: dict[str, type] = {
    "parse": ParseError,
    "extract": ExtractError,
    "classify": ClassifyError,
    "split": SplitError,
    "redact": RedactError,
}


@dataclass
class Job:
    """Handle returned by ``client.submit_<op>(...)``.

    Pass to :py:meth:`HyperAPIClient.wait_for_job` to block until the job is
    done, or to :py:meth:`HyperAPIClient.get_job` for a single-shot status
    check. The ``op`` field is what tells :py:meth:`wait_for_job` which typed
    exception to raise on ``status: "failed"``.
    """

    job_id: str
    status: str
    poll_url: str
    op: str  # "parse" | "extract" | "classify" | "split" | "redact"
    submitted_at: float = field(default_factory=time.monotonic)


# ── Helpers (module-private) ────────────────────────────────────────────────

def _parse_retry_after(value: str | None, *, default: int = 60) -> int:
    """Parse a `Retry-After` header. Server emits seconds-since-now."""
    if not value:
        return default
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def _safe_text(response: httpx.Response) -> str:
    """Return the response body as a string with API keys scrubbed."""
    try:
        return _strip_api_key(response.text) or ""
    except Exception:  # noqa: BLE001  # pragma: no cover
        # Unreachable in practice: httpx.Response.text never raises for the codepaths we hit.
        return ""


def _server_message(response: httpx.Response, fallback: str) -> str:
    """Pull a human-readable message out of a server response.

    Prefers the JSON ``message`` field when present, falls back to the body
    text, and finally to the supplied fallback. Always API-key-scrubbed.
    """
    body = _safe_text(response)
    try:
        parsed = json.loads(body)
        if isinstance(parsed, dict):
            for key in ("message", "detail", "error"):
                if key in parsed and parsed[key]:
                    return str(parsed[key])
    except (json.JSONDecodeError, ValueError):
        pass
    return body or fallback


def _request_id_of(response: httpx.Response, sent: str | None = None) -> str | None:
    """Best-effort extraction of the request id we should attach to errors."""
    return response.headers.get("x-request-id") or response.headers.get("x-correlation-id") or sent


def _rate_limit_error_from(resp: httpx.Response, rid: str | None) -> RateLimitError:
    """Build a RateLimitError from a 429 response envelope.

    Kong's hyperapi-auth plugin rate-limits every /v1 route (submits and
    job polls alike), always with a valid JSON envelope. Shared by both
    clients so the field extraction stays in one place.
    """
    retry_after = _parse_retry_after(resp.headers.get("retry-after"))
    tier = None
    limit = None
    window_seconds = None
    degraded = False
    try:
        body = resp.json()
        if isinstance(body, dict):
            tier = body.get("tier")
            limit = body.get("limit")
            # Bug #86 (b): server now emits the sliding-window length so
            # callers know "limit per N seconds" rather than guessing RPS.
            window_seconds = body.get("window_seconds")
            # Bug #86 (a): free-tier fail-closed sets degraded:true so
            # customers can distinguish "temporarily unavailable, retry"
            # from "actually exceeded my quota".
            degraded = bool(body.get("degraded", False))
    except (json.JSONDecodeError, ValueError):  # pragma: no cover
        # Kong's hyperapi-auth plugin always emits a valid JSON envelope on 429.
        pass
    return RateLimitError(
        _server_message(resp, "Rate limit exceeded"),
        retry_after=retry_after,
        tier=tier,
        limit=limit,
        window_seconds=window_seconds,
        degraded=degraded,
        status_code=429,
        request_id=rid,
    )


# ── Client ─────────────────────────────────────────────────────────────────

class HyperAPIClient:
    """Client for the HyperAPI document intelligence platform.

    Quickstart::

        from hyperapi import HyperAPIClient

        client = HyperAPIClient(api_key="hk_live_…")
        result = client.extract("invoice.pdf")
        print(result["result"])
        client.close()

    Or as a context manager::

        with HyperAPIClient(api_key="hk_live_…") as client:
            print(client.parse("invoice.pdf")["result"]["ocr"])

    Threading
    ---------
    ``HyperAPIClient`` is **not** thread-safe (httpx.Client isn't either).
    Construct one client per thread, or use ``threading.local()``.

    Polling
    -------
    Long-running ops (parse/extract/classify/split/process) submit to the
    server with ``X-Async: true`` and poll ``/v1/jobs/{job_id}`` until done.
    Each individual HTTP request is sub-second so the SDK is edge-timeout
    immune. Override the polling cadence per-call:

        client.extract("scan.pdf", poll_timeout=600, poll_interval=5)

    Or globally on the constructor (defaults match the platform playground).
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
        """Initialize a HyperAPI client.

        Args:
            api_key: API key for authentication. Falls back to ``HYPERAPI_KEY``
                environment variable. Required.
            base_url: API base URL. Falls back to ``HYPERAPI_URL`` env var,
                then ``https://apis.hyperbots.com``.
            timeout: Per-HTTP-call timeout in seconds (NOT the total time
                allowed for an entire job — see ``poll_timeout`` for that).
                Default 120 s, which generously covers a single submit or
                a single status poll on production traffic.
            poll_interval: Seconds between successive polls of
                ``/v1/jobs/{id}``. Default 3.0 (matches the platform
                playground).
            poll_timeout: Total wall-clock seconds the SDK will wait for a
                single job to complete. Raises ``JobTimeoutError`` past this.
                Default 1800.0 (30 min, matches playground).
            poll_max_transient_retries: How many times to retry a single
                ``/v1/jobs/{id}`` GET if it returns 5xx or has a connection
                error. Default 3. Fast-fails on 401/404.
            poll_transient_retry_delay: Seconds to sleep between transient-retry
                attempts on a single poll. Default 0.5; bump this if your
                network is bursty enough that 3 retries in 1.5 s isn't enough
                breathing room.
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
        # User-Agent includes the httpx + Python versions so backend log analytics
        # and customer firewalls can identify the underlying HTTP stack + runtime
        # without sniffing other headers. Format mirrors the de-facto convention
        # popularized by the OpenAI / Stripe SDKs.
        py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        self._user_agent = (
            f"hyperapi-sdk-python/{__version__} "
            f"(httpx/{httpx.__version__}; Python/{py_ver})"
        )
        self._client = httpx.Client(
            timeout=timeout,
            headers={"User-Agent": self._user_agent},
        )

    # ── Repr ─────────────────────────────────────────────────────────────

    def __repr__(self) -> str:
        tail = self.api_key[-4:] if self.api_key else ""
        return f"HyperAPIClient(api_key='hk_***{tail}', base_url='{self.base_url}')"

    # ── Headers ──────────────────────────────────────────────────────────

    def _get_headers(self, *, async_mode: bool = False) -> dict[str, str]:
        """Per-request headers. Generates a fresh X-Request-ID per call so
        every request can be correlated end-to-end across the platform."""
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

    def upload_document(
        self,
        file_path: str | Path,
        content_type: str | None = None,
    ) -> str:
        """Upload a document via the presigned-URL flow and return its document_key.

        Performs the two HTTP hops (presigned-URL fetch + S3 PUT) so the
        returned ``document_key`` can be reused across subsequent calls
        without re-uploading the file.

        Args:
            file_path: Path to the file to upload.
            content_type: MIME type. Auto-detected from the extension if omitted.

        Returns:
            The ``document_key`` to pass to inference endpoints.

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
            resp = self._client.post(
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
                s3_resp = self._client.put(
                    upload_url,
                    content=f.read(),
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
        caller wants to handle explicitly.
        """
        sc = resp.status_code
        rid = _request_id_of(resp, request_id)

        if sc == 401:
            # Message references self.base_url so customers on staging / on-prem /
            # self-hosted deployments aren't told to "go to the prod dashboard" —
            # the URL pattern points at the environment they actually authenticated
            # against. Generic "your dashboard" wording stays portable across
            # deployments that may have different dashboard URL conventions.
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

    def _submit_via_path(
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
        ``pii_config``). Sent in the form body on both the presigned and the
        direct-multipart paths.
        """
        request_timeout = timeout or self.timeout
        error_cls = _OP_TO_ERROR[op_name]
        content_type = CONTENT_TYPES.get(
            file_path.suffix.lower(), "application/octet-stream"
        )
        headers = self._get_headers(async_mode=True)
        request_id = headers["X-Request-ID"]
        extra_form = data or {}

        try:
            if use_presigned:
                document_key = self.upload_document(file_path, content_type=content_type)
                response = self._client.post(
                    f"{self.base_url}{endpoint}",
                    data={"document_key": document_key, **extra_form},
                    params=params,
                    headers=headers,
                    timeout=request_timeout,
                )
            else:
                with open(file_path, "rb") as f:
                    files = {"file": (file_path.name, f, content_type)}
                    response = self._client.post(
                        f"{self.base_url}{endpoint}",
                        files=files,
                        data=extra_form,
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

    def _submit_via_doc_key(
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
            response = self._client.post(
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

    def get_job(self, job_id: str) -> dict:
        """Single-shot poll. No retry, no waiting. Raises on 4xx; returns the
        raw envelope on 200 (``{status, result?, error?, ...}``)."""
        headers = self._get_headers()
        request_id = headers["X-Request-ID"]

        try:
            resp = self._client.get(
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
        # Defend against a 200 with malformed JSON — some misconfigured proxies will
        # return text/html bodies on 200. Without this guard, a JSONDecodeError
        # escapes to the caller as an un-typed exception.
        try:
            return resp.json()
        except (json.JSONDecodeError, ValueError) as e:
            raise HyperAPIError(
                f"Malformed job-status response: {e}",
                status_code=200,
                request_id=rid,
            ) from e

    # ── Jobs management ──────────────────────────────────────────────────

    def list_recent_jobs(
        self,
        *,
        limit: int = 20,
        source: str | None = None,
    ) -> list[dict]:
        """List the org's recent jobs (summary rows, newest first).

        Args:
            limit: Maximum rows to return. Valid range [1, 100]; out-of-range
                values are NOT clamped — the server silently falls back to
                the default 20 (e.g. ``limit=200`` returns 20 rows, not 100).
            source: Optional filter — ``"api"`` or ``"playground"``. Unknown
                values are ignored server-side (no error).

        Returns:
            A list of summary dicts (``job_id``, ``request_id``, ``status``,
            ``task``, ``endpoint``, ``pages``, ``filename``, ``source``,
            ``is_test``, ``created_at``, ``completed_at``, ``error``).
            ``status`` may be ``"cancelled"`` for jobs stopped via
            :py:meth:`delete_job`. Summaries never include results — fetch
            the full envelope for one job with :py:meth:`get_job`.
        """
        headers = self._get_headers()
        request_id = headers["X-Request-ID"]
        params: dict[str, Any] = {"limit": limit}
        if source is not None:
            params["source"] = source

        try:
            resp = self._client.get(
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
        # Same malformed-200 defence as get_job, plus envelope-shape guard:
        # the contract is {"jobs": [...]} — anything else is a broken proxy.
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

    def delete_job(self, job_id: str) -> None:
        """Cancel a job (``DELETE /v1/jobs/{job_id}``).

        Despite the HTTP verb, the server semantics are *cancel*, not erase:
        a pending or running job is stopped (its status becomes
        ``"cancelled"``); a job that already completed or failed is left
        untouched and keeps its result. Idempotent — missing, expired, and
        already-terminal job ids all return success, so calling this twice
        is safe.

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
            resp = self._client.delete(
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

    def _poll_with_retry(self, job_id: str) -> dict:
        """Poll one time, retrying on transient failures (5xx, connection errors).

        Fast-fails on 401 (bad key) and 404 (job missing / org mismatch). Bounds
        the retry budget so a single poll attempt can't dominate poll_timeout.
        """
        last_err: Exception | None = None
        for attempt in range(self._poll_max_transient_retries + 1):
            try:
                envelope = self.get_job(job_id)
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
                    time.sleep(self._poll_transient_retry_delay)
                    continue
                raise
        # Unreachable in practice: the loop either returns, raises, or re-raises last_err on the final attempt.
        if last_err:  # pragma: no cover
            raise last_err
        raise HyperAPIError("Poll exhausted retries with no response")  # pragma: no cover

    def wait_for_job(
        self,
        job: Job | str,
        *,
        timeout: float | None = None,
        interval: float | None = None,
    ) -> dict:
        """Poll a job until it completes, fails, or the timeout elapses.

        Args:
            job: A :py:class:`Job` returned by ``submit_<op>`` (preferred — the
                ``op`` field lets us raise the right typed exception on
                failure), or a raw ``job_id`` string (falls back to generic
                ``HyperAPIError`` on failure).
            timeout: Total wall-clock seconds. Defaults to the constructor's
                ``poll_timeout``.
            interval: Seconds between polls. Defaults to the constructor's
                ``poll_interval``.

        Returns:
            The final ``result`` envelope from the server when ``status=completed``.

        Raises:
            JobTimeoutError: If ``timeout`` elapses with the job still pending.
            HyperAPIError (or subclass): If the job ends with ``status=failed``,
                or the poll itself fails after exhausting transient retries.
        """
        if isinstance(job, Job):
            job_id = job.job_id
            op = job.op
            # Use the Job's actual submission time so JobTimeoutError reports
            # total time-since-submit, not just the wait_for_job loop duration.
            # Useful when callers persist a Job and call wait_for_job later.
            start = job.submitted_at
        else:
            job_id = job
            op = "unknown"
            start = time.monotonic()

        # Don't compute elapsed from `deadline - (timeout or self._poll_timeout)`
        # — `or` treats timeout=0 as falsy and silently swaps in the constructor.
        loop_start = time.monotonic()
        budget = timeout if timeout is not None else self._poll_timeout
        deadline = loop_start + budget
        wait_s = interval if interval is not None else self._poll_interval

        while True:
            envelope = self._poll_with_retry(job_id)
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

            # Sleep, but never past the deadline.
            remaining = deadline - time.monotonic()
            time.sleep(min(wait_s, max(0.0, remaining)))

    def wait_for_jobs(
        self,
        jobs: Iterable[Job],
        *,
        timeout: float | None = None,
        interval: float | None = None,
    ) -> list[dict]:
        """Poll multiple jobs concurrently (round-robin).

        Returns the results in the same order as ``jobs``. Single shared
        deadline. Raises on the first failed job (other in-flight jobs continue
        running on the server but are abandoned by the SDK). Useful for
        ``process()`` to collapse parse + extract polling overhead.
        """
        jobs_list = list(jobs)
        if not jobs_list:
            return []

        start = time.monotonic()
        budget = timeout if timeout is not None else self._poll_timeout
        deadline = start + budget
        wait_s = interval if interval is not None else self._poll_interval
        # Sentinel `{}` (not None) means "completed, but the server's result envelope
        # was empty" — preserves order in the returned list. The loop only exits when
        # every index has been written, so we never return more or fewer entries than
        # the input.
        results: list[dict] = [{} for _ in jobs_list]
        pending: list[int] = list(range(len(jobs_list)))

        while pending:
            still_pending: list[int] = []
            for idx in pending:
                envelope = self._poll_with_retry(jobs_list[idx].job_id)
                status = envelope.get("status")
                if status == "completed":
                    # `envelope.get("result", envelope)` falls back to the full envelope
                    # if `result` key is absent; if it's explicitly None, coerce to {}
                    # so the position in the returned list is preserved.
                    res = envelope.get("result", envelope)
                    results[idx] = res if isinstance(res, dict) else {}
                elif status == "failed":
                    raise self._exception_for_failed_job(
                        jobs_list[idx].op, envelope, jobs_list[idx].job_id
                    )
                else:
                    still_pending.append(idx)
            pending = still_pending

            if not pending:
                break
            if time.monotonic() >= deadline:
                pending_ids = ",".join(jobs_list[i].job_id for i in pending)
                raise JobTimeoutError(
                    f"Jobs [{pending_ids}] did not complete within {budget}s.",
                    job_id=pending_ids,
                    elapsed_s=time.monotonic() - start,
                )
            remaining = deadline - time.monotonic()
            time.sleep(min(wait_s, max(0.0, remaining)))

        # results is already in input-order; never filter — the input length is the
        # output length, always.
        return results

    def _exception_for_failed_job(
        self, op: str, envelope: dict, job_id: str
    ) -> HyperAPIError:
        """Build the right typed exception for a ``status: failed`` envelope.

        The op-to-class registry returns the per-op subclass when known
        (``ParseError``, ``ExtractError``, …) and falls back to
        ``HyperAPIError`` for raw-job-id calls where we couldn't determine
        which op originated the job. The HTTP status (if present in the
        envelope's ``error_status_code``) is prefixed onto the message so
        ``str(e)`` is self-describing in support tickets without needing the
        caller to also surface ``e.status_code``.
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

    def submit_parse(
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

        ``image_path`` is a deprecated alias for ``file_path`` retained for
        v0.1.x backward compatibility — pass exactly one. The other
        ``submit_<op>`` methods don't take it because parse is the only op
        that historically accepted bare images via that name.

        ``mode="advanced"`` runs layout-aware structured OCR: each
        ``result["pages"]`` entry gains a ``structured`` dict with ``html``,
        ``markdown``, and ``regions``. Only meaningful on the default OCR
        engine; noticeably slower than ``"fast"`` but well within the default
        ``poll_timeout``.

        ``include_boxes=True`` adds per-segment bounding boxes to each entry of
        ``result["pages"]`` (standard OCR engine only; the layout-aware engine
        returns an empty list).

        ``include_image=True`` adds a presigned ``image_url`` + ``dimensions``
        to each ``result["pages"]`` entry, pointing to the deskew-corrected
        page image.
        """
        path = self._resolve_path(file_path, image_path)
        return self._submit_via_path(
            "/v1/parse", "parse", path,
            params={
                "mode": mode,
                "include_boxes": include_boxes,
                "include_image": include_image,
            },
            use_presigned=use_presigned,
        )

    def submit_extract(
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
        ``category``: ``"fast"`` (default, Paddle text extraction) or
        ``"advanced"`` (Chandra layout-aware parsing for dense tables/forms —
        higher accuracy, slower, costs more; available on paid tiers).
        """
        path = self._resolve_path(file_path)
        return self._submit_via_path(
            "/v1/extract", "extract", path,
            params={"category": category, "parse_mode": parse_mode},
            use_presigned=use_presigned,
        )

    def submit_extract_advanced(
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
        return self._submit_via_path(
            "/v1/extract-omni", "extract", path,
            params={"parse_mode": parse_mode},
            use_presigned=use_presigned,
        )

    def submit_classify(
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
        return self._submit_via_path(
            "/v1/classify", "classify", path,
            params={"mode": mode},
            data=data,
            use_presigned=use_presigned,
        )

    def submit_split(
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
        return self._submit_via_path(
            "/v1/split", "split", path,
            params={"mode": mode},
            data=data,
            use_presigned=use_presigned,
        )

    def submit_redact(
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
        return self._submit_via_path(
            "/v1/redact", "redact", path,
            params={"mode": mode, "include_logos": include_logos},
            data=data,
            use_presigned=use_presigned,
        )

    # ── Batch API (async, deferred) ──────────────────────────────────────

    _BATCH_TERMINAL = frozenset(
        {"completed", "completed_with_errors", "failed", "canceled"}
    )

    def _batch_request(
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
            resp = self._client.request(
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

    def create_batch(
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

        Returns immediately with ``{batch_id, status, total_items}``; the work
        runs on spare capacity. Poll :meth:`get_batch` (or :meth:`wait_for_batch`)
        for per-document results (each result lands in S3, keyed by ``doc_index``).

        Args:
            endpoint: One of ``/v1/parse``, ``/v1/classify``, ``/v1/split``,
                ``/v1/redact`` (MVP). ``/v1/extract`` is not yet batch-supported.
            document_keys: Keys from :meth:`upload_document`, one per document.
            options: Task-specific options, forwarded to each document's run.
            parse_mode: ``fast`` for ``/v1/parse`` (advanced is not yet batchable).
            webhook_url: Optional completion webhook (delivered when available).
            metadata: Optional caller-supplied key/value tags stored with the
                batch and echoed back on :meth:`get_batch` / :meth:`list_batches`
                and in the completion webhook payload.
            estimated_pages_per_doc: Hint for the up-front quota check.
            idempotency_key: Resubmitting with the same key returns the existing
                batch instead of creating a duplicate.
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
        return self._batch_request("POST", "/v1/batch", json_body=body, extra_headers=extra)

    def create_batch_from_files(
        self,
        *,
        endpoint: str,
        file_paths: list[str | Path],
        **kwargs,
    ) -> dict:
        """Upload each file (presigned flow) then :meth:`create_batch`."""
        document_keys = [self.upload_document(p) for p in file_paths]
        return self.create_batch(endpoint=endpoint, document_keys=document_keys, **kwargs)

    def get_batch(self, batch_id: str, *, limit: int = 200, offset: int = 0) -> dict:
        """Batch status: ``{status, counts, items:[{doc_index, status, ...}]}``."""
        return self._batch_request(
            "GET", f"/v1/batch/{batch_id}", params={"limit": limit, "offset": offset}
        )

    def list_batches(self, *, limit: int = 50, offset: int = 0) -> dict:
        """List the org's batches, newest first."""
        return self._batch_request("GET", "/v1/batch", params={"limit": limit, "offset": offset})

    def cancel_batch(self, batch_id: str) -> dict:
        """Cancel a batch — stops queued items; in-flight items finish."""
        return self._batch_request("DELETE", f"/v1/batch/{batch_id}")

    def wait_for_batch(
        self,
        batch_id: str,
        *,
        timeout: float = 3600.0,
        poll_interval: float = 5.0,
    ) -> dict:
        """Poll :meth:`get_batch` until the batch reaches a terminal state.

        Raises :class:`JobTimeoutError` if ``timeout`` elapses (the batch keeps
        running server-side — resume with a larger timeout or :meth:`get_batch`).
        """
        start = time.monotonic()
        deadline = start + timeout
        while True:
            status = self.get_batch(batch_id)
            if status.get("status") in self._BATCH_TERMINAL:
                return status
            if time.monotonic() >= deadline:
                raise JobTimeoutError(
                    f"Batch {batch_id} did not complete within {timeout}s.",
                    job_id=batch_id,
                    elapsed_s=time.monotonic() - start,
                )
            time.sleep(min(poll_interval, max(0.0, deadline - time.monotonic())))

    # ── Public convenience methods (submit + wait) ───────────────────────

    def parse(
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

        Args:
            file_path: Path to the file (PDF, PNG, JPG, WEBP, TIFF, GIF).
            image_path: Deprecated alias for file_path.
            mode: ``"fast"`` (default — plain text + boxes) or ``"advanced"``
                (layout-aware structured OCR: each ``result["pages"]``
                entry gains a ``structured`` dict with ``html``, ``markdown``,
                and ``regions``). Advanced only applies on the default OCR
                engine and is noticeably slower.
            include_boxes: When True, each ``result["pages"]`` entry includes a
                ``boxes`` list of ``{"text", "bbox": [left, top, right, bottom],
                "confidence"}`` segments. Standard OCR engine only; the
                layout-aware engine returns [].
            include_image: When True, each ``result["pages"]`` entry includes an
                ``image_url`` (presigned GET for the deskew-corrected page) and
                ``dimensions`` (``{"width", "height"}`` in that image's pixel space,
                which matches the box coordinate space).
            use_presigned: Use the presigned-S3 upload flow (default True).
            poll_timeout: Override the constructor's poll_timeout for this call.
            poll_interval: Override the constructor's poll_interval for this call.

        Returns:
            Response envelope with ``result["ocr"]``.
        """
        job = self.submit_parse(
            file_path=file_path,
            image_path=image_path,
            mode=mode,
            include_boxes=include_boxes,
            include_image=include_image,
            use_presigned=use_presigned,
        )
        return self.wait_for_job(job, timeout=poll_timeout, interval=poll_interval)

    def extract(
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

        Submits asynchronously and polls until done. Bypasses any edge-timeout
        ceiling because each individual HTTP request stays sub-second.

        This is the Basic extractor. ``category="financial"`` (default) runs the
        two-leg IDP adapter (invoices/receipts); ``"non_financial"`` runs a
        generic single-pass extractor. For auto-detecting Advanced extraction,
        use :py:meth:`extract_advanced`.

        Returns:
            Response envelope with ``result`` containing entities and line items,
            and ``result["ocr_text"]`` with the raw OCR.
        """
        job = self.submit_extract(
            file_path,
            category=category,
            parse_mode=parse_mode,
            use_presigned=use_presigned,
        )
        return self.wait_for_job(job, timeout=poll_timeout, interval=poll_interval)

    def extract_advanced(
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
        job = self.submit_extract_advanced(
            file_path,
            parse_mode=parse_mode,
            use_presigned=use_presigned,
        )
        return self.wait_for_job(job, timeout=poll_timeout, interval=poll_interval)

    def classify(
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
        job = self.submit_classify(
            file_path,
            mode=mode,
            options=options,
            use_presigned=use_presigned,
        )
        return self.wait_for_job(job, timeout=poll_timeout, interval=poll_interval)

    def split(
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
        job = self.submit_split(
            file_path,
            mode=mode,
            options=options,
            use_presigned=use_presigned,
        )
        return self.wait_for_job(job, timeout=poll_timeout, interval=poll_interval)

    def redact(
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
        overlays synthetic replacements. ``include_logos=True`` also detects and
        masks logos. ``pii_config`` customizes the detected PII type set.

        Returns:
            Response envelope with ``result`` containing ``images`` (redacted
            pages), ``updated_text_block``, and a ``summary`` of masked counts.
        """
        job = self.submit_redact(
            file_path,
            mode=mode,
            pii_config=pii_config,
            include_logos=include_logos,
            use_presigned=use_presigned,
        )
        return self.wait_for_job(job, timeout=poll_timeout, interval=poll_interval)

    def process(
        self,
        file_path: str | Path | None = None,
        *,
        image_path: str | Path | None = None,
        poll_timeout: float | None = None,
        poll_interval: float | None = None,
    ) -> dict:
        """Parse and extract in one call.

        Uploads the document once, then submits the parse and extract legs
        back-to-back (two sequential POSTs from the SDK). The platform runs
        the two legs concurrently on the backend, reusing the shared
        ``document_key`` so OCR is computed once, so total wall-clock time
        ≈ max(parse, extract) + a small polling overhead, not their sum.
        Polls both jobs round-robin via ``wait_for_jobs``.

        Returns:
            ``{"ocr": <parse text>, "data": <extract structured fields>}``
        """
        path = self._resolve_path(file_path, image_path)
        document_key = self.upload_document(path)
        parse_job = self._submit_via_doc_key(
            "/v1/parse", "parse", document_key,
            params={},
        )
        extract_job = self._submit_via_doc_key(
            "/v1/extract", "extract", document_key,
            params={},
        )
        results = self.wait_for_jobs(
            [parse_job, extract_job],
            timeout=poll_timeout,
            interval=poll_interval,
        )
        parse_result, extract_result = results
        return {
            "ocr": parse_result.get("ocr") if isinstance(parse_result, dict) else None,
            "data": extract_result if isinstance(extract_result, dict) else {},
        }

    # ── Lifecycle ────────────────────────────────────────────────────────

    def close(self) -> None:
        """Close the underlying HTTP client and release its connection pool."""
        self._client.close()

    def __enter__(self) -> HyperAPIClient:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()
