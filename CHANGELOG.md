# Changelog

All notable changes to `hyperapi-sdk` are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Documentation

- **Large advanced-parse documents are now described.** A server-side change
  admits advanced parses beyond the 60-page / 50 MB `413` — up to 500 pages and
  512 MiB — for callers that satisfy every condition: `mode="advanced"`,
  asynchronous submission, an org on the `custom` tier, and an API key rather
  than the dashboard. Availability is per-deployment.

  **No SDK code change was needed and none was made.** The SDK already submits
  with `X-Async: true`, already imposes no client-side size or page limit, and
  the gateway tags API-key traffic as `api` on its own. What was missing was any
  description of how such a job *behaves*, which differs from an ordinary parse:

  - the completed result carries **no inline `pages`/`ocr`** — the payload sits
    behind a presigned `result["result_url"]`, re-signed on every poll and valid
    for minutes;
  - `result["metadata"]["gaps"]` lists page ranges that failed after retries.
    The document still completes, and only pages that processed are billed;
  - segment progress (`segments_total` / `segments_done` / `segments_failed`)
    is on the job envelope from `get_job()`, not on what `wait_for_job()`
    returns.

  Documented in the README, `submit_parse()`, and `parse()` (sync + async).

  Note the default `poll_timeout` of **3600 s can be shorter than a 500-page
  job**. `wait_for_job` raises `JobTimeoutError` at the deadline, but the job
  keeps running server-side and stays retrievable via `get_job()` for 24 h. Pass
  a larger `poll_timeout` for these documents.

## [0.6.0] — 2026-07-24

Ships everything below. The **Basic/Advanced extract product split** landed in
0.5.0 (the `category` keyword) and the **Edit API** + reliability fixes in 0.6.0;
they are grouped here because both minors released together. `pyproject`/
`__version__` are at 0.6.0.

Basic/Advanced extract product split. Additive — `extract()`/`submit_extract()`
keep their existing signatures plus a new keyword-only `category` defaulting to
`"financial"` (today's behavior).

### Fixed

- **Async job polling no longer fails on a rate-limit `429`.** Job-status polls
  share the submit's per-organization rate bucket (rolling 60 s window), so on
  the **free tier (1 req / 60 s)** the submit spent the token and the immediate
  first poll `429`ed — aborting the whole `wait_for_job`. This affected *every*
  submit-then-poll convenience method (`parse`, `extract`, `extract_advanced`,
  `classify`, `split`, `redact`, `edit` / `edit_detect` / `edit_fill`,
  `process`), not just `extract_advanced`.
  Now `wait_for_job` / `wait_for_jobs` / `wait_for_batch` catch the `429`, sleep
  the server's `Retry-After`, and resume within the job's `poll_timeout`; if the
  remaining budget can't outlast the window they raise `RateLimitError` (with
  `retry_after` intact) so you can resume manually. A single-shot `get_job()`
  still raises immediately — only the waiting helpers back off.
- **`Retry-After` HTTP-date form is now honored.** `_parse_retry_after` parsed
  only delta-seconds; an RFC-7231 HTTP-date (which a fronting CDN/ALB may emit)
  silently fell back to 60 s, producing a wrong backoff. Both forms are parsed.
- **`wait_for_job` no longer returns `None` / leaks the raw envelope.** A
  completed job whose `result` is `null` or absent now returns `{}` (matching
  `wait_for_jobs`) instead of `None` — which crashed documented
  `result["result"]` access — or the raw job envelope, which carried server-side
  status/timing/receipt fields.
- **API-key scrubbing now covers service-account keys.** `_strip_api_key`
  redacted `hk_live_*` / `hk_test_*` but not `hk_service_*`; a leaked service key
  echoed in a server error body could reach exception text and logs.

### Added

