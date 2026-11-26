
<p align="center">
<a href="https://apis.hyperbots.com/"><img src="https://images.g2crowd.com/uploads/vendor/image/1515319/9eadfb55dd882c428f4f82ee306dabcd.png" width="115"></a>
  <p align="center"><strong>HyperAPI: </strong>Stop Prompting, Start Programming Financial Intelligence.</p>
</p>
<p align="center">
  <a href="https://github.com/hyprbots/hyperapi-sdk"><img src="https://img.shields.io/badge/python-3.9+-blue.svg" alt="Python 3.9+"></a>
  <a href="https://github.com/hyprbots/hyperapi-sdk/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-Apache%202.0-blue.svg" alt="License"></a>
</p>

---

**HyperAPI-SDK** is a document intelligence framework composed of Parse, Extract, Classify, Split, Redact, and more APIs. Whether you are dealing with low-quality scans or complex multi-document binders, HyperAPI is engineered for production-grade reliability.

## Architecture

Every operation follows the same **OCR → task** path on the server:

```
                          ┌─────────────────┐
                          │  Upload Document │
                          └────────┬────────┘
                                   │
                    ┌──────────────▼──────────────┐
                    │             OCR              │
                    │                              │
                    │  ┌──────────┐ ┌───────────┐  │
                    │  │   fast   │ │ advanced  │  │
                    │  │ (default)│ │  (layout) │  │
                    │  └──────────┘ └───────────┘  │
                    └──────────────┬──────────────┘
                                   │ OCR output
                    ┌──────────────▼──────────────┐
                    │       Task processing        │
                    │                              │
                    │  parse → (skip, OCR only)    │
                    │  extract → structured extract│
                    │  classify → classification   │
                    │  split → document splitting  │
                    │  redact → redaction          │
                    └─────────────────────────────┘
```

The API runs OCR first, then applies the task you asked for to the OCR output. For `parse`, the task step is skipped — you get the OCR result directly.

## Why Choose HyperAPI?

Commercial LLMs (GPT, Claude, Gemini) understand what they *see*. HyperAPI understands what's *correct*.

Real-World Case: The Billing Typo

```
Invoice Line Item:
  Date: 08/11/2025
  Activity: Hours
  Quantity: 0.15  ← Document shows "0.15" (typo for 0:15)
  Rate: 350.00
  Amount: 87.50

❌ Commercial LLMs: quantity = 0.15  (52.50 ≠ 87.50, math doesn't work)
✅ HyperAPI:        quantity = 0:15  (validates: 0.25 hrs × 350 = 87.50 ✓)
```

## Installation

```bash
pip install hyperapi-sdk
```

The package installs as `hyperapi-sdk` and imports as `hyperapi`. Python 3.9 or later.

