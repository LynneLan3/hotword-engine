"""Offline M7A Steam Candidate Research loader/runner tests."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import fetch_pending_steam_candidate_research_jobs as fetcher
import steam_candidate_research_runner as runner


def _job(**overrides) -> dict:
    job = {
        "job_id": "steam-research-4026250-20260823",
        "job_type": "STEAM_CANDIDATE_RESEARCH",
        "steam_app_id": "4026250",
        "game_name": "Project P.I.T.T.",
        "steam_url": "https://store.steampowered.com/app/4026250/",
        "research_cycle_date": "2026-08-23",
        "steam_signals": {
            "first_round_type": "🔥 趋势候选",
            "followers": 4200,
            "followers_gain_7d": 1100,
        },
        "manual_signals": {"trends_result": "未检查", "keyword_opportunity": "未检查"},
        "serp_queries": ["Project P.I.T.T."],
        "requested_checks": ["GAME_WIDE_SOCIAL", "GOOGLE_ORGANIC_SERP"],
        "created_at": "2026-08-23T01:00:00Z",
    }
    job.update(overrides)
    return job


def _serp_items(count: int) -> list[dict]:
    return [
        {
            "position": i,
            "title": f"Project P.I.T.T. Guide {i}",
            "domain": "pitt-guides.example.com" if i > 2 else "youtube.com",
            "url": f"https://pitt-guides.example.com/{i}",
            "snippet": "Project P.I.T.T. walkthrough and tips",
        }
        for i in range(1, count + 1)
    ]


def _social_result():
    return {
        "evidence_count": 3,
        "source_failures": {},
        "clusters": [
            {
                "topic_key": "pitt-guide",
                "topic": "How do I solve the puzzle?",
                "intent_family": "GUIDE",
                "intent": "TASK",
                "representative_questions": ["How do I solve the puzzle?"],
                "evidence_count": 3,
                "source_families": ["COMMUNITY", "VIDEO"],
                "providers": ["reddit", "youtube"],
                "freshness_score": 4,
                "engagement_score": 7,
                "task_intent_score": 80,
                "decision": "NEW",
                "reason": "internal social classification",
                "evidence": [{"provider": "reddit", "title": "Puzzle help"}],
            },
            {
                "topic_key": "pitt-build",
                "topic": "Best build",
                "intent_family": "BUILD",
                "intent": "GENERIC",
                "representative_questions": [],
                "evidence_count": 1,
                "source_families": ["COMMUNITY"],
                "providers": ["reddit"],
                "freshness_score": 1,
                "engagement_score": 1,
                "task_intent_score": 10,
                "decision": "WATCH",
                "reason": "internal social classification",
                "evidence": [],
            },
        ],
    }


class ContractAndFetcherTests(unittest.TestCase):
    def test_contract_validation_and_injected_fetch(self) -> None:
        with self.assertRaises(SystemExit):
            fetcher.to_steam_candidate_research_job({"job_type": "OTHER"})

        calls = []

        def injected(url: str):
            calls.append(url)
            return {"jobs": [_job(), {**_job(), "job_id": "second"}]}

        payload = fetcher.fetch_pending_steam_candidate_research_jobs(
            "https://example.test/pending",
            fetch_fn=injected,
        )
        self.assertEqual(calls, ["https://example.test/pending"])
        with tempfile.TemporaryDirectory() as tmp:
            count, selected = fetcher.save_pending_steam_candidate_research_jobs(
                payload, root=Path(tmp)
            )
            self.assertEqual(count, 2)
            self.assertEqual(selected["job_id"], _job()["job_id"])
            self.assertEqual(
                json.loads((Path(tmp) / "input/steam_candidate_research_job.json").read_text())["job_id"],
                _job()["job_id"],
            )


class RunnerTests(unittest.TestCase):
    def _run(self, *, social=None, serp=None, count=7, root=None):
        social = social or _social_result()
        serp = serp or {"status": "SUPPORTED", "items": _serp_items(count), "metadata": {"http_status": 200}}
        social_patch = {"side_effect": social} if isinstance(social, BaseException) else {"return_value": social}
        with mock.patch.object(runner.gws, "run_game_wide_social_discovery", **social_patch) as social_call, \
             mock.patch.object(runner.sdp, "probe_searchapi_google_organic", return_value=serp) as serp_call:
            result = runner.run_candidate_research(_job())
        return result, social_call, serp_call

    def test_both_success_uses_social_and_m3_serp_summary(self) -> None:
        result, social_call, serp_call = self._run(count=7)
        self.assertEqual(result["research_status"], "COMPLETED")
        self.assertEqual(result["social"]["evidence_count"], 3)
        self.assertEqual(result["social"]["actionable_cluster_count"], 1)
        self.assertEqual(result["social"]["watch_cluster_count"], 1)
        self.assertEqual(result["serp"]["organic_count"], 7)
        self.assertEqual(result["serp"]["competition_summary"]["organic_count"], 7)
        self.assertEqual(len(result["serp"]["competition_summary"]["result_classifications"]), 7)
        social_job = social_call.call_args.args[0]
        self.assertEqual(social_job["site_key"], "steam:4026250")
        self.assertEqual(social_job["aliases"], [])
        self.assertEqual(social_job["existing_pages"], [])
        self.assertEqual(social_job["gsc_queries"], [])
        self.assertEqual(serp_call.call_args.kwargs["hl"], "en")
        self.assertEqual(serp_call.call_args.kwargs["gl"], "us")
        self.assertEqual(serp_call.call_args.kwargs["device"], "desktop")
        self.assertEqual(serp_call.call_args.args[0], "Project P.I.T.T.")
        self.assertNotIn("decision", json.dumps(result))
        self.assertNotRegex(json.dumps(result), r"BUILD|REJECT|RECOMMEND_ADVANCE|RECOMMEND_WATCH|RECOMMEND_REJECT")

    def test_organic_ten_results_and_artifacts(self) -> None:
        result, _social_call, _serp_call = self._run(count=10)
        self.assertEqual(result["serp"]["organic_count"], 10)
        with tempfile.TemporaryDirectory() as tmp:
            job_path = Path(tmp) / "input/job.json"
            job_path.parent.mkdir(parents=True)
            job_path.write_text(json.dumps(_job()), encoding="utf-8")
            with mock.patch.object(runner.gws, "run_game_wide_social_discovery", return_value=_social_result()), \
                 mock.patch.object(runner.sdp, "probe_searchapi_google_organic", return_value={"status": "SUPPORTED", "items": _serp_items(10), "metadata": {}}):
                outcome = runner.run_job(job_path, root=Path(tmp))
            job_dir = Path(tmp) / "jobs" / _job()["job_id"]
            self.assertTrue(outcome["ok"])
            self.assertTrue((job_dir / "job.json").exists())
            self.assertTrue((job_dir / "steam_candidate_research_result.json").exists())
            self.assertTrue((job_dir / "status.json").exists())

    def test_social_failure_serp_success(self) -> None:
        result, _social_call, _serp_call = self._run(social=RuntimeError("social unavailable"), count=7)
        self.assertEqual(result["research_status"], "COMPLETED")
        self.assertEqual(result["social"]["status"], "UNAVAILABLE")
        self.assertEqual(result["serp"]["status"], "AVAILABLE")

    def test_serp_failure_social_success(self) -> None:
        result, _social_call, _serp_call = self._run(serp={"status": "UNAVAILABLE", "error": "credit"})
        self.assertEqual(result["research_status"], "COMPLETED")
        self.assertEqual(result["social"]["status"], "AVAILABLE")
        self.assertEqual(result["serp"]["status"], "UNAVAILABLE")

    def test_both_fail_is_failed(self) -> None:
        result, _social_call, _serp_call = self._run(
            social=RuntimeError("social unavailable"),
            serp={"status": "UNAVAILABLE", "error": "searchapi unavailable"},
        )
        self.assertEqual(result["research_status"], "FAILED")

    def test_missing_searchapi_key_is_unavailable_without_crash(self) -> None:
        with mock.patch.object(runner.gws, "run_game_wide_social_discovery", return_value=_social_result()), \
             mock.patch.dict(os.environ, {}, clear=True):
            result = runner.run_candidate_research(_job(), fetch_fn=lambda *_args, **_kwargs: self.fail("no SearchApi request without key"))
        self.assertEqual(result["research_status"], "COMPLETED")
        self.assertEqual(result["serp"]["status"], "UNAVAILABLE")
        self.assertEqual(result["serp"]["error"], "missing_searchapi_api_key")


if __name__ == "__main__":
    unittest.main()
