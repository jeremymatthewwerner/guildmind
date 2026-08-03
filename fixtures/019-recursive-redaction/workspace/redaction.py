"""Object-key redaction over JSON-shaped values."""

from typing import Any

JsonValue = None | bool | int | float | str | list[Any] | dict[str, Any]


def redact_keys(value: JsonValue, blocked: list[str]) -> JsonValue:
    """Remove blocked keys from only the root object."""

    if not isinstance(value, dict):
        return value
    blocked_keys = set(blocked)
    return {key: item for key, item in value.items() if key not in blocked_keys}
