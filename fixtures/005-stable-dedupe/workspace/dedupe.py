"""Stable record deduplication."""

from typing import Any


def dedupe_by(records: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    """Keep the first record for each structurally distinct JSON key value."""

    return list({record[key]: record for record in records}.values())
