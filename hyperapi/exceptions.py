"""HyperAPI Exceptions.

The taxonomy is intentionally narrow. Every error carries `status_code` and
`request_id` so callers can branch on the specifics without a separate class
for every server-side failure mode.
"""

from __future__ import annotations

import re


# Covers every issued key family: external `hk_live_*` / `hk_test_*`, plus
# service-account `hk_service_*` (per the platform's four auth strategies). A
# secret scrubber must fail safe — a new prefix should be added here the moment
# it ships, since an un-scrubbed key in an error body is a credential leak.
_API_KEY_PATTERN = re.compile(r"hk_(?:live|test|service)_[A-Za-z0-9_\-]+")


def _strip_api_key(text: str | None) -> str | None:
    """Scrub `hk_live_*` / `hk_test_*` / `hk_service_*` substrings from server
    messages before they end up in exception text. Defends against the rare
    server response that echoes the request header into the error body."""
    if not text:
        return text
    return _API_KEY_PATTERN.sub("hk_***redacted", text)


class HyperAPIError(Exception):
    """Base exception for HyperAPI errors.

    Attributes:
        message: Human-readable description.
        status_code: HTTP status code if the error originated from an HTTP
            response; None for client-side errors.
        request_id: The X-Request-ID we sent for the originating call. Use
            this when filing a support ticket — the backend logs are keyed
            by it.
    """

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        request_id: str | None = None,
    ):
        self.message = _strip_api_key(message) or message
        self.status_code = status_code
        self.request_id = request_id
        super().__init__(self.message)


class AuthenticationError(HyperAPIError):
    """Raised when the API key is missing, malformed, or rejected by the server (401)."""
    pass


class RateLimitError(HyperAPIError):
    """Raised when the server returns 429 Too Many Requests.

    Wait `retry_after` seconds before retrying — the server tells us exactly
    when the sliding window will allow another request.

    Attributes:
        retry_after: Seconds to wait before retrying (parsed from the
            `Retry-After` response header).
        tier: The plan tier on the API key (e.g., "free", "pro", "enterprise").
        limit: The numeric limit that was exceeded (per the configured window).
        window_seconds: Length of the sliding window the server's rate limiter
            uses, in seconds. ``limit`` is "N requests per ``window_seconds``."
            The free tier is 1 per 60s — NOT 1 RPS. (Bug #86 b.) Older server
            versions may not surface this; in that case it stays ``None`` and
            callers should fall back to the public docs.
        degraded: True when the server reports temporary rate-limit service
            degradation rather than a customer exceeding their configured
            quota. Treat it as transient and retry after ``retry_after``.
    """

    def __init__(
        self,
        message: str = "Rate limit exceeded",
        *,
        retry_after: int = 60,
        tier: str | None = None,
        limit: int | None = None,
        window_seconds: int | None = None,
        degraded: bool = False,
        status_code: int | None = 429,
        request_id: str | None = None,
    ):
        super().__init__(message, status_code=status_code, request_id=request_id)
        self.retry_after = retry_after
        self.tier = tier
        self.limit = limit
        self.window_seconds = window_seconds
        self.degraded = degraded


class JobTimeoutError(HyperAPIError):
    """Raised when a polling loop exceeds its `poll_timeout` budget.

    The job is still running on the server — the SDK has just stopped waiting.
    Customers can resume by passing the `job_id` to `client.get_job()` or
    `client.wait_for_job()` with a larger timeout.
    """

    def __init__(
        self,
        message: str,
        *,
        job_id: str,
        elapsed_s: float,
        request_id: str | None = None,
    ):
        super().__init__(message, status_code=None, request_id=request_id)
        self.job_id = job_id
        self.elapsed_s = elapsed_s


class ParseError(HyperAPIError):
    """Raised when document parsing fails (sync HTTP error or job status=failed)."""
    pass


class ExtractError(HyperAPIError):
    """Raised when field extraction fails (sync HTTP error or job status=failed)."""
    pass


class ClassifyError(HyperAPIError):
    """Raised when document classification fails (sync HTTP error or job status=failed)."""
    pass


class SplitError(HyperAPIError):
    """Raised when document splitting fails (sync HTTP error or job status=failed)."""
    pass


class RedactError(HyperAPIError):
    """Raised when redaction/deidentification fails (sync HTTP error or job status=failed)."""
    pass


class EditError(HyperAPIError):
    """Raised when form detect/fill fails (sync HTTP error or job status=failed).

    Covers both legs of the two-call edit flow — `/v1/edit/detect` and
    `/v1/edit/fill` — since they share one job taxonomy server-side.
    """
    pass


class DocumentUploadError(HyperAPIError):
    """Raised when the presigned-URL flow fails (presigned fetch or S3 PUT)."""
    pass
