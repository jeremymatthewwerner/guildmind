import unittest

from inventory import inventory_delta


class InventoryDeltaVisibleTests(unittest.TestCase):
    def test_duplicate_count_changes_are_reported(self) -> None:
        self.assertEqual(
            inventory_delta(
                ["bolt", "bolt", "nut"],
                ["bolt", "nut", "nut", "screw"],
            ),
            {
                "added": [
                    {"item": "nut", "count": 1},
                    {"item": "screw", "count": 1},
                ],
                "removed": [{"item": "bolt", "count": 1}],
            },
        )


if __name__ == "__main__":
    unittest.main()
