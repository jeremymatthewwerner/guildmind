import unittest

from roman import parse_roman


class RomanParserVisibleTests(unittest.TestCase):
    def test_parses_multiple_subtractive_pairs(self) -> None:
        self.assertEqual(parse_roman("MCMXCIV"), 1994)


if __name__ == "__main__":
    unittest.main()
