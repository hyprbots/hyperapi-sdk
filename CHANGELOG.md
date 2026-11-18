# Changelog

Notable user-facing changes to the HyperAPI Python SDK are documented here.
The project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.7.1] - 2026-11-17

First release on PyPI: `pip install hyperapi-sdk`.

### Changed

- License changed from MIT to **Apache-2.0** (adds an explicit patent grant). Earlier GitHub-only versions remain MIT.

- `schema` (on `extract` / `submit_extract`, both clients): a `str` is always
  parsed as JSON text and never read as a file name; to load a schema file, pass
  `pathlib.Path("template.json")`. A string that is not valid JSON raises
  `ValueError` pointing at `pathlib.Path`. **Behaviour change for installs from
  GitHub:** code that passed a file path as a plain string must wrap it in
  `Path(...)`. This also fixes `OSError: File name too long` for JSON strings
  over ~255 characters.
- `process()` docstring: `data` is the extract job's envelope; fields are at
  `result["data"]["result"]["entities"]` / `["line_items"]` / `["summary"]`.
- Status polls retry only network errors and 5xx. Every 4xx (400, 403, 422, …)
  now fails on the first answer instead of being retried, and retries stop when
  the wait's own timeout passes: `wait_for_job` / `wait_for_jobs` /
  `wait_for_batch` then raise `JobTimeoutError` rather than sleeping past it.
- `download_pages` takes `max_page_bytes` (default 200 MiB). Pages are streamed
  to a temporary file and renamed into place, so a failed download never
  leaves a partial page. On the async client at most 8 pages download at once.
- Async `create_batch_from_files` uploads at most 8 files at once and cancels
  the remaining uploads when one fails.
- Python 3.13 is supported and tested.

### Fixed

- `wait_for_batch` retries a transient 5xx or network error during a poll
  instead of aborting the whole wait.
- Malformed server replies raise the SDK's own errors (with `status_code` and
  `request_id`) instead of `JSONDecodeError`, `KeyError` or `AttributeError`:
  a non-JSON or non-object body on upload, submit, job poll or batch calls, or
  a reply without `document_key` / `upload_url` / `job_id` / `batch_id`. A
  failed job whose `error` is an object no longer raises `TypeError`.
- `download_pages` network errors raise `HyperAPIError` instead of raw `httpx`
  exceptions.
- `JobTimeoutError` and `JobCancelledError` (and every other SDK exception)
  survive `pickle`, so they can be returned from `multiprocessing` and
  `concurrent.futures` workers.
- `base_url` accepts an `httpx.URL` (and other non-string values) by converting
  it with `str()`.

### Security

- A `str` schema can no longer make the SDK read and upload a local file, so an
  application forwarding user-supplied schema text cannot be steered into
  sending files from its host.
- The API key (argument or `HYPERAPI_KEY`) is stripped of surrounding whitespace,
  so a trailing newline from a file or secret mount no longer breaks requests. A
  key containing inner whitespace, control or non-ASCII characters raises
  `AuthenticationError` whose message does not include the key. `client.api_key`
  is now read-only.
- Transport errors (connection failures, timeouts) raise the SDK exception
  without chaining the underlying `httpx` exception (`__cause__` is `None`).
  The chained exception carried the request headers, including `X-API-Key` in
  clear, and could print the full key in tracebacks and error reporters.

- `base_url` (and `HYPERAPI_URL`) must be an absolute `http(s)` URL with a host;
  anything else raises `ValueError`. Plain `http` to a non-loopback host warns,
  because the API key would travel unencrypted.
  URLs with a username or password (`user@host`), a query string or a fragment
  are rejected.
- Minimum `httpx` raised to 0.28.1, which pulls in an `h11` without the
  chunked-encoding request-smuggling flaw (CVE-2025-43859).
- Release pipeline: build and publish run as separate jobs; only the publish job
  can request a PyPI token, and it runs no repository code. All GitHub Actions
  are pinned to commit SHAs, checkouts do not keep credentials, and CI runs
  `pip-audit` on every change. Dependabot keeps actions and dependencies current.
- README warns that the `hyperapi` name on PyPI is not ours.
- Job and batch ids are percent-encoded in URL paths, so an id such as
  `"../limits?x="` cannot reach another endpoint or add a query string; empty
  ids raise `ValueError`.
- `base_url` with an empty query or fragment (`https://host?`, `https://host#`)
  is rejected.
