# Changelog

All notable changes to `hyperapi-sdk` are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Basic/Advanced extract product split. Additive — `extract()`/`submit_extract()`
keep their existing signatures plus a new keyword-only `category` defaulting to
`"financial"` (today's behavior).

### Added

- **`extract(category=...)` / `submit_extract(category=...)`** (sync + async) —
  the Basic extractor. `category="financial"` (default) runs the two-leg IDP
  adapter (invoices/receipts); `category="non_financial"` runs a generic
  single-pass extractor. Same envelope, same submit+poll path.

- **`extract_advanced()` / `submit_extract_advanced()`** (sync + async) —
  Advanced extraction. Auto-detects the document type (no `category`):
  invoice-family documents route to the two-leg IDP adapter, everything else to
  schema-less grounded extraction. Submits to `/v1/extract-omni`.

  ```python
  client.extract("invoice.pdf")                          # Basic, financial
  client.extract("policy.pdf", category="non_financial") # Basic, generic
  client.extract_advanced("unknown.pdf")                 # Advanced, auto-detect
  ```

- **`parse_mode` on all extract methods** — `extract()`, `submit_extract()`,
  `extract_advanced()`, `submit_extract_advanced()` (sync + async) take a
  keyword-only `parse_mode` selecting the Stage-1 OCR engine: `"fast"` (default,
  Paddle text extraction) or `"advanced"` (Chandra layout-aware parsing for
  dense tables/forms; paid tiers). Independent of `category`; rides as
  `?parse_mode=` on `/v1/extract` and `/v1/extract-omni`. Mirrors parse's `mode`
  OCR-depth knob, now that the backend supports it on the extract endpoints.

  ```python
  client.extract("invoice.pdf", parse_mode="advanced")
  client.extract_advanced("form.pdf", parse_mode="advanced")
  ```

### Fixed

- Corrected `extract()` docstrings that described a `mode="omni"` route — omni
  extraction is the Advanced surface (`/v1/extract-omni`), now exposed via the
  dedicated `extract_advanced()` method.

### Removed

- `ocr_engine` parameter from all client methods (OCR engine selection is
  handled server-side; use `mode` for OCR depth on parse).
- `mode` parameter from `extract()` / `submit_extract()` (sync + async). It did
  not select the Advanced tier — the server routes Basic vs Advanced by
  `category`, never `mode`, so `mode="advanced"` silently ran Basic. Advanced
  extraction is the dedicated `extract_advanced()` method; Basic takes only
  `category`. (`mode` was never an OCR-depth knob on extract — that is parse-only.)

## [0.4.0] — 2026-06-10

Platform API sync. No breaking changes — purely additive (all new parameters
are keyword-only with backward-compatible defaults).

### Added

- **`parse(mode=...)` / `submit_parse(mode=...)`** (sync + async) —
  `mode="advanced"` runs layout-aware structured OCR: each
  `result["pages"]` entry gains a `structured` dict with `html`, `markdown`,
  and `regions`. Default `mode="fast"` is unchanged behavior.

  ```python
  result = client.parse("report.pdf", mode="advanced")
  print(result["pages"][0]["structured"]["markdown"])
  ```

- **`classify(options=...)` / `split(options=...)`** (and their `submit_`
  twins, sync + async) — pass service-level knobs as a dict, sent as a JSON
  form field and validated server-side. Classify: `mode`
  (`"fast"|"balanced"|"thorough"` — pipeline depth, distinct from the top-level
  `mode=` task selector), `active_classes`, `custom_llm_classes`,
  `system_instruction`, token/page budgets. Split: `use_thinking`,
  `segment_classes`, `extend_segment_classes`, `custom_domain_guidelines`.

  ```python
  client.classify("doc.pdf", options={"mode": "thorough", "active_classes": ["invoice", "po"]})
  client.split("binder.pdf", options={"use_thinking": False})
  ```

- **`list_recent_jobs(limit=20, source=None)`** (sync + async) — list the
  org's recent jobs as summary rows (`GET /v1/jobs/recent`). `limit` valid
  range is [1, 100] — out-of-range values are not clamped; the server
  silently falls back to the default 20. `source` filters `"api"` vs
  `"playground"`.

- **`delete_job(job_id)`** (sync + async) — cancel a job
  (`DELETE /v1/jobs/{job_id}`). Server semantics are *cancel*: in-flight jobs
  stop (status becomes `"cancelled"`); completed jobs keep their result.
  Idempotent.

### Documentation

- `extract(mode="omni")` documented — schema-less grounded extraction.
- `CREDENTIALS` PII type (passwords, API keys, tokens, secrets) documented for
  `redact()` / `pii_config`.

### Internal

- 429 envelope parsing consolidated into a single shared
  `_rate_limit_error_from()` helper (was duplicated four times across the two
  clients).

## [0.3.0] — 2026-06-02

Redact / deidentify. No breaking changes — purely additive.

### Added

- **`redact()` / `submit_redact()`** (sync + async) — mask or deidentify PII in
  a document. `mode="redact"` applies black boxes; `mode="deidentify"` overlays
  synthetic replacements. `include_logos=True` also masks logos. `pii_config`
  (`{"mode": "extend"|"replace", "types": [...]}`) customizes the detected PII
  type set. Same submit+poll path as `extract` — timeout-immune at any edge.

  ```python
  result = client.redact("contract.pdf", mode="deidentify", include_logos=True)
  for page_png_b64 in result["images"]:
      ...
  ```

- **`RedactError`** — raised on a redact HTTP error or `status=failed` job,
  consistent with the other per-op error classes.

## [0.2.0] — 2026-05-26

Async client. No breaking changes — purely additive.

### Added

- **`AsyncHyperAPIClient`** — an `async`/`await` twin of `HyperAPIClient` for
  use inside event-loop-based runtimes (FastAPI, agent loops, Discord/Slack
  bots, polyglot microservices). Same constructor signature, same method
  names, same `Job` dataclass, same typed exceptions — swap the import and
  prefix calls with `await`.

  ```python
  import asyncio
  from hyperapi import AsyncHyperAPIClient

  async def main():
      async with AsyncHyperAPIClient() as client:
          result = await client.extract("invoice.pdf")
          print(result)

  asyncio.run(main())
  ```

- **`examples/async_quickstart.py`** — standalone script + FastAPI integration
  snippet showing the recommended one-client-per-app + lifespan-close pattern.
- **`pytest-asyncio>=0.23`** added to the `[dev]` optional dependencies; pytest
  `asyncio_mode = "auto"` enabled so async test functions don't need per-test
  markers.
- **Async-aware test fixtures** in `tests/conftest.py`: `async_client` and
  `slow_poll_async_client` mirror the existing sync fixtures.
- **Full mirror of the L1 mocked contract suite** as async tests
  (`tests/test_async_*.py`) — every sync `test_contract_*.py` has an async
  twin. `respx` mocks the underlying `httpx` transport, so the same fixtures
  work for both clients.

### Notes

- The async client uses `httpx.AsyncClient` — no new runtime dependency
  (`httpx>=0.25` already provides both).
- `User-Agent` carries an `-async` suffix
  (`hyperapi-sdk-python/0.2.0-async (httpx/…; Python/…)`) so backend log
  analytics can distinguish sync vs async traffic without sniffing other
  signals.
- `AsyncHyperAPIClient.wait_for_jobs(...)` uses `asyncio.gather` under the
  hood — true concurrent polling on one event loop. The first failure raises
  and cancels the rest; the sync client's "comma-joined pending job_ids"
  shape on timeout becomes a single job_id under async because gather raises
  immediately on the first `JobTimeoutError`.
- The internal MCP server (`services/hyperapi-mcp/server/hyperapi_client.py`)
  still ships its own `aiohttp`-based client — migrating it to the new
  `AsyncHyperAPIClient` is a follow-up cleanup, not blocking 0.2.0.

### Fixed

- **401 and 402 error messages are now environment-aware.** Previously
  hardcoded `https://apis.hyperbots.com/dashboard` regardless of the
  client's `base_url` — confusing for customers on staging, on-prem, or
  self-hosted deployments. The messages now reference `self.base_url`
  ("Invalid API key for `<base_url>`. Check your HyperAPI dashboard for the
  correct key.") and never mention the prod URL by name. Applies to both
  `HyperAPIClient` and `AsyncHyperAPIClient`.

### Compatibility

- **No breaking changes.** `client.py` is essentially v0.1.0 + the
  env-aware error message fix above. Existing imports (`HyperAPIClient`,
  `Job`, every exception class) work unchanged.
- The only public surface added is `AsyncHyperAPIClient`, re-exported from
  the top-level `hyperapi` package.
- Internal helpers (`_OP_TO_ERROR`, `_parse_retry_after`, `_safe_text`,
  `_server_message`, `_request_id_of`) are now imported by `async_client.py`
  from `client.py` — they remain leading-underscore-private and not part of
  the public API.

[0.2.0]: https://github.com/hyprbots/hyperapi-sdk/releases/tag/v0.2.0

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
