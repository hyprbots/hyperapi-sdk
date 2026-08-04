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

### Changed

- Parse and extract defaults can defer to organization-level processing policy;
  explicitly supplied fast or advanced modes remain authoritative.
- Long-running polling uses a one-hour default deadline and honors both forms of
  the HTTP `Retry-After` header.

### Fixed

- Polling recovers from rate-limit responses when the retry window fits within
  the caller's deadline.
- Structured server errors surface their human-readable message.
- Empty completed-job results return an empty object consistently.
- Schema strings distinguish JSON content from JSON file paths.
