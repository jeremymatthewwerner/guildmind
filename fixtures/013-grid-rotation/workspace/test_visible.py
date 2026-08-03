import unittest

from grid import rotate_grid


class GridRotationVisibleTests(unittest.TestCase):
    def test_rotates_a_wide_rectangle(self) -> None:
        self.assertEqual(
            rotate_grid([[1, 2, 3], [4, 5, 6]]),
            [[4, 1], [5, 2], [6, 3]],
        )


if __name__ == "__main__":
    unittest.main()
