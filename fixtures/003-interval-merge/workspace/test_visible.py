import unittest

from intervals import merge_intervals


class IntervalVisibleTests(unittest.TestCase):
    def test_merges_an_overlap(self) -> None:
        self.assertEqual(merge_intervals([[1, 3], [2, 4]]), [[1, 4]])


if __name__ == "__main__":
    unittest.main()
