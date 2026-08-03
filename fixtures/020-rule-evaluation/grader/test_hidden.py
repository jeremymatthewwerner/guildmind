import unittest

from rules import evaluate_rule


class RuleEvaluationHiddenTests(unittest.TestCase):
    def test_present_falsy_fact_is_false(self) -> None:
        self.assertFalse(evaluate_rule({"fact": "count"}, {"count": 0}))

    def test_negated_missing_fact_is_true(self) -> None:
        self.assertTrue(evaluate_rule({"not": {"fact": "missing"}}, {}))

    def test_empty_all_uses_true_identity(self) -> None:
        self.assertTrue(evaluate_rule({"all": []}, {}))

    def test_empty_any_uses_false_identity(self) -> None:
        self.assertFalse(evaluate_rule({"any": []}, {}))

    def test_nested_operators_and_missing_branch_are_order_independent(self) -> None:
        rule = {
            "any": [
                {"all": [{"fact": "a"}, {"not": {"fact": "b"}}]},
                {"fact": "c"},
            ]
        }
        reverse = {"any": list(reversed(rule["any"]))}
        facts = {"a": True, "b": False}
        self.assertTrue(evaluate_rule(rule, facts))
        self.assertTrue(evaluate_rule(reverse, facts))


if __name__ == "__main__":
    unittest.main()