- Presigned URLs from the server (`upload_url`, page `image_url`, and the final
  hop of a download redirect) must be `https`; plain `http` is accepted only
  when `base_url` itself is `http` (local testing). Presigned requests still
  carry no API key.
- `upload_document` refuses directories, pipes and devices before reading.
- Release build: the build tools and setuptools are installed from a
  hash-locked requirements file and the wheel is built without isolation, so
  the release is built by exactly the pinned, hash-checked setuptools.

## [0.7.0] - 2026-11-04

The distribution is now named `hyperapi-sdk` (import name `hyperapi`). Install specs that name
`hyperapi @ git+…` must change to `hyperapi-sdk @ git+…`.

### Added

- Synchronous and asynchronous clients for parse, extract, classify, split,
  redact, edit, job-management, and batch operations.
- Basic and advanced extraction, including optional JSON schema templates.
- Explicit submit-and-poll helpers for long-running document jobs.
- Page-image downloads for operations that return signed image URLs.
- Typed exceptions with request IDs and sanitized API-key text.
- Python 3.9-3.12 contract-test coverage and typed-package metadata.
- `force_refresh` on `parse`, `extract`, `classify`, `split`, `redact` and their
  `submit_*` twins: re-run instead of returning the cached result (#47, #49).
- `force_refresh` on `extract_advanced` / `submit_extract_advanced`.
- `redact(output="urls")`: masked pages returned as presigned `pages[*].image_url`
  links instead of inline base64, with a higher page cap where offered;
  `download_pages()` accepts the result (#52).
- `get_limits()` for `GET /v1/limits`: the account's effective upload ceiling and
  per-operation page caps. Page caps are plan-dependent and should be read from
  this call rather than hard-coded.
- Parse keyword arguments `include_layout`, `parse_figures` and `groups_profile`,
  matching the `/v1/parse` query parameters.
- Upload content types for Office documents (`.docx`, `.doc`, `.xlsx`, `.xls`,
  `.pptx`, `.ppt`), text and data (`.csv`, `.txt`, `.md`, `.rtf`, `.json`,
  `.xml`), fixed-layout pages (`.xps`, `.oxps`), email (`.eml`, `.msg`) and
  HEIC/HEIF images (#56).
- Every exception exposes `code`, the machine-readable error code from the API's
  error envelope when one was returned.

### Changed

- Parse and extract defaults can defer to organization-level processing policy;
  explicitly supplied fast or advanced modes remain authoritative.
- Long-running polling uses a one-hour default deadline and honors both forms of
  the HTTP `Retry-After` header.
- `parse()` / `submit_parse()`: `include_boxes` and `include_image` default to
  `None` instead of `False`. When omitted, nothing is sent and the API default
  applies (boxes, page images and layout are on in fast mode); pass `False` to
  opt out. Code that passed `include_boxes=True` keeps working.
- A job removed with `delete_job()` is reported by `get_job()` as `failed` with
  an error starting `cancelled:`; `wait_for_job()` now raises the new
  `JobCancelledError` (carrying `job_id`) instead of polling to its deadline.
- Quota exhaustion (`code == "page_quota_exceeded"`) is not retried by the
  waiting helpers; it is raised immediately.
- Edit methods raise `EditError("The edit API is not available for this
  account.")` on accounts where the edit API is not enabled.

### Documentation

- Parse docstrings and the tutorial describe the current response shape: fast
  PDF, XPS and image pages always carry `dimensions`, `page_size` and
  `background`; boxes return line-level `boxes` plus per-word
  `word_boxes` with typography (`font`, `size`, `color`, `bold`, `italic`,
  `background`, `font_source`). No SDK code change — the fields pass through
  in the raw result dict (#54).
- `list_recent_jobs()` rows carry `document_key`; use it, not `filename`, to
  map jobs back to documents (#50).
- README "Limits by operation" table describing per-operation page ceilings and
  `GET /v1/limits` (#51), since revised to point at `get_limits()`.
- README documents the fast-mode parse response (`boxes`, `word_boxes`,
  `layout`, `blocks`, `groups`, `gaps`, `figure_parse`), the full list of
  supported upload formats, and the parse, redact and job-listing parameters.

### Fixed

- Polling recovers from rate-limit responses when the retry window fits within
  the caller's deadline.
- Structured server errors surface their human-readable message.
- Empty completed-job results return an empty object consistently.
- Schema strings distinguish JSON content from JSON file paths.
- `redact(output=...)` rejects values other than `"inline"` and `"urls"`
  client-side with `ValueError` instead of sending them (#53).
