import unittest
from pathlib import Path

from addition import add


class AdditionHiddenTests(unittest.TestCase):
    def test_grader_is_outside_the_submitted_workspace(self) -> None:
        self.assertFalse(Path(__file__).resolve().is_relative_to(Path.cwd().resolve()))

    def test_adds_two_negative_integers(self) -> None:
        self.assertEqual(add(-8, -5), -13)

    def test_adds_mixed_sign_integers(self) -> None:
        self.assertEqual(add(7, -11), -4)

    def test_zero_is_an_identity(self) -> None:
        self.assertEqual(add(0, 19), 19)
