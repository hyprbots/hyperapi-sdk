"""
HyperAPI Python SDK
Financial document processing APIs that scale well.
"""

from .client import HyperAPIClient
from .exceptions import HyperAPIError, AuthenticationError, ParseError, ExtractError, ClassifyError, SplitError, DocumentUploadError

__version__ = "0.2.0"
__all__ = ["HyperAPIClient", "HyperAPIError", "AuthenticationError", "ParseError", "ExtractError", "ClassifyError", "SplitError", "DocumentUploadError"]
