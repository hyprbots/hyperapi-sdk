
<p align="center">
<a href="https://apis.hyperbots.com/"><img src="https://images.g2crowd.com/uploads/vendor/image/1515319/9eadfb55dd882c428f4f82ee306dabcd.png" width="115"></a>
  <p align="center"><strong>HyperAPI: </strong>Stop Prompting, Start Programming Financial Intelligence.</p>
</p>
<p align="center">
  <a href="https://github.com/hyprbots/hyperapi-sdk"><img src="https://img.shields.io/badge/python-3.9+-blue.svg" alt="Python 3.9+"></a>
  <a href="https://github.com/hyprbots/hyperapi-sdk/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-MIT-green.svg" alt="License"></a>
</p>

---

**HyperAPI-SDK** is a document intelligence framework composed of Parse, Extract, Classify, Split, Redact, Edit, and more APIs. Whether you are dealing with low-quality scans or complex multi-document binders, HyperAPI is engineered for production-grade reliability.

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
git clone https://github.com/hyprbots/hyperapi-sdk
cd hyperapi-sdk
python3 -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
python -m pip install .
```

PyPI installation will be documented after the first package release.

## Quick Start

```python
from hyperapi import HyperAPIClient

# Set HYPERAPI_KEY in your environment before starting the application.
client = HyperAPIClient()

# Process a financial document (parse + extract in one call)
result = client.process("invoice.png")

print(result["data"]["invoice_number"])  # "7816"
print(result["data"]["line_items"])      # Validated line items
print(result["data"]["total"])           # "$1,800.00"
```

## Two-Step Pipeline

For more control, use parse and extract separately:

```python
# Step 1: Parse document (OCR)
ocr_result = client.parse("invoice.png")
print(ocr_result["result"]["ocr"])  # Markdown-formatted text

# Step 2: Extract structured fields (from the same file)
fields = client.extract("invoice.png")
print(fields["result"])

# Parse a document with complex layout
ocr_result = client.parse("complex_table.pdf")
```

## Advanced Parse (structured layout)

`mode="advanced"` runs layout-aware structured OCR: each page gains a
`structured` dict with `html`, `markdown`, and `regions` — tables come back as
tables, not flattened text. Slower than `mode="fast"`. When `mode` is omitted
the SDK sends nothing and the API default applies (`fast` unless your
organization is configured otherwise).

```python
result = client.parse("annual_report.pdf", mode="advanced")
page = result["result"]["pages"][0]
print(page["structured"]["markdown"])   # layout-aware markdown
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
billable processing runs. The ceilings depend on your plan, so read them from
the API instead of hard-coding them:

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

## Extract: Basic vs Advanced

Basic `extract()` runs the tier you select with `category`; **Advanced**
`extract_advanced()` auto-detects the document type for you (no `category`).

```python
# Basic Financial: HyperAPI uses the invoice and receipt fields
fields = client.extract("invoice.pdf")                        # category="financial" (default)

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

# Advanced — auto-detects the type, no category to pick
fields = client.extract_advanced("mixed_document.pdf")
print(fields["result"]["entities"])
```

Both also accept `parse_mode` to pick the OCR engine — `"fast"`
(default, quick text extraction) or `"advanced"` (advanced layout-aware parsing
for dense tables and forms; higher accuracy, slower, costs more, paid tiers).
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

> Redact has a plan-dependent page cap — over-cap files return `413` before any
> processing is billed. Read it from `client.get_limits()["redact"]`; see
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

## Edit (form detect + fill)

The edit API is not enabled for every account: where it is off, `edit_detect()`,
`edit_fill()` and `edit()` raise `EditError("The edit API is not available for
this account.")`.

Detect every blank fillable field on a form, then draw values onto it and get
back rendered page images. A two-call flow: `edit_detect()` returns the schema,
`edit_fill()` renders — the schema and page images stay server-side, so you only
ever carry the field list and your values.

```python
# One call — detect + natural-language fill. `content` only: this call is detecting
# the schema for the first time, so there are no field indices for you to key
# `values` by yet. Use the two legs below to fill by index.
result = client.edit("intake_form.pdf", content="Patient is Jane Doe, DOB 1985-04-02")
print(result["form_schema"])        # every detected field, in fill-index order
print(result["fills"])              # what the model mapped onto each field
print(result["pages"][0]["image_url"])
print(result["detect_job_id"])      # reuse it to correct values — no re-detect, no re-charge
```

