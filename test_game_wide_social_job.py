#!/usr/bin/env python3
"""Tests for GAME_WIDE scope DEMAND_DISCOVERY job runner with callback."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import game_wide_social_runner as gwsr
import run_game_wide_social_job as gjr
import research_job_runner as rjr
import fetch_pending_game_wide_jobs as fpj


def _job(**overrides) -> dict:
    job = {
        "job_type": gwsr.JOB_TYPE,
        "job_id": "game-wide-ms2-20260821",
        "site_key": "ms2",
        "site": "Mortal Shell II",
        "game": "Mortal Shell II",
        "game_name": "Mortal Shell II",
        "aliases": ["Mortal Shell 2"],
        "lookback_hours": 24,
        "providers": ["reddit", "steam", "youtube"],
        "created_at": "2026-08-21T10:00:00+08:00",
    }
    job.update(overrides)
    return job


def _result(**overrides) -> dict:
    r: dict = {
        "job_id": "game-wide-ms2-20260821",
        "job_type": gwsr.JOB_TYPE,
        "site_key": "ms2",
        "game_name": "Mortal Shell II",
        "status": "COMPLETED",
        "run_at": "2026-08-21T10:05:00+00:00",
        "source_counts": {"reddit": 5, "steam": 10, "youtube": 8},
        "source_failures": {},
        "evidence_count": 23,
        "clusters": [
            {
                "topic_key": "skip-prologue",
                "topic": "How do I skip the prologue?",
                "intent": "TASK",
                "task_intent_score": 80,
                "decision": "EXPAND",
                "evidence_count": 3,
                "source_families": ["COMMUNITY", "VIDEO"],
                "providers": ["steam", "youtube"],
                "evidence": [
                    {"url": "https://example.test/1", "title": "skip prologue"},
                    {"url": "https://example.test/2", "title": "skip prologue 2"},
                ],
            },
            {
                "topic_key": "crash",
                "topic": "Game crashes on launch",
                "intent": "TASK",
                "task_intent_score": 75,
                "decision": "NEW",
                "evidence_count": 2,
                "source_families": ["COMMUNITY"],
                "providers": ["steam"],
                "evidence": [
                    {"url": "https://example.test/3", "title": "crash"},
                ],
            },
        ],
        "decision_counts": {"NEW": 1, "EXPAND": 1, "WATCH": 0, "IGNORE": 0},
    }
    r.update(overrides)
    return r


class CallbackPayloadTests(unittest.TestCase):
    """Unit tests for callback payload construction."""

    def test_completed_payload_structure(self):
        job = _job()
        result = _result()
        body = gjr.build_game_wide_completed_callback_body(job, result)
        self.assertEqual(body["job_id"], "game-wide-ms2-20260821")
        self.assertEqual(body["research_type"], "DEMAND_DISCOVERY")
        self.assertEqual(body["execution_status"], "COMPLETED")
        self.assertEqual(body["discovery_scope"], {"scope": "GAME_WIDE"})
        self.assertEqual(body["evidence_count"], 23)
        self.assertEqual(body["cluster_count"], 2)
        self.assertEqual(body["decision_counts"], {"NEW": 1, "EXPAND": 1, "WATCH": 0, "IGNORE": 0})
        self.assertEqual(len(body["top_clusters"]), 2)
        self.assertIn("result_path", body)
        # Must NOT contain radar_id
        self.assertNotIn("radar_id", body)

    def test_top_clusters_truncated(self):
        job = _job()
        clusters = [
            {"topic_key": f"t-{i}", "topic": f"topic {i}", "intent": "TASK",
             "task_intent_score": 50, "decision": "WATCH", "evidence_count": 1,
             "source_families": ["COMMUNITY"], "providers": ["steam"],
             "evidence": [{"url": f"https://e.test/{i}"}]}
            for i in range(10)
        ]
        result = _result(clusters=clusters)
        body = gjr.build_game_wide_completed_callback_body(job, result)
        self.assertEqual(len(body["top_clusters"]), 5)

    def test_failed_payload_structure(self):
        job = _job()
        body = gjr.build_game_wide_failed_callback_body(job, RuntimeError("boom"))
        self.assertEqual(body["job_id"], "game-wide-ms2-20260821")
        self.assertEqual(body["research_type"], "DEMAND_DISCOVERY")
        self.assertEqual(body["execution_status"], "FAILED")
        self.assertEqual(body["error"], "boom")
        self.assertNotIn("radar_id", body)

    def test_no_radar_id_in_any_payload(self):
        job = _job()
        result = _result()
        completed = gjr.build_game_wide_completed_callback_body(job, result)
        failed = gjr.build_game_wide_failed_callback_body(job, "err")
        self.assertNotIn("radar_id", completed)
        self.assertNotIn("radar_id", failed)


class SenderExecutionTests(unittest.TestCase):
    """Integration tests for run_job with mocked runner and callback."""

    def _run_sender(
        self, *, callback_ok: bool = True, runner_raises: bool = False
    ) -> tuple[dict, dict]:
        job = _job()
        result = _result()

        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            job_dir = tmp_root / "jobs" / job["job_id"]
            job_dir.mkdir(parents=True)
            job_path = job_dir / "job.json"
            job_path.write_text(json.dumps(job), encoding="utf-8")
            status_path = job_dir / "status.json"
            status_path.write_text(
                json.dumps({"job_id": job["job_id"], "status": "RUNNING"}),
                encoding="utf-8",
            )

            if runner_raises:
                run_patch = patch.object(
                    gwsr, "run_game_wide_social_discovery",
                    side_effect=RuntimeError("social boom"),
                )
            else:
                run_patch = patch.object(
                    gwsr, "run_game_wide_social_discovery",
                    return_value=result,
                )

            with (
                run_patch,
                patch.object(gwsr, "write_json"),  # suppress artifact writes
                patch.object(rjr, "post_callback_body", return_value=callback_ok) as _cb,
                patch.object(gjr, "ROOT", tmp_root),
            ):
                sender = gjr.run_job(job_path)

            saved_status = json.loads(status_path.read_text(encoding="utf-8"))

        return sender, saved_status

    def test_success_callback_ok(self):
        sender, saved = self._run_sender(callback_ok=True)
        self.assertEqual(sender["execution_status"], "COMPLETED")
        self.assertTrue(sender["callback_ok"])
        self.assertEqual(sender["job_id"], "game-wide-ms2-20260821")
        self.assertEqual(gjr.exit_code_from_run(sender["execution_status"], sender["callback_ok"]), 0)
        self.assertTrue(saved.get("callback_ok"))

    def test_success_callback_fail(self):
        sender, saved = self._run_sender(callback_ok=False)
        self.assertEqual(sender["execution_status"], "COMPLETED")
        self.assertFalse(sender["callback_ok"])
        self.assertEqual(gjr.exit_code_from_run(sender["execution_status"], sender["callback_ok"]), 1)
        self.assertFalse(saved.get("callback_ok"))

    def test_runner_raises(self):
        sender, saved = self._run_sender(runner_raises=True)
        self.assertEqual(sender["execution_status"], "FAILED")
        # Callback is still attempted with FAILED payload; mock returns True
        self.assertTrue(sender["callback_ok"])
        self.assertEqual(gjr.exit_code_from_run(sender["execution_status"], sender["callback_ok"]), 1)


class FetcherContractTests(unittest.TestCase):
    """Test A/B/C: fetcher contract validation for GAME_WIDE scope."""

    def _make_api_job(self, **overrides) -> dict:
        base = {
            "job_id": "game-wide-ms2-20260821",
            "job_type": "GAME_WIDE_SOCIAL_DISCOVERY",
            "research_type": "DEMAND_DISCOVERY",
            "site": "Mortal Shell II",
            "game": "Mortal Shell II",
            "discovery_scope": {"scope": "GAME_WIDE", "lookback_hours": 24},
            "seed_terms": ["Mortal Shell II", "Mortal Shell 2"],
            "source_families_requested": ["COMMUNITY", "VIDEO"],
            "created_at": "2026-08-21T10:00:00+08:00",
        }
        base.update(overrides)
        return base

    def test_A_game_wide_contract_fields(self):
        """Test A: GAME_WIDE job has research_type=DEMAND_DISCOVERY, scope=GAME_WIDE, job_type=GAME_WIDE_SOCIAL_DISCOVERY."""
        job = self._make_api_job()
        out = fpj.to_game_wide_job(job)
        self.assertEqual(out["research_type"], "DEMAND_DISCOVERY")
        self.assertEqual(out["job_type"], "GAME_WIDE_SOCIAL_DISCOVERY")
        self.assertEqual(out["discovery_scope"]["scope"], "GAME_WIDE")
        self.assertIn("seed_terms", out)
        self.assertIn("source_families_requested", out)
        # Must NOT contain these removed fields
        self.assertNotIn("gsc_queries", out)
        self.assertNotIn("existing_pages", out)
        self.assertNotIn("recent_interventions", out)

    def test_B_fetcher_accepts_demand_discovery_game_wide(self):
        """Test B: fetcher accepts DEMAND_DISCOVERY + GAME_WIDE scope."""
        job = self._make_api_job()
        out = fpj.to_game_wide_job(job)
        self.assertEqual(out["job_id"], "game-wide-ms2-20260821")
        self.assertEqual(out["research_type"], "DEMAND_DISCOVERY")

    def test_C_fetcher_rejects_non_game_wide_scope(self):
        """Test C: fetcher rejects DEMAND_DISCOVERY + scope != GAME_WIDE (ANCHOR)."""
        job = self._make_api_job(
            discovery_scope={"scope": "ANCHOR", "page_url": "/some-page"}
        )
        with self.assertRaises(SystemExit):
            fpj.to_game_wide_job(job)

    def test_C_fetcher_rejects_wrong_research_type(self):
        """Test C variant: fetcher rejects research_type != DEMAND_DISCOVERY."""
        job = self._make_api_job(research_type="CONTENT_RESEARCH")
        with self.assertRaises(SystemExit):
            fpj.to_game_wide_job(job)

    def test_D_completed_callback_discovery_scope_is_dict(self):
        """Test D: completed callback has discovery_scope as dict, not string."""
        job = _job()
        result = _result()
        body = gjr.build_game_wide_completed_callback_body(job, result)
        self.assertIsInstance(body["discovery_scope"], dict)
        self.assertEqual(body["discovery_scope"]["scope"], "GAME_WIDE")
        self.assertEqual(body["research_type"], "DEMAND_DISCOVERY")

    def test_E_failed_callback_discovery_scope_is_dict(self):
        """Test E: failed callback has discovery_scope as dict with scope=GAME_WIDE."""
        job = _job()
        body = gjr.build_game_wide_failed_callback_body(job, "err")
        self.assertIsInstance(body["discovery_scope"], dict)
        self.assertEqual(body["discovery_scope"]["scope"], "GAME_WIDE")
        self.assertEqual(body["research_type"], "DEMAND_DISCOVERY")


if __name__ == "__main__":
    unittest.main()
