import unittest

from wrapping import wrap_words


class WordWrapVisibleTests(unittest.TestCase):
    def test_words_that_exactly_fill_a_line_stay_together(self) -> None:
        self.assertEqual(wrap_words(["red", "blue"], 8), ["red blue"])


if __name__ == "__main__":
    unittest.main()
