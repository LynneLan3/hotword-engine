#!/usr/bin/env python3
"""Pure temporary-directory tests for the M5 local artifact runner."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import research_recommendation as rr
import research_recommendation_job_runner as rjr


def _search_result(**overrides) -> dict:
    result = {
        "job_id": "search-project-pitt",
        "game": "Project P.I.T.T.",
        "execution_status": "COMPLETED",
        "search_demand_status": "CONFIRMED",
        "search_evidence_count": 1,
        "matched_queries": ["Project P.I.T.T. guide"],
        "serp_evidence_count": 7,
        "serp_competition_summaries": [
            {
                "query": "Project P.I.T.T.",
                "organic_count": 7,
                "signals": [rr.SIGNAL_LOW_GUIDE_DENSITY, rr.SIGNAL_HIGH_VIDEO_UGC_PRESENCE],
            }
        ],
        "all_evidence": [{"raw": "must not be copied"}],
    }
    result.update(overrides)
    return result


def _social_result(**overrides) -> dict:
    result = {
        "job_id": "social-project-pitt",
        "game_name": "Project P.I.T.T.",
        "status": "COMPLETED",
        "evidence_count": 20,
        "clusters": [{"decision": "NEW", "evidence": [{"raw": "must not be copied"}]}],
        "decision_counts": {"NEW": 1, "EXPAND": 0, "WATCH": 0, "IGNORE": 0},
    }
    result.update(overrides)
    return result


def _job(**overrides) -> dict:
    job = {
        "job_id": "recommend-project-pitt-automation-20260823",
        "job_type": "RESEARCH_RECOMMENDATION",
        "site_key": "project-pitt",
        "game_name": "Project P.I.T.T.",
        "search_result_path": "inputs/search_demand_result.json",
        "social_result_path": "inputs/game_wide_social_result.json",
        "created_at": "2026-08-23T10:00:00+08:00",
    }
    job.update(overrides)
    return job


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


class ResearchRecommendationJobRunnerTests(unittest.TestCase):
    def test_1_search_and_social_write_three_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_json(root / "inputs/search_demand_result.json", _search_result())
            _write_json(root / "inputs/game_wide_social_result.json", _social_result())
            outcome = rjr.run_recommendation_job(_job(), root=root)

            self.assertTrue(outcome["ok"])
            job_dir = root / "jobs" / "recommend-project-pitt-automation-20260823"
            self.assertTrue((job_dir / "job.json").exists())
            self.assertTrue((job_dir / "research_recommendation.json").exists())
            self.assertTrue((job_dir / "status.json").exists())
            result = json.loads((job_dir / "research_recommendation.json").read_text())
            status = json.loads((job_dir / "status.json").read_text())
            self.assertEqual(result["recommendation"], rr.RECOMMEND_ADVANCE)
            self.assertEqual(result["source_artifacts"]["search_job_id"], "search-project-pitt")
            self.assertEqual(result["source_artifacts"]["social_job_id"], "social-project-pitt")
            self.assertEqual(status["status"], rjr.EXEC_COMPLETED)
            self.assertEqual(status["result_path"], "jobs/recommend-project-pitt-automation-20260823/research_recommendation.json")

    def test_2_missing_social_is_completed_and_m4_records_missing_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_json(root / "inputs/search_demand_result.json", _search_result())
            outcome = rjr.run_recommendation_job(_job(), root=root)
            result = outcome["result"]
            self.assertTrue(outcome["ok"])
            self.assertEqual(result["confidence"], rr.CONFIDENCE_MEDIUM)
            self.assertIn("SOCIAL_EVIDENCE_NOT_AVAILABLE", result["missing_evidence"])
            self.assertIsNone(result["source_artifacts"]["social_job_id"])

    def test_3_missing_search_is_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            outcome = rjr.run_recommendation_job(_job(), root=Path(tmp))
            self.assertFalse(outcome["ok"])
            status = json.loads(
                (Path(tmp) / "jobs" / "recommend-project-pitt-automation-20260823" / "status.json").read_text()
            )
            self.assertEqual(status["status"], rjr.EXEC_FAILED)
            self.assertIn("search result not found", status["error"])

    def test_4_non_object_search_json_is_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "inputs/search_demand_result.json"
            path.parent.mkdir(parents=True)
            path.write_text("[]", encoding="utf-8")
            outcome = rjr.run_recommendation_job(_job(), root=root)
            self.assertFalse(outcome["ok"])
            self.assertEqual(outcome["status"], rjr.EXEC_FAILED)

    def test_5_provenance_wrapper_does_not_copy_raw_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_json(root / "inputs/search_demand_result.json", _search_result())
            _write_json(root / "inputs/game_wide_social_result.json", _social_result())
            outcome = rjr.run_recommendation_job(_job(), root=root)
            serialized = json.dumps(outcome["result"])
            self.assertNotIn("all_evidence", serialized)
            self.assertNotIn('"clusters"', serialized)
            self.assertIn("source_artifacts", outcome["result"])
            self.assertEqual(outcome["result"]["source_artifacts"]["search_result_path"], "inputs/search_demand_result.json")

    def test_6_different_game_source_is_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_json(
                root / "inputs/search_demand_result.json",
                _search_result(game="A Different Game"),
            )
            outcome = rjr.run_recommendation_job(_job(), root=root)
            self.assertFalse(outcome["ok"])
            self.assertIn("game", outcome["error"])

    def test_7_unknown_game_identity_does_not_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            search = _search_result()
            search.pop("game")
            _write_json(root / "inputs/search_demand_result.json", search)
            outcome = rjr.run_recommendation_job(_job(), root=root)
            self.assertTrue(outcome["ok"])

    def test_8_recommendation_values_are_not_candidate_decisions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_json(root / "inputs/search_demand_result.json", _search_result())
            outcome = rjr.run_recommendation_job(_job(social_result_path=""), root=root)
            serialized = json.dumps(outcome["result"])
            self.assertIn(outcome["result"]["recommendation"], rr.RECOMMENDATIONS)
            self.assertNotIn("candidate_decision", serialized)
            self.assertNotIn("decision_owner", serialized)
            self.assertNotIn("publish", serialized)
            self.assertNotIn("build_site", serialized)

    def test_9_cli_exit_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            search_path = root / "inputs/search_demand_result.json"
            job_path = root / "input/research_recommendation_job.json"
            _write_json(search_path, _search_result())
            job = _job(
                search_result_path="inputs/search_demand_result.json",
                social_result_path="",
            )
            _write_json(job_path, job)
            with mock.patch.object(rjr, "ROOT", root):
                self.assertEqual(rjr.main([str(root / "missing.json")]), 2)
                self.assertEqual(rjr.main([str(job_path)]), 0)
            self.assertTrue(search_path.exists())
            self.assertTrue(
                (root / "jobs" / "recommend-project-pitt-automation-20260823" / "research_recommendation.json").exists()
            )


if __name__ == "__main__":
    unittest.main()
