import unittest

from changes import apply_changes


class OrderedChangesHiddenTests(unittest.TestCase):
    def test_delete_then_set_uses_shifted_list_index(self) -> None:
        self.assertEqual(
            apply_changes(
                {"items": ["a", "b", "c", "d"]},
                [
                    {"op": "delete", "path": ["items", 1]},
                    {"op": "set", "path": ["items", 1], "value": "C"},
                ],
            ),
            {"items": ["a", "C", "d"]},
        )

    def test_insert_then_delete_uses_current_list(self) -> None:
        self.assertEqual(
            apply_changes(
                {"items": [1, 3]},
                [
                    {"op": "insert", "path": ["items"], "index": 1, "value": 2},
                    {"op": "delete", "path": ["items", 0]},
                ],
            ),
            {"items": [2, 3]},
        )

    def test_root_replacement_precedes_nested_insert(self) -> None:
        self.assertEqual(
            apply_changes(
                {"old": True},
                [
                    {"op": "set", "path": [], "value": {"items": []}},
                    {"op": "insert", "path": ["items"], "index": 0, "value": "first"},
                ],
            ),
            {"items": ["first"]},
        )

    def test_nested_delete_and_set_preserve_siblings(self) -> None:
        self.assertEqual(
            apply_changes(
                {"a": {"keep": 1, "drop": 2}, "z": 0},
                [
                    {"op": "delete", "path": ["a", "drop"]},
                    {"op": "set", "path": ["a", "keep"], "value": 9},
                ],
            ),
            {"a": {"keep": 9}, "z": 0},
        )

    def test_inserted_items_participate_in_later_paths(self) -> None:
        self.assertEqual(
            apply_changes(
                {"rows": [{"id": "a"}, {"id": "b"}]},
                [
                    {
                        "op": "insert",
                        "path": ["rows"],
                        "index": 0,
                        "value": {"id": "x"},
                    },
                    {"op": "set", "path": ["rows", 1, "id"], "value": "A"},
                ],
            ),
            {"rows": [{"id": "x"}, {"id": "A"}, {"id": "b"}]},
        )
