"""Deterministic JSON serialization and SHA-256 helpers for domain records."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel


def _json_compatible(value: Any) -> Any:
    """Convert supported values to an unambiguous JSON-compatible representation."""
    if isinstance(value, BaseModel):
        return _json_compatible(value.model_dump(mode="python", by_alias=True))
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("canonical JSON does not support non-finite floats")
        return value
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("canonical JSON requires timezone-aware datetimes")
        utc_value = value.astimezone(UTC)
        return utc_value.isoformat(timespec="microseconds").replace("+00:00", "Z")
    if isinstance(value, Enum):
        return _json_compatible(value.value)
    if isinstance(value, Mapping):
        converted: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("canonical JSON object keys must be strings")
            converted[key] = _json_compatible(item)
        return converted
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
        return [_json_compatible(item) for item in value]
    raise TypeError(f"unsupported canonical JSON value: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    """Serialize a domain model or JSON value deterministically.

    Keys are sorted, insignificant whitespace is removed, Unicode is preserved, and
    aware datetimes are normalized to UTC with fixed microsecond precision.
    """
    return json.dumps(
        _json_compatible(value),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def sha256_bytes(value: bytes) -> str:
    """Return the lowercase hexadecimal SHA-256 digest of bytes."""
    return hashlib.sha256(value).hexdigest()


def canonical_sha256(value: Any) -> str:
    """Hash the UTF-8 bytes of :func:`canonical_json`."""
    return sha256_bytes(canonical_json(value).encode("utf-8"))
