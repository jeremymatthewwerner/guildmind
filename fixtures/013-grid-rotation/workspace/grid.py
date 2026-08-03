"""Clockwise rotation of rectangular grids."""

from typing import Any


def rotate_grid(grid: list[list[Any]]) -> list[list[Any]]:
    """Rotate a square grid 90 degrees clockwise."""

    size = len(grid)
    return [[grid[size - 1 - row][column] for row in range(size)] for column in range(size)]
