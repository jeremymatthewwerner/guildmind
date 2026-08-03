import unittest

from redaction import redact_keys


class RecursiveRedactionVisibleTests(unittest.TestCase):
    def test_blocked_keys_are_removed_below_objects_and_arrays(self) -> None:
        self.assertEqual(
            redact_keys(
                {
                    "token": "outer",
                    "profile": {"token": "inner", "name": "Ada"},
                    "items": [{"secret": 1, "keep": 2}, "secret"],
                },
                ["token", "secret"],
            ),
            {
                "profile": {"name": "Ada"},
                "items": [{"keep": 2}, "secret"],
            },
        )


if __name__ == "__main__":
    unittest.main()
