import unittest

from routes import match_route


class RouteMatcherVisibleTests(unittest.TestCase):
    def test_repeated_separators_are_ignored(self) -> None:
        self.assertEqual(
            match_route("/teams/:team/items/:item", "//teams//red///items/42//"),
            {"team": "red", "item": "42"},
        )


if __name__ == "__main__":
    unittest.main()
