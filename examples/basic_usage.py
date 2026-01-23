"""
HyperAPI Basic Usage Example

This example demonstrates how to:
1. Parse a document image to get OCR text
2. Extract structured fields from the OCR text
"""
import os
import sys
from pathlib import Path

# Add parent directory to path for local development
sys.path.insert(0, str(Path(__file__).parent.parent))

from hyperapi import HyperAPIClient, HyperAPIError


def main():
    # Check for API key
    if not os.environ.get("HYPERAPI_KEY"):
        print("Error: HYPERAPI_KEY environment variable not set")
        print("Run: source scripts/env.demo.sh")
        sys.exit(1)

    # Initialize client
    client = HyperAPIClient()
    print(f"Connected to: {client.base_url}")
    print()

    # Find a test image
    test_images = [
        Path(__file__).parent.parent / "tutorial" / "test_invoice.png",
        Path("/saif/thinkify_server/test_demo.png"),
    ]

    image_path = None
    for img in test_images:
        if img.exists():
            image_path = img
            break

    if not image_path:
        print("No test image found. Please provide an image path:")
        print("  python basic_usage.py /path/to/invoice.png")
        sys.exit(1)

    print(f"Processing: {image_path}")
    print("-" * 50)

    try:
        # Step 1: Parse the document
        print("\n[1] Parsing document...")
        parse_result = client.parse(image_path)

        print("OCR Text (first 500 chars):")
        print(parse_result["ocr"][:500])
        print("...")
        print()

        # Step 2: Extract structured fields
        print("[2] Extracting fields...")
        extract_result = client.extract(parse_result["ocr"])

        data = extract_result["data"]
        print("\nExtracted Fields:")
        print(f"  Invoice #:    {data.get('invoice_number', 'N/A')}")
        print(f"  Date:         {data.get('invoice_date', 'N/A')}")
        print(f"  Due Date:     {data.get('due_date', 'N/A')}")
        print(f"  Terms:        {data.get('terms', 'N/A')}")
        print(f"  Vendor:       {data.get('vendor_name', 'N/A')}")
        print(f"  Bill To:      {data.get('bill_to_name', 'N/A')}")
        print(f"  Line Items:   {len(data.get('line_items', []))} items")
        print(f"  Total:        ${data.get('total', 'N/A')}")

    except HyperAPIError as e:
        print(f"Error: {e.message}")
        if e.status_code:
            print(f"Status: {e.status_code}")
        sys.exit(1)


if __name__ == "__main__":
    main()
