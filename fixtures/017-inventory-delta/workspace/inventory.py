"""Inventory comparison over repeated item names."""

Change = dict[str, str | int]


def inventory_delta(before: list[str], after: list[str]) -> dict[str, list[Change]]:
    """Return alphabetic one-count differences between the two item sets."""

    before_items = set(before)
    after_items = set(after)
    return {
        "added": [{"item": item, "count": 1} for item in sorted(after_items - before_items)],
        "removed": [{"item": item, "count": 1} for item in sorted(before_items - after_items)],
    }
