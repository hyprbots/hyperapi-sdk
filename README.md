
<p align="center">
<a href="https://apis.hyperbots.com/"><img src="https://images.g2crowd.com/uploads/vendor/image/1515319/9eadfb55dd882c428f4f82ee306dabcd.png" width="115"></a>
  <p align="center"><strong>HyperAPI: </strong>Stop Prompting, Start Programming Financial Intelligence.</p>
</p>
<p align="center">
  <a href="https://github.com/hyprbots/hyperapi-sdk"><img src="https://img.shields.io/badge/python-3.9+-blue.svg" alt="Python 3.9+"></a>
  <a href="https://github.com/hyprbots/hyperapi-sdk/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-MIT-green.svg" alt="License"></a>
</p>

---

**HyperAPI-SDK** is a document intelligence framework composed of Parse, Extract, Classify, Split, Redact, and more APIs. Whether you are dealing with low-quality scans or complex multi-document binders, HyperAPI is engineered for production-grade reliability.

## Architecture

HyperAPI uses a **two-stage pipeline** for document processing:

```
                          ┌─────────────────┐
                          │  Upload Document │
                          └────────┬────────┘
                                   │
                    ┌──────────────▼──────────────┐
                    │     Stage 1: OCR Engine      │
                    │                              │
                    │  ┌──────────┐ ┌───────────┐  │
                    │  │ standard │ │  layout-  │  │
                    │  │ (default)│ │  aware    │  │
                    │  └──────────┘ └───────────┘  │
                    └──────────────┬──────────────┘
                                   │ ocr_pages + ocr_text
                    ┌──────────────▼──────────────┐
                    │   Stage 2: Task processing   │
                    │                              │
                    │  parse → (skip, OCR only)    │
                    │  extract → structured extract│
                    │  classify → classification   │
                    │  split → document splitting  │
                    │  redact → redaction          │
                    └─────────────────────────────┘
```

**Stage 1** runs OCR server-side. **Stage 2** routes the OCR output to task-specific processing. For `parse`, Stage 2 is skipped — you get the raw OCR result directly.

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

Or install from source:
```bash
git clone https://github.com/hyprbots/hyperapi-sdk
cd hyperapi-sdk
pip install -e .
```

## Quick Start

```python
from hyperapi import HyperAPIClient

# Initialize with your API key
client = HyperAPIClient(api_key="hk_live_your_key")

# Or use environment variable
# export HYPERAPI_KEY="hk_live_your_key"
client = HyperAPIClient()

# Process a financial document (parse + extract in one call)
result = client.process("invoice.png")

print(result["data"]["invoice_number"])  # "7816"
print(result["data"]["line_items"])      # Validated line items
print(result["data"]["total"])           # "$1,800.00"
```

> **Test keys (`hk_test_…`) are admin-issued only.** They get a 1,000 req/min
> ceiling and skip billing entirely (no charges, no usage-export rows), which
> is why they aren't a self-serve developer feature. If your CI needs a
> non-billable smoke-test key, contact your platform admin. SDK users
> integrating with production should default to `hk_live_…`.

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
tables, not flattened text. Slower than the default `mode="fast"`.

```python
result = client.parse("annual_report.pdf", mode="advanced")
page = result["pages"][0]
print(page["structured"]["markdown"])   # layout-aware markdown
print(page["structured"]["regions"])    # typed regions with bounding boxes
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

`redact()` masks PII with black boxes; `mode="deidentify"` overlays synthetic
replacements instead. Built-in PII types: PERSON_NAME, COMPANY_NAME, EMAIL,
ADDRESS, WEBSITE, PHONE, and CREDENTIALS (passwords, API keys, tokens,
secrets).

```python
result = client.redact("contract.pdf", mode="deidentify", include_logos=True)
for page_png_b64 in result["images"]:
    ...                                  # masked page images
print(result["summary"])                 # {"PERSON_NAME": 2, "EMAIL": 1, ...}

# Customize the detected PII type set
client.redact(
    "form.pdf",
    pii_config={"mode": "extend", "types": [{"name": "SSN"}]},
)
```

## Jobs Management

```python
# List the org's recent jobs (summary rows, newest first)
for row in client.list_recent_jobs(limit=10, source="api"):
    print(row["job_id"], row["task"], row["status"])

