import unittest
from unittest.mock import patch

import research_job_runner as job_runner
import research_runner as rr


class ActionResearchTopicBindingTests(unittest.TestCase):
    def setUp(self):
        self.previous_type = rr._ACTIVE_RESEARCH_TYPE
        rr.reset_filter_log()

    def tearDown(self):
        rr._ACTIVE_RESEARCH_TYPE = self.previous_type

    def use_page_optimization(self):
        rr._ACTIVE_RESEARCH_TYPE = "PAGE_OPTIMIZATION_RESEARCH"

    def test_page_optimization_queries_do_not_inject_beta_terms(self):
        self.use_page_optimization()
        queries = rr.build_search_queries(
            "Mortal Shell II",
            "Gloombound Flame",
            "light extinguished lantern mortal shell 2",
            ["mortal shell 2 lantern", "mortal shell 2 night mode"],
        )
        joined = " | ".join(queries).lower()
        self.assertIn("gloombound flame", joined)
        self.assertIn("light extinguished lantern", joined)
        for forbidden in ("beta save", "beta rewards", "progress carry over", "save transfer", "open beta bonuses"):
            self.assertNotIn(forbidden, joined)
        terms = rr.core_terms_for("Mortal Shell II", "Gloombound Flame")
        self.assertNotIn("carry over", terms)
        self.assertNotIn("flayed harbinger", terms)

    def test_gloombound_keeps_topic_evidence_and_filters_beta_evidence(self):
        self.use_page_optimization()
        beta_item, beta_reason = rr.evidence_item(
            "reddit",
            "Mortal Shell II beta rewards",
            "https://reddit.com/r/mortalshell/comments/1/beta",
            "Mortal Shell II beta rewards unlock the Flayed Harbinger after 30 minutes of beta progress; Night Mode is not part of this reward discussion.",
            "Mortal Shell II",
            "Gloombound Flame",
            source_scoped=True,
            match_on_excerpt=True,
            source_query="light extinguished lantern mortal shell 2",
            related_queries=["mortal shell 2 lantern", "mortal shell 2 night mode"],
        )
        self.assertIsNone(beta_item)
        self.assertEqual(beta_reason, "topic_not_relevant")

        relevant_item, relevant_reason = rr.evidence_item(
            "reddit",
            "Gloombound Flame lantern",
            "https://reddit.com/r/mortalshell/comments/1/lantern",
            "In Mortal Shell II, the Gloombound Flame lights the extinguished lantern and changes how Night Mode works.",
            "Mortal Shell II",
            "Gloombound Flame",
            source_scoped=True,
            match_on_excerpt=True,
            source_query="light extinguished lantern mortal shell 2",
            related_queries=["mortal shell 2 lantern", "mortal shell 2 night mode"],
        )
        self.assertIsNotNone(relevant_item, relevant_reason)

    def test_slayer_filters_beta_carry_over_and_keeps_direct_evidence(self):
        self.use_page_optimization()
        beta_item, _ = rr.evidence_item(
            "steam",
            "Mortal Shell II beta progress",
            "https://steamcommunity.com/app/123/discussions/0/1/",
            "Mortal Shell II beta progress and Flayed Harbinger rewards carry over to the full game save.",
            "Mortal Shell II",
            "Slayer Seal",
            source_scoped=True,
            match_on_excerpt=True,
            source_query="slayer seal mortal shell 2",
            related_queries=["call forth the night", "mortal shell 2 difficulty"],
        )
        self.assertIsNone(beta_item)

        relevant_item, relevant_reason = rr.evidence_item(
            "steam",
            "Slayer Seal difficulty",
            "https://steamcommunity.com/app/123/discussions/0/2/",
            "Mortal Shell II Slayer Seal changes the difficulty when players call forth the night, and the seal is explained here.",
            "Mortal Shell II",
            "Slayer Seal",
            source_scoped=True,
            match_on_excerpt=True,
            source_query="slayer seal mortal shell 2",
            related_queries=["call forth the night", "mortal shell 2 difficulty"],
        )
        self.assertIsNotNone(relevant_item, relevant_reason)

    @patch.object(rr, "http_get", side_effect=RuntimeError("offline"))
    def test_action_page_fetch_failure_is_empty_not_beta_fallback(self, _http_get):
        page, status = rr.fetch_existing_page(
            "/mortal-shell-ii/gloombound-flame/",
            "Mortal Shell II",
            "PAGE_OPTIMIZATION_RESEARCH",
            return_status=True,
        )
        self.assertEqual((page, status), ("", "FAILED"))

        legacy_page, legacy_status = rr.fetch_existing_page(
            "/mortal-shell-ii/beta-progress-carry-over/",
            "Mortal Shell II",
            "",
            return_status=True,
        )
        self.assertIn("carry over", legacy_page.lower())
        self.assertEqual(legacy_status, "FALLBACK_BETA")

    def test_page_optimization_safety_requires_topic_specific_evidence(self):
        result = rr.recommend(
            [],
            "Gloombound Flame lantern Night Mode",
            8,
            "Mortal Shell II",
            "PAGE_OPTIMIZATION_RESEARCH",
            topic_relevant_evidence_n=2,
        )
        self.assertEqual(result["action"], "WATCH")
        self.assertEqual(result["reason"], "Insufficient topic-specific evidence")

    def test_content_decision_confidence_is_low_for_insufficient_topic_evidence(self):
        job = {
            "research_type": "PAGE_OPTIMIZATION_RESEARCH",
            "source_action": "OPTIMIZE_EXISTING",
            "action_context": {"clusterQueries": ["slayer seal"]},
        }
        result = {
            "recommendation": {"action": "EXPAND_EXISTING", "reason": "gap"},
            "evidence": [{"evidence": str(i)} for i in range(5)],
            "topic_relevant_evidence_count": 2,
            "content_gaps": [],
            "review_summary": "gap",
        }
        decision = job_runner.build_content_decision(job, result)
        self.assertEqual(decision["primary_decision"], "EXPAND_EXISTING")
        self.assertEqual(decision["confidence"], "LOW")


if __name__ == "__main__":
    unittest.main()
