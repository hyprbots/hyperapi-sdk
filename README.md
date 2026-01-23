# HyperAPI Python SDK

Financial document processing APIs that scale well.

## Installation

```bash
# Quick install
./scripts/install.sh

# Or manual install
pip install -e .
```

## Configuration

Set up your environment:

```bash
# Copy the sample config
cp scripts/env.sample.sh scripts/env.sh

# Edit with your API key
nano scripts/env.sh

# Source the config
source scripts/env.sh
```

## Quick Start

```python
from hyperapi import HyperAPIClient

# Initialize client (reads HYPERAPI_KEY from environment)
client = HyperAPIClient()

# Parse a document image
result = client.parse("invoice.png")
print(result["ocr"])

# Extract structured fields
fields = client.extract(result["ocr"])
print(fields["data"])

# Or do both in one call
output = client.process("invoice.png")
print(output["data"]["invoice_number"])
```

## API Reference

### HyperAPIClient

```python
client = HyperAPIClient(
    api_key="your-key",      # Or set HYPERAPI_KEY env var
    base_url="http://...",   # Optional, defaults to production
    timeout=120.0            # Request timeout in seconds
)
```

### Methods

#### `parse(image_path)`
Parse a document image using OCR.

- **Input**: Path to PNG/JPG image
- **Output**: `{"type": "layout", "ocr": "..."}`

#### `extract(ocr_text)`
Extract structured fields from OCR text.

- **Input**: OCR text string
- **Output**: `{"type": "extract", "data": {...}}`

#### `process(image_path)`
Parse and extract in one call.

- **Input**: Path to image
- **Output**: `{"ocr": "...", "data": {...}}`

## Examples

See the `examples/` directory for complete tutorials.
