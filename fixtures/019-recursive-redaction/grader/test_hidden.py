import unittest

from redaction import redact_keys


class RecursiveRedactionHiddenTests(unittest.TestCase):
    def test_array_objects_and_case_sensitive_keys(self) -> None:
        self.assertEqual(
            redact_keys(
                [
                    {"password": "x", "child": {"password": "y", "Password": "keep"}},
                    {"ok": [{"password": "z"}]},
                ],
                ["password"],
            ),
            [{"child": {"Password": "keep"}}, {"ok": [{}]}],
        )

    def test_empty_containers_are_preserved(self) -> None:
        self.assertEqual(
            redact_keys(
                {"empty_map": {}, "empty_list": [], "drop": {}, "nested": [{}, []]},
                ["drop"],
            ),
            {"empty_map": {}, "empty_list": [], "nested": [{}, []]},
        )

    def test_scalar_root_is_unchanged(self) -> None:
        self.assertEqual(redact_keys(0, ["ignored"]), 0)

    def test_scalar_values_equal_to_blocked_keys_are_unchanged(self) -> None:
        self.assertEqual(
            redact_keys(
                {"keep": "secret", "items": ["secret", {"keep": "secret"}]},
                ["secret"],
            ),
            {"keep": "secret", "items": ["secret", {"keep": "secret"}]},
        )

    def test_empty_blocked_list_changes_nothing(self) -> None:
        self.assertEqual(
            redact_keys({"a": None, "b": False, "c": [0, "", {"d": 1}]}, []),
            {"a": None, "b": False, "c": [0, "", {"d": 1}]},
        )


if __name__ == "__main__":
    unittest.main()
