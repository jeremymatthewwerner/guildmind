import unittest

from versions import latest_versions


class LatestVersionsVisibleTests(unittest.TestCase):
    def test_numeric_components_and_first_seen_name_order(self) -> None:
        self.assertEqual(
            latest_versions(
                [
                    {"name": "core", "version": "1.9.0", "label": "old"},
                    {"name": "ui", "version": "2.0.0", "label": "ui"},
                    {"name": "core", "version": "1.10.0", "label": "new"},
                ]
            ),
            [
                {"name": "core", "version": "1.10.0", "label": "new"},
                {"name": "ui", "version": "2.0.0", "label": "ui"},
            ],
        )


if __name__ == "__main__":
    unittest.main()
