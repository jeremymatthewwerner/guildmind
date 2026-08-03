import unittest

from grid import rotate_grid


class GridRotationHiddenTests(unittest.TestCase):
    def test_rotates_a_tall_rectangle(self) -> None:
        self.assertEqual(
            rotate_grid([[1, 2], [3, 4], [5, 6]]),
            [[5, 3, 1], [6, 4, 2]],
        )

    def test_rotates_a_single_row_into_a_column(self) -> None:
        self.assertEqual(rotate_grid([["a", "b", "c"]]), [["a"], ["b"], ["c"]])

    def test_rotates_a_single_column_into_a_row(self) -> None:
        self.assertEqual(rotate_grid([[1], [2], [3]]), [[3, 2, 1]])

    def test_empty_grid_stays_empty(self) -> None:
        self.assertEqual(rotate_grid([]), [])

    def test_preserves_arbitrary_json_values(self) -> None:
        self.assertEqual(
            rotate_grid([[{"id": "a"}, None], [True, 7]]),
            [[True, {"id": "a"}], [7, None]],
        )


if __name__ == "__main__":
    unittest.main()
