"""Tests for machine-written Today Action field derivation."""

from __future__ import annotations

import unittest

import steam_candidate_machine_fields as fields


class SteamCandidateMachineFieldsTests(unittest.TestCase):
    def test_social_verdict_strong_with_actionable_clusters(self) -> None:
        verdict, one_liner = fields.compute_social_verdict(
            {
                "status": "AVAILABLE",
                "evidence_count": 12,
                "actionable_cluster_count": 2,
                "watch_cluster_count": 1,
                "top_clusters": [
                    {
                        "topic": "build guide",
                        "providers": ["reddit", "youtube"],
                        "representative_questions": ["best early build"],
                    }
                ],
            }
        )
        self.assertEqual(verdict, "强")
        self.assertIn("reddit", one_liner.lower())

    def test_keyword_opportunity_from_autocomplete(self) -> None:
        self.assertEqual(
            fields.compute_keyword_opportunity({"autocomplete": {"status": "AVAILABLE", "guide_intent": True}}),
            "有",
        )
        self.assertEqual(
            fields.compute_keyword_opportunity({"autocomplete": {"status": "AVAILABLE", "guide_intent": False}}),
            "无",
        )

    def test_normalize_machine_recommendation_display(self) -> None:
        self.assertEqual(fields.normalize_machine_recommendation_display("RECOMMEND_BUILD"), "BUILD")
        self.assertEqual(fields.normalize_machine_recommendation_display("RECOMMEND_WATCH"), "WATCH")
        self.assertEqual(fields.normalize_machine_recommendation_display("RECOMMEND_REJECT"), "REJECT")
        self.assertEqual(fields.normalize_machine_recommendation_display("ALREADY_BUILT"), "ALREADY_BUILT")

    def test_normalize_master_machine_recommendation_uses_skip(self) -> None:
        self.assertEqual(fields.normalize_master_machine_recommendation("RECOMMEND_BUILD"), "BUILD")
        self.assertEqual(fields.normalize_master_machine_recommendation("RECOMMEND_WATCH"), "WATCH")
        self.assertEqual(fields.normalize_master_machine_recommendation("RECOMMEND_REJECT"), "SKIP")
        self.assertEqual(fields.normalize_master_machine_recommendation("REJECT"), "SKIP")
        self.assertEqual(fields.normalize_master_machine_recommendation("ALREADY_BUILT"), "SKIP")
        self.assertEqual(fields.normalize_master_machine_recommendation("SKIP"), "SKIP")

    def test_build_master_outcome_machine_fields(self) -> None:
        outcome = fields.build_master_outcome_machine_fields(
            social={
                "status": "AVAILABLE",
                "evidence_count": 12,
                "actionable_cluster_count": 2,
                "watch_cluster_count": 0,
                "top_clusters": [],
            },
            preflight_result={"serp": {"dedicated_guide_domains": ["a.example"], "brand_serp_guide_density": "MODERATE"}},
            recommendation="RECOMMEND_REJECT",
            confidence="MEDIUM",
        )
        self.assertEqual(outcome["social_result"], "强")
        self.assertEqual(outcome["serp_competition"], "中")
        self.assertEqual(outcome["machine_recommendation"], "SKIP")
        self.assertEqual(outcome["machine_confidence"], "MEDIUM")


if __name__ == "__main__":
    unittest.main()
