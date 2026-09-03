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


if __name__ == "__main__":
    unittest.main()