> **Install `hyperapi-sdk`, never `hyperapi`.** The `hyperapi` name on PyPI does not
> belong to us. The only official package is
> [`hyperapi-sdk`](https://pypi.org/project/hyperapi-sdk/), published from this repository
> with PyPI Trusted Publishing and signed attestations.

To install from a local clone instead:

```bash
git clone https://github.com/hyprbots/hyperapi-sdk
cd hyperapi-sdk
python3 -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
python -m pip install .
```

## Quick Start

```python
from hyperapi import HyperAPIClient

# Set HYPERAPI_KEY in your environment before starting the application.
client = HyperAPIClient()

# Process a financial document (parse + extract in one call)
result = client.process("invoice.png")

print(result["ocr"])                           # parsed text of the document
fields = result["data"]["result"]              # extracted fields
print(fields["entities"])                      # header and party fields
print(fields["line_items"])                    # validated line items
print(fields["summary"])                       # totals, tax and currency
```

These fields come from the financial extractor, which is the default. If your
organization's default extract category is set to `non_financial`, the fields are
under `result["data"]["result"]["data"]` instead; call `client.extract(...,
category="financial")` to choose explicitly.

## Typed responses

Every method returns a typed model (pydantic v2), so editors complete field
names and type checkers catch typos. The models are generated from the API's
OpenAPI description (`openapi/hyperapi-public.yaml`).

```python
from hyperapi import HyperAPIClient
from hyperapi.types import ParseResponse

client = HyperAPIClient()
result: ParseResponse = client.parse("invoice.pdf")

print(result.result.ocr)                  # typed attribute access
for page in result.result.pages:
    print(page.page_number, len(page.text))

print(result["result"]["ocr"])            # dict-style access still works
data = result.to_dict()                   # a plain dict (e.g. for json.dumps)
```

| Method | Returns |
|---|---|
| `parse()` | `ParseResponse` |
| `extract()`, `extract_advanced()` | `ExtractResponse` |
| `classify()` / `split()` / `redact()` | `ClassifyResponse` / `SplitResponse` / `RedactResponse` |
| `process()` | `ProcessResult` (`.ocr`, `.data`) |
| `get_job()` | `JobEnvelope` (`.result` is the operation's model) |
| `list_recent_jobs()` | `list[RecentJob]` |
| `get_limits()` | `UploadLimits` |
| `create_batch()` / `get_batch()` / `list_batches()` | `BatchCreateResponse` / `BatchStatusResponse` / `BatchListResponse` |

- **New fields are kept.** A field the API adds later is still on the model
  (`result["new_field"]`) and in `to_dict()`.
- **Nothing breaks on an unexpected reply.** If a reply does not match its
  model, you get a `GenericResponse` with the same data and the same access
  styles, and the SDK logs `response_model_mismatch` once.
- **A JSON key that clashes with a method** gets a trailing underscore as an
  attribute: `batch.items_` (the key is still `batch["items"]`).

## Two-Step Pipeline

For more control, use parse and extract separately:

```python
# Step 1: Parse document (OCR)
ocr_result = client.parse("invoice.png")
print(ocr_result["result"]["ocr"])  # plain text, laid out like the page

# Step 2: Extract structured fields (from the same file)
fields = client.extract("invoice.png")
print(fields["result"])

# Parse a document with complex layout
ocr_result = client.parse("complex_table.pdf")
```

## Advanced Parse (structured layout)

`mode="advanced"` (Plus plan or higher) returns structured output: each page gains a
`structured` dict with `html`, `markdown`, and `regions` — tables come back as
tables, not flattened text. Slower than `mode="fast"`. When `mode` is omitted
the SDK sends nothing and the API default applies (`fast` unless your
organization is configured otherwise).

```python
result = client.parse("annual_report.pdf", mode="advanced")
page = result["result"]["pages"][0]
print(page["structured"]["markdown"])   # the page as markdown
print(page["structured"]["regions"])    # typed regions with bounding boxes
```

### Fast-mode response shape

In `fast` mode the API returns boxes, page images and the document layout by
default. The SDK's `include_boxes`, `include_image`, `include_layout` and
`parse_figures` arguments default to `None`, which leaves the choice to the API;
pass `False` (or `parse_figures="none"`) to opt out of a part you do not need.
`mode="advanced"` returns `structured` instead and produces none of the fields
below.

```python
result = client.parse("invoice.pdf")                     # API defaults: everything on
lean   = client.parse("invoice.pdf", include_boxes=False, include_image=False,
                      include_layout=False)              # text and geometry only
```

| Field | Where | Meaning |
|---|---|---|
| `ocr` | `result` | Full text of the document, pages joined by blank lines; prefer `pages` for per-page work |
| `pages[].text` | per page | The page's text |
| `pages[].dimensions` / `page_size` / `background` | per page | Pixel space of every `bbox`; the page's own size (`pt` for PDF/XPS, `px` for images); page background colour. PDF, XPS and image inputs |
| `pages[].boxes` | per page | Line boxes `{text, bbox: [left, top, right, bottom], confidence}`. Off with `include_boxes=False`. Office documents return boxes only together with a page image |
| `pages[].word_boxes` | per page | Per-word boxes with typography: `font`, `size`, `color`, `bold`, `italic`, `background`, `font_source` (`document` from the file's text layer, `model` estimated from pixels). PDF, XPS and images; keys omitted when not stated |
| `pages[].image_url` | per page | Presigned page image, valid about 15 minutes. Off with `include_image=False` |
| `pages[].layout` / `pages[].layout_text` | per page | Detected regions in reading order with `bbox`, and the text rebuilt around them. `result.layout_text` carries the whole document. Off with `include_layout=False` |
| `pages[].gaps` | per page | Large empty vertical bands between two consecutive blocks in the same column, as `{bbox, height, after, before, ratio}`. Present only on pages that have one. Information only |
| `blocks[]` | `result` | The document's components (headings, paragraphs, tables, figures, …) in reading order across all pages: `id` (`b<page>-<n>`), `order`, `page`, `label`, `bbox`, `html`, `heading` (level 1-6), `group`, `furniture`, `style`. `empty: true` marks a block with no text and nothing drawn that is kept because it still occupies its box. `paint` on an embedded-picture block gives its `index` in the page's paint order and the `asset` it was drawn from |
| `groups[]` | `result` | The outline built over `blocks`: `section` and `preamble` groups form the tree; `figure`, `table`, `list`, `formula`, `algorithm`, `note` and `aside` are self-contained; `page_header` / `page_footer` hold running heads and feet (skip them when rebuilding text). Each has `id`, `kind`, `pages`, `children` (block and group ids in reading order), `boxes`, and `level` / `heading` on sections |
| `figure_parse` | `result` | Summary of figure parsing: `requested`, `available`, `blocks_total`, `blocks_parsed`, `blocks_skipped`, `gaps`. Regions that were parsed gain `content` and `parse_status` in `layout` |
| `metadata.groups_profile` | envelope | The grouping profile actually applied, reported when `groups_profile="B"` was requested (falls back to `A` where `B` cannot be used) |

`parse_figures` takes a comma-separated subset of `image`, `chart`, `table` (or
`"none"`) and requires the layout to be on; a parsed chart's `content` is a
markdown table, an image's is its caption or text, a table's is HTML.
`groups_profile="A"` (the default) gives a tree in reading order in which every
block belongs to exactly one group; `"B"` ranks headings by measured type size,
binds captions to their figure or table, joins tables that continue across
pages and is not in reading order (use `blocks[].order`). The full field
reference is on the public docs site at
[apis.hyperbots.com/docs/api/parse](https://apis.hyperbots.com/docs/api/parse).

Re-parsing the same document within 24 hours returns the **cached** OCR output,
so a retry after a bad parse hands back the same bad parse. Pass
`force_refresh=True` to re-run OCR and overwrite the stored result:

```python
result = client.parse("annual_report.pdf", mode="advanced", force_refresh=True)
```

A forced run is billed at full price where a cached one costs nothing, so use
it when a result actually looks wrong — not as a default.

### Large documents (beyond the advanced page cap)

Advanced parse has a page and byte ceiling that depends on your plan; past
either limit the server returns `413`. Read the effective numbers for your
account from `client.get_limits()` rather than hard-coding them:

```python
limits = client.get_limits()                       # GET /v1/limits
print(limits["parse_advanced"])                    # {"max_bytes": ..., "max_pages": ...}
print(limits.get("bulk_parse"))                    # None when the raised path is not offered
```

The raised ceiling (`bulk_parse` in the limits response) applies only when every
one of these holds, or you get the ordinary `413`:

| Condition | Why |
|---|---|
| `mode="advanced"` | `mode="fast"` is never page-capped |
| Submitted **asynchronously** | A multi-hour job can't be served on one connection |
| Plan that includes bulk advanced parse | Hours of processing time is a provisioned privilege |
| Called with an **API key** (not the dashboard) | The playground has a human waiting on a short timeout |
| Within the `bulk_parse` page and byte limits | The hard ceiling for this path |

The SDK already satisfies the async and API-key conditions — every convenience
method submits with `X-Async: true`, and the server records the source of the
request. Nothing to pass; the server decides. The server never tells you *which*
condition you missed, so a surprise `413` on a large document is most likely
the plan.

> **Availability.** The raised ceiling is not offered on every account. If
> `bulk_parse` is absent from `get_limits()`, the standard `parse_advanced`
> ceiling stands.

The document is processed in page ranges, but stays **one**
request — one `request_id`, one usage record, one job. Two things differ from an
ordinary parse:

**1. The result arrives as a URL, not inline.** A 500-page structured result is
far too large to embed in the job, so `pages` and `ocr` are *absent* and you get
a presigned `result_url` instead:

```python
import httpx

job = client.submit_parse("500_page_report.pdf", mode="advanced")
result = client.wait_for_job(job, timeout=7200)     # see the deadline note below

if url := result.get("result_url"):                 # large-document delivery
    payload = httpx.get(url).json()                 # fetch promptly — expires in minutes
    pages = payload["result"]["pages"]
else:
    pages = result["result"]["pages"]                # ordinary inline result

meta = result["metadata"]
print(meta["total_pages"], meta["segments_total"], meta["segments_failed"])
```

`result_url` is re-signed on **every poll** and expires in minutes. If it lapses,
call `get_job(job.job_id)` again for a fresh one — the job itself lives 24 h.

**2. It can complete with `gaps`.** Page ranges that failed after retries are
reported in `metadata["gaps"]`; the document still completes, and **only pages
that actually processed are billed**. `gaps` is always present on a large-document
result, empty when every segment succeeded — so test the field, don't infer
completeness from counts:

```python
for gap in result["metadata"]["gaps"]:
    print(f"pages {gap} missing")
```

To watch progress while it runs, poll `get_job()` yourself — the segment counters
sit on the job envelope, and `wait_for_job` returns only the result:

```python
import time

job = client.submit_parse("500_page_report.pdf", mode="advanced")
while (envelope := client.get_job(job.job_id))["status"] == "pending":
    print(envelope.get("segments_done"), "/", envelope.get("segments_total"))
    time.sleep(30)
```

> **Raise `poll_timeout` for these.** The default is **3600 s (60 min)**, and a
> 500-page advanced parse can exceed it. `wait_for_job` gives up at the deadline,
> but the job **keeps running** server-side — a `JobTimeoutError` is the client
> abandoning the wait, not the platform abandoning the work. Re-attach with
> `client.get_job(job.job_id)` (jobs live 24 h, and progress refreshes that
> window). Pass a deadline that matches the document, e.g.
> `client.parse(..., poll_timeout=7200)`.

## Limits by operation

Upload accepts any supported file up to your byte ceiling, but each operation
enforces its own page ceiling when the document is submitted — **before** any
billable processing runs. The ceilings can differ by plan and change over time,
so read them from the API instead of hard-coding them:

```python
limits = client.get_limits()
for task, ceiling in limits.items():
    if isinstance(ceiling, dict):
        print(task, ceiling["max_bytes"], ceiling.get("max_pages"))   # max_pages None = no page cap
```

| Key in `get_limits()` | Operation | Notes |
|---|---|---|
| `max_upload_bytes` | upload | Your effective upload grant; a task may admit less than this |
| `parse_fast` | Parse (fast) | `max_pages` is `null`: never page-capped |
| `parse_advanced` | Parse (advanced) | Page-capped |
| `bulk_parse` | Parse (advanced, async, bulk) | Present only where the raised ceiling above is offered |
| `extract_fast` | Extract (fast) | Never page-capped |
| `extract_advanced` | Extract (advanced) | Present only where advanced extract is offered |
| `classify` | Classify | Reads the first pages it needs |
| `split` | Split | |
| `redact` | Redact / Deidentify | `max_pages` caps inline output. `urls_max_pages` is present only where `output="urls"` is offered and caps that mode. Over-cap documents return `413` up front, before any OCR is billed |

A missing key means that mode is not offered to your account, never "unlimited".
The same `limits` object rides on every `/v1/documents/upload` response.
Asynchronous submissions return `200` when the job is *accepted* — the final
verdict, including a limit rejection, lands on the job record you poll.

A submission the server refuses outright with `413` (a document over the
operation's page cap) raises that operation's error — `ParseError`,
`ExtractError`, `ClassifyError`, `SplitError` or `RedactError` — with
`status_code == 413` and the server's message. `DocumentUploadError` is raised
only by the upload step, for example a file over `max_upload_bytes`.

## Extract: Basic vs Advanced

Basic `extract()` runs the tier you select with `category`; **Advanced**
`extract_advanced()` auto-detects the document type for you (no `category`).

```python
# Basic Financial: HyperAPI uses the invoice and receipt fields
fields = client.extract("invoice.pdf")                        # no category: organization default (platform default: financial)

# Basic Other: define the exact output shape for this request
schema = {
    "policy_number": None,
    "active": False,
    "coverage": {"medical": "unselected"},
}
fields = client.extract(
    "policy.pdf",
    category="non_financial",
    schema=schema,
)
print(fields["result"]["data"])

# The schema can also come from a JSON file: pass a Path.
# A plain str is always read as JSON text, never as a file name.
from pathlib import Path
fields = client.extract("policy.pdf", category="non_financial",
                        schema=Path("schema.json"))

# Advanced — auto-detects the type, no category to pick
fields = client.extract_advanced("mixed_document.pdf")
print(fields["result"]["entities"])
```

Both also accept `parse_mode` to pick the parse mode — `"fast"`
(default, quick text extraction) or `"advanced"` (structured output with markdown,
suited to dense tables and forms; slower, costs more, Plus plan or higher).
It's independent of `category` and of the Basic/Advanced split:

```python
fields = client.extract("dense_invoice.pdf", parse_mode="advanced")
fields = client.extract_advanced("complex_form.pdf", parse_mode="advanced")
```

## Classify / Split Options

`classify()` and `split()` accept an `options` dict of service-level knobs,
validated server-side:

```python
# Classifier: pipeline depth, restricted class set, custom instructions
client.classify(
    "doc.pdf",
    options={
        "mode": "thorough",                       # "fast" | "balanced" | "thorough"
        "active_classes": ["invoice", "purchase_order"],
        "system_instruction": "Prefer invoice when ambiguous.",
    },
)

# Splitter: thinking toggle, custom segment classes, domain guidelines
client.split(
    "binder.pdf",
    options={
        "use_thinking": True,
        "extend_segment_classes": True,
        "segment_classes": {"remittance_advice": "Payment remittance advice"},
    },
)
```

Note: `options["mode"]` (classifier pipeline depth) is a different knob from
the top-level `mode=` keyword (task selector) — they travel in different parts
of the request.

## Redact / Deidentify

> A page cap applies to redact — an over-cap file is refused with `413` (raised
> as `RedactError`) before any processing is billed. Read the current values
> with `client.get_limits()["redact"]`; see
> [Limits by operation](#limits-by-operation).


`redact()` masks PII with black boxes; `mode="deidentify"` overlays synthetic
replacements instead. Built-in PII types: PERSON_NAME, COMPANY_NAME, EMAIL,
ADDRESS, WEBSITE, PHONE, and CREDENTIALS (passwords, API keys, tokens,
secrets).

```python
result = client.redact("contract.pdf", mode="deidentify", include_logos=True)
for page_png_b64 in result["result"]["images"]:
    ...                                  # masked page images (inline base64)
print(result["result"]["summary"])       # {"PERSON_NAME": 2, "EMAIL": 1, ...}

# Streaming output — masked pages as short-lived presigned links instead of
# inline base64, with a higher page cap. Feature-detect first:
# client.get_limits()["redact"] carries urls_max_pages only where the mode is
# offered. Not combinable with include_logos.
result = client.redact("long-contract.pdf", output="urls")
client.download_pages(result, "masked/")   # pages[*].image_url — works as-is

# Customize the detected PII type set
client.redact(
    "form.pdf",
    pii_config={"mode": "extend", "types": [{"name": "SSN"}]},
)
```

## Saving page images

`download_pages()` writes a result's page images into a folder, so you don't have
to chase presigned URLs before they expire. It works with `parse()` results that
carry page images (on by default) and with `redact(output="urls")`.

```python
parsed = client.parse("contract.pdf")
paths = client.download_pages(parsed, "out/pages")
print(paths)                                             # [PosixPath('out/pages/page-1.webp'), …]

masked = client.redact("contract.pdf", output="urls")
client.download_pages(masked, "out/masked", prefix="masked")   # out/masked/masked-1.png …
```

The folder is created if missing, files are named `<prefix>-<page number>.<ext>`, and the
extension follows what the server actually served — `.webp` for parse, `.png` for redact.
On the async client it's the same call with `await`, and pages download concurrently.

If the URLs have already expired you get a clear error rather than a bare 403 — re-poll
with `get_job(job_id)` for fresh ones (the job lives 24 h) and call it again.

> Default (inline) `redact()` is the exception: it returns `images` as **inline base64
> strings**, not URLs, so there is nothing to download — decode them yourself with
> `base64.b64decode()`. With `output="urls"` the result carries `pages[*].image_url`
> instead, and `download_pages()` accepts it like any other paged result.

## Jobs Management

```python
# List the org's recent jobs (summary rows, newest first)
for row in client.list_recent_jobs(limit=100, source="api"):
    print(row["job_id"], row["task"], row["status"])

# Cancel an in-flight job (idempotent; completed jobs keep their result)
job = client.submit_extract("big_binder.pdf")
client.delete_job(job.job_id)
```

A cancelled job is reported by `get_job()` as `status: "failed"` with an `error`
that starts with `cancelled:`. `wait_for_job()` on such a job raises
`JobCancelledError` (carrying `job_id`) as soon as it sees that state, rather
than waiting until its deadline.

### Recovering which job belongs to which document

Each summary row carries a `document_key` — the server-side handle for that
job's document — alongside `filename`. If you are reconciling a batch, match on
`document_key` and not on `filename`: `filename` is whatever was sent at upload
and is not required to be unique, so fifty documents named `invoice.pdf`
produce fifty rows you cannot tell apart.

```python
for row in client.list_recent_jobs(limit=100):
    print(row["job_id"], row["document_key"], row["filename"], row["status"])
```

The robust pattern with this SDK is to keep the `job_id` at submit time —
`submit_*` returns a `Job` immediately, so the mapping costs nothing:

```python
submitted = {}
for path in paths:
    job = client.submit_parse(path)
    submitted[job.job_id] = path        # your own record

for job_id, path in submitted.items():
    print(path, client.get_job(job_id)["status"])
```

> **Note.** `submit_*` performs the upload internally and does not surface the
> `document_key` it used, and no public method accepts a pre-uploaded
> `document_key`. So while `document_key` identifies rows uniquely, you cannot
> currently pre-compute it to build your own key→file map through the SDK —
> retain the `job_id` instead. `document_key` is `None` on older jobs, and the
> listing only covers the last 24 hours.

## Batch API (async, deferred)

Process many documents in one async job. You submit already-uploaded documents,
get a `batch_id` back immediately, and poll (or wait) for per-document results —
the work runs on spare capacity, so it never competes with your interactive
calls. Supported endpoints: `/v1/parse`, `/v1/extract`, `/v1/extract-omni` (advanced extract), `/v1/classify`, `/v1/split`, `/v1/redact`.

For `/v1/parse` batches, `parse_mode="advanced"` requests advanced parsing.
Advanced parse in a batch is plan-dependent: a batch that asks for it may be
rejected with `400` or `403` where it is not available to your account. Each
finished item's `result_url` is a time-limited presigned link to its result
JSON — download it promptly, and re-poll `get_batch()` for a fresh one if it
has lapsed.

```python
# Upload first, or use the convenience that uploads for you:
batch = client.create_batch_from_files(
    endpoint="/v1/classify",
    file_paths=["a.pdf", "b.pdf", "c.pdf"],
    idempotency_key="nightly-2026-07-07",   # resubmitting returns the same batch
)
print(batch["batch_id"], batch["status"], batch["total_items"])

# Block until the batch finishes (or poll get_batch yourself):
result = client.wait_for_batch(batch["batch_id"], timeout=3600)
print(result["counts"])                      # {total, queued, processing, done, failed}
for item in result["items"]:
    print(item["doc_index"], item["status"], item.get("result_url"))

# Or drive it manually:
client.get_batch(batch["batch_id"])          # single status poll
client.list_batches(limit=20)                # your recent batches
client.cancel_batch(batch["batch_id"])       # stop queued items (in-flight finish)

# Already have document_keys (from client.upload_document)? Skip the upload:
client.create_batch(endpoint="/v1/parse", document_keys=[k1, k2], parse_mode="fast")
```

The async client mirrors every method (`await async_client.create_batch(...)`,
`await async_client.wait_for_batch(...)`, and `create_batch_from_files` uploads
the files concurrently).

## How It Works (Submit + Poll)

Every long-running operation (`parse`, `extract`, `classify`, `split`, `process`) submits asynchronously and polls a job-status endpoint under the hood. From the caller's perspective the SDK looks synchronous — `result = client.extract(file)` blocks until the result is ready — but **each individual HTTP request stays sub-second**, so the SDK is timeout-immune at any CDN edge.

```
client.extract(file)
   │
   ├─ POST /v1/documents/upload  (presigned S3 URL)
   ├─ PUT  <S3 URL>              (upload bytes directly)
   ├─ POST /v1/extract  X-Async: true  →  { job_id, poll_url }
   ├─ GET  /v1/jobs/{id}  (every 3 s, retries on 5xx)
   ├─ GET  /v1/jobs/{id}  …
   └─ GET  /v1/jobs/{id}  →  { status: "completed", result: {...} }
```

This is the pattern used by [LlamaParse](https://docs.llamaindex.ai/en/stable/llama_cloud/llama_parse/) and [Reducto](https://docs.reducto.ai/) for the same reason.

> **Rate limits and polling.** The API rate-limits requests **per organization** over a rolling 60-second window; the **free tier allows 1 request per 60 s**. Job-status polls (`GET /v1/jobs/{id}`) have their own generous per-organization limit and do not use up your plan's request limit, so a submit followed by polling does not trip a `429` on its own. Polling a very large number of jobs at the same time can still be throttled; when that happens, `wait_for_job` / `wait_for_jobs` / `wait_for_batch` **sleep for the server's `Retry-After` and resume**, rather than failing the call. If the remaining `poll_timeout` is shorter than that wait, the call raises `RateLimitError` (with `retry_after` set) so you can wait and resume with `wait_for_job`. A single-shot `get_job()` still raises `RateLimitError` immediately — only the *waiting* helpers back off. Submissions are what your plan limits: a rate-limited `submit_<op>` (or convenience call) raises `RateLimitError`, so on the free tier run one operation at a time.

## Async client

`AsyncHyperAPIClient` is an `async`/`await` twin of `HyperAPIClient` for code that already runs inside an event loop — FastAPI / Starlette / aiohttp servers, LLM agent loops, Discord/Slack bots, and any high-concurrency processor that fans out with `asyncio.gather`. Same constructor signature, same method names, same `Job` dataclass, same typed exceptions — swap the import and add `await`.

```python
import asyncio
from hyperapi import AsyncHyperAPIClient

async def main():
    async with AsyncHyperAPIClient() as client:    # reads HYPERAPI_KEY
        result = await client.extract("invoice.pdf")
        print(result)

asyncio.run(main())
```

### When to pick which client

> **If your code already says `async def` somewhere — use the async client. Otherwise, use sync.**

| You're building… | Use |
|---|---|
| Python script / Jupyter notebook / Airflow task / Celery worker | `HyperAPIClient` (sync) |
| FastAPI / Starlette / aiohttp service | `AsyncHyperAPIClient` |
| LLM agent loop that interleaves multiple `await` calls | `AsyncHyperAPIClient` |
| Discord / Slack / Telegram bot | `AsyncHyperAPIClient` |
| Fan-out across many docs in one process | `AsyncHyperAPIClient` + `asyncio.gather` |

A sync call inside an async event loop blocks the **entire** event loop (not just your handler) for the duration of the call. For HyperAPI's multi-minute extracts that means every other in-flight request stalls. The async client yields between polls so the loop stays responsive.

### FastAPI integration

One client per application, reused across requests, closed on shutdown via the lifespan handler:

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI, UploadFile
from hyperapi import AsyncHyperAPIClient

@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.hyperapi = AsyncHyperAPIClient()        # reads HYPERAPI_KEY
    try:
        yield
    finally:
        await app.state.hyperapi.aclose()             # release connection pool

app = FastAPI(lifespan=lifespan)

@app.post("/extract")
async def extract_endpoint(file: UploadFile):
    # save UploadFile to a tmp path, then:
    return await app.state.hyperapi.extract(tmp_path)
```

A runnable version (standalone CLI + FastAPI snippet) lives at [`examples/async_quickstart.py`](https://github.com/hyprbots/hyperapi-sdk/blob/main/examples/async_quickstart.py).

### Concurrent fan-out

`asyncio.gather` runs many operations concurrently on one event loop — no thread pool, no GIL contention:

```python
async with AsyncHyperAPIClient() as client:
    results = await asyncio.gather(*(client.extract(p) for p in paths))
```

Each submission counts against your plan's request limit, so size the fan-out to
it: a submission over the limit raises `RateLimitError` (with `retry_after`).
The status polls behind each call have their own limit and do not add to it.

### Notes on parity

- **`wait_for_jobs([...])`** uses `asyncio.gather` under the hood — true concurrent polling, with each job given the full timeout. The first job to fail or time out raises, and the rest are cancelled; a timeout raises `JobTimeoutError` whose `job_id` is that one job's id. The sync client shares one deadline across all jobs instead, and on timeout raises a single `JobTimeoutError` whose `job_id` is the comma-joined ids of every job still pending (for example `"A,B"`).
- **Submit-and-poll is identical** — `AsyncHyperAPIClient.submit_extract(...)` returns the same `Job` dataclass; `await client.wait_for_job(job)` blocks the coroutine without blocking the event loop.
- **`User-Agent`** carries an `-async` suffix so server-side analytics can split traffic by client type.

### Advanced: explicit submit + poll

If you want fire-and-forget semantics (e.g., to integrate with your own job queue or render a custom progress UI), use `submit_<op>(...)` to get a `Job` handle, then `wait_for_job(...)` or `get_job(...)` yourself:

```python
from hyperapi import HyperAPIClient

client = HyperAPIClient(api_key="hk_live_your_key")

# Submit and walk away
job = client.submit_extract("contract.pdf")
print(job.job_id)

# … later, in a different process ...
status = client.get_job(job.job_id)            # one-shot poll, no waiting
if status["status"] == "completed":
    print(status["result"])

# or block until done with full control over cadence
result = client.wait_for_job(job, timeout=600, interval=5)
```

`submit_parse`, `submit_extract`, `submit_classify`, `submit_split`, and `submit_redact` are all available.

### Polling configuration

Polling cadence is configurable on the constructor (defaults match the platform playground):

```python
client = HyperAPIClient(
    api_key="hk_live_…",
    poll_interval=3.0,            # seconds between polls (default 3)
    poll_timeout=3600.0,          # max wait per job in seconds (default 3600)
    poll_max_transient_retries=3, # per-poll retries on transient 5xx (default 3)
)
```

Or per-call:

```python
result = client.extract("scan.pdf", poll_timeout=600, poll_interval=5)
```

## API Reference

### `HyperAPIClient` and `AsyncHyperAPIClient`

```python
client = HyperAPIClient(
    api_key: str = None,                # API key (or set HYPERAPI_KEY env var)
    base_url: str = None,               # API endpoint (default: https://apis.hyperbots.com)
    timeout: float = 120.0,             # per-HTTP-call timeout (NOT total job time)
    poll_interval: float = 3.0,         # seconds between job-status polls
    poll_timeout: float = 3600.0,       # max wall-clock seconds to wait for a job
    poll_max_transient_retries: int = 3,# transient-5xx retries on a single poll
)

# Async equivalent — identical constructor signature, every method is `async def`:
client = AsyncHyperAPIClient(api_key=..., base_url=..., ...)
# Use as an async context manager so the httpx.AsyncClient pool is released:
async with AsyncHyperAPIClient() as client:
    result = await client.extract("invoice.pdf")
```

### Methods

Every method below exists on both clients with identical signatures. On `AsyncHyperAPIClient` each is a coroutine — prefix with `await`.

| Method | Pipeline | Description |
|--------|----------|-------------|
| `upload_document(file)` | presigned upload | Upload file, returns `document_key` (valid 24 h). |
| `parse(file)` | OCR only | Parse document into structured text. Submit + poll under the hood. |
| `extract(file, *, category=None, schema=None)` | OCR → structured extraction | Basic extractor. Financial uses the built-in fields. Other (`non_financial`) accepts a JSON output schema. Submit + poll. |
| `extract_advanced(file)` | OCR → structured extraction | Advanced extractor — auto-detects the document type (no `category`). Submit + poll. |
| `classify(file)` | OCR → classification | Classify document type. Submit + poll. |
| `split(file)` | OCR → document splitting | Split multi-document binders. Submit + poll. |
| `redact(file)` | OCR → redaction | Mask or deidentify PII (and optionally logos). Submit + poll. |
| `download_pages(result, dir, *, max_page_bytes=200 MiB)` | — | Write a result's page images to a folder (`parse()` with page images, or `redact(output="urls")`). A page larger than `max_page_bytes` raises `HyperAPIError` and leaves no file. |
| `process(file)` | OCR → parse + extract | Combined parse + extract sharing one upload. Submit + poll on both legs. |
| `submit_parse / submit_extract / submit_extract_advanced / submit_classify / submit_split / submit_redact` | OCR (+ task) | Submit asynchronously, return a `Job` immediately. |
| `get_job(job_id)` | — | One-shot status poll. No waiting, no retry. |
| `get_limits()` | — | Your account's effective upload and per-operation ceilings (`GET /v1/limits`). |
| `list_recent_jobs(*, limit=20, source=None)` | — | List the org's recent jobs (summary rows, newest first). |
| `delete_job(job_id)` | — | Cancel a job. Idempotent; completed jobs keep their result. A cancelled job reads back as `failed` with an error starting `cancelled:`. |
| `wait_for_job(job, *, timeout=None, interval=None)` | — | Block until the job completes, fails, or `timeout` elapses. |
| `wait_for_jobs([job1, job2], …)` | — | Poll multiple jobs concurrently. Sync: round-robin loop. Async: `asyncio.gather`. |
| **Lifecycle (sync)** `close()` / `with HyperAPIClient() as client:` | — | Release HTTP connection pool. |
| **Lifecycle (async)** `await aclose()` / `async with AsyncHyperAPIClient() as client:` | — | Release HTTP connection pool. |

### Common Parameters

| Parameter | Type | Default | Available On |
|-----------|------|---------|-------------|
| `mode` (parse) | `"fast"` \| `"advanced"` \| `None` | `None` (API default: `"fast"`) | parse — when omitted nothing is sent and the API default applies; `"advanced"` adds per-page `structured` layout |
| `include_boxes` (parse) | `bool \| None` | `None` (API default: on in `fast` mode) | parse — `False` leaves out `boxes` and `word_boxes` |
| `include_image` (parse) | `bool \| None` | `None` (API default: on in `fast` mode) | parse — `False` leaves out per-page `image_url` |
| `include_layout` (parse) | `bool \| None` | `None` (API default: on in `fast` mode) | parse — `False` leaves out `layout`, `layout_text`, `blocks`, `groups` and turns figure parsing off. `fast` mode only; `True` with `mode="advanced"` returns `400` |
| `parse_figures` (parse) | `str \| None` | `None` (API default: `"image,chart,table"` when the layout is on) | parse — comma-separated subset of `image`, `chart`, `table`, or `"none"`; requires the layout |
| `groups_profile` (parse) | `"A"` \| `"B"` \| `None` | `None` (API default: `"A"`) | parse — how `groups[]` is built; see the fast-mode response shape above |
| `force_refresh` | `bool` | `False` | parse / extract / extract_advanced / classify / split / redact — re-run instead of returning the cached result (a forced run is billed) |
| `category` (extract) | `"financial"` \| `"non_financial"` \| `None` | `None` (organization default; platform default `"financial"`; `"non_financial"` if `schema` is given) | extract — Basic extractor profile; `extract_advanced()` auto-detects instead (no `category`) |
| `schema` (extract) | `dict \| str \| Path \| None` | `None` | extract — blank JSON template that defines the output shape for this request. With `category="non_financial"` the template is filled directly; with `category="financial"` the invoice extractor runs and its output is remapped into your template. A `schema` with no `category` is sent as `"non_financial"`. A `str` is parsed as JSON text; pass a `pathlib.Path` to load a JSON file |
| `mode` (classify / split) | `str` | `"default"` | classify / split — task selector |
| `mode` (redact) | `"redact"` \| `"deidentify"` | `"redact"` | redact |
| `output` (redact) | `"inline"` \| `"urls"` | `"inline"` | redact — `"urls"` returns masked pages as presigned `pages[*].image_url` links with a higher page cap, where offered; other values raise `ValueError` client-side |
| `options` | `dict \| None` | `None` | classify / split — service knobs, sent as a JSON form field |
| `pii_config` / `include_logos` | `dict \| None` / `bool` | `None` / `False` | redact |
| `source` (list_recent_jobs) | `"api"` \| `"playground"` \| `None` | `None` | list_recent_jobs — channel filter; omitted returns all channels |
| `use_presigned` | `bool` | `True` | parse / extract / classify / split / redact |
| `poll_timeout` | `float` | constructor's | parse / extract / classify / split / redact / process |
| `poll_interval` | `float` | constructor's | parse / extract / classify / split / redact / process |

### Exceptions

| Class | Raised on |
|---|---|
| `HyperAPIError` | Base — anything not specifically classified |
| `AuthenticationError` | 401 from server, missing API key |
| `RateLimitError` | 429 from server (carries `retry_after`, `tier`, `limit`) |
| `ParseError` / `ExtractError` / `ClassifyError` / `SplitError` / `RedactError` | The corresponding op fails — including a `413` when the document is over that operation's page cap (`status_code == 413`; the message says which limit) |
| `DocumentUploadError` | The upload step fails: the upload-URL request or the file upload itself, including a file over the upload size limit (`413`) or of an unsupported type (`415`) |
| `JobTimeoutError` | `wait_for_job` exceeded its timeout (job still running on server) |
| `JobCancelledError` | The job being waited on was cancelled with `delete_job()` (reported by the API as `failed` with an error starting `cancelled:`); carries `job_id` |

Every exception carries `status_code` (HTTP status if applicable), `request_id`
(the `X-Request-ID` the SDK sent — include it when you open a
[GitHub issue](https://github.com/hyprbots/hyperapi-sdk/issues) or contact
support at hyperapi-support@hyperbots.com) and `code`, the
machine-readable error code from the API's error envelope when one was
returned (for example `page_quota_exceeded`), otherwise `None`. A quota
exhaustion (`code == "page_quota_exceeded"`) is not retried: the waiting
helpers raise immediately instead of backing off, since retrying cannot
succeed until the quota resets or the plan changes.

### Supported Formats

The upload content type is derived from the file extension:

| Family | Extensions |
|---|---|
| PDF | `.pdf` |
| Images | `.png`, `.jpg`, `.jpeg`, `.gif`, `.webp`, `.tiff`, `.tif`, `.heic`, `.heif` |
| Office documents | `.docx`, `.doc`, `.xlsx`, `.xls`, `.pptx`, `.ppt` |
| Text and data | `.csv`, `.txt`, `.md`, `.rtf`, `.json`, `.xml` |
| Fixed-layout pages | `.xps`, `.oxps` |
| Email | `.eml`, `.msg` |

An unrecognised extension is rejected by the upload step with `415`.

## Operational Notes

### Threading and concurrency
- `HyperAPIClient` is **not thread-safe** (httpx.Client isn't either). Construct one client per worker thread, or use `threading.local()`.
- `AsyncHyperAPIClient` **is coroutine-safe** — a single instance can be shared across many concurrent `asyncio.gather` operations. Construct one client per application, not per request.

### Corporate proxy / custom CA
Standard httpx env vars are honored — `HTTPS_PROXY`, `HTTP_PROXY`, `NO_PROXY`, `SSL_CERT_FILE`. To pin a custom CA bundle:

```bash
export SSL_CERT_FILE=/path/to/corporate-ca.pem
```

### SOCKS proxy
If your environment routes through a SOCKS proxy, install the socks extra:

```bash
pip install "hyperapi-sdk[socks]"
```

### Logging
You can turn on SDK logs at any level:

```python
import logging
logging.getLogger("hyperapi").setLevel(logging.INFO)
```

The SDK never logs API keys, file contents, or presigned-URL signatures.

### Idempotency
The SDK does **not** auto-retry submit POSTs. Resubmitting after a transient failure would create a duplicate job and bill twice. If you need retries on submit, wrap the call in your own retry logic; the polling loop already retries transient failures during `GET /v1/jobs/{id}`.

## Tutorials

| Tutorial | Description |
|----------|-------------|
| [`tutorial/The_Billing_Typo.ipynb`](https://github.com/hyprbots/hyperapi-sdk/blob/main/tutorial/The_Billing_Typo.ipynb) | Compare HyperAPI vs GPT-4, Claude, Gemini on extraction task when typos are present|
| [`tutorial/HyperAPI_SDK_Walkthrough.ipynb`](https://github.com/hyprbots/hyperapi-sdk/blob/main/tutorial/HyperAPI_SDK_Walkthrough.ipynb) | One sample document through parse, classify, split, redact, extract and batch, with the response shape printed at each step |

## Papers
If you use **HyperAPI** or ideas related to its document intelligence and validation pipeline in your research, please cite the following papers:

```bibtex
@inproceedings{haq2026breaking,
  title={Breaking the annotation barrier with DocuLite: A scalable and privacy-preserving framework for financial document understanding},
  author={Haq, Saiful and Singh, Daman Deep and Bhat, Akshata A and Tamataam, Krishna Chaitanya Reddy and Khatri, Prashant and Nizami, Abdullah and Kaushik, Abhay and Chhaya, Niyati and Pandey, Piyush},
  booktitle={4th Deployable AI Workshop},
  year={2026}
}
```

```
@article{bhatsavior,
  title={SAVIOR: Sample-efficient Alignment of Vision-Language Models for OCR Representation},
  author={Bhat, Akshata A and Naganna, Sharath and Haq, Saiful and Khatri, Prashant and Arun, Neha and Chhaya, Niyati and Pandey, Piyush and Bhattacharyya, Pushpak}
}
```

## Testing

The public test suite is hermetic: it validates request construction, response
parsing, polling, and typed errors without contacting a live HyperAPI service.

```bash
pip install -e ".[dev]"
ruff check hyperapi/
pytest tests/ -v
```

## License

Apache License 2.0 - see [LICENSE](https://github.com/hyprbots/hyperapi-sdk/blob/main/LICENSE) and [NOTICE](https://github.com/hyprbots/hyperapi-sdk/blob/main/NOTICE) for details.

## Links

- **GitHub**: [github.com/hyprbots/hyperapi-sdk](https://github.com/hyprbots/hyperapi-sdk)
