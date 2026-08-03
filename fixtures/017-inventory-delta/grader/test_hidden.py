import unittest

from inventory import inventory_delta


class InventoryDeltaHiddenTests(unittest.TestCase):
    def test_multiple_removals_of_one_item_are_counted(self) -> None:
        self.assertEqual(
            inventory_delta(["a", "a", "a", "b"], ["a", "b"]),
            {"added": [], "removed": [{"item": "a", "count": 2}]},
        )

    def test_multiple_additions_keep_after_first_seen_order(self) -> None:
        self.assertEqual(
            inventory_delta([], ["x", "x", "y"]),
            {
                "added": [
                    {"item": "x", "count": 2},
                    {"item": "y", "count": 1},
                ],
                "removed": [],
            },
        )

    def test_reordering_an_unchanged_multiset_has_no_delta(self) -> None:
        self.assertEqual(
            inventory_delta(["a", "b", "a"], ["b", "a", "a"]),
            {"added": [], "removed": []},
        )

    def test_added_and_removed_entries_use_their_respective_input_order(self) -> None:
        self.assertEqual(
            inventory_delta(
                ["old", "old", "keep", "gone"],
                ["new", "keep", "new", "old", "alpha"],
            ),
            {
                "added": [
                    {"item": "new", "count": 2},
                    {"item": "alpha", "count": 1},
                ],
                "removed": [
                    {"item": "old", "count": 1},
                    {"item": "gone", "count": 1},
                ],
            },
        )

    def test_empty_inventories_keep_both_output_keys(self) -> None:
        self.assertEqual(inventory_delta([], []), {"added": [], "removed": []})


if __name__ == "__main__":
    unittest.main()
