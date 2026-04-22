"""
HyperAPI Python SDK
Financial document processing APIs that scale well.
"""

from .client import HyperAPIClient
from .exceptions import (
    AuthenticationError,
    ClassifyError,
    DocumentUploadError,
    ExtractError,
    HyperAPIError,
    ParseError,
    SplitError,
)

__version__ = "0.2.0"
__all__ = ["HyperAPIClient", "HyperAPIError", "AuthenticationError", "ParseError", "ExtractError", "ClassifyError", "SplitError", "DocumentUploadError"]
