import unittest

from rules import evaluate_rule


class RuleEvaluationVisibleTests(unittest.TestCase):
    def test_all_and_not_are_applied_to_truthy_fact_values(self) -> None:
        self.assertFalse(
            evaluate_rule(
                {
                    "all": [
                        {"fact": "enabled"},
                        {"not": {"fact": "suspended"}},
                    ]
                },
                {"enabled": True, "suspended": True},
            )
        )


if __name__ == "__main__":
    unittest.main()
