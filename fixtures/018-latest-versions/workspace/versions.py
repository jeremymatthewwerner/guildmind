"""Stable selection of the latest named version records."""

from typing import Any

Record = dict[str, Any]


def latest_versions(records: list[Record]) -> list[Record]:
    """Return lexically greatest versions in alphabetical name order."""

    selected: dict[str, Record] = {}
    for record in records:
        name = record["name"]
        current = selected.get(name)
        if current is None or record["version"] > current["version"]:
            selected[name] = record
    return [selected[name] for name in sorted(selected)]
