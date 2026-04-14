"""
HyperAPI Client

Request flow
============
Every endpoint follows the same two-stage pipeline inside the inference router:

    Stage 1 — OCR (always runs)
        Upload → doc-processor /convert → page images
        → OCR engine (paddle-ocr or doc-intent) → full_text + page_texts

    Stage 2 — Task-specific LLM (skipped for parse)
        parse:      returns OCR text directly (no Stage 2)
        extract:    OCR output → extract-service /v2/extract
        classify:   OCR output → classifier-splitter /v2/classify
        split:      OCR output → classifier-splitter /v2/split

OCR engines
-----------
paddle      PaddleOCR (default) — fast, high-throughput production OCR
doc-intent  Doc-Intent VLM — vision-language model, better on complex layouts
"""

import os
import time
import uuid
from pathlib import Path
from typing import Union, Optional, Literal

import httpx

from .exceptions import (
    AuthenticationError,
    ParseError,
    ExtractError,
    ClassifyError,
    SplitError,
    DocumentUploadError,
    HyperAPIError,
)


CONTENT_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".tiff": "image/tiff",
    ".tif": "image/tiff",
    ".pdf": "application/pdf",
}

OCREngine = Literal["paddle", "doc-intent"]

_DEFAULT_TIMEOUT = 120.0
_EXTRACT_TIMEOUT = 600.0
_JOB_POLL_INTERVAL = 2.0
_JOB_POLL_MAX_WAIT = 600.0


