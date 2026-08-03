import unittest

from versions import latest_versions


class LatestVersionsHiddenTests(unittest.TestCase):
    def test_release_outranks_prerelease(self) -> None:
        self.assertEqual(
            latest_versions(
                [
                    {"name": "api", "version": "1.0.0-rc.1", "label": "candidate"},
                    {"name": "api", "version": "1.0.0", "label": "release"},
                    {"name": "api", "version": "1.0.0-rc.10", "label": "later-candidate"},
                ]
            ),
            [{"name": "api", "version": "1.0.0", "label": "release"}],
        )

    def test_prerelease_identifiers_follow_semver_order(self) -> None:
        self.assertEqual(
            latest_versions(
                [
                    {"name": "numeric", "version": "1.0.0-alpha.2"},
                    {"name": "kind", "version": "1.0.0-alpha.10", "label": "numeric"},
                    {"name": "numeric", "version": "1.0.0-alpha.10"},
                    {"name": "kind", "version": "1.0.0-alpha.beta", "label": "text"},
                ]
            ),
            [
                {"name": "numeric", "version": "1.0.0-alpha.10"},
                {"name": "kind", "version": "1.0.0-alpha.beta", "label": "text"},
            ],
        )

    def test_longer_equal_prefix_prerelease_is_later(self) -> None:
        self.assertEqual(
            latest_versions(
                [
                    {"name": "worker", "version": "3.2.1-alpha", "label": "short"},
                    {"name": "worker", "version": "3.2.1-alpha.1", "label": "long"},
                ]
            ),
            [{"name": "worker", "version": "3.2.1-alpha.1", "label": "long"}],
        )

    def test_build_metadata_tie_keeps_first_record(self) -> None:
        self.assertEqual(
            latest_versions(
                [
                    {"name": "db", "version": "2.1.3+aaa", "label": "first"},
                    {"name": "db", "version": "2.1.3+zzz", "label": "second"},
                ]
            ),
            [{"name": "db", "version": "2.1.3+aaa", "label": "first"}],
        )

    def test_empty_input_returns_empty_list(self) -> None:
        self.assertEqual(latest_versions([]), [])


if __name__ == "__main__":
    unittest.main()
