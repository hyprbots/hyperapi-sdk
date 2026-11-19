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

    OCR (always runs)
        Upload → page images → OCR text (full_text + page_texts)

    Task-specific processing (skipped for parse)
        parse:      OCR text only (no task step)
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
from collections.abc import Callable, Iterable
from urllib.parse import quote

import httpx

from .exceptions import (
    AuthenticationError,
    ClassifyError,
    DocumentUploadError,
    ExtractError,
    HyperAPIError,
    JobCancelledError,
    JobTimeoutError,
    ParseError,
    RateLimitError,
    RedactError,
    SplitError,
    _strip_api_key,
)


# File extension -> upload content_type. Every value must be one the API's
# upload endpoint accepts; any other type is answered with 415. An unknown
# extension falls back to application/octet-stream and is rejected.
CONTENT_TYPES = {
    ".pdf": "application/pdf",
    # Images
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".tiff": "image/tiff",
    ".tif": "image/tiff",
    ".heic": "image/heic",
    ".heif": "image/heif",
    # Office documents
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc": "application/msword",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls": "application/vnd.ms-excel",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".ppt": "application/vnd.ms-powerpoint",
    # Text and data
    ".csv": "text/csv",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".rtf": "application/rtf",
    ".json": "application/json",
    ".xml": "application/xml",
    # Fixed-layout pages
    ".xps": "application/vnd.ms-xpsdocument",
    ".oxps": "application/oxps",
    # Email
    ".eml": "message/rfc822",
    ".msg": "application/vnd.ms-outlook",
}

ParseMode = Literal["fast", "advanced"]
# Basic extract document family. ``financial`` (platform default) uses the built-in
# invoice and receipt fields; ``non_financial`` uses the generic
# single-pass extractor. Advanced extraction (auto-detect) is a separate method.
ExtractCategory = Literal["financial", "non_financial"]

# Per-HTTP-call defaults — single submit / single poll, NOT total job time.
_DEFAULT_TIMEOUT = 120.0

# Polling defaults. Long-running document jobs can take up to an hour, so the
# SDK deadline is deliberately longer than a typical interactive request.
_DEFAULT_POLL_INTERVAL_S = 3.0
_DEFAULT_POLL_TIMEOUT_S = 3600.0
_DEFAULT_POLL_MAX_TRANSIENT_RETRIES = 3
_DEFAULT_POLL_TRANSIENT_RETRY_DELAY_S = 0.5


_DEFAULT_BASE_URL = "https://apis.hyperbots.com"
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _resolve_base_url(base_url: str | httpx.URL | None) -> str:
    """Pick the API base URL and check that it is safe to send the API key to.

    The API key travels in a header on every request, so the URL must be an
    absolute ``http(s)`` URL with a host. Plain ``http`` is accepted (local and
    test servers need it) but warns unless the host is a loopback address.
    Non-string values such as ``httpx.URL`` are converted with ``str()``.
    """
    if base_url is not None and not isinstance(base_url, str):
        base_url = str(base_url)
    raw = (base_url or os.environ.get("HYPERAPI_URL") or _DEFAULT_BASE_URL).strip()
    try:
        url = httpx.URL(raw)
    except Exception as exc:  # httpx raises InvalidURL or ValueError
        raise ValueError(f"Invalid base_url {raw!r}: {exc}") from None
    if url.scheme not in ("https", "http") or not url.host:
        raise ValueError(
            f"Invalid base_url {raw!r}: use an absolute https:// URL, "
            f"for example {_DEFAULT_BASE_URL!r}."
        )
    # "https://apis.hyperbots.com@other.example" sends the key to other.example,
    # and credentials in the URL would show up in reprs and error messages.
    if url.userinfo:
        raise ValueError(
            f"Invalid base_url for host {url.host!r}: it must not contain "
            "a username or password ('user@host')."
        )
    # An empty "?" or "#" parses as no query/fragment but would still be glued
    # in front of every request path ("https://host?/v1/parse").
    if url.query or url.fragment or "?" in raw or "#" in raw or any(c.isspace() for c in raw):
        raise ValueError(
            f"Invalid base_url {raw!r}: it must not contain a query string, "
            "a fragment or whitespace."
        )
    if url.scheme == "http" and url.host not in _LOOPBACK_HOSTS:
        warnings.warn(
            f"base_url {raw!r} uses plain http: the API key is sent unencrypted. "
            "Use https:// outside local testing.",
            stacklevel=3,
        )
    return raw.rstrip("/")


_API_KEY_REQUIRED = "API key required. Pass api_key or set HYPERAPI_KEY environment variable."


def _clean_api_key(value: str | None) -> str:
    """Return the API key with surrounding whitespace removed, or refuse it.

    Keys read from files and secret mounts often carry a trailing newline; left
    in, the HTTP layer rejects the header with an error that quotes the full
    key. Inner whitespace, control characters and non-ASCII cannot be part of
    an issued key and cannot travel in a header, so they are refused here — and
    the message never includes the key.
    """
    key = value.strip() if isinstance(value, str) else ""
    if not key:
        raise AuthenticationError(_API_KEY_REQUIRED)
    if not key.isascii() or any(ch.isspace() or not ch.isprintable() for ch in key):
        raise AuthenticationError(
            "API key contains whitespace or control characters; "
            "check how it is loaded (for example a stray newline)."
        )
    return key