class HyperAPIClient:
    """
    Client for interacting with HyperAPI.

    Usage::

        from hyperapi import HyperAPIClient

        client = HyperAPIClient(api_key="hk_live_...")

        # Parse a document (OCR via paddle-ocr by default)
        result = client.parse("invoice.pdf")
        print(result["result"]["ocr"])

        # Use doc-intent engine for complex layouts
        result = client.parse("invoice.pdf", ocr_engine="doc-intent")

        # Extract structured fields (runs OCR → extract-service)
        fields = client.extract("invoice.pdf")
        print(fields["result"])

        # Async processing — returns immediately, poll for result
        job = client.parse("invoice.pdf", async_mode=True)
        result = client.poll_job(job["job_id"])
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: float = _DEFAULT_TIMEOUT,
    ):
        """
        Initialize HyperAPI client.

        Args:
            api_key: API key for authentication. If not provided, reads from
                     HYPERAPI_KEY environment variable.
            base_url: Base URL for the API. If not provided, reads from
                      HYPERAPI_URL environment variable, then falls back to
                      the production URL.
            timeout: Default request timeout in seconds (default: 120s).
                     Extract uses 600s by default due to LLM processing time.
        """
        self.api_key = api_key or os.environ.get("HYPERAPI_KEY")
        if not self.api_key:
            raise AuthenticationError(
                "API key required. Pass api_key or set HYPERAPI_KEY environment variable."
            )

        self.base_url = (
            base_url
            or os.environ.get("HYPERAPI_URL")
            or "https://apis.hyperbots.com"
        ).rstrip("/")
        self.timeout = timeout
        self._client = httpx.Client(timeout=timeout)

    def _get_headers(self, async_mode: bool = False) -> dict:
        """Get request headers with API key and a unique request ID for tracing."""
        headers = {
            "X-API-Key": self.api_key,
            "X-Request-ID": str(uuid.uuid4()),
        }
        if async_mode:
            headers["X-Async"] = "true"
        return headers

    # ── Upload ───────────────────────────────────────────────────────────

    def upload_document(
        self,
        file_path: Union[str, Path],
        content_type: Optional[str] = None,
    ) -> str:
        """
        Upload a document to S3 via the presigned URL flow and return its document_key.

        This executes steps 1 and 2 of the 3-step upload flow:
          1. POST /v1/documents/upload  → {document_key, upload_url, expires_in}
          2. PUT {upload_url}           → file bytes direct to S3 (bypasses Kong)

        The returned document_key is then passed to parse/extract/classify/split (step 3).

        Args:
            file_path: Path to the file to upload.
            content_type: MIME type. Auto-detected from file extension if not provided.

        Returns:
            document_key string to use in subsequent API calls.

        Raises:
            FileNotFoundError: If the file does not exist.
            DocumentUploadError: If the presigned URL request or S3 PUT fails.
        """
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"File not found: {path}")

        if content_type is None:
            content_type = CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")

        # Step 1 — get presigned upload URL
        try:
            resp = self._client.post(
                f"{self.base_url}/v1/documents/upload",
                json={"filename": path.name, "content_type": content_type},
                headers=self._get_headers(),
            )
        except httpx.RequestError as e:
            raise DocumentUploadError(f"Failed to get upload URL: {str(e)}")

        if resp.status_code == 401:
            raise AuthenticationError("Invalid API key", status_code=401)
        if resp.status_code != 200:
            raise DocumentUploadError(
                f"Upload URL request failed: {resp.text}",
                status_code=resp.status_code,
            )

        upload_data = resp.json()
        document_key = upload_data["document_key"]
        upload_url = upload_data["upload_url"]

        # Step 2 — PUT file bytes directly to S3
        try:
            with open(path, "rb") as f:
                s3_resp = self._client.put(
                    upload_url,
                    content=f.read(),
                    headers={
                        "Content-Type": content_type,
                        "x-amz-server-side-encryption": "AES256",
                    },
                )
        except httpx.RequestError as e:
            raise DocumentUploadError(f"S3 upload failed: {str(e)}")

        if s3_resp.status_code not in (200, 204):
            raise DocumentUploadError(
                f"S3 PUT failed (status {s3_resp.status_code}). "
                "Ensure x-amz-server-side-encryption: AES256 is present.",
                status_code=s3_resp.status_code,
            )

        return document_key

    # ── Job polling (async mode) ─────────────────────────────────────────

    def poll_job(
        self,
        job_id: str,
        *,
        poll_interval: float = _JOB_POLL_INTERVAL,
        max_wait: float = _JOB_POLL_MAX_WAIT,
    ) -> dict:
        """
        Poll an async job until it completes or times out.

        When you call any endpoint with ``async_mode=True``, the API returns
        a job_id immediately. Use this method to poll until the result is ready.

        Args:
            job_id: The job ID returned from the async call.
            poll_interval: Seconds between polls (default: 2s).
            max_wait: Maximum total wait time in seconds (default: 600s).

        Returns:
            The completed job result dict (same shape as a synchronous response).

        Raises:
            HyperAPIError: If the job fails or polling times out.
        """
        start = time.time()
        while True:
            elapsed = time.time() - start
            if elapsed >= max_wait:
                raise HyperAPIError(
                    f"Job {job_id} did not complete within {max_wait}s",
                    status_code=504,
                )

            try:
                resp = self._client.get(
                    f"{self.base_url}/v1/jobs/{job_id}",
                    headers=self._get_headers(),
                )
            except httpx.RequestError as e:
                raise HyperAPIError(f"Failed to poll job {job_id}: {str(e)}")

            if resp.status_code != 200:
                raise HyperAPIError(
                    f"Job poll failed: {resp.text}",
                    status_code=resp.status_code,
                )

            data = resp.json()
            status = data.get("status")

            if status == "completed":
                return data.get("result", data)
            if status == "failed":
                raise HyperAPIError(
                    f"Job {job_id} failed: {data.get('error', 'Unknown error')}",
                )

            time.sleep(poll_interval)

    # ── Core helpers ─────────────────────────────────────────────────────

    def _call_endpoint(
        self,
        endpoint: str,
        file_path: Path,
        *,
        error_cls: type,
        ocr_engine: OCREngine = "paddle",
        mode: Optional[str] = None,
        use_presigned: bool = True,
        async_mode: bool = False,
        timeout: Optional[float] = None,
    ) -> dict:
        """
        Internal helper that handles the upload + API call pattern shared by
        all four endpoints. Returns the parsed JSON response.
        """
        request_timeout = timeout or self.timeout
        params: dict = {"ocr_engine": ocr_engine}
        if mode is not None:
            params["mode"] = mode

        try:
            if use_presigned:
                content_type = CONTENT_TYPES.get(file_path.suffix.lower(), "application/octet-stream")
                document_key = self.upload_document(file_path, content_type=content_type)
                response = self._client.post(
                    f"{self.base_url}{endpoint}",
                    data={"document_key": document_key},
                    params=params,
                    headers=self._get_headers(async_mode=async_mode),
                    timeout=request_timeout,
                )
            else:
                content_type = CONTENT_TYPES.get(file_path.suffix.lower(), "application/octet-stream")
                with open(file_path, "rb") as f:
                    files = {"file": (file_path.name, f, content_type)}
                    response = self._client.post(
                        f"{self.base_url}{endpoint}",
                        files=files,
                        params=params,
                        headers=self._get_headers(async_mode=async_mode),
                        timeout=request_timeout,
                    )

            if response.status_code == 401:
                raise AuthenticationError("Invalid API key", status_code=401)
            if response.status_code == 402:
                raise error_cls("Insufficient credits", status_code=402)
            if response.status_code == 429:
                raise error_cls("Rate limit exceeded", status_code=429)
            if response.status_code != 200:
                raise error_cls(
                    f"{endpoint} failed: {response.text}",
                    status_code=response.status_code,
                )

            return response.json()

        except (AuthenticationError, DocumentUploadError):
            raise
        except error_cls:
            raise
        except httpx.TimeoutException:
            raise error_cls("Request timed out", status_code=504)
        except httpx.RequestError as e:
            raise error_cls(f"Request failed: {str(e)}")

    def _resolve_path(
        self,
        file_path: Union[str, Path, None],
        image_path: Union[str, Path, None] = None,
    ) -> Path:
        """Resolve and validate file path."""
        raw = file_path or image_path
        if raw is None:
            raise ValueError("file_path is required")
        path = Path(raw)
        if not path.exists():
            raise FileNotFoundError(f"File not found: {path}")
        return path

    # ── Public API ───────────────────────────────────────────────────────

    def parse(
        self,
        file_path: Union[str, Path] = None,
        *,
        image_path: Union[str, Path] = None,
        ocr_engine: OCREngine = "paddle",
        use_presigned: bool = True,
        async_mode: bool = False,
    ) -> dict:
        """
        Parse a document using OCR.

        Pipeline: file → doc-processor → OCR engine → raw text

        The OCR engine determines how the document is read:
        - ``"paddle"`` (default): PaddleOCR — fast, high-throughput
        - ``"doc-intent"``: Doc-Intent VLM — better on complex/noisy layouts

        Args:
            file_path: Path to the file (PDF, PNG, JPG, WEBP, TIFF, GIF).
            image_path: Deprecated alias for file_path.
            ocr_engine: OCR engine to use — "paddle" (default) or "doc-intent".
            use_presigned: Upload via S3 presigned URL (default True).
            async_mode: If True, returns a job_id immediately. Poll with poll_job().

        Returns:
            Synchronous: response envelope with ``result["result"]["ocr"]``.
            Async: ``{"job_id": "...", "status": "pending", "poll_url": "..."}``.

        Raises:
            ParseError: If parsing fails or times out.
        """
        path = self._resolve_path(file_path, image_path)
        return self._call_endpoint(
            "/v1/parse", path,
            error_cls=ParseError,
            ocr_engine=ocr_engine,
            use_presigned=use_presigned,
            async_mode=async_mode,
        )

    def extract(
        self,
        file_path: Union[str, Path],
        *,
        ocr_engine: OCREngine = "paddle",
        mode: str = "default",
        use_presigned: bool = True,
        async_mode: bool = False,
    ) -> dict:
        """
        Extract structured data from a document (entities + line items).

        Pipeline: file → OCR engine → extract-service → structured fields

        The OCR stage runs first (same as parse), then the OCR output is sent
        to the extract service which returns entities and line items.

        Args:
            file_path: Path to the file (PDF, PNG, JPG, WEBP, TIFF).
            ocr_engine: OCR engine — "paddle" (default) or "doc-intent".
            mode: Processing mode (default: "default").
            use_presigned: Upload via S3 presigned URL (default True).
            async_mode: If True, returns a job_id immediately. Poll with poll_job().

        Returns:
            Synchronous: response envelope with ``result["result"]`` containing
            entities and line items, plus ``result["ocr_text"]`` with the raw OCR.
            Async: ``{"job_id": "...", "status": "pending", "poll_url": "..."}``.

        Raises:
            ExtractError: If extraction fails or times out.
        """
        path = self._resolve_path(file_path)
        return self._call_endpoint(
            "/v1/extract", path,
            error_cls=ExtractError,
            ocr_engine=ocr_engine,
            mode=mode,
            use_presigned=use_presigned,
            async_mode=async_mode,
            timeout=_EXTRACT_TIMEOUT,
        )

    def classify(
        self,
        file_path: Union[str, Path],
        *,
        ocr_engine: OCREngine = "paddle",
        mode: str = "default",
        use_presigned: bool = True,
        async_mode: bool = False,
    ) -> dict:
        """
        Classify a document type (invoice, contract, receipt, etc.).

        Pipeline: file → OCR engine → classifier-splitter /v2/classify

        The OCR stage runs first, then the OCR output is sent to the
        classifier-splitter service for document type classification.

        Args:
            file_path: Path to the file (PDF, PNG, JPG, WEBP, TIFF).
            ocr_engine: OCR engine — "paddle" (default) or "doc-intent".
            mode: Processing mode (default: "default").
            use_presigned: Upload via S3 presigned URL (default True).
            async_mode: If True, returns a job_id immediately. Poll with poll_job().

        Returns:
            Synchronous: response envelope with classification result.
            Async: ``{"job_id": "...", "status": "pending", "poll_url": "..."}``.

        Raises:
            ClassifyError: If classification fails or times out.
        """
        path = self._resolve_path(file_path)
        return self._call_endpoint(
            "/v1/classify", path,
            error_cls=ClassifyError,
            ocr_engine=ocr_engine,
            mode=mode,
            use_presigned=use_presigned,
            async_mode=async_mode,
        )

    def split(
        self,
        file_path: Union[str, Path],
        *,
        ocr_engine: OCREngine = "paddle",
        mode: str = "default",
        use_presigned: bool = True,
        async_mode: bool = False,
    ) -> dict:
        """
        Split a multi-document PDF into individual document segments.

        Pipeline: file → OCR engine → classifier-splitter /v2/split

        The OCR stage runs first, then the OCR output is sent to the
        classifier-splitter service to detect document boundaries.

        Args:
            file_path: Path to the file (PDF, PNG, JPG, WEBP, TIFF).
            ocr_engine: OCR engine — "paddle" (default) or "doc-intent".
            mode: Processing mode (default: "default").
            use_presigned: Upload via S3 presigned URL (default True).
            async_mode: If True, returns a job_id immediately. Poll with poll_job().

        Returns:
            Synchronous: response envelope with ``result["result"]["segments"]``.
            Async: ``{"job_id": "...", "status": "pending", "poll_url": "..."}``.

        Raises:
            SplitError: If splitting fails or times out.
        """
        path = self._resolve_path(file_path)
        return self._call_endpoint(
            "/v1/split", path,
            error_cls=SplitError,
            ocr_engine=ocr_engine,
            mode=mode,
            use_presigned=use_presigned,
            async_mode=async_mode,
        )

    def process(
        self,
        file_path: Union[str, Path] = None,
        *,
        image_path: Union[str, Path] = None,
        ocr_engine: OCREngine = "paddle",
    ) -> dict:
        """
        Parse and extract in one call.

        Uploads the document once, then calls parse and extract using the
        same document_key (avoiding a second upload). Note that extract runs
        its own OCR stage internally, but the router's Redis OCR cache
        deduplicates the second OCR run for the same file.

        Args:
            file_path: Path to the file (PDF, PNG, JPG, etc.)
            image_path: Deprecated alias for file_path.
            ocr_engine: OCR engine — "paddle" (default) or "doc-intent".

        Returns:
            dict with keys:
                - ocr: Raw OCR text from parse
                - data: Extracted structured fields from extract
        """
        path = self._resolve_path(file_path, image_path)
        content_type = CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")
        document_key = self.upload_document(path, content_type=content_type)

        parse_params = {"ocr_engine": ocr_engine}
        extract_params = {"ocr_engine": ocr_engine, "mode": "default"}
        headers = self._get_headers()

        parse_response = self._client.post(
            f"{self.base_url}/v1/parse",
            data={"document_key": document_key},
            params=parse_params,
            headers=headers,
        )
        if parse_response.status_code == 401:
            raise AuthenticationError("Invalid API key", status_code=401)
        if parse_response.status_code == 402:
            raise ParseError("Insufficient credits", status_code=402)
        if parse_response.status_code == 429:
            raise ParseError("Rate limit exceeded", status_code=429)
        if parse_response.status_code != 200:
            raise ParseError(
                f"Parse failed: {parse_response.text}",
                status_code=parse_response.status_code,
            )

        extract_response = self._client.post(
            f"{self.base_url}/v1/extract",
            data={"document_key": document_key},
            params=extract_params,
            headers=self._get_headers(),
            timeout=_EXTRACT_TIMEOUT,
        )
        if extract_response.status_code == 401:
            raise AuthenticationError("Invalid API key", status_code=401)
        if extract_response.status_code == 402:
            raise ExtractError("Insufficient credits", status_code=402)
        if extract_response.status_code == 429:
            raise ExtractError("Rate limit exceeded", status_code=429)
        if extract_response.status_code != 200:
            raise ExtractError(
                f"Extract failed: {extract_response.text}",
                status_code=extract_response.status_code,
            )

        parse_result = parse_response.json()
        extract_result = extract_response.json()

        return {
            "ocr": parse_result["result"]["ocr"],
            "data": extract_result.get("result", {}),
        }

    # ── Lifecycle ────────────────────────────────────────────────────────

    def close(self):
        """Close the HTTP client."""
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
