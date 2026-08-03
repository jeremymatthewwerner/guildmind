import unittest

from topology import topological_order


class TopologicalOrderVisibleTests(unittest.TestCase):
    def test_dependency_order_can_differ_from_global_sort(self) -> None:
        self.assertEqual(
            topological_order(
                ["compile", "deploy", "test"],
                [["compile", "test"], ["test", "deploy"]],
            ),
            ["compile", "test", "deploy"],
        )


if __name__ == "__main__":
    unittest.main()
