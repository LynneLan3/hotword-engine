from __future__ import annotations

import json
import unittest

import search_demand_providers as providers


class SearchDemandProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        providers.reset_searchapi_paid_circuit()

    def test_account_rate_limit_blocks_paid_search_without_search_call(self) -> None:
        requests = []

        def fetch(url, headers):
            requests.append(url)
            self.assertTrue(url.endswith("/api/v1/me"))
            return {
                "ok": True,
                "status": 200,
                "body": json.dumps({
                    "remaining_credits": 20,
                    "searches_this_hour": 100,
                    "hourly_rate_limit": 100,
                }),
                "url": url,
            }

        providers.reset_searchapi_paid_circuit()
        result = providers.probe_searchapi_google_organic(
            "Example Game", api_key="secret", fetch_fn=fetch
        )
        self.assertEqual(result["provider_state"], providers.PAID_PROVIDER_RATE_LIMITED)
        self.assertEqual(result["provider_reason"], "searchapi_hourly_rate_limit_reached")
        self.assertEqual(len(requests), 1)

    def test_first_searchapi_429_opens_run_circuit_for_serp_and_trends(self) -> None:
        requests = []

        def fetch(url, headers):
            requests.append(url)
            if url.endswith("/api/v1/me"):
                body = {"remaining_credits": 20, "searches_this_hour": 1, "hourly_rate_limit": 100}
                return {"ok": True, "status": 200, "body": json.dumps(body), "url": url}
            return {"ok": False, "status": 429, "body": "rate limited", "url": url, "headers": {"Retry-After": "3600"}}

        providers.reset_searchapi_paid_circuit()
        first = providers.probe_searchapi_google_organic("Example Game", api_key="secret", fetch_fn=fetch)
        second = providers.probe_searchapi_google_organic("Example Game guide", api_key="secret", fetch_fn=fetch)
        trends = providers.probe_searchapi_google_trends("Example Game", api_key="secret", fetch_fn=fetch)

        self.assertEqual(first["provider_state"], providers.PAID_PROVIDER_RATE_LIMITED)
        self.assertEqual(second["provider_state"], providers.PAID_PROVIDER_RATE_LIMITED)
        self.assertEqual(trends["provider_state"], providers.PAID_PROVIDER_RATE_LIMITED)
        self.assertEqual(len(requests), 2)  # /me plus the first paid search only.
        self.assertEqual(first["metadata"]["retry_after"], "3600")

    def test_google_trends_uses_searchapi_and_computes_relative_strength(self) -> None:
        requests = []
        payload = {
            "interest_over_time": {
                "averages": [{"query": "Example Game", "value": 50}],
                "timeline_data": [
                    {"values": [{"query": "Example Game", "extracted_value": 20}]},
                    {"values": [{"query": "Example Game", "extracted_value": 80}]},
                ],
            }
        }

        def fetch(url, headers):
            requests.append((url, headers))
            if url.endswith("/api/v1/me"):
                return {
                    "ok": True,
                    "status": 200,
                    "body": json.dumps({
                        "remaining_credits": 20,
                        "searches_this_hour": 1,
                        "hourly_rate_limit": 100,
                    }),
                    "url": url,
                }
            return {"ok": True, "status": 200, "body": json.dumps(payload), "url": url}

        result = providers.probe_searchapi_google_trends("Example Game", api_key="secret", fetch_fn=fetch)
        self.assertEqual(result["status"], providers.STATUS_SUPPORTED)
        self.assertEqual(result["items"][0]["strength"], "强")
        self.assertEqual(result["metadata"]["relative_growth"], 3.0)
        self.assertEqual(len(requests), 2)
        self.assertTrue(requests[0][0].endswith("/api/v1/me"))
        self.assertIn("engine=google_trends", requests[1][0])
        self.assertEqual(requests[1][1]["Authorization"], "Bearer secret")

    def test_google_trends_missing_key_never_fetches(self) -> None:
        result = providers.probe_searchapi_google_trends("Example Game", api_key="")
        self.assertEqual(result["status"], providers.STATUS_UNAVAILABLE)
        self.assertEqual(result["error"], "missing_searchapi_api_key")


if __name__ == "__main__":
    unittest.main()
