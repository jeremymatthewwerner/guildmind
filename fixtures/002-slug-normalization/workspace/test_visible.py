import unittest

from slug import slugify


class SlugVisibleTests(unittest.TestCase):
    def test_collapses_outer_punctuation_and_spaces(self) -> None:
        self.assertEqual(slugify(" Hello   World! "), "hello-world")


if __name__ == "__main__":
    unittest.main()
