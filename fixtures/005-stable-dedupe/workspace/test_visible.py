import unittest

from dedupe import dedupe_by


class DedupeVisibleTests(unittest.TestCase):
    def test_preserves_the_first_duplicate_record(self) -> None:
        records = [
            {"id": "a", "value": 1},
            {"id": "a", "value": 2},
            {"id": "b", "value": 3},
        ]
        self.assertEqual(
            dedupe_by(records, "id"),
            [{"id": "a", "value": 1}, {"id": "b", "value": 3}],
        )


if __name__ == "__main__":
    unittest.main()
