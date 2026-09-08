from __future__ import annotations

import unittest
import urllib.error
from unittest import mock

import game_wide_social_runner as gws
import research_runner as rr


def _job() -> dict:
    return {
        "job_id": "g040-reddit-breaker",
        "job_type": "GAME_WIDE_SOCIAL_DISCOVERY",
        "site_key": "steam:123",
        "game_name": "Example Game",
        "aliases": [],
        "lookback_hours": 48,
        "providers": ["reddit", "steam", "youtube"],
        "gsc_queries": [],
        "existing_pages": [],
        "recent_interventions": [],
        "created_at": "2026-09-08T00:00:00Z",
    }


class RedditFailSoftTests(unittest.TestCase):
    def test_reddit_429_aborts_without_retry_or_backoff(self) -> None:
        error = urllib.error.HTTPError("https://reddit.example", 429, "rate limited", {}, None)
        with mock.patch.object(rr.urllib.request, "urlopen", side_effect=error) as urlopen, \
             mock.patch.object(rr.time, "sleep") as sleep:
            with self.assertRaisesRegex(rr.RedditRateLimitedError, "reddit_http_429"):
                rr.collect_reddit("Example Game", "Example Game")
        self.assertEqual(urlopen.call_count, 1)
        sleep.assert_not_called()

    def test_daily_reddit_circuit_skips_later_candidates_but_keeps_other_free_providers(self) -> None:
        gws.reset_daily_provider_circuit_breaker()
        calls: list[str] = []

        def collect(provider: str, _seed: str, _job: dict, _appids: list[str]):
            calls.append(provider)
            if provider == "reddit":
                raise rr.RedditRateLimitedError("reddit_http_429")
            return [], 0

        try:
            with mock.patch.object(gws, "collect_provider_for_seed", side_effect=collect):
                first = gws.run_game_wide_social_discovery(_job())
                second = gws.run_game_wide_social_discovery({**_job(), "job_id": "g040-reddit-breaker-2"})
        finally:
            gws.reset_daily_provider_circuit_breaker()

        self.assertEqual(calls, ["reddit", "steam", "youtube", "steam", "youtube"])
        for result in (first, second):
            self.assertEqual(result["source_states"]["reddit"], "RATE_LIMITED")
            self.assertEqual(result["source_failures"]["reddit"], "reddit_http_429")
            self.assertEqual(result["source_counts"]["steam"], 0)
            self.assertEqual(result["source_counts"]["youtube"], 0)


if __name__ == "__main__":
    unittest.main()
