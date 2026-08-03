import unittest

from wrapping import wrap_words


class WordWrapHiddenTests(unittest.TestCase):
    def test_greedily_fills_multiple_lines(self) -> None:
        self.assertEqual(
            wrap_words(["one", "two", "three", "four"], 10),
            ["one two", "three four"],
        )

    def test_resumes_packing_after_an_oversized_word(self) -> None:
        self.assertEqual(
            wrap_words(["encyclopedia", "a", "bb"], 4),
            ["encyclopedia", "a bb"],
        )

    def test_empty_input_has_no_lines(self) -> None:
        self.assertEqual(wrap_words([], 6), [])

    def test_single_oversized_word_is_not_split(self) -> None:
        self.assertEqual(wrap_words(["oversized"], 3), ["oversized"])

    def test_line_breaks_preserve_input_order(self) -> None:
        self.assertEqual(
            wrap_words(["a", "bb", "c", "ddd"], 4),
            ["a bb", "c", "ddd"],
        )


if __name__ == "__main__":
    unittest.main()
