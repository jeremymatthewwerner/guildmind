import unittest

from runs import decode_runs


class RunDecoderHiddenTests(unittest.TestCase):
    def test_decodes_multiple_runs(self) -> None:
        self.assertEqual(decode_runs("3:a;2:b;"), ["a", "a", "a", "b", "b"])

    def test_decodes_quoted_semicolon_and_backslash(self) -> None:
        self.assertEqual(decode_runs(r"2:\;;1:\\;"), [";", ";", "\\"])

    def test_treats_digit_and_colon_symbols_literally(self) -> None:
        self.assertEqual(decode_runs("1:7;2::;"), ["7", ":", ":"])

    def test_empty_encoding_returns_an_empty_list(self) -> None:
        self.assertEqual(decode_runs(""), [])

    def test_zero_count_is_invalid(self) -> None:
        self.assertIsNone(decode_runs("0:a;"))
