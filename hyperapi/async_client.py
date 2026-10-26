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
    _cancelled_job_error,
    _download_error,
    _edit_fill_body,
    _edit_unavailable_error,
    _error_fields,
    _page_targets,
    _parse_ocr_text,
    _parse_params,
    _rate_limit_error_from,
    _rate_limit_wait,
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
            base_url or os.environ.get("HYPERAPI_URL") or "https://apis.hyperbots.com"
        ).rstrip("/")
        self.timeout = timeout
        self._poll_interval = poll_interval
        self._poll_timeout = poll_timeout
        self._poll_max_transient_retries = poll_max_transient_retries
        self._poll_transient_retry_delay = poll_transient_retry_delay

        from . import __version__  # local import to avoid circular at module load

        py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        # Note the "-async" suffix in the User-Agent so server-side analytics can
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
        return (
            f"AsyncHyperAPIClient(api_key='hk_***{tail}', base_url='{self.base_url}')"
        )

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

    def _resolve_schema(self, schema: dict | str | Path | None) -> dict | None:
        """Accept a schema as an already-loaded dict, a path to a JSON file,
        or a raw pasted JSON string — whichever is most convenient for the
        caller. Returns None unchanged (no schema given)."""
        if schema is None or isinstance(schema, dict):
            return schema

        # A Path, or a string that is not JSON text, is a filesystem path — and
        # a path that does not exist must say so. Previously any missing path
        # fell through to json.loads("/some/missing.json") and reported "must be
        # a dict, a path to a JSON file, or a JSON string", pointing the caller
        # at the wrong problem. _resolve_path (the sibling helper for documents)
        # already raises FileNotFoundError here.
        if isinstance(schema, Path):
            if not schema.exists():
                raise FileNotFoundError(f"Schema file not found: {schema}")
            raw = schema.read_text()
        else:
            raw = str(schema)
            # Treat the string as a FILE REFERENCE only when it plausibly is
            # one: a single line that either ends in .json or actually exists.
            # Everything else goes to json.loads, so free-form text (markdown,
            # prose, a bare array) is reported as invalid JSON rather than as a
            # missing file — naming the real rule instead of the wrong problem.
            stripped = raw.strip()
            looks_like_path = (
                "\n" not in stripped
                and len(stripped) < 4096
                and (stripped.lower().endswith(".json") or Path(stripped).exists())
            )
            if looks_like_path:
                path = Path(stripped)
                if not path.exists():
                    raise FileNotFoundError(f"Schema file not found: {stripped}")
                raw = path.read_text()

        try:
            parsed = json.loads(raw)
        except ValueError as e:
            raise ValueError(
                f"schema must be a dict, a path to a JSON file, or a JSON string: {e}"
            )
        if not isinstance(parsed, dict):
            raise ValueError("schema must resolve to a JSON object")
        return parsed

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
            content_type = CONTENT_TYPES.get(
                path.suffix.lower(), "application/octet-stream"
            )

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

        # Step 1 — request an authenticated presigned upload URL.
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

        # Step 2 — upload bytes directly to object storage using the signed URL.
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
            raise DocumentUploadError(
                f"S3 upload failed: {e}", request_id=request_id
            ) from e

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

    # ── Limits ───────────────────────────────────────────────────────────

    async def get_limits(self) -> dict:
        """This key's per-task ceilings (``GET /v1/limits``) — a pure read.

        Limits vary by plan, so pre-flight against this rather than hard-coding
        a number. The response is::

            {
              "max_upload_bytes": int,            # what upload itself admits
              "parse_fast":      {"max_bytes": int, "max_pages": int | None},
              "parse_advanced":  {"max_bytes": int, "max_pages": int | None},
              "bulk_parse":      {...},            # only when offered to you
              "extract_fast":    {...},
              "extract_advanced":{...},            # only when offered to you
              "classify":        {...},
              "split":           {...},
              "redact":          {"max_bytes": int, "max_pages": int | None,
                                  "urls_max_pages": int | None}
            }

        ``max_pages`` is ``None`` where the task does not reject on page
        count. The optional blocks (``bulk_parse``, ``extract_advanced``) are
        absent when that mode is not available to this key — absent means
        "not offered", never "unlimited". Likewise ``redact.urls_max_pages``
        is present only while ``output="urls"`` is accepted; its presence is
        the feature detection for that mode (see :py:meth:`submit_redact`).

        Returns:
            The decoded JSON body as a dict.

        Raises:
            AuthenticationError: 401 (bad key).
            RateLimitError: 429.
            HyperAPIError: Any other ≥400, or a malformed body.
        """
        headers = self._get_headers()
        request_id = headers["X-Request-ID"]

        try:
            resp = await self._client.get(
                f"{self.base_url}/v1/limits", headers=headers
            )
        except httpx.RequestError as e:
            raise HyperAPIError(f"Limits request failed: {e}", request_id=request_id) from e

        rid = _request_id_of(resp, request_id)
        if resp.status_code == 401:
            raise AuthenticationError(
                "Invalid API key.", status_code=401, request_id=rid
            )
        if resp.status_code == 429:
            raise _rate_limit_error_from(resp, rid)
        if resp.status_code >= 400:
            raise HyperAPIError(
                _server_message(resp, f"Limits request failed (HTTP {resp.status_code})"),
                status_code=resp.status_code,
                request_id=rid,
                **_error_fields(resp),
            )
        try:
            body = resp.json()
            if not isinstance(body, dict):
                raise TypeError(f"body is {type(body).__name__}, expected object")
            return body
        except (json.JSONDecodeError, ValueError, TypeError) as e:
            raise HyperAPIError(
                f"Malformed limits response: {e}",
                status_code=200,
                request_id=rid,
            ) from e

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
                **_error_fields(resp),
            )
        if sc == 429:
            raise _rate_limit_error_from(resp, rid)
        if sc == 413:
            raise DocumentUploadError(
                _server_message(resp, "File exceeds upload size limit"),
                status_code=413,
                request_id=rid,
                **_error_fields(resp),
            )
        if sc == 415:
            raise DocumentUploadError(
                _server_message(resp, "Unsupported file type"),
                status_code=415,
                request_id=rid,
                **_error_fields(resp),
            )
        if sc == 408:
            raise error_cls(
                _server_message(resp, "Service deadline exceeded"),
                status_code=408,
                request_id=rid,
                **_error_fields(resp),
            )
        if sc == 403:
            raise HyperAPIError(
                _server_message(resp, "Forbidden"),
                status_code=403,
                request_id=rid,
                **_error_fields(resp),
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
        force_refresh: bool = False,
        data: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Job:
        """Submit a pipeline op asynchronously. Returns a Job handle.

        ``force_refresh=True`` adds ``?force_refresh=true``, which makes the
        API reprocess the document instead of returning the stored result
        (results are kept for 24 hours). Without it an identical (document +
        task + params) returns the STORED result, so a retry can look like a
        no-op. Only send it when the caller explicitly asked to re-run: a
        reprocess is billed at full price, where a stored result costs nothing.

        Omitted entirely when False so an ordinary request is byte-identical to
        before this parameter existed.

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
            document_key = await self.upload_document(
                file_path, content_type=content_type
            )
            response = await self._client.post(
                f"{self.base_url}{endpoint}",
                data={"document_key": document_key, **extra_form},
                params={**params, "force_refresh": "true"} if force_refresh else params,
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
            unavailable = _edit_unavailable_error(
                response, endpoint, _request_id_of(response, request_id)
            )
            if unavailable is not None:
                raise unavailable
            raise error_cls(
                _server_message(response, f"{op_name} submission failed"),
                status_code=response.status_code,
                request_id=_request_id_of(response, request_id),
                **_error_fields(response),
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
            unavailable = _edit_unavailable_error(
                response, endpoint, _request_id_of(response, request_id)
            )
            if unavailable is not None:
                raise unavailable
            raise error_cls(
                _server_message(response, f"{op_name} submission failed"),
                status_code=response.status_code,
                request_id=_request_id_of(response, request_id),
                **_error_fields(response),
            )

        return self._job_from_envelope(response.json(), op=op_name)

    async def _submit_json(
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
            response = await self._client.post(
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
            unavailable = _edit_unavailable_error(
                response, endpoint, _request_id_of(response, request_id)
            )
            if unavailable is not None:
                raise unavailable
            raise error_cls(
                _server_message(response, f"{op_name} submission failed"),
                status_code=response.status_code,
                request_id=_request_id_of(response, request_id),
                **_error_fields(response),
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
            raise HyperAPIError(f"Job poll failed: {e}", request_id=request_id) from e

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
            # Surface poll-time and submit-time 429 responses consistently.
            raise _rate_limit_error_from(resp, rid)
        if resp.status_code >= 400:
            raise HyperAPIError(
                _server_message(resp, f"Poll failed (HTTP {resp.status_code})"),
                status_code=resp.status_code,
                request_id=rid,
                **_error_fields(resp),
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

        Args:
            limit: Maximum rows to return. Valid range [1, 100]; out-of-range
                values are NOT clamped — the server silently falls back to
                the default 20 (e.g. ``limit=200`` returns 20 rows, not 100).
            source: Optional filter — ``"api"`` or ``"playground"``. Unknown
                values are ignored server-side (no error).

        Returns:
            A list of summary dicts (``job_id``, ``request_id``, ``status``,
            ``task``, ``endpoint``, ``pages``, ``filename``, ``document_key``,
            ``source``, ``is_test``, ``created_at``, ``completed_at``,
            ``error``).
            ``status`` may be ``"cancelled"`` for jobs stopped via
            :py:meth:`delete_job`. Summaries never include results — fetch
            the full envelope for one job with :py:meth:`get_job`.

            ``document_key`` is the server-side handle for that job's
            document, and is what to match on when reconciling a batch —
            ``filename`` is whatever was sent at upload and carries no
            uniqueness guarantee, so fifty documents named ``invoice.pdf``
            yield fifty rows you cannot tell apart. Note that ``submit_*``
            uploads internally and does not surface the ``document_key`` it
            used, and no public method accepts a pre-uploaded one, so retain
            the ``job_id`` if you need your own file→job map. ``None`` on older
            jobs.
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
                _server_message(
                    resp, f"Recent-jobs request failed (HTTP {resp.status_code})"
                ),
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

        Despite the HTTP verb, the server semantics are *cancel*, not erase:
        a pending or running job is stopped; a job that already completed or
        failed is left untouched and keeps its result. Idempotent — missing,
        expired, and already-terminal job ids all return success, so calling
        this twice is safe.

        How a cancelled job reports afterwards: ``GET /v1/jobs/{job_id}``
        (:py:meth:`get_job`) returns ``status="failed"`` with an ``error``
        beginning ``"cancelled:"`` — there is no separate ``"cancelled"``
        status on the job itself. :py:meth:`wait_for_job` and
        :py:meth:`wait_for_jobs` decode that and raise
        :class:`~hyperapi.JobCancelledError` rather than the per-op failure
        class. The summaries from :py:meth:`list_recent_jobs` do show such
        jobs as ``status="cancelled"``.

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
            raise HyperAPIError(f"Job cancel failed: {e}", request_id=request_id) from e

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
        raise HyperAPIError(
            "Poll exhausted retries with no response"
        )  # pragma: no cover

    async def wait_for_job(
        self,
        job: Job | str,
        *,
        timeout: float | None = None,
        interval: float | None = None,
    ) -> dict:
        """Poll a job until it completes, fails, or the timeout elapses.

        Yields to the event loop between polls (``await asyncio.sleep``)
        instead of blocking the thread; otherwise identical to the sync client.

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
            JobCancelledError: If the job was cancelled via
                :py:meth:`delete_job` while this call was waiting (the job
                reports ``status=failed`` with an ``error`` beginning
                ``"cancelled:"``; this is decoded for you).
            RateLimitError: If a poll is answered with a 429 that this call
                cannot wait out — either ``retry_after`` exceeds the remaining
                budget, or the 429 is an exhausted monthly page quota
                (``e.is_quota_exhausted``), which waiting would not clear.
                Ordinary rate-window 429s are absorbed by backing off.
            HyperAPIError (or subclass): If the job ends with ``status=failed``,
                or the poll itself fails after exhausting transient retries.
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
            try:
                envelope = await self._poll_with_retry(job_id)
            except RateLimitError as e:
                # The API rate-limits polls and submits together; on the free
                # tier the submit spent the allowance so the poll 429s. Back off and
                # resume within the deadline instead of failing the wait.
                wait = _rate_limit_wait(e, deadline - time.monotonic())
                if wait is None:
                    raise
                logger.info(
                    "poll_rate_limited",
                    extra={
                        "job_id": job_id,
                        "retry_after": wait,
                        "degraded": e.degraded,
                    },
                )
                await asyncio.sleep(wait)
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

        # Each task waits up to `budget`. `gather` propagates the first failure
        # but does NOT cancel its siblings — so on failure we cancel them
        # explicitly. This matters more now that a rate-limited poll backs off
        # (sleeps Retry-After) instead of fast-failing: without the cancel, one
        # failed/timed-out job would leave the others polling for up to the full
        # poll_timeout. Cancelling gives the sync round-robin's "first failure
        # wins; pending jobs abandoned" semantics.
        budget = timeout if timeout is not None else self._poll_timeout

        tasks = [
            asyncio.ensure_future(
                self.wait_for_job(j, timeout=budget, interval=interval)
            )
            for j in jobs_list
        ]
        try:
            results = await asyncio.gather(*tasks)
        except BaseException:
            for t in tasks:
                t.cancel()
            # Drain the cancellations so they don't surface as "Task exception
            # was never retrieved" on the event loop.
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

        # `wait_for_job` returns the envelope's `result` payload ({} if missing
        # or null). Coerce non-dicts to {} so positional ordering is preserved
        # exactly like the sync version.
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
        cancelled = _cancelled_job_error(envelope, job_id)
        if cancelled is not None:
            return cancelled
        raw = _strip_api_key(envelope.get("error")) or "Job failed"
        status_code = envelope.get("status_code")
        message = f"(HTTP {status_code}) {raw}" if status_code else raw
        cls = _OP_TO_ERROR.get(op, HyperAPIError)
        return cls(
            message,
            status_code=status_code,
            request_id=envelope.get("request_id"),
            code=envelope.get("error_code")
            if isinstance(envelope.get("error_code"), str)
            else None,
        )

    # ── Public submit_<op> methods ───────────────────────────────────────

    async def submit_parse(
        self,
        file_path: str | Path | None = None,
        *,
        image_path: str | Path | None = None,
        mode: ParseMode | None = None,
        include_boxes: bool | None = None,
        include_image: bool | None = None,
        include_layout: bool | None = None,
        parse_figures: str | None = None,
        groups_profile: str | None = None,
        force_refresh: bool = False,
        use_presigned: bool = True,
    ) -> Job:
        """Submit a parse job asynchronously and return immediately.

        ``image_path`` is a deprecated alias for ``file_path`` retained for
        v0.1.x backward compatibility — pass exactly one.

        Omit ``mode`` to use the organization default (platform default:
        ``"fast"``). ``mode="advanced"`` explicitly runs layout-aware structured OCR: each
        ``result["result"]["pages"]`` entry gains a ``structured`` dict with ``html``,
        ``markdown``, and ``regions``. Only meaningful on the default OCR
        engine; noticeably slower than ``"fast"`` but, for documents under the
        page cap, well within the default ``poll_timeout``.

        Output flags
        ------------
        ``include_boxes``, ``include_image`` and ``include_layout`` are
        tri-state. **Leave one at ``None`` (the default) and the SDK does not
        send it, so the API's own default applies — which, in ``"fast"`` mode,
        is ON for all three.** Pass ``False`` to opt out of that piece of
        output explicitly; pass ``True`` to ask for it regardless of the
        server default. Only a flag you set reaches the wire.

        ``include_boxes`` controls, on each entry of ``result["result"]["pages"]``,
        a ``boxes`` list of line boxes (``{"text", "bbox": [left, top, right,
        bottom], "confidence"}``) and, for PDF, XPS and image inputs, a
        ``word_boxes`` list with one entry per word carrying typography:
        ``font``, ``size`` (in ``page_size["unit"]``), ``color``, ``bold``,
        ``italic``, ``background`` (only where it differs from the page) and
        ``font_source`` (``"document"`` when declared by the file's text layer,
        ``"model"`` when estimated from the pixels, which can be wrong). Keys are
        omitted when not stated; ``word_boxes`` is ``[]`` when none were
        produced. Box coordinates are pixels in the page's ``dimensions``
        space. Office documents (docx, pptx) return ``boxes`` only with page
        images on; ``mode="advanced"`` returns no boxes.

        PDF, XPS and image pages from ``mode="fast"`` always carry
        ``dimensions`` (``{"width", "height"}`` in pixels), ``page_size``
        (``{"width", "height", "unit"}``, ``"pt"`` for PDF/XPS, ``"px"`` for
        images) and ``background`` (the page's background colour, hex).

        ``include_image`` controls a presigned ``image_url`` on each
        ``result["result"]["pages"]`` entry, pointing to the deskew-corrected
        page image (plus ``dimensions`` where the page does not already carry
        it). The URL is valid for about 15 minutes; a page whose image cannot
        be presigned is returned without ``image_url`` rather than failing.

        ``include_layout`` (``"fast"`` mode only) controls, per page, ``layout``
        (the detected regions in reading order, ``bbox`` in the same pixel
        space as ``boxes``) and ``layout_text``, plus ``result["result"]
        ["layout_text"]``, the text rebuilt around the regions. ``False`` also
        turns figure parsing off. ``mode="advanced"`` has its own
        ``structured`` layout; sending ``include_layout=True`` with it is a
        ``400``.

        ``parse_figures`` (``"fast"`` mode, needs the layout on) is a
        comma-separated list of region kinds to parse further — any of
        ``"image"``, ``"chart"``, ``"table"`` (e.g. ``"table"`` or
        ``"image,chart,table"``). Each matching region gains ``content`` (a
        chart's data as a markdown table, an image's caption or text, a table
        as HTML) and a ``parse_status``; ``result["result"]["figure_parse"]``
        summarises the run. Best-effort: OCR and layout are returned even when
        a region cannot be parsed. Omitted, the API parses all three kinds
        when the layout is on. Pass ``"none"`` to opt out. An explicit list
        with ``include_layout=False`` is a ``400``.

        ``groups_profile`` (``"fast"`` mode, layout on) selects how the
        document outline in ``result["result"]["groups"]`` is built: ``"A"``
        (the API default — every block in exactly one group, the tree in
        reading order, a ``preamble`` group before the first heading, one
        ``page_header`` / ``page_footer`` group per page) or ``"B"`` (headings
        ranked by measured type size, running titles marked ``furniture:
        true``, captions bound to their figure or table, tables joined across
        pages, no ``list`` groups; not in reading order — use
        ``blocks[].order``). Case-insensitive; unknown values are treated as
        ``"A"``. ``result["result"]["metadata"]["groups_profile"]`` reports the
        profile applied.

        ``force_refresh=True`` re-runs OCR instead of returning the stored
        result. Parse results are cached as OCR output, so what this
        bypasses is the OCR output itself, for 24 hours. Without it a retry
        after a bad parse returns the same bad parse for the rest of the day.
        Covers both ``mode="fast"`` and ``mode="advanced"``, which sit in
        separate cache namespaces. A forced run is billed at full price where
        a cached one costs nothing, so send it only when the caller explicitly
        asked to re-run.

        Large documents
        ---------------
        ``"fast"`` mode has no page limit. ``"advanced"`` mode accepts up to 60
        pages per the public reference (500 for eligible plans when submitted
        asynchronously, as this method always does) and answers ``413`` above
        that; the byte cap and the exact page figures for *your* key are
        returned by :py:meth:`get_limits` (``parse_advanced`` and, when
        offered, ``bulk_parse``). The server does not report which condition
        kept a document out of the larger allowance.

        Such a document runs as page-range segments but stays ONE job. Its
        completed result differs in shape, so branch on it:

        - ``pages`` / ``ocr`` are NOT inline. The payload is delivered via a
          presigned ``result["result_url"]``, re-signed on every poll and valid
          for minutes — fetch it promptly or re-poll for a fresh one.
        - ``result["metadata"]["gaps"]`` lists page ranges that failed after
          retries. The document still completes; only pages that actually
          processed are billed. Always present, empty when nothing failed.
        - Segment progress (``segments_total`` / ``segments_done`` /
          ``segments_failed``) sits on the job envelope from
          :py:meth:`get_job`, not on the result :py:meth:`wait_for_job` returns.

        These can run past the default 3600 s ``poll_timeout``. Exceeding it
        raises ``JobTimeoutError`` but does NOT stop the job — re-attach with
        :py:meth:`get_job` (jobs live 24 h) or pass a larger ``poll_timeout``.
        """
        path = self._resolve_path(file_path, image_path)
        return await self._submit_via_path(
            "/v1/parse",
            "parse",
            path,
            params=_parse_params(
                mode=mode,
                include_boxes=include_boxes,
                include_image=include_image,
                include_layout=include_layout,
                parse_figures=parse_figures,
                groups_profile=groups_profile,
            ),
            force_refresh=force_refresh,
            use_presigned=use_presigned,
        )

    async def submit_extract(
        self,
        file_path: str | Path,
        *,
        category: ExtractCategory | None = None,
        parse_mode: ParseMode | None = None,
        schema: dict | str | Path | None = None,
        force_refresh: bool = False,
        use_presigned: bool = True,
    ) -> Job:
        """Submit a Basic extract job asynchronously and return immediately.

        ``category`` selects the Basic extractor. Omit it to use the organization
        default (platform default: ``"financial"``). ``"financial"`` (
        the built-in invoice and receipt fields) or ``"non_financial"``
        (extract-fast — see ``schema`` below). For auto-detecting Advanced
        extraction, use :py:meth:`submit_extract_advanced`.

        ``parse_mode`` explicitly selects the OCR engine, independent of
        ``category``: ``"fast"`` (fast text extraction) or
        ``"advanced"`` (advanced layout-aware parsing for dense tables/forms —
        higher accuracy, slower, costs more; available on paid tiers). Omit it
        to use the organization default (platform default: ``"fast"``).

        ``schema`` (``category="non_financial"`` only): a blank template —
        the exact shape you want back, with ``None``/``False``/``"unselected"``
        as placeholders, e.g. ``{"patient_name": None, "insurance":
        {"Medicare": "unselected", "Self-Pay": "unselected"}}``. Pass it as an
        already-loaded dict, a path to a JSON file, or a raw pasted JSON
        string. The response fills in that SAME structure from the document.
        Ephemeral — used for this call only, nothing is stored server-side;
        pass a different ``schema`` next call for a different shape, no
        merge/versioning to reason about. Omit to use this org's stored
        default template instead (set up separately, offline).
        """
        path = self._resolve_path(file_path)
        resolved_schema = self._resolve_schema(schema)
        # The API applies `schema` to non_financial only; on financial it is
        # ignored server-side. Silently dropping it would return plausible
        # output shaped by a different extractor, with nothing to tell the
        # caller their template never applied — so this fails loudly instead.
        if resolved_schema is not None and category == "financial":
            raise ValueError(
                'schema applies to category="non_financial" only '
                f'(got category="{category}")'
            )
        # A schema with no declared category: send non_financial explicitly rather
        # than deferring. The SDK cannot read the organization default (that endpoint is
        # dashboard-only, unreachable with an API key), so deferring would risk resolving
        # to financial — where the server ignores `schema` silently and the caller
        # gets invoice-shaped output with no indication their template was dropped.
        # Passing a schema is only meaningful on non_financial, so inferring it is
        # faithful to the caller's intent and closes the one undetectable failure.
        effective_category = (
            "non_financial" if category is None and resolved_schema is not None else category
        )
        data = (
            {"schema": json.dumps(resolved_schema)}
            if resolved_schema is not None
            else None
        )
        return await self._submit_via_path(
            "/v1/extract",
            "extract",
            path,
            params={
                # Omitted when None so the server-side default applies — same
                # mechanism `parse_mode` below already uses. Sending a value
                # unconditionally is what made an organization's configured
                # default category unreachable from the SDK.
                **({"category": effective_category} if effective_category is not None else {}),
                **({"parse_mode": parse_mode} if parse_mode is not None else {}),
            },
            data=data,
            force_refresh=force_refresh,
            use_presigned=use_presigned,
        )

    async def submit_extract_advanced(
        self,
        file_path: str | Path,
        *,
        parse_mode: ParseMode | None = None,
        force_refresh: bool = False,
        use_presigned: bool = True,
    ) -> Job:
        """Submit an Advanced extract job asynchronously and return immediately.

        Advanced extraction auto-detects the document type (no ``category``
        needed): invoice-family documents use the built-in invoice and receipt
        fields, everything else schema-less grounded extraction. Returns the same
        envelope shape as :py:meth:`submit_extract`.

        ``parse_mode`` selects the OCR engine (``"fast"``/``"advanced"``,
        same meaning as :py:meth:`submit_extract`).

        ``force_refresh=True`` reprocesses instead of returning the stored
        result for an identical document and options (same meaning and same
        billing caveat as on :py:meth:`submit_extract`).
        """
        path = self._resolve_path(file_path)
        # op_name="extract" is deliberate error-taxonomy reuse (same ExtractError,
        # same response envelope) — NOT a typo. There is no "extract-omni" key in
        # _OP_TO_ERROR; do not "fix" this to one (it would KeyError).
        return await self._submit_via_path(
            "/v1/extract-omni",
            "extract",
            path,
            params={"parse_mode": parse_mode} if parse_mode is not None else {},
            force_refresh=force_refresh,
            use_presigned=use_presigned,
        )

    async def submit_classify(
        self,
        file_path: str | Path,
        *,
        mode: str = "default",
        options: dict | None = None,
        force_refresh: bool = False,
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
            "/v1/classify",
            "classify",
            path,
            params={"mode": mode},
            data=data,
            force_refresh=force_refresh,
            use_presigned=use_presigned,
        )

    async def submit_split(
        self,
        file_path: str | Path,
        *,
        mode: str = "default",
        options: dict | None = None,
        force_refresh: bool = False,
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
            "/v1/split",
            "split",
            path,
            params={"mode": mode},
            data=data,
            force_refresh=force_refresh,
            use_presigned=use_presigned,
        )

    async def submit_redact(
        self,
        file_path: str | Path,
        *,
        mode: str = "redact",
        pii_config: dict | None = None,
        include_logos: bool = False,
        output: str = "inline",
        force_refresh: bool = False,
        use_presigned: bool = True,
    ) -> Job:
        """Submit a redact/deidentify job asynchronously and return immediately.


        Inline output has a page cap; ``output="urls"`` raises it where your
        account offers that mode. Both figures vary by plan and are returned
        by :py:meth:`get_limits`: ``limits["redact"]["max_pages"]`` for inline
        and ``limits["redact"]["urls_max_pages"]`` for URL output (absent
        means URL output is not offered — feature-detect on that key).
        ``output="urls"`` returns masked pages as short-lived presigned links
        in ``result["pages"]`` (``{page_number, image_url}``) instead of inline
        base64 ``result["images"]`` — ``download_pages()`` accepts that shape
        directly. An over-cap document returns ``413`` up front, before any
        OCR is billed; ``output="urls"`` cannot be combined with
        ``include_logos`` (``400``).

        ``mode="redact"`` applies black boxes; ``mode="deidentify"`` overlays
        synthetic values. ``pii_config`` (``{"mode": "extend"|"replace",
        "types": [...]}``) customizes the detected PII types. Built-in types
        include PERSON_NAME, COMPANY_NAME, EMAIL, ADDRESS, WEBSITE, PHONE,
        and CREDENTIALS (passwords, API keys, tokens, secrets) — all with
        synthetic replacements, so each works in both modes.
        """
        if output not in ("inline", "urls"):
            # Fail loudly client-side: only the non-default literal goes on the
            # wire, so a typo ("url", "URLS") would otherwise silently run
            # inline at the inline page cap with no server-side rejection possible.
            raise ValueError(f'output must be "inline" or "urls", got {output!r}')
        path = self._resolve_path(file_path)
        data = (
            {"pii_config": json.dumps(pii_config)} if pii_config is not None else None
        )
        return await self._submit_via_path(
            "/v1/redact",
            "redact",
            path,
            params={"mode": mode, "include_logos": include_logos, **({"output": "urls"} if output == "urls" else {})},
            data=data,
            force_refresh=force_refresh,
            use_presigned=use_presigned,
        )

    async def submit_edit_detect(
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
        return await self._submit_via_path(
            "/v1/edit/detect",
            "edit",
            path,
            params={"markdown_assist": markdown_assist},
            use_presigned=use_presigned,
        )

    async def submit_edit_fill(
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
        return await self._submit_json("/v1/edit/fill", "edit", body)

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
            raise HyperAPIError(
                f"Batch request failed: {e}", request_id=request_id
            ) from e

        rid = _request_id_of(resp, request_id)
        if resp.status_code == 401:
            raise AuthenticationError(
                "Invalid API key.", status_code=401, request_id=rid
            )
        if resp.status_code == 404:
            raise HyperAPIError("Batch not found.", status_code=404, request_id=rid)
        if resp.status_code == 429:
            raise _rate_limit_error_from(resp, rid)
        if resp.status_code >= 400:
            raise HyperAPIError(
                _server_message(
                    resp, f"Batch request failed (HTTP {resp.status_code})"
                ),
                status_code=resp.status_code,
                request_id=rid,
            )
        try:
            return resp.json()
        except (json.JSONDecodeError, ValueError) as e:
            raise HyperAPIError(
                f"Malformed batch response: {e}",
                status_code=resp.status_code,
                request_id=rid,
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

        Returns immediately with ``{batch_id, status, total_items}``; the work
        runs on spare capacity. Poll :meth:`get_batch` (or :meth:`wait_for_batch`)
        for per-document results (each result lands in S3, keyed by ``doc_index``).

        Args:
            endpoint: One of ``/v1/parse``, ``/v1/extract``, ``/v1/classify``,
                ``/v1/split``, ``/v1/redact``.
            document_keys: Keys from :meth:`upload_document`, one per document.
            options: Task-specific options, forwarded to each document's run.
            parse_mode: ``fast`` (default) or ``advanced``, for ``/v1/parse`` only.
                ``advanced`` runs layout-aware parsing. Advanced parse may not
                be available for batch on your plan: the API then rejects the
                batch with a ``400`` or ``403`` whose message says why. Batch
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
        return await self._batch_request(
            "POST", "/v1/batch", json_body=body, extra_headers=extra
        )

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

    async def get_batch(
        self, batch_id: str, *, limit: int = 200, offset: int = 0
    ) -> dict:
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
            try:
                status = await self.get_batch(batch_id)
            except RateLimitError as e:
                wait = _rate_limit_wait(e, deadline - time.monotonic())
                if wait is None:
                    raise
                logger.info(
                    "batch_poll_rate_limited",
                    extra={
                        "batch_id": batch_id,
                        "retry_after": wait,
                        "degraded": e.degraded,
                    },
                )
                await asyncio.sleep(wait)
                continue
            if status.get("status") in self._BATCH_TERMINAL:
                return status
            if time.monotonic() >= deadline:
                raise JobTimeoutError(
                    f"Batch {batch_id} did not complete within {timeout}s.",
                    job_id=batch_id,
                    elapsed_s=time.monotonic() - start,
                )
            await asyncio.sleep(
                min(poll_interval, max(0.0, deadline - time.monotonic()))
            )

    # ── Public convenience methods (submit + wait) ───────────────────────

    async def parse(
        self,
        file_path: str | Path | None = None,
        *,
        image_path: str | Path | None = None,
        mode: ParseMode | None = None,
        include_boxes: bool | None = None,
        include_image: bool | None = None,
        include_layout: bool | None = None,
        parse_figures: str | None = None,
        groups_profile: str | None = None,
        force_refresh: bool = False,
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
            mode: Explicit ``"fast"`` or ``"advanced"``. Omit to use the
                organization default (platform default: ``"fast"``). Advanced
                is layout-aware structured OCR: each ``result["result"]["pages"]``
                entry gains a ``structured`` dict with ``html``, ``markdown``,
                and ``regions``). Advanced only applies on the default OCR
                engine and is noticeably slower.
            include_boxes: Tri-state. ``None`` (default) sends nothing, so the
                API default applies — ON in ``"fast"`` mode. ``False`` opts
                out; ``True`` asks explicitly. When on, each
                ``result["result"]["pages"]`` entry includes a ``boxes`` list
                of line boxes (``{"text", "bbox": [left, top, right, bottom],
                "confidence"}``) and, for PDF, XPS and image inputs, a
                ``word_boxes`` list of per-word boxes with ``font``, ``size``,
                ``color``, ``bold``, ``italic``, ``background`` and
                ``font_source`` (``"document"`` or ``"model"``). Coordinates
                are pixels in the page's ``dimensions`` space.
                ``mode="advanced"`` returns no boxes. See :py:meth:`submit_parse`.
            include_image: Tri-state, same convention; the API default is ON.
                When on, each ``result["result"]["pages"]`` entry includes an
                ``image_url`` (presigned GET for the deskew-corrected page,
                valid about 15 minutes). ``dimensions`` (``{"width",
                "height"}`` in that image's pixel space, which matches the box
                coordinate space) is present on PDF, XPS and image pages in
                fast mode regardless, alongside ``page_size`` and
                ``background``.
            include_layout: Tri-state, same convention; the API default is ON
                in ``"fast"`` mode. When on, each page gains ``layout`` and
                ``layout_text`` and the result gains ``layout_text``. Not for
                ``mode="advanced"`` (``True`` there is a ``400``). See
                :py:meth:`submit_parse`.
            parse_figures: Comma-separated region kinds to parse further —
                ``"image"``, ``"chart"``, ``"table"`` — or ``"none"``.
                Omitted, the API parses all three when the layout is on.
                Needs the layout on. See :py:meth:`submit_parse`.
            groups_profile: ``"A"`` (API default) or ``"B"`` — how the
                document outline in ``result["result"]["groups"]`` is built.
                See :py:meth:`submit_parse`.
            force_refresh: Re-run OCR instead of returning the cached result
                (results are cached for 24 hours). See :py:meth:`submit_parse`.
            use_presigned: Use the presigned-S3 upload flow (default True).
            poll_timeout: Override the constructor's poll_timeout for this call.
            poll_interval: Override the constructor's poll_interval for this call.

        Returns:
            Response envelope; OCR text at ``result["result"]["ocr"]`` and pages
            at ``result["result"]["pages"]``.

            EXCEPT for a large advanced document (past the ordinary advanced
            page cap, admitted on plans that allow it — see
            :py:meth:`submit_parse` and :py:meth:`get_limits`): those carry no
            inline ``pages``/``ocr``. The payload is behind a presigned
            ``result["result_url"]`` and failed page ranges are listed in
            ``result["metadata"]["gaps"]``. Branch on ``result_url`` being
            present rather than assuming the inline shape, and raise
            ``poll_timeout`` — such a job can outlast the 3600 s default.
        """
        job = await self.submit_parse(
            file_path=file_path,
            image_path=image_path,
            mode=mode,
            include_boxes=include_boxes,
            include_image=include_image,
            include_layout=include_layout,
            parse_figures=parse_figures,
            groups_profile=groups_profile,
            force_refresh=force_refresh,
            use_presigned=use_presigned,
        )
        return await self.wait_for_job(
            job, timeout=poll_timeout, interval=poll_interval
        )

    async def extract(
        self,
        file_path: str | Path,
        *,
        category: ExtractCategory | None = None,
        parse_mode: ParseMode | None = None,
        schema: dict | str | Path | None = None,
        force_refresh: bool = False,
        use_presigned: bool = True,
        poll_timeout: float | None = None,
        poll_interval: float | None = None,
    ) -> dict:
        """Extract structured data from a document.

        Submits asynchronously and polls until done. Bypasses any edge-timeout
        ceiling because each individual HTTP request stays sub-second.

        This is the Basic extractor. ``category="financial"`` (default) uses the
        built-in invoice and receipt fields. ``category="non_financial"``
        is extract-fast: no document-type detection; pass ``schema`` to
        describe the documents — a blank
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
        job = await self.submit_extract(
            file_path,
            category=category,
            parse_mode=parse_mode,
            schema=schema,
            force_refresh=force_refresh,
            use_presigned=use_presigned,
        )
        return await self.wait_for_job(
            job, timeout=poll_timeout, interval=poll_interval
        )

    async def extract_advanced(
        self,
        file_path: str | Path,
        *,
        parse_mode: ParseMode | None = None,
        force_refresh: bool = False,
        use_presigned: bool = True,
        poll_timeout: float | None = None,
        poll_interval: float | None = None,
    ) -> dict:
        """Advanced extraction — auto-detects the document type, no ``category``.

        Invoice-family documents use the built-in invoice and receipt fields;
        everything else schema-less grounded extraction. Submits asynchronously and
        polls until done. Returns the same envelope shape as :py:meth:`extract`.
        ``force_refresh=True`` reprocesses instead of returning the stored
        result (see :py:meth:`submit_extract_advanced`).
        """
        job = await self.submit_extract_advanced(
            file_path,
            parse_mode=parse_mode,
            force_refresh=force_refresh,
            use_presigned=use_presigned,
        )
        return await self.wait_for_job(
            job, timeout=poll_timeout, interval=poll_interval
        )

    async def classify(
        self,
        file_path: str | Path,
        *,
        mode: str = "default",
        options: dict | None = None,
        force_refresh: bool = False,
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
        job = await self.submit_classify(
            file_path,
            mode=mode,
            options=options,
            force_refresh=force_refresh,
            use_presigned=use_presigned,
        )
        return await self.wait_for_job(
            job, timeout=poll_timeout, interval=poll_interval
        )

    async def split(
        self,
        file_path: str | Path,
        *,
        mode: str = "default",
        options: dict | None = None,
        force_refresh: bool = False,
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
        job = await self.submit_split(
            file_path,
            mode=mode,
            options=options,
            force_refresh=force_refresh,
            use_presigned=use_presigned,
        )
        return await self.wait_for_job(
            job, timeout=poll_timeout, interval=poll_interval
        )

    async def redact(
        self,
        file_path: str | Path,
        *,
        mode: str = "redact",
        pii_config: dict | None = None,
        include_logos: bool = False,
        output: str = "inline",
        force_refresh: bool = False,
        use_presigned: bool = True,
        poll_timeout: float | None = None,
        poll_interval: float | None = None,
    ) -> dict:
        """Redact or deidentify PII in a document. Submits async + polls until done.


        Inline output has a page cap; ``output="urls"`` raises it where your
        account offers that mode. Both figures vary by plan and are returned
        by :py:meth:`get_limits`: ``limits["redact"]["max_pages"]`` for inline
        and ``limits["redact"]["urls_max_pages"]`` for URL output (absent
        means URL output is not offered — feature-detect on that key).
        ``output="urls"`` returns masked pages as short-lived presigned links
        in ``result["pages"]`` (``{page_number, image_url}``) instead of inline
        base64 ``result["images"]`` — ``download_pages()`` accepts that shape
        directly. An over-cap document returns ``413`` up front, before any
        OCR is billed; ``output="urls"`` cannot be combined with
        ``include_logos`` (``400``).

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
            output=output,
            force_refresh=force_refresh,
            use_presigned=use_presigned,
        )
        return await self.wait_for_job(
            job, timeout=poll_timeout, interval=poll_interval
        )

    async def edit_detect(
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
        job = await self.submit_edit_detect(
            file_path,
            markdown_assist=markdown_assist,
            use_presigned=use_presigned,
        )
        envelope = await self.wait_for_job(
            job, timeout=poll_timeout, interval=poll_interval
        )
        # The server's envelope has no field pointing back at the job, but the
        # fill leg needs exactly that — surface it so callers don't have to
        # hold onto the Job separately.
        if isinstance(envelope, dict):
            envelope.setdefault("detect_job_id", job.job_id)
        return envelope

    async def edit_fill(
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
        job = await self.submit_edit_fill(
            detect_job_id,
            values=values,
            content=content,
            natural_language=natural_language,
        )
        return await self.wait_for_job(
            job, timeout=poll_timeout, interval=poll_interval
        )

    async def edit(
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

        detected = await self.edit_detect(
            file_path,
            markdown_assist=markdown_assist,
            poll_timeout=poll_timeout,
            poll_interval=poll_interval,
        )
        detect_job_id = detected["detect_job_id"]
        filled = await self.edit_fill(
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

    async def download_pages(
        self,
        result: dict,
        dest_dir: str | Path,
        *,
        prefix: str = "page",
    ) -> list[Path]:
        """Download a result's page images to a folder.

        Works with whatever you just got back — :py:meth:`edit_detect` (the blank detected
        pages), :py:meth:`edit_fill` / :py:meth:`edit` (the filled ones), or
        :py:meth:`parse` with page images on (the API default; do not pass
        ``include_image=False``). Files are written as
        ``<dest_dir>/<prefix>-<page number>.png``; the directory is created if needed.
        Pages are downloaded concurrently.

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

        async def _one(url: str, path: Path) -> Path:
            # Presigned URLs carry their own SigV4 signature — send no API-key headers.
            # follow_redirects: S3 answers a cross-region GET with a 307 to the regional
            # endpoint, which would otherwise surface as a baffling "HTTP 307".
            resp = await self._client.get(url, follow_redirects=True)
            if resp.status_code >= 400:
                raise _download_error(path, resp.status_code)
            path.write_bytes(resp.content)
            return path

        return list(await asyncio.gather(*(_one(u, p) for u, p in targets)))

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
        back-to-back. The server runs the two legs concurrently; the SDK polls
        both jobs concurrently via ``asyncio.gather``.
        Total wall-clock ≈ max(parse, extract) + small polling overhead.

        Returns:
            ``{"ocr": <parse text>, "data": <extract structured fields>}``
        """
        path = self._resolve_path(file_path, image_path)
        document_key = await self.upload_document(path)
        # The two submissions could in principle be gathered too, but keeping
        # them sequential matches the sync client's behavior and the server
        # serializes them anyway through the shared document_key.
        parse_job = await self._submit_via_doc_key(
            "/v1/parse",
            "parse",
            document_key,
            params={},
        )
        extract_job = await self._submit_via_doc_key(
            "/v1/extract",
            "extract",
            document_key,
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
