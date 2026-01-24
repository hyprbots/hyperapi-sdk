"""
HyperAPI Client
"""

import os
from pathlib import Path
from typing import Union, Optional

import httpx

from .exceptions import AuthenticationError, ParseError, ExtractError


class HyperAPIClient:
    """
    Client for interacting with HyperAPI.

    Usage:
        from hyperapi import HyperAPIClient

        client = HyperAPIClient(api_key="your-api-key")

        # Parse a document
        result = client.parse("invoice.png")
        print(result["ocr"])

        # Extract structured fields
        fields = client.extract(result["ocr"])
        print(fields["data"])
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: float = 120.0
    ):
        """
        Initialize HyperAPI client.

        Args:
            api_key: API key for authentication. If not provided, reads from
                     HYPERAPI_KEY environment variable.
            base_url: Base URL for the API. If not provided, reads from
                      HYPERAPI_URL environment variable or uses default.
            timeout: Request timeout in seconds (default: 120s).
        """
        self.api_key = api_key or os.environ.get("HYPERAPI_KEY")
        if not self.api_key:
            raise AuthenticationError(
                "API key required. Pass api_key or set HYPERAPI_KEY environment variable."
            )

        self.base_url = (
            base_url
            or os.environ.get("HYPERAPI_URL")
        )
        self.timeout = timeout
        self._client = httpx.Client(timeout=timeout)

    def _get_headers(self) -> dict:
        """Get request headers with API key."""
        return {"X-API-Key": self.api_key}

    def parse(self, image_path: Union[str, Path]) -> dict:
        """
        Parse a document image using OCR.

        Args:
            image_path: Path to the image file (PNG, JPG, etc.)

        Returns:
            dict with keys:
                - type: "layout"
                - ocr: Extracted text

        Raises:
            FileNotFoundError: If image file doesn't exist
            AuthenticationError: If API key is invalid
            ParseError: If parsing fails
        """
        image_path = Path(image_path)
        if not image_path.exists():
            raise FileNotFoundError(f"Image file not found: {image_path}")

        # Determine content type
        suffix = image_path.suffix.lower()
        content_types = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".gif": "image/gif",
            ".webp": "image/webp",
            ".tiff": "image/tiff",
            ".tif": "image/tiff",
        }
        content_type = content_types.get(suffix, "application/octet-stream")

        try:
            with open(image_path, "rb") as f:
                files = {"file": (image_path.name, f, content_type)}
                response = self._client.post(
                    f"{self.base_url}/parse",
                    files=files,
                    headers=self._get_headers()
                )

            if response.status_code == 401:
                raise AuthenticationError("Invalid API key", status_code=401)

            if response.status_code != 200:
                raise ParseError(
                    f"Parse failed: {response.text}",
                    status_code=response.status_code
                )

            return response.json()

        except httpx.TimeoutException:
            raise ParseError("Request timed out", status_code=504)
        except httpx.RequestError as e:
            raise ParseError(f"Request failed: {str(e)}")

    def extract(self, ocr_text: str) -> dict:
        """
        Extract structured fields from OCR text.

        Args:
            ocr_text: OCR text from parsed document (from parse() result)

        Returns:
            dict with keys:
                - type: "extract"
                - data: Extracted fields (invoice_number, date, line_items, etc.)

        Raises:
            AuthenticationError: If API key is invalid
            ExtractError: If extraction fails
        """
        try:
            response = self._client.post(
                f"{self.base_url}/extract",
                data={"ocr_text": ocr_text},
                headers=self._get_headers(),
                timeout=600.0  # LLM calls can take longer
            )

            if response.status_code == 401:
                raise AuthenticationError("Invalid API key", status_code=401)

            if response.status_code != 200:
                raise ExtractError(
                    f"Extract failed: {response.text}",
                    status_code=response.status_code
                )

            return response.json()

        except httpx.TimeoutException:
            raise ExtractError("Request timed out", status_code=504)
        except httpx.RequestError as e:
            raise ExtractError(f"Request failed: {str(e)}")

    def extract_lineitems(self, ocr_text: str) -> dict:
        """
        Extract line items with validation-aware extraction.

        Uses LLM to extract line items and validates quantity × rate = amount.
        This catches OCR errors like "0.15" vs "0:15" time format issues.

        Args:
            ocr_text: OCR text from parsed document

        Returns:
            dict with keys:
                - type: "lineitems"
                - data: {"line_items": [...], "summary": {...}}
        """
        try:
            response = self._client.post(
                f"{self.base_url}/extract-lineitems",
                data={"ocr_text": ocr_text},
                headers=self._get_headers(),
                timeout=600.0
            )

            if response.status_code == 401:
                raise AuthenticationError("Invalid API key", status_code=401)

            if response.status_code != 200:
                raise ExtractError(
                    f"Extract failed: {response.text}",
                    status_code=response.status_code
                )

            return response.json()

        except httpx.TimeoutException:
            raise ExtractError("Request timed out", status_code=504)
        except httpx.RequestError as e:
            raise ExtractError(f"Request failed: {str(e)}")

    def extract_entities(self, ocr_text: str) -> dict:
        """
        Extract document entities (invoice number, dates, vendor info, etc.)

        Uses LLM with few-shot learning for intelligent extraction.

        Args:
            ocr_text: OCR text from parsed document

        Returns:
            dict with keys:
                - type: "entities"
                - data: Extracted entity fields
        """
        try:
            response = self._client.post(
                f"{self.base_url}/extract-entities",
                data={"ocr_text": ocr_text},
                headers=self._get_headers(),
                timeout=600.0
            )

            if response.status_code == 401:
                raise AuthenticationError("Invalid API key", status_code=401)

            if response.status_code != 200:
                raise ExtractError(
                    f"Extract failed: {response.text}",
                    status_code=response.status_code
                )

            return response.json()

        except httpx.TimeoutException:
            raise ExtractError("Request timed out", status_code=504)
        except httpx.RequestError as e:
            raise ExtractError(f"Request failed: {str(e)}")

    def process(self, image_path: Union[str, Path]) -> dict:
        """
        Parse and extract in one call.

        Args:
            image_path: Path to the image file

        Returns:
            dict with keys:
                - ocr: Raw OCR text
                - data: Extracted structured fields
        """
        parse_result = self.parse(image_path)
        extract_result = self.extract(parse_result["ocr"])

        return {
            "ocr": parse_result["ocr"],
            "data": extract_result["data"]
        }

    def close(self):
        """Close the HTTP client."""
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
