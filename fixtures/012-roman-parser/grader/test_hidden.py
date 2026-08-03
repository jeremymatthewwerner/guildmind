import unittest

from roman import parse_roman


class RomanParserHiddenTests(unittest.TestCase):
    def test_repeated_additive_symbols_are_valid(self) -> None:
        self.assertEqual(parse_roman("III"), 3)

    def test_standard_subtractive_pair_is_valid(self) -> None:
        self.assertEqual(parse_roman("XLII"), 42)

    def test_largest_additive_canonical_example(self) -> None:
        self.assertEqual(parse_roman("MMMDCCCLXXXVIII"), 3888)

    def test_illegal_repetition_is_rejected(self) -> None:
        self.assertIsNone(parse_roman("IIII"))

    def test_illegal_subtractive_pair_is_rejected(self) -> None:
        self.assertIsNone(parse_roman("IC"))


if __name__ == "__main__":
    unittest.main()
