#!/usr/bin/env python3
"""Pure fixture tests for deterministic Research Recommendation V1."""

from __future__ import annotations

import unittest

import research_recommendation as rr


def _summary(*signals: str, query: str = "q") -> dict:
    return {
        "query": query,
        "organic_count": 7,
        "signals": list(signals),
    }


def _search(
    *,
    demand_status: str = rr.SEARCH_STATUS_CONFIRMED,
    execution_status: str = rr.EXECUTION_COMPLETED,
    summaries: list[dict] | None = None,
) -> dict:
    return {
        "execution_status": execution_status,
        "search_demand_status": demand_status,
        "search_evidence_count": 1 if demand_status == rr.SEARCH_STATUS_CONFIRMED else 0,
        "matched_queries": ["q"] if demand_status == rr.SEARCH_STATUS_CONFIRMED else [],
        "serp_evidence_count": sum(int(item.get("organic_count") or 0) for item in summaries or []),
        "serp_competition_summaries": summaries or [],
    }


def _social(*decisions: str, evidence_count: int = 20) -> dict:
    clusters = [{"decision": decision} for decision in decisions]
    counts = {decision: decisions.count(decision) for decision in ("NEW", "EXPAND", "WATCH", "IGNORE")}
    return {
        "status": "COMPLETED",
        "evidence_count": evidence_count,
        "clusters": clusters,
        "decision_counts": counts,
    }