The two legs separately, for the human-in-the-loop flow — show the user what was
detected, let them correct the mapped values, re-render:

```python
detected = client.edit_detect("intake_form.pdf", markdown_assist=True)
schema = detected["result"]["form_schema"]
job_id = detected["detect_job_id"]

# 1. Model maps free text onto the schema — returns `fills` for review
proposed = client.edit_fill(job_id, content="Jane Doe, female, +1 302 405 1234",
                            natural_language=True)

# 2. User edits a value → resubmit ALL values deterministically (no model call)
final = client.edit_fill(job_id, values={"0": "Jane A. Doe", "3": "X"})
for page in final["result"]["pages"]:
    print(page["page"], page["image_url"])
```

A field's **position in `form_schema` is its fill index**. `values` accepts either
`{"0": "Jane"}` or `[{"index": 0, "value": "Jane"}]`. `markdown_assist=True` routes
detection through layout-aware parsing — more precise on dense forms, slower.

### `edit_detect` — input / output

| Argument | Type | Default | |
|---|---|---|---|
| `file_path` | `str \| Path` | *required* | PDF or image to detect on |
| `markdown_assist` | `bool` | `False` | Layout-aware detection. Also changes the returned image: `True` on a scan yields the **deskewed** page, so pixel dimensions differ from `False` |
| `use_presigned` | `bool` | `True` | Upload via presigned URL rather than multipart |
| `poll_timeout` / `poll_interval` | `float \| None` | `None` | Override the client's polling defaults |

```jsonc
{
  "status": "success", "task": "edit", "request_id": "req-…",
  "detect_job_id": "job_…",              // added by the SDK — pass to edit_fill
  "result": {
    "phase": "detect",                   // "detect" | "fill"
    "page_count": 2,
    "pages": [
      { "page": 1,
        "image_url": "https://s3…?sig=…", // presigned, ~15 min
        "size": { "w": 1195, "h": 1536 } }
    ],
    "form_schema": [
      { "field_name": "PATIENT INTAKE FORM:First Name",
        "description": "Enter the patient's first name.",
        "type": "text",                  // text|checkbox|radio|dropdown|signature
        "bbox": { "left": 0.35, "top": 0.12, "width": 0.55,
                  "height": 0.03, "page": 1 } }
    ]
  }
}
```

`bbox` is **normalized 0..1**, origin top-left, relative to `pages[bbox.page - 1]` — so it
scales to whatever size you display the image at. There is **no `fills`** on this leg;
nothing has been drawn yet. The envelope also carries the usual `model_used`, `duration_ms`
and `metadata`, plus a legacy `mode` that is now always `null`.

### `edit_fill` — input / output

| Argument | Type | Default | |
|---|---|---|---|
| `detect_job_id` | `str` | *required* | From `edit_detect`; valid **24 h** |
| `values` | `dict \| list \| None` | `None` | Required when `natural_language=False`. `{"0": "Jane"}` or `[{"index": 0, "value": "Jane"}]` |
| `content` | `str \| None` | `None` | Required when `natural_language=True` — and it must be passed **with** that flag, or the call raises |
| `natural_language` | `bool` | `False` | `False` draws `values` verbatim (no model call); `True` maps `content` onto the schema (one model call) |

```jsonc
{
  "status": "success", "task": "edit", "request_id": "req-…",
  "result": {
    "phase": "fill",
    "pages": [ { "page": 1, "image_url": "https://s3…?sig=…",
                 "size": { "w": 1195, "h": 1536 } } ],
    "fills": [ { "index": 0, "value": "Jane" } ]
  }
}
```

