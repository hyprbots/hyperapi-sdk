"""
HyperAPI Exceptions
"""


class HyperAPIError(Exception):
    """Base exception for HyperAPI errors."""

    def __init__(self, message: str, status_code: int = None):
        self.message = message
        self.status_code = status_code
        super().__init__(self.message)


class AuthenticationError(HyperAPIError):
    """Raised when API key is invalid or missing."""
    pass


class ParseError(HyperAPIError):
    """Raised when document parsing fails."""
    pass


class ExtractError(HyperAPIError):
    """Raised when field extraction fails."""
    pass
