import unittest

from transactions import summarize_transactions


class TransactionSummaryVisibleTests(unittest.TestCase):
    def test_refunds_are_signed_and_categories_keep_first_seen_order(self) -> None:
        self.assertEqual(
            summarize_transactions(
                [
                    {"category": "tools", "kind": "sale", "amount": 9},
                    {"category": "books", "kind": "refund", "amount": 2},
                    {"category": "tools", "kind": "refund", "amount": 4},
                ]
            ),
            [
                {"category": "tools", "net": 5},
                {"category": "books", "net": -2},
            ],
        )


if __name__ == "__main__":
    unittest.main()
