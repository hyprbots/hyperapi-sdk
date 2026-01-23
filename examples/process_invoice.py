"""
HyperAPI Invoice Processing Example

This example shows how to process an invoice and get structured data
in a single call using the process() method.
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from hyperapi import HyperAPIClient, HyperAPIError


def process_invoice(image_path: str):
    """Process an invoice image and print structured results."""

    client = HyperAPIClient()

    print(f"Processing: {image_path}")
    print("=" * 60)

    # Process in one call
    result = client.process(image_path)

    # Print extracted data as JSON
    print("\nExtracted Data (JSON):")
    print(json.dumps(result["data"], indent=2))

    return result


if __name__ == "__main__":
    if not os.environ.get("HYPERAPI_KEY"):
        print("Error: Set HYPERAPI_KEY environment variable")
        print("Run: source scripts/env.demo.sh")
        sys.exit(1)

    if len(sys.argv) > 1:
        image_path = sys.argv[1]
    else:
        # Default test image
        image_path = str(Path(__file__).parent.parent / "tutorial" / "test_invoice.png")

    if not Path(image_path).exists():
        print(f"Error: Image not found: {image_path}")
        sys.exit(1)

    try:
        process_invoice(image_path)
    except HyperAPIError as e:
        print(f"Error: {e.message}")
        sys.exit(1)
