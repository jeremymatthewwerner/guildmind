import unittest

from business_days import business_days


class BusinessDaysHiddenTests(unittest.TestCase):
    def test_empty_half_open_interval_counts_nothing(self) -> None:
        self.assertEqual(business_days("2024-03-04", "2024-03-04", []), 0)

    def test_weekend_before_excluded_monday_counts_only_friday(self) -> None:
        self.assertEqual(business_days("2024-05-03", "2024-05-06", []), 1)

    def test_duplicate_and_weekend_holidays_have_no_extra_effect(self) -> None:
        self.assertEqual(
            business_days(
                "2024-07-01",
                "2024-07-08",
                ["2024-07-04", "2024-07-04", "2024-07-06"],
            ),
            4,
        )

    def test_leap_day_can_be_a_holiday(self) -> None:
        self.assertEqual(
            business_days("2024-02-28", "2024-03-02", ["2024-02-29"]),
            2,
        )

    def test_weekend_start_and_excluded_tuesday_count_monday(self) -> None:
        self.assertEqual(business_days("2024-06-01", "2024-06-04", []), 1)


if __name__ == "__main__":
    unittest.main()