def _load_schema(schema: dict | str | os.PathLike | None) -> dict | None:
    """Resolve an extract ``schema`` argument to a dict (or None).

    * ``dict`` — returned as is.
    * ``pathlib.Path`` (any ``os.PathLike``) — the file is read and parsed.
    * ``str`` — always parsed as JSON text; never treated as a file name.

    A ``str`` never touches the filesystem: an application that forwards
    user-supplied schema text must not be steerable into reading (and
    uploading) local files, and a long JSON string must not be handed to
    ``Path.exists()`` (which raises ``OSError: File name too long``).
    """
    if schema is None or isinstance(schema, dict):
        return schema

    if isinstance(schema, os.PathLike):
        path = Path(schema)
        if not path.exists():
            raise FileNotFoundError(f"Schema file not found: {path}")
        try:
            parsed = json.loads(path.read_text())
        except ValueError as e:
            raise ValueError(f"Schema file {path} is not valid JSON: {e}") from None
    elif isinstance(schema, str):
        try:
            parsed = json.loads(schema)
        except ValueError as e:
            if schema.strip().lower().endswith(".json"):
                hint = (
                    "This string ends in '.json' but a str schema is always JSON "
                    "text, never a file name; to load a schema file pass "
                    'pathlib.Path("...") instead.'
                )
            else:
                hint = 'To load a schema from a file, pass pathlib.Path("...").'
            raise ValueError(f"schema string is not valid JSON ({e}). {hint}") from None
    else:
        raise TypeError(
            "schema must be a dict, a JSON string, or a pathlib.Path, "
            f"not {type(schema).__name__}"
        )

    if not isinstance(parsed, dict):
        raise ValueError("schema must resolve to a JSON object")
    return parsed


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
    """Parse a `Retry-After` header into whole seconds-from-now.

    RFC 7231 permits both ``delta-seconds`` and an ``HTTP-date``. Gateways and
    CDNs may use either form, so handle both and floor the result at zero.
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


def _dict_message(body: dict) -> str:
    """One readable line for an error body that is itself a JSON object.

    Prefers a human string under ``message`` / ``detail`` / ``error``; with
    none, summarises the object as ``key=value`` pairs so an exception never
    shows a Python dict repr.
    """
    for key in ("message", "detail", "error"):
        text = body.get(key)
        if isinstance(text, str) and text.strip():
            return text
    parts = []
    for key, value in body.items():
        if value is None:
            continue
        if isinstance(value, (dict, list)):
            value = json.dumps(value, separators=(",", ":"), default=str)
        parts.append(f"{key}={value}")
    return ", ".join(parts)


def _server_error(response: httpx.Response) -> tuple[str | None, str | None, dict | None]:
    """Decode the API's error envelope into ``(message, code, details)``.

    The API answers every error as ``{"error": {"code": str, "message": str,
    "details"?: dict}}``. ``code`` is the stable machine-readable reason (a
    status-derived constant such as ``"NOT_FOUND"``, or a specific one such as
    ``"page_quota_exceeded"``); ``details`` is optional and carries the numbers
    behind a refusal. Parsing stays tolerant of other shapes — a bare
    ``{"message": ...}`` or ``{"detail": ...}`` from an intermediary, or a
    non-JSON body — so a proxy error page never turns into a parse failure.
    Where the human text is itself an object (a structured refusal such as a
    quota answer), the message is a readable line built from it and the
    object is returned as ``details``.
    Every field returned is API-key-scrubbed; each is ``None`` when absent.
    """
    body = _safe_text(response)
    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return None, None, None
    if not isinstance(parsed, dict):
        return None, None, None

    code: str | None = None
    details: dict | None = None
    message: str | None = None
    for key in ("error", "message", "detail"):
        val = parsed.get(key)
        if not val:
            continue
        if isinstance(val, dict):
            inner_code = val.get("code")
            if isinstance(inner_code, str) and code is None:
                code = inner_code
            inner_details = val.get("details")
            if isinstance(inner_details, dict) and details is None:
                details = inner_details
            # Prefer the nested human text over stringifying the whole dict
            # (which would surface a Python dict repr like "{'code': ...}").
            text = val.get("message") or val.get("detail") or val.get("error")
            if isinstance(text, dict):
                # The text slot holds a structured refusal, e.g.
                # {"error": {"code": ..., "message": {"error": "quota_exceeded", ...}}}.
                nested_code = text.get("code")
                if isinstance(nested_code, str) and code is None:
                    code = nested_code
                if details is None:
                    details = text
                text = _dict_message(text)
            elif details is None and inner_code is None and not isinstance(
                val.get("message"), str
            ):
                # A bare object that is not the standard envelope, e.g.
                # {"detail": {"error": "quota_exceeded", "list_price_cents": 5}}.
                details = val
            val = text if text else _dict_message(val)
        if message is None:
            message = _strip_api_key(str(val))
    if code is None:
        top_code = parsed.get("code")
        if isinstance(top_code, str):
            code = top_code
    return message, code, details


def _server_message(response: httpx.Response, fallback: str) -> str:
    """Pull a human-readable message out of a server response.

    Prefers the envelope's ``error.message`` (see :func:`_server_error`), falls
    back to the body text, and finally to the supplied fallback. Always
    API-key-scrubbed.
    """
    message, _, _ = _server_error(response)
    if message:
        return message
    return _safe_text(response) or fallback


def _error_fields(response: httpx.Response) -> dict[str, Any]:
    """``code``/``details`` keyword arguments for an exception built from ``response``.

    Splat into any ``HyperAPIError`` constructor so the envelope's stable
    reason code and its optional ``details`` travel with the exception.
    """
    _, code, details = _server_error(response)
    return {"code": code, "details": details}


_CANCELLED_PREFIX = "cancelled:"


def _cancelled_job_error(envelope: dict, job_id: str) -> JobCancelledError | None:
    """Decode a ``status="failed"`` envelope that is really a cancel.

    ``DELETE /v1/jobs/{id}`` leaves the job in ``status="failed"`` with an
    ``error`` beginning ``"cancelled:"``. Returns ``None`` for any other
    failure so the caller raises its usual per-op exception.
    """
    error = envelope.get("error")
    if not (isinstance(error, str) and error.startswith(_CANCELLED_PREFIX)):
        return None
    return JobCancelledError(
        f"Job {job_id} was cancelled before it completed "
        "(DELETE /v1/jobs/{id}); submit the document again to re-run it.",
        job_id=job_id,
        request_id=envelope.get("request_id"),
    )


def _request_id_of(response: httpx.Response, sent: str | None = None) -> str | None:
    """Best-effort extraction of the request id we should attach to errors."""
    return (
        response.headers.get("x-request-id")
        or response.headers.get("x-correlation-id")
        or sent
    )


_NOT_JSON = object()


def _json_object(
    resp: httpx.Response,
    *,
    request_id: str | None,
    error_cls: type[HyperAPIError] = HyperAPIError,
    required: Iterable[str] = (),
    what: str = "server",
) -> dict:
    """Decode a success reply that must be a JSON object, or raise ``error_cls``.

    A proxy error page served with 200, a JSON list, or an envelope without
    the keys the SDK needs would otherwise escape as ``JSONDecodeError``,
    ``AttributeError`` or ``KeyError``. ``required`` keys must be present and
    not ``None`` or empty. The raised error carries the HTTP status and the
    request id.
    """
    try:
        body = resp.json()
    except ValueError:  # JSONDecodeError and UnicodeDecodeError are ValueErrors
        body = _NOT_JSON
    problem = None
    if body is _NOT_JSON:
        problem = "the body is not JSON"
    elif not isinstance(body, dict):
        problem = f"expected a JSON object, got {type(body).__name__}"
    else:
        missing = [key for key in required if body.get(key) in (None, "")]
        if missing:
            problem = "missing " + ", ".join(repr(key) for key in missing)
    if problem is not None:
        raise error_cls(
            f"Malformed {what} response (HTTP {resp.status_code}): {problem}.",
            status_code=resp.status_code,
            request_id=request_id,
        )
    return body


def _path_id(value: Any, *, name: str = "id") -> str:
    """Percent-encode a job or batch id for use as one URL path segment.

    Ids come from callers (and are often stored and replayed), so an id such
    as ``"../limits?x="`` must stay inside its own path segment instead of
    walking to another endpoint or adding a query string. Every reserved
    character, including ``/``, is encoded.
    """
    text = "" if value is None else str(value)
    if not text.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return quote(text, safe="")


def _parse_params(
    *,
    mode: str | None,
    include_boxes: bool | None,
    include_image: bool | None,
    include_layout: bool | None,
    parse_figures: str | None,
    groups_profile: str | None,
) -> dict[str, Any]:
    """Query parameters for ``POST /v1/parse`` — only what the caller set.

    Every parse option is omitted when ``None`` so the API's own default
    applies. This matters most for the ``include_*`` flags: the API treats an
    omitted flag as ON (in ``"fast"`` mode) and a literal ``false`` as an
    opt-out, so sending ``False`` on the caller's behalf would silently strip
    boxes, layout and page images from every result. Shared by both clients.
    """
    params: dict[str, Any] = {}
    if mode is not None:
        params["mode"] = mode
    if include_boxes is not None:
        params["include_boxes"] = include_boxes
    if include_image is not None:
        params["include_image"] = include_image
    if include_layout is not None:
        params["include_layout"] = include_layout
    if parse_figures is not None:
        params["parse_figures"] = parse_figures
    if groups_profile is not None:
        params["groups_profile"] = groups_profile
    return params


def _parse_ocr_text(parse_result: Any) -> Any:
    """OCR text from a parse job result. The parse envelope nests its payload
    under `result` (parse_result["result"]["ocr"]); a flattened shape would put
    it at the top level. None if absent."""
    if not isinstance(parse_result, dict):
        return None
    inner = parse_result.get("result")
    if isinstance(inner, dict) and "ocr" in inner:
        return inner["ocr"]
    return parse_result.get("ocr")


def _pages_of(result: Any) -> list[dict]:
    """Find the page list in any op's result, whichever nesting it arrived in.

    ``parse`` and ``redact`` return the platform envelope, so the pages sit at
    ``result["result"]["pages"]``. A caller who already unwrapped the envelope
    holds them at ``result["pages"]``; both are accepted.
    """
    if not isinstance(result, dict):
        raise ValueError(
            "Expected the dict returned by parse() or redact(output=\"urls\")"
        )
    for candidate in (result.get("result"), result):
        if isinstance(candidate, dict) and isinstance(candidate.get("pages"), list):
            return candidate["pages"]
    raise ValueError(
        "No 'pages' in this result. Pass the value returned by parse() with page "
        "images on (the default), or by redact(output=\"urls\")."
    )


def _page_number(page: dict, fallback: int) -> int:
    """The page's own ``page_number``, else its 1-based position in the list."""
    value = page.get("page_number")
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return fallback


