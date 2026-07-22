"""HyperAPI Python SDK.

Document intelligence APIs for parsing, extracting, classifying, and splitting
documents. Calls finish via async submit + poll under the hood (transparent to
the caller) so customer requests are never bound by edge timeouts.
"""

import logging

from .async_client import AsyncHyperAPIClient
from .client import HyperAPIClient, Job
from .exceptions import (
    AuthenticationError,
    ClassifyError,
    DocumentUploadError,
    EditError,
    ExtractError,
    HyperAPIError,
    JobTimeoutError,
    ParseError,
    RateLimitError,
    RedactError,
    SplitError,
)

__version__ = "0.6.0"

# Library-style logger: silent unless the application configures handlers.
# Customers opt in with: `logging.getLogger("hyperapi").setLevel(logging.INFO)`.
logging.getLogger(__name__).addHandler(logging.NullHandler())

__all__ = [
    "HyperAPIClient",
    "AsyncHyperAPIClient",
    "Job",
    "HyperAPIError",
    "AuthenticationError",
    "RateLimitError",
    "JobTimeoutError",
    "ParseError",
    "ExtractError",
    "EditError",
    "ClassifyError",
    "SplitError",
    "RedactError",
    "DocumentUploadError",
    "__version__",
]
