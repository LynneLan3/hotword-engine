"""Offline contract and end-to-end tests for Steam Candidate Preflight V1."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import steam_candidate_preflight as preflight
import steam_candidate_preflight_executor as executor


def _job() -> dict:
    return {
        "job_id": "steam-research-123456-20260826",
        "job_type": "STEAM_CANDIDATE_RESEARCH",
        "steam_app_id": "123456",
        "game_name": "Example Game",
        "steam_url": "https://store.steampowered.com/app/123456/",
        "research_cycle_date": "2026-08-26",
        "steam_signals": {"first_round_type": "🔥趋势"},
        "manual_signals": {"trends_result": "未检查"},
        "serp_queries": ["Example Game"],
        "requested_checks": ["GAME_WIDE_SOCIAL", "GOOGLE_ORGANIC_SERP"],
        "created_at": "2026-08-26T01:00:00Z",
    }


class SteamCandidatePreflightExecutorTests(unittest.TestCase):
    def _write_job(self, root: Path) -> Path:
        path = root / "input" / "steam_candidate_research_job.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(_job()), encoding="utf-8")
        return path

    def test_fresh_social_serp_preflight_callback_and_artifacts(self) -> None:
        events: list[str] = []

        def social_fn(job: dict) -> dict:
            events.append("social")
            return {
                "status": "AVAILABLE",
                "evidence_count": 2,
                "top_clusters": [{"topic": "Example Game puzzle"}],
            }

        def autocomplete_fn(_source: str, query: str) -> dict:
            return {"status": "BEST_EFFORT", "items": [{"text": f"{query} guide"}]}

        serp_queries: list[str] = []

        def serp_fn(query: str) -> dict:
            serp_queries.append(query)
            return {"status": "SUPPORTED", "items": []}

        def preflight_fn(job: dict, *, cache_dir: Path) -> dict:
            events.append("preflight")
            self.assertEqual(job["social_supporting_evidence"]["evidence_count"], 2)
            self.assertTrue(cache_dir.name == "steam-preflight")
            return preflight.run_preflight(
                job,
                autocomplete_fn=autocomplete_fn,
                serp_fn=serp_fn,
                cache_dir=cache_dir,
            )

        sent: list[dict] = []

        def post_fn(_url: str, body: dict) -> dict:
            events.append("callback")
            sent.append(body)
            return {"ok": True}

        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ,
            {
                executor.API_URL_ENV: "https://sheet.example/exec",
                executor.CALLBACK_TOKEN_ENV: "callback-secret",
            },
        ):
            root = Path(tmp)
            outcome = executor.run_job(
                self._write_job(root),
                root=root,
                social_fn=social_fn,
                preflight_fn=preflight_fn,
                post_fn=post_fn,
            )
            job_dir = root / "jobs" / _job()["job_id"]
            artifact = json.loads((job_dir / "steam_candidate_research_result.json").read_text())

        self.assertTrue(outcome["ok"])
        self.assertEqual(events, ["social", "preflight", "callback"])
        self.assertEqual(outcome["execution_status"], "COMPLETED")
        self.assertEqual(outcome["preflight_verdict"], preflight.MANUAL_REVIEW)
        self.assertEqual(len(serp_queries), 3)
        self.assertEqual(artifact["social"]["evidence_count"], 2)
        self.assertNotIn("token", outcome["callback_payload"])
        self.assertEqual(sent[0]["token"], "callback-secret")
        self.assertEqual(sent[0]["job_type"], "STEAM_CANDIDATE_RESEARCH")
        self.assertEqual(sent[0]["preflight_verdict"], "MANUAL_REVIEW")
        self.assertNotIn("recommendation", sent[0])

    def test_all_four_verdict_callbacks_match_apps_script_v1_contract(self) -> None:
        required = {
            "job_id", "job_type", "steam_app_id", "game_name",
            "research_cycle_date", "execution_status", "preflight_verdict",
            "preflight_checked_at", "preflight_reason",
        }
        for verdict in preflight.VERDICTS:
            result = {
                "preflight_verdict": verdict,
                "preflight_reason": f"reason_{verdict}",
                "checked_at": "2026-08-26T09:15:00+08:00",
                "provider_errors": ["provider down"] if verdict == preflight.PREFLIGHT_ERROR else [],
            }
            if verdict == preflight.WATCH:
                result["next_review_date"] = "2026-09-02"
            payload = executor._callback_payload(_job(), result)
            self.assertTrue(required.issubset(payload))
            self.assertEqual(payload["preflight_verdict"], verdict)
            self.assertIn(payload["execution_status"], {"COMPLETED", "FAILED"})
            if verdict == preflight.WATCH:
                self.assertEqual(payload["next_review_date"], "2026-09-02")
            if verdict == preflight.PREFLIGHT_ERROR:
                self.assertEqual(payload["execution_status"], "FAILED")
                self.assertTrue(payload["error"])

    def test_completed_preflight_artifact_is_reused_without_paid_or_social_calls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ,
            {
                executor.API_URL_ENV: "https://sheet.example/exec",
                executor.CALLBACK_TOKEN_ENV: "callback-secret",
            },
        ):
            root = Path(tmp)
            job_path = self._write_job(root)
            calls: list[str] = []

            def social_fn(_job: dict) -> dict:
                calls.append("social")
                return {"status": "AVAILABLE", "evidence_count": 1, "top_clusters": []}

            def preflight_fn(_job: dict, *, cache_dir: Path) -> dict:
                calls.append("preflight")
                return {
                    "preflight_verdict": preflight.WATCH,
                    "preflight_reason": "SEARCH_DEMAND_IMMATURE",
                    "checked_at": "2026-08-26T09:15:00+08:00",
                    "next_review_date": "2026-09-02",
                    "provider_errors": [],
                }

            first = executor.run_job(job_path, root=root, social_fn=social_fn, preflight_fn=preflight_fn, post_fn=lambda *_: {"ok": True})
            second = executor.run_job(
                job_path,
                root=root,
                social_fn=lambda _job: self.fail("social rerun"),
                preflight_fn=lambda *_args, **_kwargs: self.fail("preflight rerun"),
                post_fn=lambda *_: {"ok": True},
            )

        self.assertTrue(first["ok"])
        self.assertTrue(second["ok"])
        self.assertEqual(calls, ["social", "preflight"])
        self.assertTrue(second["reused_preflight_artifact"])

    def test_failed_preflight_callback_contains_error_and_no_old_recommendation_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ,
            {
                executor.API_URL_ENV: "https://sheet.example/exec",
                executor.CALLBACK_TOKEN_ENV: "callback-secret",
            },
        ):
            sent: list[dict] = []
            outcome = executor.run_job(
                self._write_job(Path(tmp)),
                root=Path(tmp),
                social_fn=lambda _job: {"status": "UNAVAILABLE", "error": "social down", "evidence_count": 0, "top_clusters": []},
                preflight_fn=lambda _job, **_kwargs: {
                    "preflight_verdict": preflight.PREFLIGHT_ERROR,
                    "preflight_reason": "ALL_PREFLIGHT_PROVIDERS_UNAVAILABLE",
                    "checked_at": "2026-08-26T09:15:00+08:00",
                    "provider_errors": ["searchapi down"],
                },
                post_fn=lambda _url, body: (sent.append(body) or {"ok": True}),
            )

        self.assertFalse(outcome["ok"])
        self.assertEqual(outcome["execution_status"], "FAILED")
        self.assertTrue(outcome["callback_payload"]["error"])
        self.assertNotIn("recommendation", outcome["callback_payload"])
        self.assertEqual(sent[0]["execution_status"], "FAILED")


if __name__ == "__main__":
    unittest.main()
