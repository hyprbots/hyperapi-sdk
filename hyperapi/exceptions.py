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
            this when filing a support ticket — the API's logs are keyed
            by it.
        code: The machine-readable ``error.code`` from the API's error
            envelope (``{"error": {"code", "message", "details"?}}``), when
            the response carried one; None for client-side errors or bodies
            without an envelope. Stable across wording changes, so branch on
            this rather than on ``message``.
        details: The optional ``error.details`` object — the numbers behind
            a refusal (for a quota refusal: ``pages_used``, ``pages_limit``,
            ``pages_pending``). None when the API sent none.
    """

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        request_id: str | None = None,
        *,
        code: str | None = None,
        details: dict | None = None,
    ):
        self.message = _strip_api_key(message) or message
        self.status_code = status_code
        self.request_id = request_id
        self.code = code
        self.details = details
        super().__init__(self.message)


class AuthenticationError(HyperAPIError):
    """Raised when the API key is missing, malformed, or rejected by the server (401)."""
    pass


class RateLimitError(HyperAPIError):
    """Raised when the server returns 429 Too Many Requests.

    A 429 means one of two things, and ``code`` tells them apart:

    - A **transient rate limit** (requests per window). Wait ``retry_after``
      seconds and retry — the server says when the window reopens. The SDK's
      polling loops do this for you.
    - The **monthly page quota is exhausted** (``code ==
      "page_quota_exceeded"``, ``is_quota_exhausted`` is True). Retrying does
      not help; the quota resets at the next billing period or on a plan
      upgrade. The SDK's polling loops raise this immediately instead of
      backing off.

    Attributes:
        retry_after: Seconds to wait before retrying (parsed from the
            `Retry-After` response header).
        tier: The plan tier on the API key (e.g., "free", "pro", "enterprise").
        limit: The numeric limit that was exceeded (per the configured window).
        window_seconds: Length of the sliding window the server's rate limiter
            uses, in seconds. ``limit`` is "N requests per ``window_seconds``."
            Not every response carries it; it stays ``None`` when absent.
        degraded: True when the server reports temporary rate-limit service
            degradation rather than a customer exceeding their configured
            quota. Treat it as transient and retry after ``retry_after``.
        code: See :class:`HyperAPIError`. ``"page_quota_exceeded"`` marks the
            quota case; ``details`` then carries ``pages_used``,
            ``pages_limit`` and ``pages_pending`` when the API reports them.
    """

    QUOTA_EXHAUSTED_CODE = "page_quota_exceeded"

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
        code: str | None = None,
        details: dict | None = None,
    ):
        super().__init__(
            message,
            status_code=status_code,
            request_id=request_id,
            code=code,
            details=details,
        )
        self.retry_after = retry_after
        self.tier = tier
        self.limit = limit
        self.window_seconds = window_seconds
        self.degraded = degraded

    @property
    def is_quota_exhausted(self) -> bool:
        """True when this 429 is the monthly page quota, not a rate window.

        Waiting ``retry_after`` will not clear it; do not retry automatically.
        """
        return self.code == self.QUOTA_EXHAUSTED_CODE


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


class JobCancelledError(HyperAPIError):
    """Raised by a polling loop when the job was cancelled (``DELETE /v1/jobs/{id}``).

    The job's own status report does not use a separate ``"cancelled"``
    state: ``GET /v1/jobs/{id}`` returns ``status="failed"`` with an
    ``error`` that begins ``"cancelled:"``. The SDK decodes that into this
    exception so a deliberate cancel is never mistaken for a processing
    failure. ``list_recent_jobs()`` summaries do show such jobs as
    ``status="cancelled"``.

    Attributes:
        job_id: The cancelled job.
    """

    def __init__(
        self,
        message: str,
        *,
        job_id: str,
        request_id: str | None = None,
    ):
        super().__init__(message, status_code=None, request_id=request_id)
        self.job_id = job_id


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
