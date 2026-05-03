# Changelog

All notable changes to `hyperapi-sdk` are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] — 2026-05-03

First public release.

### Added

- **Submit + poll architecture for long-running ops.** `parse`, `extract`,
  `classify`, `split`, and `process` now submit asynchronously (`X-Async: true`)
  and poll `GET /v1/jobs/{id}` under the hood. Each individual HTTP request
  stays sub-second, so the SDK is timeout-immune at any CDN edge — tested
  against CloudFront's 30 s `origin_response_timeout` where the previous
  synchronous shape returned 504s on every non-trivial extract.
- **`Job` dataclass** returned by the explicit `submit_<op>(...)` methods —
  carries `job_id`, `status`, `poll_url`, and `op` for typed-error mapping
  on failure.
- **Polling helpers**: `client.get_job(job_id)` (one-shot status check) and
  `client.wait_for_job(job, *, timeout, interval)` (blocking poll loop).
  `client.wait_for_jobs([job1, job2])` round-robin polls multiple jobs in
  parallel — used internally by `process()` to interleave parse + extract
  polls.
- **`RateLimitError`** with `retry_after`, `tier`, and `limit` parsed from
  the `Retry-After` response header and the JSON body.
- **`JobTimeoutError`** raised when `wait_for_job` exceeds its `poll_timeout`.
- **`request_id` on every exception.** Pass it to support tickets — the
  backend logs are keyed by `X-Request-ID`.
- **Constructor knobs** for polling: `poll_interval` (default 3 s),
  `poll_timeout` (default 1800 s), `poll_max_transient_retries` (default 3).
  Defaults match the platform playground's polling cadence.
- **Per-call polling overrides**: `client.extract(file, poll_timeout=600)`.
- **Sanitized `__repr__`** masks the API key (`hk_***xxxx`).
- **`User-Agent: hyperapi-sdk-python/0.1.0`** sent on every request.
- **`py.typed`** marker so mypy/pyright pick up inline type hints.
- **Library logger** (`logging.getLogger("hyperapi")`) with a `NullHandler`.
  Customers opt into INFO/DEBUG with `setLevel`.
- **`socks` extra** for customers behind SOCKS proxies: `pip install hyperapi-sdk[socks]`.
- **API-key scrubbing** in error text. Server bodies that echo a `hk_live_*`
  or `hk_test_*` substring are sanitized to `hk_***redacted` before going
  into the exception's `message`.

### Notes

This release intentionally restores the v0.1.x async/poll design that was
stripped in the never-published `0.2.0` artifact (commit `2512454`,
2026-04-15: *"Remove async_mode/poll_job (not publicly released)"*). That
strip was administrative cleanup, not architectural choice — it broke
synchronous extract through CloudFront's edge timeout. v0.1.0 reinstates
the async pattern with industry-standard naming (matching Reducto's
`run_job` shape and LlamaParse's hidden-polling default).

The SDK has never been published to PyPI before. Calling this v0.1.0 reflects
the actual public-release count: zero prior.

### Fixed

- Synchronous `extract` and `process` calls no longer 504 on CloudFront's
  30 s edge timeout — every call goes through submit + poll, where each HTTP
  request finishes in under a second.
- Per-poll transient retry on 5xx / connection errors (3 attempts by default
  with 500 ms delay) absorbs the residual Sentinel-auth flake currently
  tracked as platform bug #84.

### Compatibility

- Public method signatures of `parse`, `extract`, `classify`, `split`,
  `process`, `upload_document` are backward-compatible with the v0.2.0
  shape (no `async_mode` param to remove — was never publicly released).
- Existing imports (`HyperAPIClient`, exception classes) keep working.
- Two new top-level exports: `Job` and `JobTimeoutError`. `RateLimitError`
  is also new — previously rate-limited responses raised the per-op error
  with `status_code=429`; they now raise `RateLimitError` (which still
  inherits from `HyperAPIError`, so `except HyperAPIError` catches both).

[0.1.0]: https://github.com/hyprbots/hyperapi-sdk/releases/tag/v0.1.0
