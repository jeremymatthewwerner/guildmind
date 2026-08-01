import unittest

from addition import add


class AdditionVisibleTests(unittest.TestCase):
    def test_adds_two_positive_integers(self) -> None:
        self.assertEqual(add(2, 3), 5)


if __name__ == "__main__":
    unittest.main()
