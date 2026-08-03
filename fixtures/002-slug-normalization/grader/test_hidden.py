import unittest

from slug import slugify


class SlugHiddenTests(unittest.TestCase):
    def test_collapses_all_whitespace_runs(self) -> None:
        self.assertEqual(slugify("Multiple\tSpaces\nHere"), "multiple-spaces-here")

    def test_collapses_mixed_separator_runs(self) -> None:
        self.assertEqual(slugify("API_v2--Ready!"), "api-v2-ready")

    def test_retains_unicode_alphanumeric_characters(self) -> None:
        self.assertEqual(slugify("Crème brûlée"), "crème-brûlée")

    def test_uses_unicode_case_folding(self) -> None:
        self.assertEqual(slugify("Straße & CAFÉ"), "strasse-café")

    def test_only_separators_produce_an_empty_slug(self) -> None:
        self.assertEqual(slugify("---"), "")
