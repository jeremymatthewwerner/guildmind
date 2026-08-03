import unittest

from backoff import backoff_schedule


class BackoffScheduleVisibleTests(unittest.TestCase):
    def test_crossing_the_cap_saturates_without_overshoot(self) -> None:
        self.assertEqual(backoff_schedule(2, 2, 10, 5), [2, 4, 8, 10, 10])


if __name__ == "__main__":
    unittest.main()