def _image_suffix(url: str) -> str:
    """Extension from the URL path — redact serves .png, parse serves .webp. Never guess:
    writing webp bytes into a .png name breaks anything that trusts the extension."""
    stem = url.split("?", 1)[0].rsplit("/", 1)[-1]
    suffix = Path(stem).suffix.lower()
    return (
        suffix
        if suffix in {".png", ".webp", ".jpg", ".jpeg", ".tif", ".tiff"}
        else ".png"
    )


def _page_targets(
    result: Any, dest_dir: str | Path, prefix: str
) -> list[tuple[str, Path]]:
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
                "For parse(), leave include_image unset or pass include_image=True."
            )
        out.append(
            (url, dest / f"{prefix}-{_page_number(page, i + 1)}{_image_suffix(url)}")
        )
    return out


# Downloads and uploads of presigned URLs.
_DEFAULT_MAX_PAGE_BYTES = 200 * 1024 * 1024
# The async client runs at most this many presigned transfers at once.
_TRANSFER_CONCURRENCY = 8


def _check_server_url(
    url: Any,
    *,
    base_url: str,
    what: str,
    error_cls: type[HyperAPIError] = HyperAPIError,
) -> None:
    """Refuse a server-supplied URL (presigned upload/download) that is not https.

    Plain ``http`` is accepted only when the client itself talks to a plain
    ``http`` base URL (a local or test server). The URL is never echoed in
    the error: presigned URLs carry a signature.
    """
    scheme = ""
    if isinstance(url, str) and url:
        try:
            scheme = httpx.URL(url).scheme
        except Exception:  # noqa: BLE001  # httpx raises InvalidURL or ValueError
            scheme = ""
    if scheme == "https":
        return
    if scheme == "http" and httpx.URL(base_url).scheme == "http":
        return
    raise error_cls(
        f"Refusing a {what} that does not use https (scheme {scheme or 'missing'!r}). "
        "Presigned URLs from the API are always https; plain http is accepted only "
        "when base_url itself is http (local testing)."
    )


