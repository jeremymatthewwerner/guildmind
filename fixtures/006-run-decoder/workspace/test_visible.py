import unittest

from runs import decode_runs


class RunDecoderVisibleTests(unittest.TestCase):
    def test_decodes_a_multi_digit_count(self) -> None:
        self.assertEqual(decode_runs("12:x;"), ["x"] * 12)


if __name__ == "__main__":
    unittest.main()
