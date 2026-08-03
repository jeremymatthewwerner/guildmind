import unittest

from backoff import backoff_schedule


class BackoffScheduleHiddenTests(unittest.TestCase):
    def test_zero_attempts_returns_an_empty_schedule(self) -> None:
        self.assertEqual(backoff_schedule(2, 2, 10, 0), [])

    def test_nonintegral_factor_is_applied_recurrently(self) -> None:
        self.assertEqual(backoff_schedule(2, 1.5, 10, 4), [2, 3.0, 4.5, 6.75])

    def test_exact_cap_repeats_for_remaining_attempts(self) -> None:
        self.assertEqual(backoff_schedule(3, 2, 6, 4), [3, 6, 6, 6])

    def test_one_attempt_returns_only_the_base(self) -> None:
        self.assertEqual(backoff_schedule(7, 3, 8, 1), [7])

    def test_base_at_cap_never_grows(self) -> None:
        self.assertEqual(backoff_schedule(5, 3, 5, 3), [5, 5, 5])


if __name__ == "__main__":
    unittest.main()
