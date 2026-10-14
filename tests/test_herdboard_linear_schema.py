import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import graphql

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins/herdboard/src"))

from herdboard import linear  # noqa: E402
from linear_fake import FakeLinear, linear_schema, validation_errors  # noqa: E402


class SchemaTests(unittest.TestCase):
    def test_schema_loads(self):
        self.assertIn("Issue", linear_schema().type_map)

    def test_fake_loads_schema_before_requests(self):
        with patch("linear_fake.linear_schema", wraps=linear_schema) as load_schema:
            with FakeLinear({}) as fake:
                load_schema.assert_called_once_with()
                self.assertEqual(fake.requests, [])

    def test_queries_are_registered(self):
        # Every module constant that holds a query is in the registry.
        constants = {name for name in dir(linear) if name.endswith("_QUERY")}
        self.assertEqual(constants, set(linear.QUERIES))
        self.assertGreaterEqual(len(constants), 12)

    def test_every_query_is_valid(self):
        for name, query in linear.QUERIES.items():
            with self.subTest(query=name):
                errors = graphql.validate(linear_schema(), graphql.parse(query))
                self.assertEqual([e.message for e in errors], [])

    def test_validation_catches_an_invalid_query(self):
        query = "query Viewer { viewer { id noSuchField } }"
        self.assertTrue(any("noSuchField" in m for m in validation_errors(query, {})))

    def test_filters_match_the_schema(self):
        filters = [linear.recent_filter(14), linear.recent_filter(0),
                   {"project": {"id": {"eq": "p"}}, "labels": {"some": {"id": {"in": ["l"]}}},
                    **linear.recent_filter(7)}]
        for filt in filters:
            with self.subTest(filter=filt):
                self.assertEqual(validation_errors(linear.ISSUES_QUERY, {"first": 10, "filter": filt}), [])
        self.assertEqual(validation_errors(linear.LABELS_BY_ID_QUERY,
                                           {"first": 10, "filter": {"id": {"in": ["a"]}}}), [])
        self.assertEqual(validation_errors(linear.ISSUE_QUERY, {"id": "ENG-1"}), [])

    def test_validation_catches_a_bad_filter(self):
        bad = {"first": 10, "filter": {"completedAt": {"gte": 5, "bogus": True}}}
        self.assertNotEqual(validation_errors(linear.ISSUES_QUERY, bad), [])

    def test_invalid_query_gets_a_graphql_error_from_the_fake(self):
        from linear_fake import FakeLinear
        with FakeLinear({}) as fake:
            client = linear.LinearClient("test-key", endpoint=fake.endpoint)
            with self.assertRaisesRegex(linear.LinearError, "noSuchField"):
                client._request("Viewer", "query Viewer { viewer { noSuchField } }")


if __name__ == "__main__":
    unittest.main()
