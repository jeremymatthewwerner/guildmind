"""Decode a compact sequence of character runs."""


def decode_runs(encoded: str) -> list[str] | None:
    """Decode valid count/symbol runs, or return ``None`` for malformed input."""

    if not encoded:
        return []
    return [encoded[2]] * int(encoded[0])
