"""Base class for every response model.

Models are generated from ``openapi/hyperapi-public.yaml`` (see
``scripts/generate_types.py``) and all derive from :class:`HyperAPIModel`.

Two promises hold for every model:

* **Forward compatible.** Unknown fields the API adds later are kept, not
  rejected (``extra="allow"``), and reach ``to_dict()`` unchanged.
* **Dict-style access still works.** ``result["result"]["ocr"]``,
  ``result.get("usage")`` and ``"pages" in result`` behave as they did when
  the SDK returned plain dicts, so 0.7 code keeps running. Attribute access
  (``result.result.ocr``) is the typed way and gives editor completion.

Use :meth:`to_dict` (or ``model_dump()``) where a real ``dict`` is required,
e.g. ``json.dumps(result.to_dict())``.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from pydantic import BaseModel, ConfigDict

_MISSING = object()


class HyperAPIModel(BaseModel):
    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
        protected_namespaces=(),
        arbitrary_types_allowed=False,
    )

    # ── Mapping-style access (compatibility with the 0.7 dict results) ────

    @classmethod
    def _field_for_key(cls, key: str) -> str | None:
        """The attribute name that holds JSON key ``key`` (aliases included)."""
        for name, info in cls.model_fields.items():
            if (info.alias or name) == key:
                return name
        return None

    def _lookup(self, key: Any) -> Any:
        if not isinstance(key, str):
            return _MISSING
        name = self._field_for_key(key)
        if name is not None:
            if name not in self.model_fields_set:
                return _MISSING  # absent in the reply, as a dict would be
            return getattr(self, name)
        extra = self.__pydantic_extra__ or {}
        return extra.get(key, _MISSING)

    def __getitem__(self, key: str) -> Any:
        value = self._lookup(key)
        if value is _MISSING:
            raise KeyError(key)
        return value

    def get(self, key: str, default: Any = None) -> Any:
        value = self._lookup(key)
        return default if value is _MISSING else value

    def __contains__(self, key: object) -> bool:
        return self._lookup(key) is not _MISSING

    def keys(self) -> list[str]:
        return list(self.to_dict().keys())

    def items(self) -> list[tuple[str, Any]]:
        return [(k, self[k]) for k in self.keys()]

    def values(self) -> list[Any]:
        return [self[k] for k in self.keys()]

    def __iter__(self) -> Iterator[str]:  # type: ignore[override]
        # Iterate JSON keys like a dict (pydantic's default yields (name, value) pairs).
        return iter(self.keys())

    def __len__(self) -> int:
        return len(self.keys())

    def __eq__(self, other: object) -> bool:
        if isinstance(other, dict):
            return self.to_dict() == other
        if isinstance(other, HyperAPIModel):
            return self.to_dict() == other.to_dict()
        return NotImplemented

    __hash__ = None  # mutable, like the dicts it replaces

    # ── Conversion ────────────────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        """A plain ``dict`` with the reply's JSON keys, exactly the fields the
        API sent (none added, unknown ones kept)."""
        return self.model_dump(mode="json", by_alias=True, exclude_unset=True)

    def to_json(self, **kwargs: Any) -> str:
        return self.model_dump_json(by_alias=True, exclude_unset=True, **kwargs)
