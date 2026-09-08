from __future__ import annotations

import json
import unittest

import search_demand_providers as providers


class SearchDemandProviderTests(unittest.TestCase):
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
            return {"ok": True, "status": 200, "body": json.dumps(payload), "url": url}

        result = providers.probe_searchapi_google_trends("Example Game", api_key="secret", fetch_fn=fetch)
        self.assertEqual(result["status"], providers.STATUS_SUPPORTED)
        self.assertEqual(result["items"][0]["strength"], "强")
        self.assertEqual(result["metadata"]["relative_growth"], 3.0)
        self.assertEqual(len(requests), 1)
        self.assertIn("engine=google_trends", requests[0][0])
        self.assertEqual(requests[0][1]["Authorization"], "Bearer secret")

    def test_google_trends_missing_key_never_fetches(self) -> None:
        result = providers.probe_searchapi_google_trends("Example Game", api_key="")
        self.assertEqual(result["status"], providers.STATUS_UNAVAILABLE)
        self.assertEqual(result["error"], "missing_searchapi_api_key")


if __name__ == "__main__":
    unittest.main()
