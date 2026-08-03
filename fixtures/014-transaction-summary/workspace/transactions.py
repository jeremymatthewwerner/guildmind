"""Stable aggregation of categorized transactions."""

from typing import Any


def summarize_transactions(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return alphabetically ordered positive transaction totals by category."""

    totals: dict[str, int] = {}
    for row in rows:
        category = row.get("category")
        amount = row["amount"]
        if not isinstance(category, str) or amount == 0:
            continue
        totals[category] = totals.get(category, 0) + amount
    return [{"category": category, "net": totals[category]} for category in sorted(totals)]
