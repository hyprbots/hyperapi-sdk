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
import random
import sys
import time
import uuid
import warnings
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Literal
from collections.abc import Iterable

import httpx

from .exceptions import (
    AuthenticationError,
    ClassifyError,
    DocumentUploadError,
    EditError,
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
    # Both legs of the two-call edit flow (detect + fill) share one op key —
    # the server meters and fails them under the same /v1/edit endpoint.
    "edit": EditError,
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
    op: str  # "parse" | "extract" | "classify" | "split" | "redact" | "edit"
    submitted_at: float = field(default_factory=time.monotonic)


# ── Helpers (module-private) ────────────────────────────────────────────────

def _parse_retry_after(value: str | None, *, default: int = 60) -> int:
    """Parse a `Retry-After` header into whole seconds-from-now.

    RFC 7231 permits two forms: ``delta-seconds`` (what Kong emits) and an
    ``HTTP-date``. A fronting CDN/ALB — not just Kong — can 429/503 and emit the
    date form; parsing only the integer form silently fell back to ``default``
    and produced a wrong backoff. Handle both; floor at 0.
    """
    if not value:
        return default
    value = value.strip()
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        pass
    # HTTP-date form, e.g. "Wed, 21 Oct 2026 07:28:00 GMT".
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return default
    if dt is None:  # some Python versions return None on unparseable input
        return default
    if dt.tzinfo is None:  # RFC dates are UTC; treat naive as UTC
        dt = dt.replace(tzinfo=timezone.utc)
    delta = (dt - datetime.now(timezone.utc)).total_seconds()
    return max(0, int(delta))


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
                val = parsed.get(key)
                if not val:
                    continue
                # Structured errors nest the human text: FastAPI wraps e.g.
                # {"detail": {"code": "page_quota_exceeded", "message": "..."}}.
                # Prefer that inner message over stringifying the whole dict
                # (which would surface a Python dict repr like "{'code': ...}").
                if isinstance(val, dict):
                    val = val.get("message") or val.get("detail") or val.get("error") or str(val)
                return str(val)
    except (json.JSONDecodeError, ValueError):
        pass
    return body or fallback


def _request_id_of(response: httpx.Response, sent: str | None = None) -> str | None:
    """Best-effort extraction of the request id we should attach to errors."""
    return response.headers.get("x-request-id") or response.headers.get("x-correlation-id") or sent


def _parse_ocr_text(parse_result: Any) -> Any:
    """OCR text from a parse job result. The parse envelope nests its payload
    under `result` (parse_result["result"]["ocr"]); a flattened shape would put
    it at the top level. None if absent. See BUG-04."""
    if not isinstance(parse_result, dict):
        return None
    inner = parse_result.get("result")
    if isinstance(inner, dict) and "ocr" in inner:
        return inner["ocr"]
    return parse_result.get("ocr")


def _edit_fill_body(
    detect_job_id: str,
    *,
    values: dict | list | None,
    content: str | None,
    natural_language: bool,
) -> dict[str, Any]:
    """Build (and pre-validate) the JSON body for ``POST /v1/edit/fill``.

    The server rejects both mode/field mismatches with a 400; catching them
    here turns a round-trip into an immediate ``ValueError``. Shared by both
    clients so the two stay in lockstep.
    """
    if natural_language:
        if not (content or "").strip():
            raise ValueError("natural_language=True requires non-empty content")
        return {
            "detect_job_id": detect_job_id,
            "natural_language": True,
            "content": content,
        }
    if values is None:
        raise ValueError(
            "natural_language=False requires values "
            '({"0": "Jane"} or [{"index": 0, "value": "Jane"}])'
        )
    return {
        "detect_job_id": detect_job_id,
        "natural_language": False,
        "values": values,
    }


def _pages_of(result: Any) -> list[dict]:
    """Find the page list in any op's result, whichever nesting it arrived in.

    ``edit_detect``/``edit_fill``/``parse`` return the platform envelope, so the pages sit at
    ``result["result"]["pages"]``; the composite ``edit()`` flattens them to ``result["pages"]``.
    Accepting both means callers can hand us whatever they just got back.
    """
    if not isinstance(result, dict):
        raise ValueError("Expected the dict returned by edit_detect/edit_fill/edit/parse")
    for candidate in (result.get("result"), result):
        if isinstance(candidate, dict) and isinstance(candidate.get("pages"), list):
            return candidate["pages"]
    raise ValueError(
        "No 'pages' in this result. Pass the value returned by edit_detect(), edit_fill(), "
        "edit(), or parse(include_image=True)."
    )


def _page_number(page: dict, fallback: int) -> int:
    """edit pages are keyed `page`; parse pages are keyed `page_number`."""
    for key in ("page", "page_number"):
        value = page.get(key)
        if isinstance(value, int):
            return value
    return fallback


def _image_suffix(url: str) -> str:
    """Extension from the URL path — edit serves .png, parse serves .webp. Never guess:
    writing webp bytes into a .png name breaks anything that trusts the extension."""
    stem = url.split("?", 1)[0].rsplit("/", 1)[-1]
    suffix = Path(stem).suffix.lower()
    return suffix if suffix in {".png", ".webp", ".jpg", ".jpeg", ".tif", ".tiff"} else ".png"


def _page_targets(result: Any, dest_dir: str | Path, prefix: str) -> list[tuple[str, Path]]:
    """Pair each page's presigned URL with the local path it should be written to."""
    pages = _pages_of(result)
    dest = Path(dest_dir)
    out: list[tuple[str, Path]] = []
    for i, page in enumerate(pages):
        page = page or {}
        url = page.get("image_url")
        if not url:
            raise ValueError(
                f"Page {_page_number(page, i + 1)} has no 'image_url' to download. "
                "For parse(), pass include_image=True."
            )
        out.append((url, dest / f"{prefix}-{_page_number(page, i + 1)}{_image_suffix(url)}"))
    return out


def _download_error(page_path: Path, status: int) -> HyperAPIError:
    hint = ""
    if status in (403, 401):
        # SigV4 presigned URLs live ~15 min; the job itself survives 24 h.
        hint = (" The presigned URL has most likely expired — re-poll the job with "
                "get_job(job_id) for fresh URLs, then download again.")
    return HyperAPIError(f"Could not download {page_path.name} (HTTP {status}).{hint}",
                         status_code=status)


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


def _rate_limit_wait(err: RateLimitError, remaining: float) -> float | None:
    """Decide how to handle a 429 raised *during a poll loop*.

    The gateway rate-limits job-status polls in the same per-org bucket as the
    submit — so on a low tier (free = 1 req / 60 s) the submit spends the token
    and the first poll(s) 429. That's transient: the window clears, so the poll
    loop should sleep ``Retry-After`` and resume rather than fail the whole wait.

    Returns the seconds to sleep before re-polling, or ``None`` when the job's
    remaining deadline can't outlast the rate window — in which case the caller
    re-raises the ``RateLimitError`` (it still carries ``retry_after`` so the
    user can wait and resume via ``submit_<op>`` + ``wait_for_job``). Shared by
    both clients so the sync/async decision can't drift; each loop supplies its
    own sleep primitive.
    """
    if remaining <= 0:
        return None
    # `retry_after` defaults to 60 and is floored at 0 by _parse_retry_after;
    # floor to 1 here so a server-sent "retry now" (0) can't hot-spin the loop.
    # `... or 0` guards a None from any future RateLimitError construction path.
    wait = max(err.retry_after or 0, 1)
    if wait > remaining:
        return None
    # Add a little jitter so a concurrent async fan-out (asyncio.gather over N
    # jobs) doesn't wake every poller on the same tick and re-poll in a
    # synchronized burst. Capped so wait+jitter never exceeds the budget.
    jitter = random.uniform(0.0, min(0.5, remaining - wait))
    return float(wait) + jitter


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

    def _resolve_schema(self, schema: dict | str | Path | None) -> dict | None:
        """Accept a schema as an already-loaded dict, a path to a JSON file,
        or a raw pasted JSON string — whichever is most convenient for the
        caller. Returns None unchanged (no schema given)."""
        if schema is None or isinstance(schema, dict):
            return schema
        raw = str(schema)
        path = Path(raw)
        if path.exists():
            raw = path.read_text()
        try:
            parsed = json.loads(raw)
        except ValueError as e:
            raise ValueError(f"schema must be a dict, a path to a JSON file, or a JSON string: {e}")
        if not isinstance(parsed, dict):
            raise ValueError("schema must resolve to a JSON object")
        return parsed

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
            document_key = self.upload_document(file_path, content_type=content_type)
            response = self._client.post(
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

    def _submit_json(
        self,
        endpoint: str,
        op_name: str,
        body: dict[str, Any],
        *,
        timeout: float | None = None,
    ) -> Job:
        """Submit a pipeline op whose request body is JSON and carries no file.

        Only ``/v1/edit/fill`` uses this today: it references a prior detect
        job by id rather than uploading anything, so neither of the
        form-data + ``document_key`` helpers above fits.
        """
        request_timeout = timeout or self.timeout
        error_cls = _OP_TO_ERROR[op_name]
        headers = self._get_headers(async_mode=True)
        request_id = headers["X-Request-ID"]

        try:
            response = self._client.post(
                f"{self.base_url}{endpoint}",
                json=body,
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
            try:
                envelope = self._poll_with_retry(job_id)
            except RateLimitError as e:
                # The poll shares the submit's per-org rate bucket; on low tiers
                # the submit spent the token so the poll 429s. Back off and
                # resume within the deadline instead of failing the wait.
                wait = _rate_limit_wait(e, deadline - time.monotonic())
                if wait is None:
                    raise
                logger.info(
                    "poll_rate_limited",
                    extra={"job_id": job_id, "retry_after": wait, "degraded": e.degraded},
                )
                time.sleep(wait)
                continue
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
                # Return only the `result` payload — never the raw envelope,
                # which carries server-side status/timing/receipt fields. Coerce
                # a missing/null result to {} so the return is always a dict:
                # `.get("result")` never hits None (a bare `["result"]` still
                # KeyErrors, but never the old None TypeError). Matches wait_for_jobs.
                res = envelope.get("result")
                return res if isinstance(res, dict) else {}
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
            rate_limited = False
            for pos, idx in enumerate(pending):
                try:
                    envelope = self._poll_with_retry(jobs_list[idx].job_id)
                except RateLimitError as e:
                    # Shared per-org rate bucket: one 429 means the window is
                    # spent for everyone this pass. Sleep Retry-After once, keep
                    # this and all not-yet-polled jobs pending, and retry.
                    wait = _rate_limit_wait(e, deadline - time.monotonic())
                    if wait is None:
                        raise
                    logger.info(
                        "poll_rate_limited",
                        extra={"job_id": jobs_list[idx].job_id, "retry_after": wait,
                               "degraded": e.degraded},
                    )
                    time.sleep(wait)
                    still_pending.extend(pending[pos:])
                    rate_limited = True
                    break
                status = envelope.get("status")
                if status == "completed":
                    # Return only the `result` payload (never the raw envelope,
                    # which carries status/timing/receipt fields); coerce a
                    # missing/null result to {} to preserve list position.
                    res = envelope.get("result")
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
            if rate_limited:
                # Already slept Retry-After above; don't double-sleep the poll gap.
                continue
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
        envelope's ``status_code``) is prefixed onto the message so
        ``str(e)`` is self-describing in support tickets without needing the
        caller to also surface ``e.status_code``.
        """
        raw = _strip_api_key(envelope.get("error")) or "Job failed"
        status_code = envelope.get("status_code")
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
        ``result["result"]["pages"]`` entry gains a ``structured`` dict with ``html``,
        ``markdown``, and ``regions``. Only meaningful on the default OCR
        engine; noticeably slower than ``"fast"`` but well within the default
        ``poll_timeout``.

        ``include_boxes=True`` adds per-segment bounding boxes to each entry of
        ``result["result"]["pages"]`` (standard OCR engine only; the layout-aware engine
        returns an empty list).

        ``include_image=True`` adds a presigned ``image_url`` + ``dimensions``
        to each ``result["result"]["pages"]`` entry, pointing to the deskew-corrected
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
        schema: dict | str | Path | None = None,
        use_presigned: bool = True,
    ) -> Job:
        """Submit a Basic extract job asynchronously and return immediately.

        ``category`` selects the Basic extractor: ``"financial"`` (default —
        the two-leg IDP adapter for invoices/receipts) or ``"non_financial"``
        (extract-fast — see ``schema`` below). For auto-detecting Advanced
        extraction, use :py:meth:`submit_extract_advanced`.

        ``parse_mode`` selects the Stage-1 OCR engine, independent of
        ``category``: ``"fast"`` (default, fast text extraction) or
        ``"advanced"`` (advanced layout-aware parsing for dense tables/forms —
        higher accuracy, slower, costs more; available on paid tiers).

        ``schema`` (``category="non_financial"`` only): a blank template —
        the exact shape you want back, with ``None``/``False``/``"unselected"``
        as placeholders, e.g. ``{"patient_name": None, "insurance":
        {"Medicare": "unselected", "Self-Pay": "unselected"}}``. Pass it as an
        already-loaded dict, a path to a JSON file, or a raw pasted JSON
        string — whichever's convenient. The response fills in that SAME
        structure from the document. Ephemeral — used for this call only,
        nothing is stored server-side; pass a different ``schema`` next call
        for a different shape, no merge/versioning to reason about. Omit
        entirely to use this org's stored default template instead (see
        :py:meth:`extract_fast`); prefer that method's name for this path —
        it exists for discoverability, this is the same call.
        """
        path = self._resolve_path(file_path)
        resolved_schema = self._resolve_schema(schema)
        data = {"schema": json.dumps(resolved_schema)} if resolved_schema is not None else None
        return self._submit_via_path(
            "/v1/extract", "extract", path,
            params={"category": category, "parse_mode": parse_mode},
            data=data,
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

    def submit_edit_detect(
        self,
        file_path: str | Path,
        *,
        markdown_assist: bool = False,
        use_presigned: bool = True,
    ) -> Job:
        """Submit a form-field detection job asynchronously and return immediately.

        Leg 1 of the two-call edit flow. Detects every blank fillable field on
        a form (PDF or image) and returns a ``form_schema`` plus rendered page
        images. Feed the resulting ``job_id`` to :py:meth:`submit_edit_fill`.

        ``markdown_assist=True`` routes detection through layout-aware parsing
        (markdown/form-block extraction) — more precise on dense forms, slower.

        Edit is metered **once, here**; the fill leg is included.
        """
        path = self._resolve_path(file_path)
        return self._submit_via_path(
            "/v1/edit/detect", "edit", path,
            params={"markdown_assist": markdown_assist},
            use_presigned=use_presigned,
        )

    def submit_edit_fill(
        self,
        detect_job_id: str,
        *,
        values: dict | list | None = None,
        content: str | None = None,
        natural_language: bool = False,
    ) -> Job:
        """Submit a form-fill job asynchronously and return immediately.

        Leg 2 of the two-call edit flow. References the completed detect job by
        id — the schema and page images stay server-side, so nothing heavy
        crosses the wire.

        Two mutually exclusive modes:

        - ``natural_language=False`` (default) — you supply every value in
          ``values``, either ``{"0": "Jane", "3": "X"}`` or
          ``[{"index": 0, "value": "Jane"}]``, where the key/index is the
          field's position in ``form_schema``. No model call, deterministic.
        - ``natural_language=True`` — ``content`` is free text that one model
          call maps onto the schema. The mapped ``fills`` come back in the
          result so a UI can show them for review and re-submit the corrected
          set with ``natural_language=False``.

        Raises:
            ValueError: If the supplied fields don't match the chosen mode.
        """
        body = _edit_fill_body(
            detect_job_id,
            values=values,
            content=content,
            natural_language=natural_language,
        )
        return self._submit_json("/v1/edit/fill", "edit", body)

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
            endpoint: One of ``/v1/parse``, ``/v1/extract``, ``/v1/classify``,
                ``/v1/split``, ``/v1/redact``.
            document_keys: Keys from :meth:`upload_document`, one per document.
            options: Task-specific options, forwarded to each document's run.
            parse_mode: ``fast`` (default) or ``advanced``, for ``/v1/parse`` only.
                ``advanced`` runs layout-aware parsing and requires a paid
                plan (Pro/Enterprise) — the API returns 403 otherwise. Batch
                advanced parse runs on the lowest-priority lane, yielding to
                interactive traffic, so it may take longer under load (24h SLA).
                Ignored by non-``/v1/parse`` endpoints.
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
            try:
                status = self.get_batch(batch_id)
            except RateLimitError as e:
                wait = _rate_limit_wait(e, deadline - time.monotonic())
                if wait is None:
                    raise
                logger.info(
                    "batch_poll_rate_limited",
                    extra={"batch_id": batch_id, "retry_after": wait, "degraded": e.degraded},
                )
                time.sleep(wait)
                continue
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

        The returned dict is the response envelope; the parse payload is nested
        one level under ``result`` — OCR text at ``result["result"]["ocr"]`` and
        the page list at ``result["result"]["pages"]``.

        Args:
            file_path: Path to the file (PDF, PNG, JPG, WEBP, TIFF, GIF).
            image_path: Deprecated alias for file_path.
            mode: ``"fast"`` (default — plain text + boxes) or ``"advanced"``
                (layout-aware structured OCR: each ``result["result"]["pages"]``
                entry gains a ``structured`` dict with ``html``, ``markdown``,
                and ``regions``). Advanced only applies on the default OCR
                engine and is noticeably slower.
            include_boxes: When True, each ``result["result"]["pages"]`` entry
                includes a ``boxes`` list of ``{"text", "bbox": [left, top, right,
                bottom], "confidence"}`` segments. Standard OCR engine only; the
                layout-aware engine returns [].
            include_image: When True, each ``result["result"]["pages"]`` entry
                includes an ``image_url`` (presigned GET for the deskew-corrected
                page) and ``dimensions`` (``{"width", "height"}`` in that image's
                pixel space, which matches the box coordinate space).
            use_presigned: Use the presigned-S3 upload flow (default True).
            poll_timeout: Override the constructor's poll_timeout for this call.
            poll_interval: Override the constructor's poll_interval for this call.

        Returns:
            Response envelope; OCR text at ``result["result"]["ocr"]`` and pages
            at ``result["result"]["pages"]``.
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
        schema: dict | str | Path | None = None,
        use_presigned: bool = True,
        poll_timeout: float | None = None,
        poll_interval: float | None = None,
    ) -> dict:
        """Extract structured data from a document.

        Submits asynchronously and polls until done. Bypasses any edge-timeout
        ceiling because each individual HTTP request stays sub-second.

        This is the Basic extractor. ``category="financial"`` (default) runs the
        two-leg IDP adapter (invoices/receipts). ``category="non_financial"``
        is extract-fast: no document-type detection at all (one org is
        assumed to send one kind of document) — pass ``schema``, a blank
        template with ``None``/``False``/``"unselected"`` as placeholders
        (e.g. ``{"patient_name": None, "insurance": {"Medicare":
        "unselected", "Self-Pay": "unselected"}}``, as a dict, a path to a
        JSON file, or a raw JSON string), and the response fills in that SAME
        structure from the document — nothing is stored server-side, pass a
        different ``schema`` next call for a different shape, no
        merge/versioning to reason about. Omit ``schema`` to use this org's
        stored default template instead (set up separately, offline — not
        through this SDK); with no schema and no stored default, this
        currently falls back to a generic extraction (a known gap, not yet
        unified with this flow). For auto-detecting Advanced extraction (no
        ``category`` needed), use :py:meth:`extract_advanced`.

        Returns:
            Response envelope. For ``category="financial"``: the structured
            payload is one level under ``result``:
            ``result["result"]["entities"]`` (header/party fields),
            ``result["result"]["line_items"]`` (per-line rows), and
            ``result["result"]["summary"]`` (document-level totals/tax/currency —
            e.g. the grand total lives here, ``summary["total_amount"]``, NOT in
            entities). For ``category="non_financial"`` (extract-fast): the
            filled-in template is at ``result["result"]["data"]`` (same shape
            as ``schema``, or the org's stored default). Raw OCR is at the
            envelope top level, ``result["ocr_text"]``, in both cases.
        """
        job = self.submit_extract(
            file_path,
            category=category,
            parse_mode=parse_mode,
            schema=schema,
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

        Returns:
            Response envelope. The prediction is one level under ``result``:
            ``result["result"]["document_type"]`` (e.g. ``"invoice"``), with
            ``confidence`` (a categorical label such as ``"very_high"``, not a
            float), ``domain``, and ``needs_review`` alongside it. Raw OCR is at
            the envelope top level in ``result["ocr_text"]``.
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

        Returns:
            Response envelope. The detected parts are at
            ``result["result"]["segmentation"]["splits"]`` — a list where each
            item has ``split_label``, ``start_page``, ``end_page``,
            ``confidence``, and ``sub_documents``. (Note the ``segmentation``
            wrapper: this is one level deeper than the other operations' payloads.)
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

    def edit_detect(
        self,
        file_path: str | Path,
        *,
        markdown_assist: bool = False,
        use_presigned: bool = True,
        poll_timeout: float | None = None,
        poll_interval: float | None = None,
    ) -> dict:
        """Detect the blank fillable fields on a form. Submits async + polls until done.

        Leg 1 of the two-call edit flow — pass the returned job's id to
        :py:meth:`edit_fill`. Because the fill leg needs that id, this method
        also stamps it onto the envelope as ``detect_job_id``.

        Returns:
            Response envelope. The payload is nested one level under
            ``result``: ``result["result"]["form_schema"]`` is a flat,
            page-numbered list of fields (**a field's position in that list is
            its fill index**), and ``result["result"]["pages"]`` is the page
            images the boxes are relative to.

        Note:
            Each page carries a presigned ``image_url``, not image bytes. Those
            URLs are short-lived — download them promptly, or re-poll the job
            with :py:meth:`get_job` to have the server mint fresh ones.
        """
        job = self.submit_edit_detect(
            file_path,
            markdown_assist=markdown_assist,
            use_presigned=use_presigned,
        )
        envelope = self.wait_for_job(job, timeout=poll_timeout, interval=poll_interval)
        # The server's envelope has no field pointing back at the job, but the
        # fill leg needs exactly that — surface it so callers don't have to
        # hold onto the Job separately.
        if isinstance(envelope, dict):
            envelope.setdefault("detect_job_id", job.job_id)
        return envelope

    def edit_fill(
        self,
        detect_job_id: str,
        *,
        values: dict | list | None = None,
        content: str | None = None,
        natural_language: bool = False,
        poll_timeout: float | None = None,
        poll_interval: float | None = None,
    ) -> dict:
        """Fill the fields found by a prior :py:meth:`edit_detect` call.

        See :py:meth:`submit_edit_fill` for the two modes. Not separately
        metered — the document was charged at detect time.

        Returns:
            Response envelope with ``result["result"]["fills"]`` (the
            index/value pairs that were drawn, including the model's mapping
            when ``natural_language=True``) and ``result["result"]["pages"]``
            (the rendered pages, as presigned ``image_url`` s).
        """
        job = self.submit_edit_fill(
            detect_job_id,
            values=values,
            content=content,
            natural_language=natural_language,
        )
        return self.wait_for_job(job, timeout=poll_timeout, interval=poll_interval)

    def edit(
        self,
        file_path: str | Path,
        *,
        content: str,
        markdown_assist: bool = False,
        poll_timeout: float | None = None,
        poll_interval: float | None = None,
    ) -> dict:
        """Detect and fill a form in one call, from free text.

        Chains :py:meth:`edit_detect` + :py:meth:`edit_fill`. ``content`` is
        free text that one model call maps onto the schema this call just
        detected.

        **There is deliberately no ``values=`` here.** Values are keyed by a
        field's position in ``form_schema``, and this call detects that schema
        for the first time — so the caller cannot know the indices yet, and
        detection is a model call whose field ordering is not stable between
        runs. To fill by index, run the two legs: :py:meth:`edit_detect` to see
        the schema, then :py:meth:`edit_fill` with ``values``. That is also the
        human-in-the-loop flow — show the detected fields, let the user correct
        the mapped values, re-render.

        Returns:
            ``{"detect_job_id": str, "form_schema": [...], "fills": [...],
            "pages": [...]}`` — ``pages`` being the *filled* pages. The
            ``detect_job_id`` is returned so a correction round can follow with
            :py:meth:`edit_fill` without re-detecting (and without re-paying).

        Raises:
            ValueError: If ``content`` is empty or whitespace.
        """
        # Check before the detect leg — that is the metered one.
        if not (content or "").strip():
            raise ValueError("edit() requires non-empty content=")

        detected = self.edit_detect(
            file_path,
            markdown_assist=markdown_assist,
            poll_timeout=poll_timeout,
            poll_interval=poll_interval,
        )
        detect_job_id = detected["detect_job_id"]
        filled = self.edit_fill(
            detect_job_id,
            content=content,
            natural_language=True,
            poll_timeout=poll_timeout,
            poll_interval=poll_interval,
        )
        detect_payload = detected.get("result") or {}
        fill_payload = filled.get("result") or {}
        return {
            "detect_job_id": detect_job_id,
            "form_schema": detect_payload.get("form_schema") or [],
            "fills": fill_payload.get("fills") or [],
            "pages": fill_payload.get("pages") or [],
        }

    def download_pages(
        self,
        result: dict,
        dest_dir: str | Path,
        *,
        prefix: str = "page",
    ) -> list[Path]:
        """Download a result's page images to a folder.

        Works with whatever you just got back — :py:meth:`edit_detect` (the blank detected
        pages), :py:meth:`edit_fill` / :py:meth:`edit` (the filled ones), or
        :py:meth:`parse` with ``include_image=True``. Files are written as
        ``<dest_dir>/<prefix>-<page number>.png``; the directory is created if needed.

        Page images are served as **short-lived presigned URLs** (~15 min), so download
        promptly. If they have expired, re-poll with :py:meth:`get_job` — the server mints
        fresh URLs for the full 24 h the job lives — and call this again.

        Args:
            result: The dict returned by an op that produced page images.
            dest_dir: Folder to write into. Created if it does not exist.
            prefix: Filename stem; defaults to ``page`` → ``page-1.png``.

        Returns:
            The written paths, in page order.

        Raises:
            ValueError: If the result carries no pages, or a page has no ``image_url``.
            HyperAPIError: If a download fails — an expired URL reports as such.
        """
        targets = _page_targets(result, dest_dir, prefix)
        Path(dest_dir).mkdir(parents=True, exist_ok=True)

        written: list[Path] = []
        for url, path in targets:
            # Presigned URLs carry their own SigV4 signature — send no API-key headers.
            # follow_redirects: S3 answers a cross-region GET with a 307 to the regional
            # endpoint, which would otherwise surface as a baffling "HTTP 307".
            resp = self._client.get(url, follow_redirects=True)
            if resp.status_code >= 400:
                raise _download_error(path, resp.status_code)
            path.write_bytes(resp.content)
            written.append(path)
        return written

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
            "ocr": _parse_ocr_text(parse_result),
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
