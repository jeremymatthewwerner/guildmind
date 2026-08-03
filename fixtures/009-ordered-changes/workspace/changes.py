"""Ordered transformations over nested JSON values."""

from typing import Any


def apply_changes(document: Any, changes: list[dict[str, Any]]) -> Any:
    """Return the document after applying every valid change in order."""

    result = dict(document)
    for change in changes:
        if change["op"] == "set":
            result[change["path"][-1]] = change["value"]
    return result
