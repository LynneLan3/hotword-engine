"""Offline tests for the machine research executor."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import steam_candidate_machine_research_executor as executor
import steam_candidate_preflight as preflight


def _job() -> dict:
    return {
        "job_id": "steam-research-123456-20260826",
        "job_type": "STEAM_CANDIDATE_RESEARCH",
        "steam_app_id": "123456",
        "game_name": "Example Game",
        "steam_url": "https://store.steampowered.com/app/123456/",
        "research_cycle_date": "2026-08-26",
        "steam_signals": {"first_round_type": "🔥趋势"},
        "manual_signals": {"trends_result": "未检查", "keyword_opportunity": "未检查"},
        "serp_queries": ["Example Game"],
        "requested_checks": ["GAME_WIDE_SOCIAL", "GOOGLE_ORGANIC_SERP"],
        "created_at": "2026-08-26T01:00:00Z",
    }


class SteamCandidateMachineResearchExecutorTests(unittest.TestCase):
    def _write_job(self, root: Path) -> Path:
        path = root / "input" / "steam_candidate_research_job.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(_job()), encoding="utf-8")
        return path

    def test_manual_review_runs_full_callback_with_machine_fields(self) -> None:
        sent: list[dict] = []

        def social_fn(_job: dict) -> dict:
            return {
                "status": "AVAILABLE",
                "evidence_count": 5,
                "actionable_cluster_count": 1,
                "watch_cluster_count": 0,
                "top_clusters": [{"topic": "guide", "providers": ["reddit"]}],
            }

        def preflight_fn(_job: dict, **kwargs) -> dict:
            return {
                "preflight_verdict": preflight.MANUAL_REVIEW,
                "preflight_reason": "PASSED_AUTOMATIC_NOISE_AND_COMPETITION_FILTERS",
                "preflight_reason_text": "Passed filters",
                "checked_at": "2026-08-26T09:15:00+08:00",
                "autocomplete": {"status": "AVAILABLE", "guide_intent": True},
                "serp": {
                    "dedicated_guide_domains": [],
                    "brand_serp_guide_density": "LOW",
                    "guide_query_guide_density": "LOW",
                    "wiki_query_guide_density": "LOW",
                },
            }

        def research_fn(job: dict) -> dict:
            return {
                "job_id": job["job_id"],
                "job_type": "STEAM_CANDIDATE_RESEARCH",
                "steam_app_id": job["steam_app_id"],
                "game_name": job["game_name"],
                "steam_signals": job["steam_signals"],
                "manual_signals": job["manual_signals"],
                "social": {"status": "AVAILABLE", "evidence_count": 5, "actionable_cluster_count": 1, "top_clusters": []},
                "serp": {"status": "AVAILABLE", "organic_count": 8, "competition_summary": {"signals": ["LOW_GUIDE_DENSITY"]}},
                "research_status": "COMPLETED",
            }

        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ,
            {
                "STEAM_CANDIDATE_RESEARCH_API_URL": "https://sheet.example/exec",
                "STEAM_CANDIDATE_RESEARCH_CALLBACK_TOKEN": "callback-secret",
            },
        ):
            root = Path(tmp)
            outcome = executor.run_job(
                self._write_job(root),
                root=root,
                social_fn=social_fn,
                preflight_fn=preflight_fn,
                research_fn=research_fn,
                post_fn=lambda _url, body: (sent.append(body) or {"ok": True}),
            )

        self.assertTrue(outcome["ok"])
        self.assertEqual(outcome["preflight_verdict"], preflight.MANUAL_REVIEW)
        self.assertIn("recommendation", sent[0])
        self.assertEqual(sent[0]["machine_fields"]["keyword_opportunity"], "有")
        self.assertEqual(sent[0]["machine_recommendation"], "BUILD")

    def test_non_manual_review_delegates_to_preflight_callback(self) -> None:
        sent: list[dict] = []

        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ,
            {
                "STEAM_CANDIDATE_RESEARCH_API_URL": "https://sheet.example/exec",
                "STEAM_CANDIDATE_RESEARCH_CALLBACK_TOKEN": "callback-secret",
            },
        ):
            root = Path(tmp)
            outcome = executor.run_job(
                self._write_job(root),
                root=root,
                social_fn=lambda _job: {"status": "AVAILABLE", "evidence_count": 1, "top_clusters": []},
                preflight_fn=lambda _job, **kwargs: {
                    "preflight_verdict": preflight.WATCH,
                    "preflight_reason": "SEARCH_DEMAND_IMMATURE",
                    "checked_at": "2026-08-26T09:15:00+08:00",
                    "next_review_date": "2026-09-02",
                },
                post_fn=lambda _url, body: (sent.append(body) or {"ok": True}),
            )

        self.assertTrue(outcome["ok"])
        self.assertEqual(sent[0]["preflight_verdict"], "WATCH")
        self.assertNotIn("recommendation", sent[0])

    def test_default_machine_executor_reuses_social_and_preflight_serp(self) -> None:
        sent: list[dict] = []
        social_calls: list[str] = []
        serp_calls: list[str] = []

        def social_fn(_job: dict) -> dict:
            social_calls.append("social")
            return {
                "status": "AVAILABLE",
                "evidence_count": 2,
                "actionable_cluster_count": 1,
                "watch_cluster_count": 0,
                "top_clusters": [{
                    "topic": "Puzzle help",
                    "representative_questions": ["How puzzle 1?", "How puzzle 2?"],
                    "providers": ["reddit"],
                    "evidence_count": 2,
                    "evidence": [{"title": "Player question"}],
                }],
            }

        def serp_fn(query: str) -> dict:
            serp_calls.append(query)
            return {"status": "SUPPORTED", "items": [{
                "title": "Example Game puzzle answer",
                "domain": "youtube.com",
                "url": "https://youtube.com/example",
            }]}

        def preflight_fn(job: dict, **kwargs) -> dict:
            return preflight.run_preflight(
                job,
                autocomplete_fn=lambda _source, _query: {"status": "BEST_EFFORT", "items": []},
                serp_fn=serp_fn,
                **kwargs,
            )

        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ,
            {
                "STEAM_CANDIDATE_RESEARCH_API_URL": "https://sheet.example/exec",
                "STEAM_CANDIDATE_RESEARCH_CALLBACK_TOKEN": "callback-secret",
            },
        ):
            root = Path(tmp)
            outcome = executor.run_job(
                self._write_job(root),
                root=root,
                social_fn=social_fn,
                preflight_fn=preflight_fn,
                post_fn=lambda _url, body: (sent.append(body) or {"ok": True}),
            )
            artifact = json.loads(
                (root / "jobs" / _job()["job_id"] / "steam_candidate_research_result.json").read_text()
            )

        self.assertTrue(outcome["ok"])
        self.assertEqual(social_calls, ["social"])
        self.assertEqual(serp_calls, ["Example Game", "How puzzle 1?", "How puzzle 2?"])
        self.assertEqual(len(artifact["launch_topics"]), 2)
        self.assertTrue(artifact["serp"]["provider_metadata"]["reused_preflight"])
        self.assertIn("recommendation", sent[0])


if __name__ == "__main__":
    unittest.main()