Note the differences from the detect leg: `fills` **is** present (it's the only place you
see what was actually drawn, including the model's mapping in `natural_language` mode),
and `form_schema` is **not** echoed back — you already hold it.

`edit()` flattens both legs into one flat dict instead of the envelope:
`{"detect_job_id", "form_schema", "fills", "pages"}`, where `pages` are the **filled** ones.

> Page images come back as **short-lived presigned `image_url`s**, not bytes.
> Download them promptly; re-polling the job with `get_job()` mints fresh URLs.

Billing: charged once at **detect** (by page count). The fill leg is included, so
re-rendering after a user correction costs nothing.

## Saving page images

`download_pages()` writes any result's page images into a folder, so you don't have
to chase presigned URLs before they expire. It works with the blank pages from
`edit_detect()`, the rendered ones from `edit_fill()` / `edit()`, and with
`parse(include_image=True)`.

```python
detected = client.edit_detect("intake_form.pdf")
client.download_pages(detected, "out/blank")                    # out/blank/page-1.png …

# Deterministic fill — you supply the values (no model call)
filled = client.edit_fill(detected["detect_job_id"], values={"0": "Jane"})
paths = client.download_pages(filled, "out/filled", prefix="filled")
print(paths)                                                    # [PosixPath('out/filled/filled-1.png'), …]

# Natural-language fill — one model call maps the text onto the schema.
# `content` needs natural_language=True; on its own it raises (the default mode expects `values`).
described = client.edit_fill(
    detected["detect_job_id"],
    content="Patient is Jane Doe, DOB 1985-04-02, phone +1 302 405 1234",
    natural_language=True,
)
print(described["result"]["fills"])                             # what the model mapped, for review
client.download_pages(described, "out/described", prefix="nl")  # out/described/nl-1.png …

# One-shot detect + fill. Its pages sit at the top level, not under "result" —
# download_pages() accepts either nesting.
result = client.edit("intake_form.pdf", content="Patient is Jane Doe, DOB 1985-04-02")
client.download_pages(result, "out/oneshot")                    # out/oneshot/page-1.png …
```

The folder is created if missing, files are named `<prefix>-<page number>.<ext>`, and the
extension follows what the server actually served — `.png` for edit, `.webp` for parse.
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
    print(path, client.get_job(job_id).status)
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
calls. Supported endpoints: `/v1/parse`, `/v1/extract`, `/v1/classify`, `/v1/split`, `/v1/redact`.

For `/v1/parse` batches, `parse_mode="advanced"` requests layout-aware parsing.
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

> **Rate limits and polling.** The API rate-limits requests **per organization**, and job-status polls (`GET /v1/jobs/{id}`) count against the same limit as the submit — the window is a rolling 60 seconds. The **free tier allows 1 request per 60 s**, so a submit immediately followed by a poll would otherwise trip a `429`. The SDK handles this for you: when a poll is rate-limited, `wait_for_job` / `wait_for_jobs` / `wait_for_batch` **sleep for the server's `Retry-After` and resume**, rather than failing the call. If the job's remaining `poll_timeout` is shorter than the rate window the call raises `RateLimitError` (with `retry_after` set) so you can wait and resume via `submit_<op>` + `wait_for_job`. A single-shot `get_job()` still raises `RateLimitError` immediately — only the *waiting* helpers back off. On the free tier, prefer one blocking convenience call at a time over manual tight-loop polling.

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

A runnable version (standalone CLI + FastAPI snippet) lives at [`examples/async_quickstart.py`](examples/async_quickstart.py).

### Concurrent fan-out

`asyncio.gather` fans out hundreds of in-flight ops on one event loop — no thread pool, no GIL contention:

```python
async with AsyncHyperAPIClient() as client:
    results = await asyncio.gather(*(client.extract(p) for p in paths))
```

### Notes on parity

- **`wait_for_jobs([...])`** uses `asyncio.gather` under the hood — true concurrent polling. On timeout the first job to fail raises a `JobTimeoutError` and the rest are cancelled. (The sync client's "comma-joined `'A,B'` job_id" shape is async-specific divergence — see the CHANGELOG.)
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
| `extract(file, *, category="financial", schema=None)` | OCR → structured extraction | Basic extractor. Financial uses the built-in fields. Other (`non_financial`) accepts a JSON output schema. Submit + poll. |
| `extract_advanced(file)` | OCR → structured extraction | Advanced extractor — auto-detects the document type (no `category`). Submit + poll. |
| `classify(file)` | OCR → classification | Classify document type. Submit + poll. |
| `split(file)` | OCR → document splitting | Split multi-document binders. Submit + poll. |
| `redact(file)` | OCR → redaction | Mask or deidentify PII (and optionally logos). Submit + poll. |
| `edit_detect(file)` | form-field detection | Detect blank fillable fields → `form_schema` + page images. Submit + poll. |
| `edit_fill(detect_job_id, *, values=… \| content=…)` | form fill | Draw values onto a detected form → rendered pages. Submit + poll. |
| `edit(file, *, content=…)` | detect → fill | Both legs in one call. Content-only — no `values`, since this call is detecting the schema for the first time. |
| `download_pages(result, dir)` | — | Write any result's page images to a folder (edit legs, or `parse(include_image=True)`). |
| `process(file)` | OCR → parse + extract | Combined parse + extract sharing one upload. Submit + poll on both legs. |
| `submit_parse / submit_extract / submit_extract_advanced / submit_classify / submit_split / submit_redact / submit_edit_detect / submit_edit_fill` | OCR (+ task) | Submit asynchronously, return a `Job` immediately. |
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
| `category` (extract) | `"financial"` \| `"non_financial"` | `"financial"` | extract — Basic extractor profile; `extract_advanced()` auto-detects instead (no `category`) |
| `schema` (extract) | `dict \| str \| Path \| None` | `None` | Basic + Other only (`category="non_financial"`): blank JSON template that defines the output shape for this request |
| `mode` (classify / split) | `str` | `"default"` | classify / split — task selector |
| `mode` (redact) | `"redact"` \| `"deidentify"` | `"redact"` | redact |
| `output` (redact) | `"inline"` \| `"urls"` | `"inline"` | redact — `"urls"` returns masked pages as presigned `pages[*].image_url` links with a higher page cap, where offered; other values raise `ValueError` client-side |
| `options` | `dict \| None` | `None` | classify / split — service knobs, sent as a JSON form field |
| `pii_config` / `include_logos` | `dict \| None` / `bool` | `None` / `False` | redact |
| `source` (list_recent_jobs) | `"api"` \| `"playground"` \| `None` | `None` | list_recent_jobs — channel filter; omitted returns all channels |
| `markdown_assist` | `bool` | `False` | edit_detect / edit — layout-aware detection (more precise, slower) |
| `values` / `content` | `dict \| list \| None` / `str \| None` | `None` | edit_fill / edit — deterministic values vs. natural-language text (exactly one) |
| `use_presigned` | `bool` | `True` | parse / extract / classify / split / redact / edit_detect |
| `poll_timeout` | `float` | constructor's | parse / extract / classify / split / redact / process / edit* |
| `poll_interval` | `float` | constructor's | parse / extract / classify / split / redact / process / edit* |

### Exceptions

| Class | Raised on |
|---|---|
| `HyperAPIError` | Base — anything not specifically classified |
| `AuthenticationError` | 401 from server, missing API key |
| `RateLimitError` | 429 from server (carries `retry_after`, `tier`, `limit`) |
| `ParseError` / `ExtractError` / `ClassifyError` / `SplitError` / `RedactError` / `EditError` | The corresponding op fails. `EditError` is also raised with "not available for this account" where the edit API is not enabled |
| `DocumentUploadError` | Presigned-URL flow or S3 PUT fails |
| `JobTimeoutError` | `wait_for_job` exceeded its timeout (job still running on server) |
| `JobCancelledError` | The job being waited on was cancelled with `delete_job()` (reported by the API as `failed` with an error starting `cancelled:`); carries `job_id` |

Every exception carries `status_code` (HTTP status if applicable), `request_id`
(the `X-Request-ID` we sent — paste it into a support ticket) and `code`, the
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
pip install ".[socks]"
```

### Logging
Customers opt into SDK logs at any level:

```python
import logging
logging.getLogger("hyperapi").setLevel(logging.INFO)
```

The SDK never logs API keys, file contents, or presigned-URL signatures.

### Idempotency
The SDK does **not** auto-retry submit POSTs. Resubmitting after a transient failure would create a duplicate job and bill twice. Customers wrap their own retry logic if needed; the polling loop already retries transient failures during `GET /v1/jobs/{id}`.

## Tutorials

| Tutorial | Description |
|----------|-------------|
| [`tutorial/The_Billing_Typo.ipynb`](tutorial/The_Billing_Typo.ipynb) | Compare HyperAPI vs GPT-4, Claude, Gemini on extraction task when typos are present|

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

MIT License - see [LICENSE](LICENSE) for details.

## Links

- **GitHub**: [github.com/hyprbots/hyperapi-sdk](https://github.com/hyprbots/hyperapi-sdk)
