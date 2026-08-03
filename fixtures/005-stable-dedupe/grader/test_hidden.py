import unittest

from dedupe import dedupe_by


class DedupeHiddenTests(unittest.TestCase):
    def test_preserves_interleaved_first_seen_order(self) -> None:
        records = [
            {"id": "b", "value": 1},
            {"id": "a", "value": 2},
            {"id": "b", "value": 3},
            {"id": "c", "value": 4},
            {"id": "a", "value": 5},
        ]
        self.assertEqual(dedupe_by(records, "id"), [*records[:2], records[3]])

    def test_distinguishes_falsy_json_values(self) -> None:
        records = [
            {"id": 0, "value": "zero"},
            {"id": False, "value": "false"},
            {"id": "", "value": "empty"},
            {"id": None, "value": "null"},
            {"id": 0, "value": "later"},
        ]
        self.assertEqual(dedupe_by(records, "id"), records[:4])

    def test_supports_list_valued_keys(self) -> None:
        records = [
            {"tags": ["a", "b"], "value": 1},
            {"tags": ["a", "b"], "value": 2},
            {"tags": ["b", "a"], "value": 3},
        ]
        self.assertEqual(dedupe_by(records, "tags"), [records[0], records[2]])

    def test_object_key_order_is_not_semantic(self) -> None:
        records = [
            {"meta": {"a": 1, "b": 2}, "value": 1},
            {"meta": {"b": 2, "a": 1}, "value": 2},
        ]
        self.assertEqual(dedupe_by(records, "meta"), [records[0]])

    def test_empty_input_stays_empty(self) -> None:
        self.assertEqual(dedupe_by([], "id"), [])