- **Edit API** (sync + async) — detect the blank fillable fields on a form, then
  render values onto it. Two legs, mirroring the server's two-call flow:
  `edit_detect(file, *, markdown_assist=False)` → `form_schema` + page images,
  and `edit_fill(detect_job_id, *, values=... | content=...)` → rendered pages +
  the `fills` that were drawn. A field's position in `form_schema` is its fill
  index; `values` accepts `{"0": "Jane"}` or `[{"index": 0, "value": "Jane"}]`.
  `content` (with `natural_language=True`) has one model call map free text onto
  the schema and returns the mapping for review, so a UI can let the user correct
  values and re-submit deterministically. Also `edit(file, *, content=...)`,
  which chains both legs in one call, and the matching `submit_edit_detect()` /
  `submit_edit_fill()` for explicit job control.

  The one-call `edit()` is **content-only by design** — it detects the schema for
  the first time, so the caller cannot yet know the indices `values` are keyed by,
  and detection is a model call whose field ordering is not stable between runs.
  Fill by index through the two legs instead.

  ```python
  detected = client.edit_detect("intake_form.pdf", markdown_assist=True)
  filled = client.edit_fill(detected["detect_job_id"], values={"0": "Jane Doe"})
  print(filled["result"]["pages"][0]["image_url"])
  ```

  Page images arrive as short-lived presigned `image_url`s (not bytes) — download
  promptly, or re-poll with `get_job()` for fresh URLs. Metered once at detect;
  the fill leg is included, so re-rendering after a correction is free. Failures
  raise the new `EditError`.

- **`download_pages(result, dest_dir, *, prefix="page")`** (sync + async) — writes a
  result's page images into a folder, so callers stop hand-rolling a fetch loop against
  presigned URLs. Accepts whatever the op returned: `edit_detect()` (blank pages),
  `edit_fill()` / `edit()` (rendered pages, at either nesting), and
  `parse(include_image=True)`. Files are `<prefix>-<page number>.<ext>`, with the page
  number read from `page` or `page_number` and the extension taken from the URL (edit
  serves `.png`, parse `.webp`). The destination folder is created if missing; the async
  client downloads pages concurrently. An expired URL raises a `HyperAPIError` that says
  so and points at `get_job()` rather than surfacing a bare 403.

  ```python
  detected = client.edit_detect("intake_form.pdf")
  filled = client.edit_fill(detected["detect_job_id"], values={"0": "Jane Doe"})
  client.download_pages(filled, "out/", prefix="filled")   # out/filled-1.png …
  ```

  Not applicable to `redact()`, which returns `images` as inline base64 strings rather
  than URLs — decode those with `base64.b64decode()`.

- **Batch API** (sync + async) — async, deferred processing of many documents:
  `create_batch(endpoint=..., document_keys=[...])` → `{batch_id, status,
  total_items}`, plus `get_batch()`, `list_batches()`, `cancel_batch()`,
  `wait_for_batch()`, and a `create_batch_from_files()` convenience that uploads
  first. Supports an `Idempotency-Key` (resubmit returns the same batch). MVP
  endpoints: `/v1/parse`, `/v1/classify`, `/v1/split`, `/v1/redact`.

- **`create_batch(webhook_url=..., metadata=...)`** (sync + async, also via
  `create_batch_from_files`) — optional keyword args, sent in the `POST /v1/batch`
  body only when set. `webhook_url` registers a completion callback; `metadata`
  attaches caller-supplied key/value tags echoed back on `get_batch()` /
  `list_batches()` and in the webhook payload.

  ```python
  batch = client.create_batch_from_files(
      endpoint="/v1/classify", file_paths=["a.pdf", "b.pdf", "c.pdf"]
  )
  done = client.wait_for_batch(batch["batch_id"])   # or poll get_batch()
  ```

- **`create_batch(parse_mode="advanced")`** — advanced (layout-aware)
  parse is now available for `/v1/parse` batches on paid plans (Pro/Enterprise;
  403 otherwise). No signature change — `parse_mode` already existed; this
  documents the now-live capability (the prior docstring said advanced was "not
  yet batchable"). Advanced batch runs on the lowest-priority lane and yields to
  interactive traffic (24h SLA).

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
  fast text extraction) or `"advanced"` (advanced layout-aware parsing for
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
