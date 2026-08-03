import unittest

from apportionment import apportion


class ApportionmentVisibleTests(unittest.TestCase):
    def test_equal_remainders_use_input_order(self) -> None:
        self.assertEqual(apportion(10, [1, 1, 1]), [4, 3, 3])


if __name__ == "__main__":
    unittest.main()
