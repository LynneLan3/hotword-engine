#!/usr/bin/env python3
"""M1 Action → Research → ContentDecision runner contract tests."""

from __future__ import annotations

import unittest

import research_job_runner as rjr


def evidence(count: int) -> list[dict[str, str]]:
    return [{"source": "reddit", "evidence": f"evidence {i}"} for i in range(count)]


class ActionResearchM1Tests(unittest.TestCase):
    def test_new_intent_maps_to_create_new_page(self) -> None:
        job = {
            "research_type": "NEW_INTENT_RESEARCH",
            "source_action": "RESEARCH_NEW_INTENT",
            "action_context": {
                "clusterKey": "NEW_INTENT",
                "clusterQueries": ["mortal shell 2 new intent"],
            },
        }
        result = {
            "recommendation": {"action": "NEW_CONTENT", "reason": "independent intent"},
            "evidence": evidence(5),
            "review_summary": "independent intent",
        }
        decision = rjr.build_content_decision(job, result)
        self.assertEqual(decision["primary_decision"], "CREATE_NEW_PAGE")
        self.assertEqual(decision["target_queries"], ["mortal shell 2 new intent"])
        self.assertEqual(decision["confidence"], "HIGH")

    def test_page_optimization_consumes_page_context(self) -> None:
        job = {
            "research_type": "PAGE_OPTIMIZATION_RESEARCH",
            "source_action": "OPTIMIZE_EXISTING",
            "action_context": {
                "pagePath": "/mortal-shell-ii/gloombound-flame/",
                "pageImpressions": 158,
                "clusterQueries": [
                    "gloombound flame",
                    "light extinguished lantern",
                    "lantern",
                ],
                "clusters": [{"queries": ["mortal shell 2 düstergebundene flamme"]}],
            },
        }
        result = {
            "recommendation": {"action": "EXPAND_EXISTING", "reason": "coverage gaps"},
            "content_gaps": [{"discovered_topic": "Lantern use", "player_question": "How do I use it?"}],
            "evidence": evidence(5),
            "review_summary": "coverage gaps",
        }
        decision = rjr.build_content_decision(job, result)
        self.assertEqual(decision["primary_decision"], "EXPAND_EXISTING")
        self.assertIn("ADD_FAQ", decision["secondary_actions"])
        self.assertIn("ADD_STEPS", decision["secondary_actions"])
        self.assertEqual(len(decision["target_queries"]), 4)

    def test_cannibalization_only_produces_decision(self) -> None:
        job = {
            "research_type": "CANNIBALIZATION_RESEARCH",
            "source_action": "CANNIBALIZATION",
            "action_context": {"competingPages": [{"page": "/a/"}, {"page": "/b/"}]},
        }
        decision = rjr.build_content_decision(
            job,
            {"recommendation": {"action": "EXPAND_EXISTING"}, "evidence": evidence(5)},
        )
        self.assertEqual(decision["primary_decision"], "KEEP_BOTH")
        self.assertIn("2 competing page signals", decision["decision_reason"])

    def test_callback_carries_structured_decision_for_action_job(self) -> None:
        body = rjr.build_callback_body(
            {
                "job_id": "ms2-gloom",
                "research_type": "PAGE_OPTIMIZATION_RESEARCH",
                "status": "REVIEW",
                "recommendation": {"action": "EXPAND_EXISTING"},
                "evidence_count": 5,
            },
            job={"research_type": "PAGE_OPTIMIZATION_RESEARCH", "action_context": {}},
            result={"recommendation": {"action": "EXPAND_EXISTING"}, "evidence": evidence(5)},
        )
        self.assertEqual(body["research_type"], "PAGE_OPTIMIZATION_RESEARCH")
        self.assertEqual(body["content_decision"]["primary_decision"], "EXPAND_EXISTING")


if __name__ == "__main__":
    unittest.main()
