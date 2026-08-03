import unittest

from intervals import merge_intervals


class IntervalHiddenTests(unittest.TestCase):
    def test_empty_input_stays_empty(self) -> None:
        self.assertEqual(merge_intervals([]), [])

    def test_unsorted_adjacent_intervals_merge(self) -> None:
        self.assertEqual(merge_intervals([[5, 7], [1, 2], [3, 4]]), [[1, 7]])

    def test_negative_adjacency_and_gap_are_distinct(self) -> None:
        self.assertEqual(
            merge_intervals([[-5, -3], [-2, 0], [2, 3]]),
            [[-5, 0], [2, 3]],
        )

    def test_unsorted_containment_is_coalesced(self) -> None:
        source = [[1, 10], [2, 3], [12, 14], [13, 15]]
        self.assertEqual(merge_intervals(source), [[1, 10], [12, 15]])

    def test_disjoint_singletons_remain_distinct(self) -> None:
        self.assertEqual(
            merge_intervals([[1, 1], [3, 3], [8, 9]]),
            [[1, 1], [3, 3], [8, 9]],
        )
