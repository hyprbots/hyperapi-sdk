
<p align="center">
<a href="https://apis.hyperbots.com/"><img src="https://images.g2crowd.com/uploads/vendor/image/1515319/9eadfb55dd882c428f4f82ee306dabcd.png" width="115"></a>
  <p align="center"><strong>HyperAPI: </strong>Stop Prompting, Start Programming Financial Intelligence.</p>
</p>
<p align="center">
  <a href="https://github.com/hyprbots/hyperapi-sdk"><img src="https://img.shields.io/badge/python-3.9+-blue.svg" alt="Python 3.9+"></a>
  <a href="https://github.com/hyprbots/hyperapi-sdk/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-MIT-green.svg" alt="License"></a>
</p>

---

**HyperAPI-SDK** is a document intelligence framework composed of Parse, Extract, Split(coming soon), Classify(coming soon), Layout(coming soon), Verify(coming soon), Omni(coming soon), Redact(coming soon), Summarise(coming soon), and Sheets(coming soon) APIs. Whether you are dealing with low-quality scans or complex multi-document binders, HyperAPI is engineered for production-grade reliability.

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
pip install hyperapi
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
client = HyperAPIClient(api_key="your-key", base_url="hyperapi-base-url")

# Or use environment variable
# export HYPERAPI_KEY="your-key" HYPERAPI_URL="hyperapi-base-url"
client = HyperAPIClient()

# Process a financial document
result = client.process("invoice.png")

print(result["data"]["invoice_number"])  # "7816"
print(result["data"]["line_items"])      # Validated line items
print(result["data"]["total"])           # "$1,800.00"
```

## Two-Step Pipeline

For more control, use parse and extract separately:

```python
# Step 1: Parse document
ocr_result = client.parse("invoice.png")
print(ocr_result["ocr"])  # Markdown-formatted text

# Step 2: Extract structured fields with validation
fields = client.extract(ocr_result["ocr"])
print(fields["data"]["line_items"])
```

## API Reference

### `HyperAPIClient`

```python
client = HyperAPIClient(
    api_key: str = None,      # API key (or set HYPERAPI_KEY env var)
    base_url: str = None,     # API endpoint (or set HYPERAPI_URL)
    timeout: float = 120.0    # Request timeout in seconds
)
```

### Methods

| Method | Input | Output | Description |
|--------|-------|--------|-------------|
| `parse(image_path)` | Path to image | `{"type": "layout", "ocr": "..."}` | Parse Document into text |
| `extract(ocr_text)` | Parse Document string | `{"type": "extract", "data": {...}}` | Structured extraction |
| `process(image_path)` | Path to image | `{"ocr": "...", "data": {...}}` | Combined pipeline |

### Supported Formats

- PNG, JPG, JPEG
- PDF
- Excel

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

## License

MIT License - see [LICENSE](LICENSE) for details.

## Links

- **GitHub**: [github.com/hyprbots/hyperapi-sdk](https://github.com/hyprbots/hyperapi-sdk)
