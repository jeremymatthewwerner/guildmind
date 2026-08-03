import unittest

from transactions import summarize_transactions


class TransactionSummaryHiddenTests(unittest.TestCase):
    def test_rows_without_categories_are_ignored(self) -> None:
        self.assertEqual(
            summarize_transactions(
                [
                    {"kind": "sale", "amount": 99},
                    {"category": "food", "kind": "sale", "amount": 5},
                ]
            ),
            [{"category": "food", "net": 5}],
        )

    def test_zero_amount_introduces_a_category(self) -> None:
        self.assertEqual(
            summarize_transactions([{"category": "free", "kind": "sale", "amount": 0}]),
            [{"category": "free", "net": 0}],
        )

    def test_cancellation_retains_the_zero_net_category(self) -> None:
        self.assertEqual(
            summarize_transactions(
                [
                    {"category": "fees", "kind": "sale", "amount": 3},
                    {"category": "fees", "kind": "refund", "amount": 3},
                ]
            ),
            [{"category": "fees", "net": 0}],
        )

    def test_nonalphabetic_first_seen_order_is_stable(self) -> None:
        self.assertEqual(
            summarize_transactions(
                [
                    {"category": "zeta", "kind": "sale", "amount": 1},
                    {"category": "alpha", "kind": "sale", "amount": 2},
                ]
            ),
            [
                {"category": "zeta", "net": 1},
                {"category": "alpha", "net": 2},
            ],
        )

    def test_refund_only_category_has_negative_net(self) -> None:
        self.assertEqual(
            summarize_transactions([{"category": "returns", "kind": "refund", "amount": 7}]),
            [{"category": "returns", "net": -7}],
        )


if __name__ == "__main__":
    unittest.main()
