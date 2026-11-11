import unittest
from unittest.mock import Mock, patch

from applicant.services.research_feed import collect_feed


class FeedPreflightTest(unittest.TestCase):
    def test_url_keys_and_lists_fail_before_acquiring_resources(self):
        queries = [
            {'providers': ['otta'], 'urls': {'otta': []}},
            {'providers': ['builtin'], 'urls': {'arc': []}},
            {'providers': ['builtin'], 'urls': {'builtin': [False]}},
            {'providers': ['builtin'], 'urls': {'builtin': ['url'] * 26}},
        ]
        factory = Mock()
        with patch('applicant.services.research_feed.Acquisition') as acquisition:
            for query in queries:
                with self.subTest(query=query), self.assertRaises(ValueError):
                    collect_feed('unused', query, factory=factory)
            acquisition.assert_not_called()
            factory.assert_not_called()
