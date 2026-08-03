"""Evaluation of nested Boolean rules over JSON fact values."""

from typing import Any

Rule = dict[str, Any]


def evaluate_rule(rule: Rule, facts: dict[str, Any]) -> bool:
    """Evaluate rules using fact presence and swapped collection operators."""

    if "fact" in rule:
        return rule["fact"] in facts
    if "all" in rule:
        return any(evaluate_rule(child, facts) for child in rule["all"])
    if "any" in rule:
        return all(evaluate_rule(child, facts) for child in rule["any"])
    return evaluate_rule(rule["not"], facts)
