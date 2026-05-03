
<p align="center">
<a href="https://apis.hyperbots.com/"><img src="https://images.g2crowd.com/uploads/vendor/image/1515319/9eadfb55dd882c428f4f82ee306dabcd.png" width="115"></a>
  <p align="center"><strong>HyperAPI: </strong>Stop Prompting, Start Programming Financial Intelligence.</p>
</p>
<p align="center">
  <a href="https://github.com/hyprbots/hyperapi-sdk"><img src="https://img.shields.io/badge/python-3.9+-blue.svg" alt="Python 3.9+"></a>
  <a href="https://github.com/hyprbots/hyperapi-sdk/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-MIT-green.svg" alt="License"></a>
</p>

---

**HyperAPI-SDK** is a document intelligence framework composed of Parse, Extract, Classify, Split, and more APIs. Whether you are dealing with low-quality scans or complex multi-document binders, HyperAPI is engineered for production-grade reliability.

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
                    │  │ paddle   │ │ doc-intent │  │
                    │  │ (default)│ │ (alt)      │  │
                    │  └──────────┘ └───────────┘  │
                    └──────────────┬──────────────┘
                                   │ ocr_pages + ocr_text
                    ┌──────────────▼──────────────┐
                    │     Stage 2: Task LLM        │
                    │                              │
                    │  parse → (skip, OCR only)    │
                    │  extract → extract-service   │
                    │  classify → classifier       │
                    │  split → classifier-splitter │
                    └─────────────────────────────┘
```

**Stage 1** runs OCR via one of two engines (configurable per-request). **Stage 2** routes the OCR output to a task-specific LLM service. For `parse`, Stage 2 is skipped — you get the raw OCR result directly.

### OCR Engines

| Engine | Parameter Value | Best For |
|--------|----------------|----------|
| **Paddle OCR** | `"paddle"` (default) | General documents, invoices, receipts |
| **Doc-Intent** | `"doc-intent"` | Complex layouts, multi-column, tables |

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

## Two-Step Pipeline

For more control, use parse and extract separately:

```python
# Step 1: Parse document (OCR)
ocr_result = client.parse("invoice.png")
print(ocr_result["result"]["ocr"])  # Markdown-formatted text

# Step 2: Extract structured fields (from the same file)
fields = client.extract("invoice.png")
print(fields["result"])

# Use alternative OCR engine
ocr_result = client.parse("complex_table.pdf", ocr_engine="doc-intent")
```

## API Reference

### `HyperAPIClient`

```python
client = HyperAPIClient(
    api_key: str = None,      # API key (or set HYPERAPI_KEY env var)
    base_url: str = None,     # API endpoint (default: https://apis.hyperbots.com)
    timeout: float = 120.0    # Request timeout in seconds
)
```

### Methods

| Method | Input | Pipeline | Description |
|--------|-------|----------|-------------|
| `upload_document(file)` | File path | presigned S3 | Upload file, returns `document_key` (valid 24 h) |
| `parse(file)` | File path | OCR only | Parse document into structured text |
| `extract(file)` | File path | OCR → extract-service | Extract structured fields with validation |
| `classify(file)` | File path | OCR → classifier | Classify document type |
| `split(file)` | File path | OCR → classifier-splitter | Split multi-document binders |
| `process(file)` | File path | OCR → extract | Combined parse + extract in one call (single upload) |
### Common Parameters

| Parameter | Type | Default | Available On |
|-----------|------|---------|-------------|
| `ocr_engine` | `"paddle"` \| `"doc-intent"` | `"paddle"` | All methods |
| `mode` | `str` | `"default"` | extract, classify, split |
| `use_presigned` | `bool` | `True` | All methods (S3 presigned upload) |

### Supported Formats

- PNG, JPG, JPEG
- TIFF, WEBP, GIF
- PDF

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