# Cancel an in-flight job (idempotent; completed jobs keep their result)
job = client.submit_extract("big_binder.pdf")
client.delete_job(job.job_id)
```

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

## Async client (new in 0.2.0)

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
- **`User-Agent`** carries an `-async` suffix so backend analytics can split traffic by client type.

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
    poll_timeout=1800.0,          # max wait per job in seconds (default 1800)
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
    poll_timeout: float = 1800.0,       # max wall-clock seconds to wait for a job
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
| `extract(file, *, category="financial")` | OCR → structured extraction | Basic extractor. `category="financial"` (default) or `"non_financial"`. Submit + poll. |
| `extract_advanced(file)` | OCR → structured extraction | Advanced extractor — auto-detects the document type (no `category`). Submit + poll. |
| `classify(file)` | OCR → classification | Classify document type. Submit + poll. |
| `split(file)` | OCR → document splitting | Split multi-document binders. Submit + poll. |
| `redact(file)` | OCR → redaction | Mask or deidentify PII (and optionally logos). Submit + poll. |
| `process(file)` | OCR → parse + extract | Combined parse + extract sharing one upload. Submit + poll on both legs. |
| `submit_parse / submit_extract / submit_extract_advanced / submit_classify / submit_split / submit_redact` | OCR (+ Stage 2) | Submit asynchronously, return a `Job` immediately. |
| `get_job(job_id)` | — | One-shot status poll. No waiting, no retry. |
| `list_recent_jobs(*, limit=20, source=None)` | — | List the org's recent jobs (summary rows, newest first). |
| `delete_job(job_id)` | — | Cancel a job. Idempotent; completed jobs keep their result. |
| `wait_for_job(job, *, timeout=None, interval=None)` | — | Block until the job completes, fails, or `timeout` elapses. |
| `wait_for_jobs([job1, job2], …)` | — | Poll multiple jobs concurrently. Sync: round-robin loop. Async: `asyncio.gather`. |
| **Lifecycle (sync)** `close()` / `with HyperAPIClient() as client:` | — | Release HTTP connection pool. |
| **Lifecycle (async)** `await aclose()` / `async with AsyncHyperAPIClient() as client:` | — | Release HTTP connection pool. |

### Common Parameters

| Parameter | Type | Default | Available On |
|-----------|------|---------|-------------|
| `mode` (parse) | `"fast"` \| `"advanced"` | `"fast"` | parse — `"advanced"` adds per-page `structured` layout |
| `mode` (extract) | `str` | `"default"` | extract — `"default"` runs the standard structured extraction path |
| `category` (extract) | `"financial"` \| `"non_financial"` | `"financial"` | extract — Basic extractor profile; `extract_advanced()` auto-detects instead (no `category`) |
| `mode` (classify / split) | `str` | `"default"` | classify / split — task selector |
| `mode` (redact) | `"redact"` \| `"deidentify"` | `"redact"` | redact |
| `options` | `dict \| None` | `None` | classify / split — service knobs, sent as a JSON form field |
| `pii_config` / `include_logos` | `dict \| None` / `bool` | `None` / `False` | redact |
| `use_presigned` | `bool` | `True` | parse / extract / classify / split / redact |
| `poll_timeout` | `float` | constructor's | parse / extract / classify / split / redact / process |
| `poll_interval` | `float` | constructor's | parse / extract / classify / split / redact / process |

### Exceptions

| Class | Raised on |
|---|---|
| `HyperAPIError` | Base — anything not specifically classified |
| `AuthenticationError` | 401 from server, missing API key |
| `RateLimitError` | 429 from server (carries `retry_after`, `tier`, `limit`) |
| `ParseError` / `ExtractError` / `ClassifyError` / `SplitError` / `RedactError` | The corresponding op fails |
| `DocumentUploadError` | Presigned-URL flow or S3 PUT fails |
| `JobTimeoutError` | `wait_for_job` exceeded its timeout (job still running on server) |

Every exception carries `status_code` (HTTP status if applicable) and `request_id` (the `X-Request-ID` we sent — paste it into a support ticket).

### Supported Formats

- PNG, JPG, JPEG
- TIFF, WEBP, GIF
- PDF

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
pip install hyperapi-sdk[socks]
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

The SDK has a three-layer testing strategy that runs without human intervention:

| Layer | Tool | Runs | Time | What it catches |
|---|---|---|---|---|
| **L1** Mocked contract | `pytest` + `respx` | Every PR/push | <1 min | Request shape, response parsing, error mapping |
| **L2** Customer simulator | `python -m tests.customer_sim` | Nightly cron + manual | ~5–20 min | Real-world latency, errors, OCR drift on real docs |
| **L3** Auto-issue | GitHub Actions | On L1/L2 failure | — | Opens/comments `sdk-drift` issue with run details |

```bash
# L1: hermetic, no backend needed
pip install -e ".[dev]"
pytest tests/ -v --ignore=tests/customer_sim

# L2: needs a real API key + reachable backend (NEVER production)
pip install -e ".[dev,sim]"
HYPERAPI_KEY=hk_test_xxx HYPERAPI_URL=http://localhost:8000 \
  python -m tests.customer_sim --target local --mode smoke
```

Full L2 docs (run modes, fixture corpus, metrics, regression detection, baseline updates): [`tests/customer_sim/README.md`](tests/customer_sim/README.md).

## License

MIT License - see [LICENSE](LICENSE) for details.

## Links

- **GitHub**: [github.com/hyprbots/hyperapi-sdk](https://github.com/hyprbots/hyperapi-sdk)
