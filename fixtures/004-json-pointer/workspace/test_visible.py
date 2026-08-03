import unittest

from pointer import resolve_pointer


class PointerVisibleTests(unittest.TestCase):
    def test_selects_a_top_level_object_member(self) -> None:
        self.assertEqual(resolve_pointer({"name": "guildmind"}, "/name"), "guildmind")


if __name__ == "__main__":
    unittest.main()
