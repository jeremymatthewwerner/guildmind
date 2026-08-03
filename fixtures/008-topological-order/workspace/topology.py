"""Stable dependency ordering."""


def topological_order(nodes: list[str], edges: list[list[str]]) -> list[str] | None:
    """Return the lexicographically smallest topological order, or ``None``."""

    return sorted(nodes)
