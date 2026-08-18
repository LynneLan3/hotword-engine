#!/usr/bin/env python3
"""Deterministic tests for R2C-B DEMAND_DISCOVERY callback sender."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import demand_discovery_job_runner as djr
import demand_discovery_runner as ddr
import research_job_runner as rjr


def _job(*, scope_page_topic: str = "classes", job_id: str = "demand-test-20260818") -> dict:
    return {
        "job_id": job_id,
        "research_type": "DEMAND_DISCOVERY",
        "site": "Agefield High",
        "game": "Agefield High: Rock the School",
        "radar_id": "agefield|/agefield-high-rock-the-school/classes/|QUERY_BLIND_SPOT",
        "trigger_type": "QUERY_BLIND_SPOT",
        "anchor_page": "/agefield-high-rock-the-school/classes/",
        "source_signal_summary": "x",
        "discovery_scope": {"page_topic": scope_page_topic},
        "seed_terms": ["Agefield High: Rock the School", "Agefield High: Rock the School classes"],
        "source_families_requested": ["COMMUNITY", "VIDEO"],
        "discovery_cycle_date": "2026-08-18",
        "created_at": "2026-08-18T10:00:00+08:00",
    }


def _evidence(*, provider: str, source_family: str) -> dict:
    return {
        "provider": provider,
        "source_family": source_family,
        "title": provider,
        "url": "https://example.test/u",
        "excerpt": "",
        "player_question": "",
        "signal_text": provider,
        "seed_term": "s",
        "seed_role": "GAME_WIDE",
        "matched_seed_terms": ["s"],
        "matched_seed_roles": ["GAME_WIDE"],
        "relevance": 0.8,
        "normalized_signal": "x",
    }


def _cluster(*, i: int, example_url_count: int = 4, source_families: list[str] | None = None) -> dict:
    if source_families is None:
        source_families = ["COMMUNITY"]
    return {
        "cluster_id": f"cluster-{i:03d}-sig",
        "representative_signal": f"sig-{i}",
        "representative_question": f"q-{i}",
        "topic_terms": ["t"],
        "evidence_count": i + 1,
        "providers": ["reddit"],
        "source_families": source_families,
        "independent_source_family_count": len(set(source_families)),
        "cross_validated": False,
        "example_urls": [f"https://example.test/v{i}-{j}" for j in range(example_url_count)],
        "example_excerpts": [],
    }


class CallbackPayloadTests(unittest.TestCase):
    def test_case1_agefield_no_signal_external_families_empty(self) -> None:
        job = _job()
        result = {
            "discovery_scope_label": "ANCHOR",
            "anchor_evidence_count": 0,
            "background_evidence_count": 14,
            "anchor_evidence": [],
            "demand_clusters": [],
            "cross_validated_cluster_count": 0,
            "discovery_status": "NO_SIGNAL",
        }
        body = djr.build_demand_discovery_completed_callback_body(job, result)
        self.assertEqual(body["external_source_families"], [])
        self.assertEqual(body["top_clusters"], [])
        self.assertEqual(body["execution_status"], "COMPLETED")

    def test_case2_anchor_community_only(self) -> None:
        job = _job()
        result = {
            "discovery_scope_label": "ANCHOR",
            "anchor_evidence_count": 2,
            "background_evidence_count": 0,
            "anchor_evidence": [_evidence(provider="reddit", source_family="COMMUNITY")],
            "demand_clusters": [],
            "cross_validated_cluster_count": 0,
            "discovery_status": "DISCOVERED",
        }
        body = djr.build_demand_discovery_completed_callback_body(job, result)
        self.assertEqual(body["external_source_families"], ["COMMUNITY"])

    def test_case3_anchor_video_only(self) -> None:
        job = _job()
        result = {
            "discovery_scope_label": "ANCHOR",
            "anchor_evidence_count": 2,
            "background_evidence_count": 0,
            "anchor_evidence": [_evidence(provider="youtube", source_family="VIDEO")],
            "demand_clusters": [],
            "cross_validated_cluster_count": 0,
            "discovery_status": "DISCOVERED",
        }
        body = djr.build_demand_discovery_completed_callback_body(job, result)
        self.assertEqual(body["external_source_families"], ["VIDEO"])

    def test_case4_anchor_community_plus_video(self) -> None:
        job = _job()
        result = {
            "discovery_scope_label": "ANCHOR",
            "anchor_evidence_count": 3,
            "background_evidence_count": 0,
            "anchor_evidence": [
                _evidence(provider="reddit", source_family="COMMUNITY"),
                _evidence(provider="youtube", source_family="VIDEO"),
            ],
            "demand_clusters": [],
            "cross_validated_cluster_count": 0,
            "discovery_status": "CROSS_VALIDATED",
        }
        body = djr.build_demand_discovery_completed_callback_body(job, result)
        self.assertEqual(body["external_source_families"], ["COMMUNITY", "VIDEO"])

    def test_case5_background_family_does_not_leak(self) -> None:
        job = _job()
        result = {
            "discovery_scope_label": "ANCHOR",
            "anchor_evidence_count": 1,
            "background_evidence_count": 2,
            "anchor_evidence": [_evidence(provider="reddit", source_family="COMMUNITY")],
            "background_evidence": [_evidence(provider="youtube", source_family="VIDEO")],
            "demand_clusters": [],
            "cross_validated_cluster_count": 0,
            "discovery_status": "DISCOVERED",
        }
        body = djr.build_demand_discovery_completed_callback_body(job, result)
        self.assertEqual(body["external_source_families"], ["COMMUNITY"])

    def test_case6_game_wide_external_families_empty(self) -> None:
        job = _job(scope_page_topic="")  # GAME_WIDE in runner; but here we control by result label.
        result = {
            "discovery_scope_label": "GAME_WIDE",
            "anchor_evidence_count": 2,
            "background_evidence_count": 0,
            "anchor_evidence": [
                _evidence(provider="reddit", source_family="COMMUNITY"),
                _evidence(provider="youtube", source_family="VIDEO"),
            ],
            "demand_clusters": [],
            "cross_validated_cluster_count": 1,
            "discovery_status": "DISCOVERED",
        }
        body = djr.build_demand_discovery_completed_callback_body(job, result)
        self.assertEqual(body["external_source_families"], [])

    def test_case7_top_clusters_truncate_and_example_urls_limit(self) -> None:
        job = _job()
        result = {
            "discovery_scope_label": "ANCHOR",
            "anchor_evidence_count": 2,
            "background_evidence_count": 0,
            "anchor_evidence": [_evidence(provider="reddit", source_family="COMMUNITY")],
            "demand_clusters": [_cluster(i=i, example_url_count=4) for i in range(6)],
            "cross_validated_cluster_count": 0,
            "discovery_status": "DISCOVERED",
        }
        body = djr.build_demand_discovery_completed_callback_body(job, result)
        self.assertEqual(len(body["top_clusters"]), 5)
        for c in body["top_clusters"]:
            self.assertLessEqual(len(c["example_urls"]), 3)

    def test_case8_no_content_recommendation_fields(self) -> None:
        job = _job()
        result = {
            "discovery_scope_label": "ANCHOR",
            "anchor_evidence_count": 1,
            "background_evidence_count": 0,
            "anchor_evidence": [_evidence(provider="reddit", source_family="COMMUNITY")],
            "demand_clusters": [],
            "cross_validated_cluster_count": 0,
            "discovery_status": "DISCOVERED",
        }
        body = djr.build_demand_discovery_completed_callback_body(job, result)
        forbidden = {
            "recommendation",
            "recommended_action",
            "opportunity_level",
        }
        for k in forbidden:
            self.assertNotIn(k, body)

    def test_case9_failed_identity_payload_contains_id_fields(self) -> None:
        job = _job()
        body = djr.build_demand_discovery_failed_callback_body(job, "boom")
        self.assertEqual(body["job_id"], job["job_id"])
        self.assertEqual(body["radar_id"], job["radar_id"])
        self.assertEqual(body["discovery_cycle_date"], job["discovery_cycle_date"])
        self.assertEqual(body["execution_status"], "FAILED")


class SenderExecutionTests(unittest.TestCase):
    def _run_sender(self, *, callback_ok: bool, discovery_raises: bool) -> tuple[dict, dict]:
        job = _job()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)

            input_dir = tmp_root / "input"
            input_dir.mkdir(parents=True, exist_ok=True)
            job_path = input_dir / "demand_discovery_job.json"
            job_path.write_text(json.dumps(job), encoding="utf-8")

            job_dir = tmp_root / "jobs" / job["job_id"]
            job_dir.mkdir(parents=True, exist_ok=True)
            # pre-create artifacts so we can assert token stays absent.
            (job_dir / "job.json").write_text(json.dumps(job), encoding="utf-8")
            (job_dir / "demand_discovery_result.json").write_text(json.dumps({"x": 1}), encoding="utf-8")
            initial_status = "FAILED" if discovery_raises else "COMPLETED"
            (job_dir / "status.json").write_text(
                json.dumps(
                    {
                        "job_id": job["job_id"],
                        "research_type": "DEMAND_DISCOVERY",
                        "status": initial_status,
                    }
                ),
                encoding="utf-8",
            )

            result = {
                "discovery_scope_label": "ANCHOR",
                "anchor_evidence_count": 1,
                "background_evidence_count": 14,
                "anchor_evidence": [_evidence(provider="reddit", source_family="COMMUNITY")],
                "background_evidence": [],
                "demand_clusters": [_cluster(i=0, example_url_count=2)],
                "cross_validated_cluster_count": 0,
                "discovery_status": "DISCOVERED",
            }

            if discovery_raises:
                run_patch = patch.object(ddr, "run_job", side_effect=RuntimeError("discovery boom"))
            else:
                run_patch = patch.object(
                    ddr,
                    "run_job",
                    return_value={
                        "ok": True,
                        "status": "COMPLETED",
                        "job_id": job["job_id"],
                        "result": result,
                    },
                )

            with (
                patch.object(djr, "ROOT", tmp_root),
                run_patch,
                patch.object(rjr, "post_callback_body", return_value=callback_ok) as _cb,
            ):
                sender = djr.run_job(job_path)

            # read saved artifacts
            saved_status = json.loads((job_dir / "status.json").read_text(encoding="utf-8"))
            saved_job = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
            saved_result = json.loads((job_dir / "demand_discovery_result.json").read_text(encoding="utf-8"))
            artifacts = {"status": saved_status, "job": saved_job, "result": saved_result}

            return sender, artifacts

    def test_case10_callback_success_exit_0(self) -> None:
        sender, artifacts = self._run_sender(callback_ok=True, discovery_raises=False)
        self.assertEqual(sender["execution_status"], "COMPLETED")
        self.assertTrue(sender["callback_ok"])
        self.assertEqual(djr.exit_code_from_run(sender["execution_status"], sender["callback_ok"]), 0)
        self.assertTrue(artifacts["status"]["callback_ok"])

    def test_case11_callback_failure_still_discovery_completed_exit_1(self) -> None:
        sender, artifacts = self._run_sender(callback_ok=False, discovery_raises=False)
        self.assertEqual(sender["execution_status"], "COMPLETED")
        self.assertFalse(sender["callback_ok"])
        self.assertEqual(djr.exit_code_from_run(sender["execution_status"], sender["callback_ok"]), 1)
        self.assertFalse(artifacts["status"]["callback_ok"])

    def test_case12_discovery_failed_exit_1(self) -> None:
        sender, artifacts = self._run_sender(callback_ok=True, discovery_raises=True)
        self.assertEqual(sender["execution_status"], "FAILED")
        self.assertTrue(sender["callback_ok"])  # callback transport can succeed
        self.assertEqual(djr.exit_code_from_run(sender["execution_status"], sender["callback_ok"]), 1)
        self.assertTrue(artifacts["status"]["callback_ok"])

    def test_case13_token_not_enter_artifacts(self) -> None:
        sender, artifacts = self._run_sender(callback_ok=True, discovery_raises=False)
        self.assertTrue("token" not in artifacts["status"])
        self.assertTrue("token" not in artifacts["job"])
        self.assertTrue("token" not in artifacts["result"])


if __name__ == "__main__":
    unittest.main()

