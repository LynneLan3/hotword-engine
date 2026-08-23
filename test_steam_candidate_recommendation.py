"""Offline fixtures for the deterministic M7B Steam recommendation layer."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import steam_candidate_recommendation as recommendation


ROOT = Path(__file__).resolve().parent


def _research(**overrides) -> dict:
    result = {
        "job_id": "steam-research-example-20260823",
        "job_type": "STEAM_CANDIDATE_RESEARCH",
        "steam_app_id": "123456",
        "game_name": "Example Game",
        "steam_signals": {
            "first_round_type": "🔥趋势",
            "first_round_priority": "高",
            "followers": 1000,
            "followers_gain_7d": 100,
            "growth_rate": 0.1,
            "release_stage": "Upcoming",
            "release_date": "2026-09-01",
            "days_to_release": 9,
            "review_count": 0,
            "steam_score": 80,
        },
        "manual_signals": {"trends_result": "", "keyword_opportunity": ""},
        "social": {
            "status": "AVAILABLE",
            "evidence_count": 4,
            "cluster_count": 2,
            "actionable_cluster_count": 1,
            "watch_cluster_count": 1,
            "top_clusters": [],
        },
        "serp": {
            "status": "AVAILABLE",
            "query": "Example Game",
            "organic_count": 10,
            "competition_summary": {
                "signals": ["LOW_GUIDE_DENSITY", "HIGH_VIDEO_UGC_PRESENCE"],
                "facts": {"official_result_present": False},
            },
        },
        "research_status": "COMPLETED",
        "generated_at": "2026-08-23T01:00:00Z",
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = {**result[key], **value}
        else:
            result[key] = value
    return result


class RecommendationRulesTests(unittest.TestCase):
    def test_case_1_strong_low_and_actionable_social_builds(self) -> None:
        output = recommendation.build_steam_candidate_recommendation(_research())
        self.assertEqual(output["recommendation"], "RECOMMEND_BUILD")
        self.assertEqual(output["confidence"], "HIGH")
        self.assertIn("ACTIONABLE_SOCIAL_PROBLEMS", output["reasons"])

    def test_case_2_early_low_and_video_builds_with_medium_confidence(self) -> None:
        output = recommendation.build_steam_candidate_recommendation(
            _research(
                steam_signals={"first_round_type": "🌱Early"},
                social={
                    "status": "UNAVAILABLE",
                    "evidence_count": 0,
                    "cluster_count": 0,
                    "actionable_cluster_count": 0,
                    "watch_cluster_count": 0,
                },
            )
        )
        self.assertEqual(output["recommendation"], "RECOMMEND_BUILD")
        self.assertEqual(output["confidence"], "MEDIUM")
        self.assertIn("SOCIAL_EVIDENCE_NOT_AVAILABLE", output["missing_evidence"])

    def test_case_3_high_guide_density_is_watch_even_with_social(self) -> None:
        output = recommendation.build_steam_candidate_recommendation(
            _research(
                serp={
                    "competition_summary": {
                        "signals": ["HIGH_GUIDE_DENSITY"],
                    }
                }
            )
        )
        self.assertEqual(output["recommendation"], "RECOMMEND_WATCH")
        self.assertNotEqual(output["recommendation"], "RECOMMEND_REJECT")

    def test_case_4_weak_high_guide_without_actionable_social_rejects(self) -> None:
        output = recommendation.build_steam_candidate_recommendation(
            _research(
                steam_signals={"first_round_type": "⚪低优先级"},
                social={"actionable_cluster_count": 0},
                serp={"competition_summary": {"signals": ["HIGH_GUIDE_DENSITY"]}},
            )
        )
        self.assertEqual(output["recommendation"], "RECOMMEND_REJECT")

    def test_case_5_weak_manual_signals_and_high_guide_reject(self) -> None:
        output = recommendation.build_steam_candidate_recommendation(
            _research(
                manual_signals={"trends_result": "弱", "keyword_opportunity": "无"},
                social={"actionable_cluster_count": 0},
                serp={"competition_summary": {"signals": ["HIGH_GUIDE_DENSITY"]}},
            )
        )
        self.assertEqual(output["recommendation"], "RECOMMEND_REJECT")

    def test_case_6_serp_unavailable_is_watch(self) -> None:
        output = recommendation.build_steam_candidate_recommendation(
            _research(
                serp={"status": "UNAVAILABLE", "competition_summary": None},
            )
        )
        self.assertEqual(output["recommendation"], "RECOMMEND_WATCH")

    def test_case_7_social_unavailable_without_video_is_watch(self) -> None:
        output = recommendation.build_steam_candidate_recommendation(
            _research(
                social={
                    "status": "UNAVAILABLE",
                    "evidence_count": 0,
                    "actionable_cluster_count": 0,
                },
                serp={"competition_summary": {"signals": ["LOW_GUIDE_DENSITY"]}},
            )
        )
        self.assertEqual(output["recommendation"], "RECOMMEND_WATCH")

    def test_case_8_unchecked_manual_signals_are_missing_not_reject(self) -> None:
        output = recommendation.build_steam_candidate_recommendation(
            _research(
                manual_signals={"trends_result": "未检查", "keyword_opportunity": "未检查"},
            )
        )
        self.assertIn("TRENDS_NOT_AVAILABLE", output["missing_evidence"])
        self.assertIn("KEYWORD_OPPORTUNITY_NOT_AVAILABLE", output["missing_evidence"])
        self.assertNotEqual(output["recommendation"], "RECOMMEND_REJECT")

    def test_case_9_both_provider_families_unavailable_is_low_watch(self) -> None:
        output = recommendation.build_steam_candidate_recommendation(
            _research(
                social={"status": "UNAVAILABLE", "evidence_count": 0, "actionable_cluster_count": 0},
                serp={"status": "UNAVAILABLE", "competition_summary": None},
            )
        )
        self.assertEqual(output["recommendation"], "RECOMMEND_WATCH")
        self.assertEqual(output["confidence"], "LOW")

    def test_case_10_output_has_only_prefixed_recommendation_values(self) -> None:
        output = recommendation.build_steam_candidate_recommendation(_research())
        serialized = json.dumps(output, ensure_ascii=False)
        self.assertIn(output["recommendation"], recommendation.RECOMMENDATIONS)
        for value in ("BUILD", "WATCH", "REJECT"):
            self.assertNotIn(f'"{value}"', serialized)
        self.assertNotIn("candidate_decision", serialized.lower())


class InputAndCliTests(unittest.TestCase):
    def test_consumes_existing_summary_signals_without_reclassification(self) -> None:
        result = _research(
            serp={
                "competition_summary": {
                    "signals": ["MODERATE_GUIDE_DENSITY", "SERP_DOMAIN_DIVERSE"],
                    "facts": {"organic_count": 10},
                }
            }
        )
        output = recommendation.build_steam_candidate_recommendation(result)
        self.assertEqual(output["evidence_snapshot"]["serp_guide_density"], "MODERATE")
        self.assertFalse(output["evidence_snapshot"]["serp_high_video_ugc"])

    def test_cli_writes_output_without_mutating_source(self) -> None:
        source_payload = _research()
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "steam_candidate_research_result.json"
            target = Path(tmp) / "out/recommendation.json"
            source.write_text(json.dumps(source_payload, ensure_ascii=False, indent=2), encoding="utf-8")
            before = source.read_bytes()
            completed = subprocess.run(
                [sys.executable, str(ROOT / "steam_candidate_recommendation.py"), str(source), "--output", str(target)],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(source.read_bytes(), before)
            self.assertEqual(json.loads(target.read_text())["recommendation"], "RECOMMEND_BUILD")


if __name__ == "__main__":
    unittest.main()
