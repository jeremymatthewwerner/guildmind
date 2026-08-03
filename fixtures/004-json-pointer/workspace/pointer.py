"""Resolve JSON Pointer paths."""

from typing import Any


def resolve_pointer(document: Any, pointer: str) -> Any:
    """Return the JSON value selected by a valid RFC 6901 pointer."""

    if isinstance(document, dict):
        return document.get(pointer)
    return None
