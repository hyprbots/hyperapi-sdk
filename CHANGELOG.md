# Changelog

Notable user-facing changes to the HyperAPI Python SDK are documented here.
The project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
