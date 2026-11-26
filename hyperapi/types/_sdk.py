"""Models for values the SDK composes itself (not a single API reply)."""

from __future__ import annotations

from typing import Optional, Union

from ._base import HyperAPIModel
from ._coerce import GenericResponse
from ._generated import ExtractResponse


class ProcessResult(HyperAPIModel):
    """What :py:meth:`HyperAPIClient.process` returns: parse text + the extract reply."""

    ocr: Optional[str] = None
    """The parse leg's OCR text (``None`` if the parse result carried none)."""
    data: Union[ExtractResponse, GenericResponse]
    """The extract leg's full reply; the fields are at ``data.result``."""
