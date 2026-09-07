import json
import os
import unittest
from datetime import datetime, timezone

from api.intent_gap import build_intent_gap_response
from unittest import mock


class IntentGapTests(unittest.TestCase):
    def test_contract_and_local_gap(self) -> None:
        def fake_fetch(url, headers=None, timeout=12):
            return {"status": 200, "ok": True, "url": url, "body": json.dumps({
                "organic_results": [{
                    "position": 1, "title": "How to arrest Michael guide", "link": "https://competitor.example/arrest-michael",
                    "snippet": "Arrest Michael walkthrough",
                }]
            })}

        with mock.patch.dict(os.environ, {"SEARCHAPI_API_KEY": "test-key"}):
            result = build_intent_gap_response({
                "game": "Halloween The Game",
                "lifecycle": "LAUNCH",
                "localOwnedUrls": ["https://local.example/"],
                "playerTasks": ["Play with offline bots"],
                "intents": [{"clusterKey": "ARREST_MICHAEL", "playerTask": "Arrest / detain Michael", "queries": ["how to arrest michael"]}],
            }, fetch_fn=fake_fetch, now=datetime(2026, 9, 7, tzinfo=timezone.utc))
        self.assertEqual(result["status"], "ACTIVE")
        self.assertEqual(result["signals"][0]["clusterKey"], "ARREST_MICHAEL")
        self.assertTrue(result["signals"][0]["localCoverageGap"])
        self.assertEqual(result["signals"][0]["evidence"][0]["source"], "SEARCHAPI_GOOGLE_ORGANIC")


if __name__ == "__main__":
    unittest.main()
