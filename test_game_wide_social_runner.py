#!/usr/bin/env python3
"""Deterministic tests for GAME_WIDE_SOCIAL_DISCOVERY V1."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import game_wide_social_runner as gws


def _job(**overrides) -> dict:
    job = {
        "job_type": gws.JOB_TYPE,
        "job_id": "social-test-20260820",
        "site_key": "ms2",
        "game_name": "Mortal Shell II",
        "aliases": ["Mortal Shell 2"],
        "lookback_hours": 48,
        "providers": ["reddit", "steam", "youtube"],
        "gsc_queries": [],
        "existing_pages": [],
        "recent_interventions": [],
        "created_at": "2026-08-20T10:00:00+08:00",
    }
    job.update(overrides)
    return job


def _ev(provider: str, title: str, text: str, url: str | None = None, engagement: dict | None = None) -> dict:
    return {
        "provider": provider,
        "source_family": gws.ddr.provider_to_family(provider),
        "title": title,
        "url": url or f"https://example.test/{provider}/{abs(hash(title))}",
        "excerpt": text,
        "player_question": gws.infer_question(f"{title} {text}"),
        "signal_text": gws.infer_question(f"{title} {text}") or title,
        "seed_term": "Mortal Shell II",
        "seed_role": "GAME_WIDE",
        "matched_seed_terms": ["Mortal Shell II"],
        "matched_seed_roles": ["GAME_WIDE"],
        "relevance": 0.9,
        "normalized_signal": gws.ddr.normalize_signal(title),
        "engagement": engagement or {},
    }


def _run_with_evidence(job: dict, evidence: list[dict], failures: dict | None = None) -> dict:
    original = gws.collect_social_evidence

    def fake_collect(_job: dict):
        counts = {"reddit": 0, "steam": 0, "youtube": 0}
        for item in evidence:
            counts[item["provider"]] += 1
        return evidence, counts, failures or {}

    gws.collect_social_evidence = fake_collect
    try:
        return gws.run_game_wide_social_discovery(job)
    finally:
        gws.collect_social_evidence = original


class DecisionRuleTests(unittest.TestCase):
    def test_case_a_social_first_new(self) -> None:
        evidence = [
            _ev("reddit", "Where are all the Beacons?", "I cannot find all beacons or fast travel."),
            _ev("steam", "Fast travel beacons", "How do I fast travel between beacons?"),
        ]
        result = _run_with_evidence(_job(), evidence)
        self.assertEqual(result["decision_counts"]["NEW"], 1)
        cluster = result["clusters"][0]
        self.assertEqual(cluster["decision"], "NEW")
        self.assertEqual(cluster["gsc_match"]["status"], "ABSENT")
        self.assertEqual(cluster["existing_page_match"]["status"], "NONE")

    def test_case_b_recent_intervention_forces_watch(self) -> None:
        evidence = [
            _ev("reddit", "Hotfix 2 crash", "Why does Mortal Shell 2 still crash after Hotfix 2?"),
            _ev("steam", "Crashing after hotfix", "Game keeps crashing after the hotfix."),
        ]
        job = _job(
            gsc_queries=[{"query": "mortal shell 2 crashing", "clicks": 0, "impressions": 56}],
            existing_pages=[{"url": "/mortal-shell-ii/crashing-pc/", "title": "Mortal Shell 2 Crashing on PC?"}],
            recent_interventions=[
                {"url": "/mortal-shell-ii/crashing-pc/", "date": "2026-08-20", "type": "CONTENT_UPDATE"}
            ],
        )
        result = _run_with_evidence(job, evidence)
        cluster = result["clusters"][0]
        self.assertEqual(cluster["existing_page_match"]["status"], "MATCHED")
        self.assertTrue(cluster["intervention_match"]["recent_intervention"])
        self.assertEqual(cluster["decision"], "WATCH")

    def test_case_c_partial_page_match_watches(self) -> None:
        evidence = [
            _ev("reddit", "Lost Gloom recovery", "How do I retrieve Gloom stuck below a cliff?"),
        ]
        job = _job(existing_pages=[{"url": "/mortal-shell-ii/gloom-farm/", "title": "Mortal Shell 2 Gloom Farm"}])
        result = _run_with_evidence(job, evidence)
        cluster = result["clusters"][0]
        self.assertEqual(cluster["existing_page_match"]["status"], "PARTIAL")
        self.assertEqual(cluster["decision"], "WATCH")

    def test_case_d_generic_discussion_ignored(self) -> None:
        evidence = [_ev("reddit", "Mortal Shell II is amazing", "Mortal Shell II is amazing.")]
        result = _run_with_evidence(_job(), evidence)
        self.assertEqual(result["clusters"][0]["decision"], "IGNORE")

    def test_case_e_provider_fail_does_not_fail_job(self) -> None:
        evidence = [
            _ev("reddit", "Where are all the Beacons?", "Where are all the Beacons?"),
            _ev("steam", "Fast travel beacons", "How do I fast travel?"),
        ]
        result = _run_with_evidence(_job(), evidence, {"youtube": "forced failure"})
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["source_failures"]["youtube"], "forced failure")
        self.assertEqual(result["decision_counts"]["NEW"], 1)


class ArtifactTests(unittest.TestCase):
    def test_run_job_writes_game_wide_artifacts(self) -> None:
        evidence = [
            _ev("reddit", "Where are all the Beacons?", "Where are all the Beacons?"),
            _ev("steam", "Fast travel beacons", "How do I fast travel?"),
        ]
        original_collect = gws.collect_social_evidence
        original_root = gws.ROOT

        def fake_collect(_job: dict):
            return evidence, {"reddit": 1, "steam": 1, "youtube": 0}, {}

        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            job = _job()
            job_path = tmp_root / "input" / "game_wide_social_job.json"
            job_path.parent.mkdir(parents=True)
            job_path.write_text(json.dumps(job), encoding="utf-8")
            gws.collect_social_evidence = fake_collect
            gws.ROOT = tmp_root
            try:
                run = gws.run_job(job_path)
            finally:
                gws.collect_social_evidence = original_collect
                gws.ROOT = original_root
            self.assertTrue(run["ok"])
            job_dir = tmp_root / "jobs" / job["job_id"]
            self.assertTrue((job_dir / "job.json").exists())
            self.assertTrue((job_dir / "status.json").exists())
            self.assertTrue((job_dir / "game_wide_social_result.json").exists())


if __name__ == "__main__":
    unittest.main()
