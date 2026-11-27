"""Turn a decoded JSON object into its response model, never failing the call.

The API adds fields and enum values over time. A reply that does not validate
against the bundled spec must not break a working integration, so validation
failures fall back to :class:`GenericResponse` (every key kept, dict-style
and attribute access still work) and are logged once per model, not raised.
"""

from __future__ import annotations

import logging
from typing import Any, TypeVar

from pydantic import ValidationError

from ._base import HyperAPIModel

logger = logging.getLogger("hyperapi")

M = TypeVar("M", bound=HyperAPIModel)

_warned: set[str] = set()


class GenericResponse(HyperAPIModel):
    """A reply kept as-is: the fallback when a model does not fit."""


def _generic(value: Any) -> Any:
    if isinstance(value, dict):
        return GenericResponse.model_validate({k: _generic(v) for k, v in value.items()})
    if isinstance(value, list):
        return [_generic(v) for v in value]
    return value


def coerce(model: type[M], data: Any) -> M:
    """``model.model_validate(data)``, or a :class:`GenericResponse` on mismatch."""
    try:
        return model.model_validate(data)
    except ValidationError as e:
        if model.__name__ not in _warned:
            _warned.add(model.__name__)
            logger.warning(
                "response_model_mismatch",
                extra={"model": model.__name__, "errors": e.error_count()},
            )
        return _generic(data)  # type: ignore[return-value]
