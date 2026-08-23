"""Offline M7C wrapper tests: M7A -> M7B -> Steam callback."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import fetch_pending_steam_candidate_research_jobs as fetcher
import steam_candidate_research_job_runner as wrapper


def _job() -> dict:
    return {
        "job_id": "steam-research-123456-20260823",
        "job_type": "STEAM_CANDIDATE_RESEARCH",
        "steam_app_id": "123456",
        "game_name": "Example Game",
        "steam_url": "https://store.steampowered.com/app/123456/",
        "research_cycle_date": "2026-08-23",
        "steam_signals": {"first_round_type": "🔥趋势", "first_round_priority": "P1 高"},
        "manual_signals": {"trends_result": "", "keyword_opportunity": ""},
        "serp_queries": ["Example Game"],
        "requested_checks": ["GAME_WIDE_SOCIAL", "GOOGLE_ORGANIC_SERP"],
        "created_at": "2026-08-23T01:00:00Z",
    }


def _result(status: str = "COMPLETED") -> dict:
    result = {
        "job_id": _job()["job_id"],
        "job_type": "STEAM_CANDIDATE_RESEARCH",
        "steam_app_id": "123456",
        "game_name": "Example Game",
        "steam_signals": _job()["steam_signals"],
        "manual_signals": _job()["manual_signals"],
        "social": {
            "status": "AVAILABLE",
            "evidence_count": 3,
            "cluster_count": 2,
            "actionable_cluster_count": 1,
            "watch_cluster_count": 1,
            "top_clusters": [{"topic": "automation", "evidence": [{"url": "secret"}]}],
        },
        "serp": {
            "status": "AVAILABLE",
            "query": "Example Game",
            "organic_count": 7,
            "competition_summary": {"signals": ["LOW_GUIDE_DENSITY", "HIGH_VIDEO_UGC_PRESENCE"]},
        },
        "research_status": status,
        "generated_at": "2026-08-23T01:05:00Z",
    }
    if status == "FAILED":
        result["social"] = {"status": "UNAVAILABLE", "error": "social unavailable"}
        result["serp"] = {"status": "UNAVAILABLE", "error": "searchapi unavailable"}
    return result


class SteamCandidateResearchJobRunnerTests(unittest.TestCase):
    def _write_job(self, root: Path) -> Path:
        path = root / "input" / "steam_candidate_research_job.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(_job(), ensure_ascii=False), encoding="utf-8")
        return path

    def test_completed_m7a_to_m7b_to_callback_and_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job_path = self._write_job(root)
            sent: list[tuple[str, dict]] = []

            def post_fn(url: str, body: dict) -> dict:
                sent.append((url, body))
                return {"ok": True}

            with mock.patch.dict(
                os.environ,
                {
                    "STEAM_CANDIDATE_RESEARCH_API_URL": "https://steam.example/exec",
                    "STEAM_CANDIDATE_RESEARCH_CALLBACK_TOKEN": "secret-token",
                },
            ), mock.patch.object(wrapper.m7a, "run_candidate_research", return_value=_result()):
                outcome = wrapper.run_job(job_path, root=root, post_fn=post_fn, fetch_fn=lambda *_: self.fail("provider call"))

            job_id = _job()["job_id"]
            job_dir = root / "jobs" / job_id
            self.assertTrue(outcome["ok"])
            self.assertFalse(outcome["reused_research_artifact"])
            self.assertTrue((job_dir / "steam_candidate_research_result.json").exists())
            self.assertTrue((job_dir / "steam_candidate_recommendation.json").exists())
            self.assertEqual(sent[0][0], "https://steam.example/exec")
            self.assertEqual(sent[0][1]["token"], "secret-token")
            self.assertNotIn("token", outcome["callback_payload"])
            self.assertEqual(outcome["callback_payload"]["recommendation"], "RECOMMEND_BUILD")
            self.assertEqual(outcome["callback_payload"]["serp_summary"]["organic_count"], 7)
            artifact_text = (job_dir / "steam_candidate_recommendation.json").read_text()
            self.assertNotIn("secret-token", artifact_text)
            self.assertNotIn('"BUILD"', artifact_text)
            self.assertIn(
                json.loads(artifact_text)["recommendation"],
                {"RECOMMEND_BUILD", "RECOMMEND_WATCH", "RECOMMEND_REJECT"},
            )

    def test_completed_artifact_reuse_does_not_call_provider_on_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job_path = self._write_job(root)
            with mock.patch.dict(
                os.environ,
                {
                    "STEAM_CANDIDATE_RESEARCH_API_URL": "https://steam.example/exec",
                    "STEAM_CANDIDATE_RESEARCH_CALLBACK_TOKEN": "secret-token",
                },
            ), mock.patch.object(wrapper.m7a, "run_candidate_research", return_value=_result()):
                first = wrapper.run_job(job_path, root=root, post_fn=lambda *_: {"ok": True})
            self.assertTrue(first["ok"])

            def must_not_run(*_args, **_kwargs):
                self.fail("M7A/provider must not rerun for completed artifact")

            with mock.patch.dict(
                os.environ,
                {
                    "STEAM_CANDIDATE_RESEARCH_API_URL": "https://steam.example/exec",
                    "STEAM_CANDIDATE_RESEARCH_CALLBACK_TOKEN": "secret-token",
                },
            ), mock.patch.object(wrapper.m7a, "run_candidate_research", side_effect=must_not_run):
                second = wrapper.run_job(
                    job_path,
                    root=root,
                    fetch_fn=lambda *_: self.fail("SearchApi fetch must not run"),
                    post_fn=lambda *_: {"ok": True},
                )
            self.assertTrue(second["ok"])
            self.assertTrue(second["reused_research_artifact"])

    def test_callback_failure_keeps_local_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job_path = self._write_job(root)
            with mock.patch.dict(
                os.environ,
                {
                    "STEAM_CANDIDATE_RESEARCH_API_URL": "https://steam.example/exec",
                    "STEAM_CANDIDATE_RESEARCH_CALLBACK_TOKEN": "secret-token",
                },
            ), mock.patch.object(wrapper.m7a, "run_candidate_research", return_value=_result()):
                outcome = wrapper.run_job(job_path, root=root, post_fn=lambda *_: {"ok": False})
            job_dir = root / "jobs" / _job()["job_id"]
            self.assertFalse(outcome["ok"])
            self.assertFalse(outcome["callback_ok"])
            self.assertTrue((job_dir / "steam_candidate_research_result.json").exists())
            self.assertTrue((job_dir / "steam_candidate_recommendation.json").exists())

    def test_m7a_failed_sends_failed_callback_without_recommendation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job_path = self._write_job(root)
            sent: list[dict] = []

            def post_fn(_url: str, body: dict) -> dict:
                sent.append(body)
                return {"ok": True}

            with mock.patch.dict(
                os.environ,
                {
                    "STEAM_CANDIDATE_RESEARCH_API_URL": "https://steam.example/exec",
                    "STEAM_CANDIDATE_RESEARCH_CALLBACK_TOKEN": "secret-token",
                },
            ), mock.patch.object(wrapper.m7a, "run_candidate_research", return_value=_result("FAILED")):
                outcome = wrapper.run_job(job_path, root=root, post_fn=post_fn)
            self.assertEqual(outcome["execution_status"], "FAILED")
            self.assertTrue(outcome["callback_ok"])
            self.assertNotIn("recommendation", outcome["callback_payload"])
            self.assertNotIn("steam_candidate_recommendation.json", outcome["callback_payload"].values())
            self.assertEqual(sent[0]["execution_status"], "FAILED")
            self.assertFalse((root / "jobs" / _job()["job_id"] / "steam_candidate_recommendation.json").exists())

    def test_dry_run_does_not_post_and_reuses_completed_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job_path = self._write_job(root)
            with mock.patch.object(wrapper.m7a, "run_candidate_research", return_value=_result()):
                first = wrapper.run_job(job_path, root=root, dry_run=True)
            self.assertTrue(first["ok"])
            self.assertFalse(first["sent"])

            with mock.patch.object(wrapper.m7a, "run_candidate_research", side_effect=AssertionError("rerun")):
                second = wrapper.run_job(job_path, root=root, dry_run=True, fetch_fn=lambda *_: self.fail("provider"))
            self.assertTrue(second["reused_research_artifact"])
            self.assertFalse(second["sent"])


class SteamCandidateResearchEndpointTests(unittest.TestCase):
    def test_env_configured_url_appends_pending_action(self) -> None:
        called: list[str] = []

        def fetch_fn(url: str) -> dict:
            called.append(url)
            return {"jobs": []}

        with mock.patch.dict(os.environ, {"STEAM_CANDIDATE_RESEARCH_API_URL": "https://steam.example/exec"}):
            fetcher.fetch_pending_steam_candidate_research_jobs(fetch_fn=fetch_fn)
        self.assertEqual(called, ["https://steam.example/exec?action=pendingSteamCandidateResearchJobs"])

    def test_missing_env_fails_without_gsc_fallback(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(SystemExit) as raised:
                fetcher.fetch_pending_steam_candidate_research_jobs()
        self.assertIn("STEAM_CANDIDATE_RESEARCH_API_URL is not set", str(raised.exception))
        self.assertNotIn("script.google.com/macros/s/", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
