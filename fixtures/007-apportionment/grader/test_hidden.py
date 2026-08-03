import unittest

from apportionment import apportion


class ApportionmentHiddenTests(unittest.TestCase):
    def test_exact_quotas_need_no_remainder_units(self) -> None:
        self.assertEqual(apportion(12, [1, 2, 3]), [2, 4, 6])

    def test_zero_weight_entries_receive_nothing(self) -> None:
        self.assertEqual(apportion(7, [0, 2, 0, 1]), [0, 5, 0, 2])

    def test_multiple_remainder_ties_are_stable(self) -> None:
        self.assertEqual(apportion(5, [1, 3, 3, 1]), [1, 2, 2, 0])

    def test_zero_total_supports_all_zero_weights(self) -> None:
        self.assertEqual(apportion(0, [0, 0, 0]), [0, 0, 0])

    def test_large_integer_remainders_do_not_use_float_rounding(self) -> None:
        self.assertEqual(
            apportion(1, [9007199254740992, 9007199254740993, 1]),
            [0, 1, 0],
        )