def _regular_file(file_path: str | Path) -> Path:
    """The upload path, which must be an existing regular file.

    A directory, FIFO or device would hang or fail obscurely on read.
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")
    if not path.is_file():
        raise ValueError(
            f"Not a regular file: {path}. Directories, pipes and devices cannot be uploaded."
        )
    return path


def _check_page_cap(max_page_bytes: int) -> None:
    if (
        isinstance(max_page_bytes, bool)
        or not isinstance(max_page_bytes, int)
        or max_page_bytes <= 0
    ):
        raise ValueError("max_page_bytes must be a positive int")


def _partial_path(path: Path) -> Path:
    """A unique temporary name next to ``path``; renamed over it once complete."""
    return path.with_name(f".{path.name}.{uuid.uuid4().hex[:12]}.part")


def _page_too_large(path: Path, max_page_bytes: int) -> HyperAPIError:
    return HyperAPIError(
        f"Could not download {path.name}: the image is larger than "
        f"max_page_bytes ({max_page_bytes} bytes). Nothing was written; "
        "raise max_page_bytes if this page is expected to be this large."
    )


def _check_download_reply(
    resp: httpx.Response, path: Path, *, base_url: str, max_page_bytes: int
) -> None:
    """Status, final-URL and declared-size checks before a page body is read."""
    if resp.status_code >= 400:
        raise _download_error(path, resp.status_code)
    # Redirects are followed (S3 sends a 307 to the regional endpoint); the
    # final hop must meet the same https rule as the URL the API handed out.
    _check_server_url(str(resp.url), base_url=base_url, what="page image redirect")
    declared = resp.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > max_page_bytes:
        raise _page_too_large(path, max_page_bytes)


def _download_transport_error(path: Path, exc: Exception) -> HyperAPIError:
    return HyperAPIError(f"Could not download {path.name}: {exc}")


def _download_error(page_path: Path, status: int) -> HyperAPIError:
    hint = ""
    if status in (403, 401):
        # SigV4 presigned URLs live ~15 min; the job itself survives 24 h.
        hint = (
            " The presigned URL has most likely expired — re-poll the job with "
            "get_job(job_id) for fresh URLs, then download again."
        )
    return HyperAPIError(
        f"Could not download {page_path.name} (HTTP {status}).{hint}",
        status_code=status,
    )


def _rate_limit_error_from(resp: httpx.Response, rid: str | None) -> RateLimitError:
    """Build a RateLimitError from a 429 response envelope.

    The API may rate-limit both submissions and job polls, and it also uses
    429 for a monthly page quota that is used up (``error.code ==
    "page_quota_exceeded"``). The code is carried on the exception so callers
    and the SDK's own poll loops can tell the two apart. This helper is shared
    by both clients so envelope parsing stays consistent.
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
            # The window length lets callers interpret the numeric limit.
            window_seconds = body.get("window_seconds")
            # A degraded response distinguishes transient enforcement trouble
            # from a customer exceeding their configured quota.
            degraded = bool(body.get("degraded", False))
    except (json.JSONDecodeError, ValueError):  # pragma: no cover
        # Fall back to the response headers when the body is not JSON.
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
        **_error_fields(resp),
    )


def _is_transient(err: HyperAPIError) -> bool:
    """Whether a failed poll is worth retrying: no HTTP reply at all (network
    error, timeout) or a 5xx. Every 4xx — and a malformed 2xx — fails fast:
    asking again returns the same answer."""
    return err.status_code is None or err.status_code >= 500


def _poll_deadline_error(
    kind: str, ident: str, last: HyperAPIError, start: float | None
) -> JobTimeoutError:
    """The error for a poll that is still failing transiently when the caller's
    deadline passes: the wait is over, so report a timeout (with the cause)."""
    return JobTimeoutError(
        f"{kind} {ident} did not complete in the time allowed; "
        f"the last status poll failed: {last.message}",
        job_id=ident,
        elapsed_s=time.monotonic() - start if start is not None else 0.0,
        request_id=last.request_id,
    )


def _transient_delay(
    err: HyperAPIError,
    *,
    attempt: int,
    max_retries: int,
    delay: float,
    deadline: float | None,
    kind: str,
    ident: str,
    start: float | None,
) -> float:
    """Seconds to sleep before retrying a failed poll, or raise.

    Shared by both clients' retry loops. Raises ``err`` itself when it is not
    transient or the retries are used up, and :class:`JobTimeoutError` when
    the caller's ``deadline`` has passed. The sleep never runs past the
    deadline.
    """
    if not _is_transient(err):
        raise err
    remaining = None if deadline is None else deadline - time.monotonic()
    if remaining is not None and remaining <= 0:
        raise _poll_deadline_error(kind, ident, err, start) from None
    if attempt >= max_retries:
        raise err
    wait = delay if remaining is None else min(delay, remaining)
    logger.info(
        "poll_transient_retry",
        extra={
            f"{kind.lower()}_id": ident,
            "attempt": attempt + 1,
            "delay_s": wait,
            "error": str(err)[:200],
        },
    )
    return wait