class ResearchRecommendationTests(unittest.TestCase):
    def test_case_1_confirmed_low_serp_and_video_ugc_advances(self) -> None:
        result = rr.build_research_recommendation(
            _search(summaries=[
                _summary(rr.SIGNAL_LOW_GUIDE_DENSITY, rr.SIGNAL_HIGH_VIDEO_UGC_PRESENCE),
            ]),
            _social("WATCH"),
        )
        self.assertEqual(result["recommendation"], rr.RECOMMEND_ADVANCE)
        self.assertEqual(result["confidence"], rr.CONFIDENCE_HIGH)
        self.assertIn("SEARCH_DEMAND_CONFIRMED", result["reasons"])
        self.assertIn(rr.SIGNAL_LOW_GUIDE_DENSITY, result["reasons"])
        self.assertIn(rr.SIGNAL_HIGH_VIDEO_UGC_PRESENCE, result["reasons"])

    def test_case_2_actionable_social_supports_advance(self) -> None:
        result = rr.build_research_recommendation(
            _search(summaries=[_summary(rr.SIGNAL_LOW_GUIDE_DENSITY)]),
            _social("NEW", "EXPAND", "WATCH", "IGNORE"),
        )
        self.assertEqual(result["recommendation"], rr.RECOMMEND_ADVANCE)
        self.assertIn("ACTIONABLE_SOCIAL_PROBLEMS", result["reasons"])
        self.assertEqual(result["evidence_snapshot"]["actionable_social_cluster_count"], 2)
        self.assertEqual(result["evidence_snapshot"]["watch_social_cluster_count"], 1)

    def test_case_3_confirmed_high_competition_is_watch_not_reject(self) -> None:
        result = rr.build_research_recommendation(
            _search(summaries=[_summary(rr.SIGNAL_HIGH_GUIDE_DENSITY)]),
            _social("IGNORE"),
        )
        self.assertEqual(result["recommendation"], rr.RECOMMEND_WATCH)
        self.assertNotEqual(result["recommendation"], rr.RECOMMEND_REJECT)

    def test_case_4_unconfirmed_high_competition_without_actionable_social_rejects(self) -> None:
        result = rr.build_research_recommendation(
            _search(
                demand_status="NO_SIGNAL",
                summaries=[_summary(rr.SIGNAL_HIGH_GUIDE_DENSITY)],
            ),
            _social("WATCH", "IGNORE"),
        )
        self.assertEqual(result["recommendation"], rr.RECOMMEND_REJECT)
        self.assertEqual(
            result["blocking_reasons"],
            [
                rr.SIGNAL_HIGH_GUIDE_DENSITY,
                "SEARCH_DEMAND_NOT_CONFIRMED",
                "NO_ACTIONABLE_SOCIAL_PROBLEMS",
            ],
        )

    def test_case_5_unconfirmed_low_serp_and_actionable_social_stays_watch(self) -> None:
        result = rr.build_research_recommendation(
            _search(demand_status="NO_SIGNAL", summaries=[_summary(rr.SIGNAL_LOW_GUIDE_DENSITY)]),
            _social("NEW"),
        )
        self.assertEqual(result["recommendation"], rr.RECOMMEND_WATCH)
        self.assertIn("SEARCH_DEMAND_NOT_CONFIRMED", result["blocking_reasons"])

    def test_case_6_missing_social_is_medium_and_explicit(self) -> None:
        result = rr.build_research_recommendation(
            _search(summaries=[_summary(rr.SIGNAL_LOW_GUIDE_DENSITY, rr.SIGNAL_HIGH_VIDEO_UGC_PRESENCE)]),
            None,
        )
        self.assertEqual(result["recommendation"], rr.RECOMMEND_ADVANCE)
        self.assertEqual(result["confidence"], rr.CONFIDENCE_MEDIUM)
        self.assertIn("SOCIAL_EVIDENCE_NOT_AVAILABLE", result["missing_evidence"])

    def test_case_7_missing_serp_is_low_and_watch(self) -> None:
        result = rr.build_research_recommendation(_search(summaries=[]), _social("NEW"))
        self.assertEqual(result["recommendation"], rr.RECOMMEND_WATCH)
        self.assertEqual(result["confidence"], rr.CONFIDENCE_LOW)
        self.assertIn("SERP_COMPETITION_EVIDENCE_NOT_AVAILABLE", result["missing_evidence"])

    def test_case_7b_missing_social_is_not_reject(self) -> None:
        result = rr.build_research_recommendation(
            _search(demand_status="NO_SIGNAL", summaries=[_summary(rr.SIGNAL_HIGH_GUIDE_DENSITY)]),
            None,
        )
        self.assertEqual(result["recommendation"], rr.RECOMMEND_WATCH)
        self.assertIn("SOCIAL_EVIDENCE_NOT_AVAILABLE", result["missing_evidence"])

    def test_case_8_watch_social_cluster_is_not_actionable(self) -> None:
        result = rr.build_research_recommendation(
            _search(summaries=[_summary(rr.SIGNAL_LOW_GUIDE_DENSITY)]),
            _social("WATCH"),
        )
        self.assertEqual(result["recommendation"], rr.RECOMMEND_WATCH)
        self.assertEqual(result["evidence_snapshot"]["actionable_social_cluster_count"], 0)
        self.assertEqual(result["evidence_snapshot"]["watch_social_cluster_count"], 1)

    def test_case_9_social_new_cannot_bypass_unconfirmed_demand(self) -> None:
        result = rr.build_research_recommendation(
            _search(demand_status="NO_SIGNAL", summaries=[_summary(rr.SIGNAL_LOW_GUIDE_DENSITY)]),
            _social("NEW"),
        )
        self.assertEqual(result["recommendation"], rr.RECOMMEND_WATCH)
        self.assertNotEqual(result["recommendation"], rr.RECOMMEND_ADVANCE)

    def test_serp_signal_aggregation_is_counted_per_summary(self) -> None:
        result = rr.build_research_recommendation(
            _search(
                summaries=[
                    _summary(rr.SIGNAL_LOW_GUIDE_DENSITY, rr.SIGNAL_SERP_CONTAMINATION_PRESENT),
                    _summary(rr.SIGNAL_MODERATE_GUIDE_DENSITY),
                    _summary(rr.SIGNAL_HIGH_GUIDE_DENSITY, rr.SIGNAL_HIGH_VIDEO_UGC_PRESENCE),
                ]
            ),
            _social("WATCH"),
        )
        snapshot = result["evidence_snapshot"]
        self.assertEqual(snapshot["serp_query_count"], 3)
        self.assertEqual(snapshot["serp_low_guide_density_queries"], 1)
        self.assertEqual(snapshot["serp_moderate_guide_density_queries"], 1)
        self.assertEqual(snapshot["serp_high_guide_density_queries"], 1)
        self.assertEqual(snapshot["serp_contamination_queries"], 1)
        self.assertEqual(snapshot["serp_high_video_ugc_queries"], 1)


if __name__ == "__main__":
    unittest.main()
