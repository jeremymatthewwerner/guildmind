import unittest

from pointer import resolve_pointer


class PointerHiddenTests(unittest.TestCase):
    def test_decodes_slash_and_tilde_tokens(self) -> None:
        document = {"a/b": {"~key": [10, 20]}}
        self.assertEqual(resolve_pointer(document, "/a~1b/~0key/1"), 20)

    def test_traverses_a_root_array(self) -> None:
        self.assertEqual(resolve_pointer([{"x": 1}, {"x": 2}], "/1/x"), 2)

    def test_empty_pointer_returns_the_document(self) -> None:
        document = {"nested": [1]}
        self.assertEqual(resolve_pointer(document, ""), document)

    def test_single_slash_selects_an_empty_key(self) -> None:
        self.assertEqual(resolve_pointer({"": "empty-key"}, "/"), "empty-key")

    def test_preserves_a_nested_false_value(self) -> None:
        self.assertIs(resolve_pointer({"a": [{"b": False}]}, "/a/0/b"), False)