def _rate_limit_wait(err: RateLimitError, remaining: float) -> float | None:
    """Decide how to handle a 429 raised *during a poll loop*.

    The API rate-limits polls and submits together — so on the free tier
    (1 req / 60 s) the submit spends the allowance and the first poll(s) 429. That's transient: the window clears, so the poll
    loop should sleep ``Retry-After`` and resume rather than fail the whole wait.

    Returns the seconds to sleep before re-polling, or ``None`` when the caller
    should re-raise the ``RateLimitError`` instead: either the job's remaining
    deadline can't outlast the rate window (the error still carries
    ``retry_after`` so the user can wait and resume via ``submit_<op>`` +
    ``wait_for_job``), or the 429 is not a rate window at all but an exhausted
    monthly page quota (``err.is_quota_exhausted``), which no amount of waiting
    within this call will clear. Shared by both clients so the sync/async
    decision can't drift; each loop supplies its own sleep primitive.
    """
    if err.is_quota_exhausted:
        return None
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
                a single status poll in normal use.
            poll_interval: Seconds between successive polls of
                ``/v1/jobs/{id}``. Default 3.0 (matches the platform
                playground).
            poll_timeout: Total wall-clock seconds the SDK will wait for a
                single job to complete. Raises ``JobTimeoutError`` past this.
                Default 3600.0 (60 min) — covers the maximum server-side
                async job budget. The dashboard playground deliberately
                stays at 30 min; see the module-level note.
            poll_max_transient_retries: How many times to retry a single
                ``/v1/jobs/{id}`` (or batch status) GET if it returns 5xx or
                has a connection error. Default 3. Every 4xx fails fast, and
                retries stop once the wait's own timeout has passed.
            poll_transient_retry_delay: Seconds to sleep between transient-retry
                attempts on a single poll. Default 0.5; bump this if your
                network is bursty enough that 3 retries in 1.5 s isn't enough
                breathing room.
        """
        self._api_key = _clean_api_key(api_key or os.environ.get("HYPERAPI_KEY"))

        self.base_url = _resolve_base_url(base_url)
        self.timeout = timeout
        self._poll_interval = poll_interval
        self._poll_timeout = poll_timeout
        self._poll_max_transient_retries = poll_max_transient_retries
        self._poll_transient_retry_delay = poll_transient_retry_delay

        from . import __version__  # local import to avoid circular at module load

        # User-Agent includes the httpx + Python versions so server-side analytics
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

    @property
    def api_key(self) -> str:
        """The API key this client sends (read-only; whitespace-stripped)."""
        return self._api_key

    # ── Repr ─────────────────────────────────────────────────────────────

    def __repr__(self) -> str:
        tail = self._api_key[-4:]
        return f"HyperAPIClient(api_key='hk_***{tail}', base_url='{self.base_url}')"

    # ── Headers ──────────────────────────────────────────────────────────

    def _get_headers(self, *, async_mode: bool = False) -> dict[str, str]:
        """Per-request headers. Generates a fresh X-Request-ID per call so
        every request can be correlated end-to-end across the platform."""
        headers = {
            "X-API-Key": self._api_key,
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
            ValueError: If the path is not a regular file (a directory, pipe or device).
            AuthenticationError: 401 from the server.
            DocumentUploadError: Any other failure on the presigned fetch or S3 PUT,
                including a presigned URL that is not https.
        """
        path = _regular_file(file_path)

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
            resp = self._client.post(
                f"{self.base_url}/v1/documents/upload",
                json=body,
                headers=headers,
            )
        except httpx.RequestError as e:
            raise DocumentUploadError(
                f"Failed to get upload URL: {e}", request_id=request_id
            ) from None

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

        upload_data = _json_object(
            resp,
            request_id=_request_id_of(resp, request_id),
            error_cls=DocumentUploadError,
            required=("document_key", "upload_url"),
            what="upload-URL",
        )
        document_key = upload_data["document_key"]
        upload_url = upload_data["upload_url"]
        _check_server_url(
            upload_url,
            base_url=self.base_url,
            what="presigned upload URL",
            error_cls=DocumentUploadError,
        )

        # Step 2 — upload bytes directly to object storage using the signed URL.
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
            raise DocumentUploadError(
                f"S3 upload failed: {e}", request_id=request_id
            ) from None

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

    def get_limits(self) -> dict:
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
            resp = self._client.get(f"{self.base_url}/v1/limits", headers=headers)
        except httpx.RequestError as e:
            raise HyperAPIError(f"Limits request failed: {e}", request_id=request_id) from None

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
        caller wants to handle explicitly.
        """
        sc = resp.status_code
        rid = _request_id_of(resp, request_id)

        if sc == 401:
            # Keep the message portable across hosted and self-hosted deployments.
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
            # Two different refusals share this status. On the upload endpoint
            # it is the file-size ceiling (error_cls is DocumentUploadError
            # there). On an operation endpoint it is that operation's page
            # cap, so it is raised as the operation's own error class.
            fallback = (
                "File exceeds upload size limit"
                if error_cls is DocumentUploadError
                else f"Document exceeds the page or size limit for {op_name}"
            )
            raise error_cls(
                _server_message(resp, fallback),
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

    def _submit_via_path(
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
            document_key = self.upload_document(file_path, content_type=content_type)
            response = self._client.post(
                f"{self.base_url}{endpoint}",
                data={"document_key": document_key, **extra_form},
                params={**params, "force_refresh": "true"} if force_refresh else params,
                headers=headers,
                timeout=request_timeout,
            )
        except httpx.TimeoutException:
            raise error_cls(
                "Request timed out",
                status_code=504,
                request_id=request_id,
            ) from None
        except httpx.RequestError as e:
            raise error_cls(f"Request failed: {e}", request_id=request_id) from None

        self._raise_for_known_status(
            response, request_id=request_id, error_cls=error_cls, op_name=op_name
        )
        if response.status_code not in (200, 202):
            raise error_cls(
                _server_message(response, f"{op_name} submission failed"),
                status_code=response.status_code,
                request_id=_request_id_of(response, request_id),
                **_error_fields(response),
            )

        return self._job_from_envelope(
            self._submit_reply(response, request_id, error_cls, op_name), op=op_name
        )

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
        except httpx.TimeoutException:
            raise error_cls(
                "Request timed out", status_code=504, request_id=request_id
            ) from None
        except httpx.RequestError as e:
            raise error_cls(f"Request failed: {e}", request_id=request_id) from None

        self._raise_for_known_status(
            response, request_id=request_id, error_cls=error_cls, op_name=op_name
        )
        if response.status_code not in (200, 202):
            raise error_cls(
                _server_message(response, f"{op_name} submission failed"),
                status_code=response.status_code,
                request_id=_request_id_of(response, request_id),
                **_error_fields(response),
            )

        return self._job_from_envelope(
            self._submit_reply(response, request_id, error_cls, op_name), op=op_name
        )

    @staticmethod
    def _submit_reply(
        response: httpx.Response, request_id: str, error_cls: type, op_name: str
    ) -> dict:
        """The submit reply as a dict that carries a ``job_id``, or ``error_cls``."""
        return _json_object(
            response,
            request_id=_request_id_of(response, request_id),
            error_cls=error_cls,
            required=("job_id",),
            what=f"{op_name} submission",
        )

    @staticmethod
    def _job_from_envelope(envelope: dict, *, op: str) -> Job:
        return Job(
            job_id=envelope["job_id"],
            status=envelope.get("status", "pending"),
            poll_url=envelope.get(
                "poll_url", f"/v1/jobs/{_path_id(envelope['job_id'], name='job_id')}"
            ),
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
                f"{self.base_url}/v1/jobs/{_path_id(job_id, name='job_id')}",
                headers=headers,
            )
        except httpx.RequestError as e:
            raise HyperAPIError(f"Job poll failed: {e}", request_id=request_id) from None

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
        # Defend against a 200 with malformed JSON — some misconfigured proxies will
        # return text/html bodies on 200. Without this guard, a JSONDecodeError
        # escapes to the caller as an un-typed exception.
        return _json_object(resp, request_id=rid, what="job-status")

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
            resp = self._client.get(
                f"{self.base_url}/v1/jobs/recent", params=params, headers=headers
            )
        except httpx.RequestError as e:
            raise HyperAPIError(
                f"Recent-jobs request failed: {e}", request_id=request_id
            ) from None

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
            resp = self._client.delete(
                f"{self.base_url}/v1/jobs/{_path_id(job_id, name='job_id')}",
                headers=headers,
            )
        except httpx.RequestError as e:
            raise HyperAPIError(f"Job cancel failed: {e}", request_id=request_id) from None

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

    def _retry_transient(
        self,
        fetch: Callable[[], dict],
        *,
        kind: str,
        ident: str,
        deadline: float | None = None,
        start: float | None = None,
    ) -> dict:
        """Run one status poll, retrying transient failures (network errors, 5xx).

        Every 4xx fails fast (401, 403, 404, 400, 422, ...), as does a 429 —
        the wait loops handle rate limits themselves. Retries stop at
        ``poll_max_transient_retries`` and never run past ``deadline``: once it
        has passed, :class:`JobTimeoutError` is raised instead.
        """
        attempt = 0
        while True:
            try:
                return fetch()
            except (AuthenticationError, RateLimitError):
                raise
            except HyperAPIError as e:
                time.sleep(
                    _transient_delay(
                        e,
                        attempt=attempt,
                        max_retries=self._poll_max_transient_retries,
                        delay=self._poll_transient_retry_delay,
                        deadline=deadline,
                        kind=kind,
                        ident=ident,
                        start=start,
                    )
                )
                attempt += 1

    def _poll_with_retry(
        self, job_id: str, *, deadline: float | None = None, start: float | None = None
    ) -> dict:
        """One job poll with the transient-retry policy of :meth:`_retry_transient`."""
        return self._retry_transient(
            lambda: self.get_job(job_id),
            kind="Job",
            ident=job_id,
            deadline=deadline,
            start=start,
        )

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
                envelope = self._poll_with_retry(
                    job_id, deadline=deadline, start=start
                )
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
        # Placeholder `{}` (not None) means "completed, but the server's result envelope
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
                    envelope = self._poll_with_retry(
                        jobs_list[idx].job_id, deadline=deadline, start=start
                    )
                except RateLimitError as e:
                    # Polls and submits share one rate limit: one 429 means the
                    # window is spent for everyone this pass. Sleep Retry-After once, keep
                    # this and all not-yet-polled jobs pending, and retry.
                    wait = _rate_limit_wait(e, deadline - time.monotonic())
                    if wait is None:
                        raise
                    logger.info(
                        "poll_rate_limited",
                        extra={
                            "job_id": jobs_list[idx].job_id,
                            "retry_after": wait,
                            "degraded": e.degraded,
                        },
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

    def submit_parse(
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
        v0.1.x backward compatibility — pass exactly one. The other
        ``submit_<op>`` methods don't take it because parse is the only op
        that historically accepted bare images via that name.

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
        return self._submit_via_path(
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

    def submit_extract(
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
        (template-driven — see ``schema`` below). For auto-detecting Advanced
        extraction, use :py:meth:`submit_extract_advanced`.

        ``parse_mode`` explicitly selects the OCR engine, independent of
        ``category``: ``"fast"`` (fast text extraction) or
        ``"advanced"`` (advanced layout-aware parsing for dense tables/forms —
        higher accuracy, slower, costs more; available on paid tiers). Omit it
        to use the organization default (platform default: ``"fast"``).

        ``schema``: a blank template — the exact shape you want back, with
        ``None``/``False``/``"unselected"`` as placeholders, e.g.
        ``{"patient_name": None, "insurance": {"Medicare": "unselected",
        "Self-Pay": "unselected"}}``. Pass it as an already-loaded dict, a
        ``str`` of JSON text, or a ``pathlib.Path`` to a JSON file. A ``str``
        is always parsed as JSON and never read as a file name — to load a
        file, pass ``pathlib.Path("template.json")``. With
        ``category="non_financial"`` the template is filled directly from the
        document. With ``category="financial"`` the invoice extractor runs and
        its output is remapped into your template. A ``schema`` with no
        ``category`` is sent as ``"non_financial"``. Ephemeral — used for this
        call only, nothing is stored server-side; pass a different ``schema``
        next call for a different shape, no merge/versioning to reason about.
        Omit entirely to use this org's stored default template instead (set
        up separately, offline).
        """
        path = self._resolve_path(file_path)
        resolved_schema = _load_schema(schema)
        # `schema` is honoured on both categories: non_financial fills the
        # template directly, financial runs the invoice extractor and remaps
        # its output into the template. An explicit category is sent as given.
        #
        # A schema with no declared category: send non_financial explicitly
        # rather than deferring. The two categories treat a template
        # differently, and the SDK cannot read the organization default, so
        # deferring would make the same call behave differently per account.
        # Filling the template directly is the plain reading of "here is the
        # shape I want"; callers who want the invoice extractor say so.
        effective_category = (
            "non_financial" if category is None and resolved_schema is not None else category
        )
        data = (
            {"schema": json.dumps(resolved_schema)}
            if resolved_schema is not None
            else None
        )
        return self._submit_via_path(
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

    def submit_extract_advanced(
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
        return self._submit_via_path(
            "/v1/extract-omni",
            "extract",
            path,
            params={"parse_mode": parse_mode} if parse_mode is not None else {},
            force_refresh=force_refresh,
            use_presigned=use_presigned,
        )

    def submit_classify(
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
        return self._submit_via_path(
            "/v1/classify",
            "classify",
            path,
            params={"mode": mode},
            data=data,
            force_refresh=force_refresh,
            use_presigned=use_presigned,
        )

    def submit_split(
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
        return self._submit_via_path(
            "/v1/split",
            "split",
            path,
            params={"mode": mode},
            data=data,
            force_refresh=force_refresh,
            use_presigned=use_presigned,
        )

    def submit_redact(
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
        return self._submit_via_path(
            "/v1/redact",
            "redact",
            path,
            params={"mode": mode, "include_logos": include_logos, **({"output": "urls"} if output == "urls" else {})},
            data=data,
            force_refresh=force_refresh,
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
        required: tuple[str, ...] = (),
    ) -> dict:
        """Shared request+error handling for the /v1/batch endpoints.

        Every batch reply must be a JSON object carrying the ``required`` keys.
        """
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
            raise HyperAPIError(
                f"Batch request failed: {e}", request_id=request_id
            ) from None

        rid = _request_id_of(resp, request_id)
        if resp.status_code == 401:
            raise AuthenticationError(
                "Invalid API key.", status_code=401, request_id=rid
            )
        if resp.status_code == 404 and path != "/v1/batch":
            # Only an id-addressed call (GET/DELETE /v1/batch/{id}) can mean
            # "no such batch". A 404 on the collection itself falls through
            # and reports what the server said.
            raise HyperAPIError(
                "Batch not found.",
                status_code=404,
                request_id=rid,
                **_error_fields(resp),
            )
        if resp.status_code == 429:
            raise _rate_limit_error_from(resp, rid)
        if resp.status_code >= 400:
            raise HyperAPIError(
                _server_message(
                    resp, f"Batch request failed (HTTP {resp.status_code})"
                ),
                status_code=resp.status_code,
                request_id=rid,
                **_error_fields(resp),
            )
        return _json_object(resp, request_id=rid, required=required, what="batch")

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
            endpoint: One of ``/v1/parse``, ``/v1/extract``,
                ``/v1/extract-omni`` (advanced extract), ``/v1/classify``,
                ``/v1/split``, ``/v1/redact``. The API rejects any other
                value with a ``400``.
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
        return self._batch_request(
            "POST",
            "/v1/batch",
            json_body=body,
            extra_headers=extra,
            required=("batch_id",),
        )

    def create_batch_from_files(
        self,
        *,
        endpoint: str,
        file_paths: list[str | Path],
        **kwargs,
    ) -> dict:
        """Upload each file (presigned flow) then :meth:`create_batch`."""
        document_keys = [self.upload_document(p) for p in file_paths]
        return self.create_batch(
            endpoint=endpoint, document_keys=document_keys, **kwargs
        )

    def get_batch(self, batch_id: str, *, limit: int = 200, offset: int = 0) -> dict:
        """Batch status: ``{status, counts, items:[{doc_index, status, ...}]}``."""
        return self._batch_request(
            "GET",
            f"/v1/batch/{_path_id(batch_id, name='batch_id')}",
            params={"limit": limit, "offset": offset},
        )

    def list_batches(self, *, limit: int = 50, offset: int = 0) -> dict:
        """List the org's batches, newest first."""
        return self._batch_request(
            "GET", "/v1/batch", params={"limit": limit, "offset": offset}
        )

    def cancel_batch(self, batch_id: str) -> dict:
        """Cancel a batch — stops queued items; in-flight items finish."""
        return self._batch_request(
            "DELETE", f"/v1/batch/{_path_id(batch_id, name='batch_id')}"
        )

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
                status = self._retry_transient(
                    lambda: self.get_batch(batch_id),
                    kind="Batch",
                    ident=batch_id,
                    deadline=deadline,
                    start=start,
                )
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
        job = self.submit_parse(
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
        return self.wait_for_job(job, timeout=poll_timeout, interval=poll_interval)

    def extract(
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

        This is the Basic extractor. ``category="financial"`` uses the
        built-in invoice and receipt fields. Omit ``category`` to use the
        organization default (platform default: ``"financial"``; a ``schema``
        with no ``category`` is sent as ``"non_financial"``). ``category="non_financial"``
        does no document-type detection; pass ``schema`` to
        describe the documents — a blank
        template with ``None``/``False``/``"unselected"`` as placeholders
        (e.g. ``{"patient_name": None, "insurance": {"Medicare":
        "unselected", "Self-Pay": "unselected"}}``, as a dict, a ``str`` of
        JSON text, or a ``pathlib.Path`` to a JSON file — a ``str`` is never
        read as a file name), and the response fills in that SAME
        structure from the document — nothing is stored server-side, pass a
        different ``schema`` next call for a different shape, no
        merge/versioning to reason about. Omit ``schema`` to use this org's
        stored default template instead (set up separately, offline — not
        through this SDK); with no schema and no stored default, this
        currently falls back to a generic extraction (a known gap, not yet
        unified with this flow). ``schema`` also works with
        ``category="financial"``: the invoice extractor runs and its output
        is remapped into your template. For auto-detecting Advanced
        extraction (no ``category`` needed), use :py:meth:`extract_advanced`.

        Returns:
            Response envelope. For ``category="financial"``: the structured
            payload is one level under ``result``:
            ``result["result"]["entities"]`` (header/party fields),
            ``result["result"]["line_items"]`` (per-line rows), and
            ``result["result"]["summary"]`` (document-level totals/tax/currency —
            e.g. the grand total lives here, ``summary["total_amount"]``, NOT in
            entities). For ``category="non_financial"``: the
            filled-in template is at ``result["result"]["data"]`` (same shape
            as ``schema``, or the org's stored default). Raw OCR is at the
            envelope top level, ``result["ocr_text"]``, in both cases.
        """
        job = self.submit_extract(
            file_path,
            category=category,
            parse_mode=parse_mode,
            schema=schema,
            force_refresh=force_refresh,
            use_presigned=use_presigned,
        )
        return self.wait_for_job(job, timeout=poll_timeout, interval=poll_interval)

    def extract_advanced(
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
        job = self.submit_extract_advanced(
            file_path,
            parse_mode=parse_mode,
            force_refresh=force_refresh,
            use_presigned=use_presigned,
        )
        return self.wait_for_job(job, timeout=poll_timeout, interval=poll_interval)

    def classify(
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
        job = self.submit_classify(
            file_path,
            mode=mode,
            options=options,
            force_refresh=force_refresh,
            use_presigned=use_presigned,
        )
        return self.wait_for_job(job, timeout=poll_timeout, interval=poll_interval)

    def split(
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
        job = self.submit_split(
            file_path,
            mode=mode,
            options=options,
            force_refresh=force_refresh,
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
        overlays synthetic replacements. ``include_logos=True`` also detects and
        masks logos. ``pii_config`` customizes the detected PII type set.

        Returns:
            Response envelope with ``result`` containing the masked pages —
            ``images`` (inline base64, the default) or ``pages`` with
            presigned ``image_url`` links when ``output="urls"`` — plus
            ``updated_text_block`` and a ``summary`` of masked counts.
        """
        job = self.submit_redact(
            file_path,
            mode=mode,
            pii_config=pii_config,
            include_logos=include_logos,
            output=output,
            force_refresh=force_refresh,
            use_presigned=use_presigned,
        )
        return self.wait_for_job(job, timeout=poll_timeout, interval=poll_interval)

    def download_pages(
        self,
        result: dict,
        dest_dir: str | Path,
        *,
        prefix: str = "page",
        max_page_bytes: int = _DEFAULT_MAX_PAGE_BYTES,
    ) -> list[Path]:
        """Download a result's page images to a folder.

        Works with the dict returned by :py:meth:`parse` with page images on
        (the API default; do not pass ``include_image=False``) or by
        :py:meth:`redact` with ``output="urls"``. Files are written as
        ``<dest_dir>/<prefix>-<page number><ext>``, where the extension is the
        one the server served (``.webp`` for parse, ``.png`` for redact); the
        directory is created if needed.

        Page images are served as **short-lived presigned URLs** (~15 min), so download
        promptly. If they have expired, re-poll with :py:meth:`get_job` — the server mints
        fresh URLs for the full 24 h the job lives — and call this again.

        Args:
            result: The dict returned by an op that produced page images.
            dest_dir: Folder to write into. Created if it does not exist.
            prefix: Filename stem; defaults to ``page`` → ``page-1.png``.
            max_page_bytes: Largest page image accepted, in bytes (after any
                content decoding). Default 200 MiB. A larger page stops the
                download and raises ``HyperAPIError``.

        Each page is streamed to a temporary file in ``dest_dir`` and renamed
        into place when complete, so a failure never leaves a partial page.
        Image URLs must be https (plain http only when ``base_url`` is http).

        Returns:
            The written paths, in page order.

        Raises:
            ValueError: If the result carries no pages, or a page has no ``image_url``.
            HyperAPIError: If a download fails — an expired URL reports as such — or a
                page exceeds ``max_page_bytes``, or an image URL is not https.
        """
        _check_page_cap(max_page_bytes)
        targets = _page_targets(result, dest_dir, prefix)
        for url, _ in targets:
            _check_server_url(url, base_url=self.base_url, what="page image URL")
        Path(dest_dir).mkdir(parents=True, exist_ok=True)

        written: list[Path] = []
        for url, path in targets:
            self._download_page(url, path, max_page_bytes)
            written.append(path)
        return written

    def _download_page(self, url: str, path: Path, max_page_bytes: int) -> None:
        """Stream one presigned page image to ``path`` (atomically, size-capped)."""
        partial = _partial_path(path)
        try:
            # Presigned URLs carry their own SigV4 signature — send no API-key headers.
            # follow_redirects: S3 answers a cross-region GET with a 307 to the regional
            # endpoint, which would otherwise surface as a baffling "HTTP 307".
            with self._client.stream("GET", url, follow_redirects=True) as resp:
                _check_download_reply(
                    resp, path, base_url=self.base_url, max_page_bytes=max_page_bytes
                )
                received = 0
                with open(partial, "wb") as f:
                    for chunk in resp.iter_bytes():
                        received += len(chunk)
                        if received > max_page_bytes:
                            raise _page_too_large(path, max_page_bytes)
                        f.write(chunk)
            os.replace(partial, path)
        except httpx.RequestError as e:
            raise _download_transport_error(path, e) from None
        finally:
            try:
                partial.unlink()
            except FileNotFoundError:
                pass

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
        the two legs concurrently on the server, reusing the shared
        ``document_key`` so OCR is computed once, so total wall-clock time
        ≈ max(parse, extract) + a small polling overhead, not their sum.
        Polls both jobs round-robin via ``wait_for_jobs``.

        Returns:
            ``{"ocr": <parse text>, "data": <extract job envelope>}``.
            ``data`` is the extract job's full inference envelope (the same
            dict :py:meth:`extract` returns), not the fields themselves: the
            structured fields are at ``result["data"]["result"]["entities"]``,
            ``result["data"]["result"]["line_items"]`` and
            ``result["data"]["result"]["summary"]``.
        """
        path = self._resolve_path(file_path, image_path)
        document_key = self.upload_document(path)
        parse_job = self._submit_via_doc_key(
            "/v1/parse",
            "parse",
            document_key,
            params={},
        )
        extract_job = self._submit_via_doc_key(
            "/v1/extract",
            "extract",
            document_key,
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
