import io
import unittest
from collections import defaultdict
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import mock_open, patch

import analyze
from analyze import add_session_usage, fetch_copilot_billing_usage, parse_iso8601


class FakeClient:
    def __init__(self, reports):
        self.reports = reports
        self.endpoints = []

    def call_api(self, endpoint):
        self.endpoints.append(endpoint)
        month = int(endpoint.split("month=")[1])
        return {"usageItems": self.reports[month]}


class FetchCopilotBillingUsageTests(unittest.TestCase):
    def test_filters_products_and_dates_and_preserves_repository(self):
        client = FakeClient(
            {
                10: [
                    {
                        "date": "2026-10-31",
                        "product": "Copilot",
                        "sku": "Premium requests",
                        "netAmount": 1.25,
                        "repositoryName": "owner/repo",
                    },
                    {
                        "date": "2026-10-31",
                        "product": "Actions",
                        "sku": "Linux",
                        "netAmount": 99,
                        "repositoryName": "owner/repo",
                    },
                ],
                11: [
                    {
                        "date": "2026-11-01",
                        "product": "GitHub Copilot",
                        "sku": "Chat",
                        "netAmount": 2.5,
                    },
                    {
                        "date": "2026-11-02",
                        "product": "Copilot",
                        "sku": "Premium requests",
                        "netAmount": 7,
                        "repositoryName": "owner/repo",
                    },
                ],
            }
        )

        costs = fetch_copilot_billing_usage(
            client,
            "an-org",
            parse_iso8601("2026-10-31T12:00:00Z"),
            parse_iso8601("2026-11-02T00:00:00Z"),
        )

        self.assertEqual(costs, {"owner/repo": 1.25, None: 2.5})
        self.assertEqual(len(client.endpoints), 2)
        self.assertTrue(all("/organizations/an-org/" in e for e in client.endpoints))

    def test_empty_window_does_not_query_api(self):
        client = FakeClient({})

        costs = fetch_copilot_billing_usage(
            client,
            "an-org",
            parse_iso8601("2026-11-01T00:00:00Z"),
            parse_iso8601("2026-11-01T00:00:00Z"),
        )

        self.assertEqual(costs, {})
        self.assertEqual(client.endpoints, [])


class AddSessionUsageTests(unittest.TestCase):
    def test_accumulates_premium_requests_only_inside_window(self):
        repo_stats = defaultdict(
            lambda: {
                "unfiltered_credits": 0.0,
                "filtered_credits": 0.0,
                "premium_requests": 0.0,
            }
        )

        add_session_usage(
            repo_stats, "owner/repo", {"type": "premium_requests", "amount": 3}, True
        )
        add_session_usage(
            repo_stats, "owner/repo", {"type": "premium_requests", "amount": 5}, False
        )

        self.assertEqual(repo_stats["owner/repo"]["premium_requests"], 3)


class MainTests(unittest.TestCase):
    def test_reports_billing_usage_for_a_customer_without_agent_credits(self):
        class FakeClient:
            def call_api(self, endpoint):
                if endpoint == "/agents/tasks?per_page=1000":
                    return {"tasks": [{"id": "task1"}]}
                if endpoint == "/agents/tasks/task1":
                    return {"repository": {"id": 42}, "sessions": []}
                return {
                    "usageItems": [
                        {
                            "date": "2026-10-31",
                            "product": "Copilot",
                            "sku": "Chat",
                            "netAmount": 1.25,
                            "repositoryName": "owner/repo",
                        }
                    ]
                }

            def resolve_repo_name(self, repo_id):
                return "owner/repo"

        args = SimpleNamespace(
            copilot_bill=None,
            copilot_credits=10,
            start="2026-10-31T00:00:00Z",
            end="2026-11-01T00:00:00Z",
            cache_dir=None,
            customer_map="customer-map.json",
            organization="an-org",
        )
        output = io.StringIO()
        with (
            patch.object(analyze.argparse.ArgumentParser, "parse_args", return_value=args),
            patch.object(analyze, "CachedGitHubClient", return_value=FakeClient()),
            patch("builtins.open", mock_open(read_data='{"owner/repo": "Customer"}')),
            redirect_stdout(output),
        ):
            analyze.main()

        self.assertIn("Customer: Customer", output.getvalue())
        self.assertIn("Organization Copilot Billing Usage", output.getvalue())
        self.assertIn("owner/repo", output.getvalue())
        self.assertIn("$  1.25 (organization billing usage)", output.getvalue())


if __name__ == "__main__":
    unittest.main()
