import unittest

from business_days import business_days


class BusinessDaysVisibleTests(unittest.TestCase):
    def test_end_date_is_excluded(self) -> None:
        self.assertEqual(business_days("2024-03-04", "2024-03-08", []), 4)


if __name__ == "__main__":
    unittest.main()
