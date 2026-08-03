import unittest

from topology import topological_order


class TopologicalOrderHiddenTests(unittest.TestCase):
    def test_empty_graph_has_an_empty_order(self) -> None:
        self.assertEqual(topological_order([], []), [])

    def test_disconnected_nodes_are_lexically_ordered(self) -> None:
        self.assertEqual(topological_order(["z", "a", "m"], []), ["a", "m", "z"])

    def test_diamond_uses_lexical_ready_queue_ties(self) -> None:
        self.assertEqual(
            topological_order(
                ["release", "lint", "build", "test"],
                [
                    ["build", "lint"],
                    ["build", "test"],
                    ["lint", "release"],
                    ["test", "release"],
                ],
            ),
            ["build", "lint", "test", "release"],
        )

    def test_newly_ready_node_reenters_the_lexical_queue(self) -> None:
        self.assertEqual(
            topological_order(["c", "b", "a"], [["b", "a"]]),
            ["b", "a", "c"],
        )

    def test_cycle_returns_none_instead_of_a_partial_order(self) -> None:
        self.assertIsNone(topological_order(["a", "b", "c"], [["a", "b"], ["b", "a"]]))
