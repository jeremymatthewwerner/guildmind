import unittest

from changes import apply_changes


class OrderedChangesVisibleTests(unittest.TestCase):
    def test_sets_a_nested_object_member(self) -> None:
        self.assertEqual(
            apply_changes(
                {"profile": {"name": "old", "active": True}},
                [{"op": "set", "path": ["profile", "name"], "value": "new"}],
            ),
            {"profile": {"name": "new", "active": True}},
        )


if __name__ == "__main__":
    unittest.main()
