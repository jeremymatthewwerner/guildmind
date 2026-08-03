import unittest

from routes import match_route


class RouteMatcherHiddenTests(unittest.TestCase):
    def test_literal_segments_must_match_exactly(self) -> None:
        self.assertIsNone(match_route("/teams/archive/:id", "/teams/archived/7"))

    def test_parameter_capture_preserves_percent_text(self) -> None:
        self.assertEqual(match_route("/files/:name", "/files/a%2Fb"), {"name": "a%2Fb"})

    def test_literal_percent_text_is_not_decoded(self) -> None:
        self.assertEqual(match_route("/files/%2F", "/files/%2F"), {})

    def test_root_route_ignores_all_empty_segments(self) -> None:
        self.assertEqual(match_route("/", "////"), {})

    def test_different_segment_counts_do_not_match(self) -> None:
        self.assertIsNone(match_route("/users/:id", "/users/7/history"))


if __name__ == "__main__":
    unittest.main()
