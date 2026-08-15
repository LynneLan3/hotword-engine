#!/usr/bin/env python3
"""Local regression tests for cross-game safety in research_runner.

No network. No Google Sheet callback. Does not mutate job artifacts.
"""

from __future__ import annotations

import unittest

import research_runner as rr


MS2_SNIPPET = (
    "Play the Mortal Shell II Open Beta to unlock The Flayed Harbinger. "
    "Players who progress beyond the Marrow Keep unlock prologue skip. "
    "Beta save progress will not carry over in full."
)
MS2_URL = "https://steamcommunity.com/app/2584270/discussions/0/418424007826605250/"


class CrossGameSafetyTests(unittest.TestCase):
    def test_a_approximately_up_rejects_ms2_evidence(self) -> None:
        game = "Approximately Up"
        topic = "ps5"
        source_query = "approximately up ps5"

        item, reason = rr.evidence_item(
            "steam",
            "save transfer to full game",
            MS2_URL,
            MS2_SNIPPET,
            game,
            topic,
            source_scoped=True,
            match_on_excerpt=True,
            source_query=source_query,
        )
        self.assertIsNone(item)
        self.assertEqual(reason, "cross_game")

        # Distinctive markers alone must also fail.
        for marker in (
            "Mortal Shell II open beta rewards",
            "Claim The Flayed Harbinger at launch",
            "Progress beyond the Marrow Keep",
            "see steamcommunity.com/app/2584270/discussions",
        ):
            self.assertTrue(
                rr.is_cross_game_contamination(marker, "", game),
                msg=f"should reject marker: {marker}",
            )

        # Contaminated AU artifact must be scrubbed by the final safety net.
        polluted = [
            {
                "source": "steam",
                "title": "save transfer",
                "url": MS2_URL,
                "evidence": MS2_SNIPPET,
                "excerpt": MS2_SNIPPET[:200],
                "player_question": "",
                "discovered_topic": "prologue_skip",
                "relevance": 0.8,
            }
        ]
        cleaned = rr.filter_final_evidence(polluted, game)
        self.assertEqual(cleaned, [])

        # Steam must not resolve to MS2's app id for AU.
        self.assertEqual(rr.resolve_steam_appids(game), [])

        # Reusing the polluted historical AU artifact must scrub foreign games.
        import argparse

        reused = rr.run(
            argparse.Namespace(
                game=game,
                topic=topic,
                existing_page="/approximately-up/console/",
                steam_appid=None,
                source_query=source_query,
                related_queries=["ps5"],
                out="/tmp/au_reuse_test.json",
                reuse="jobs/au-console-20260814/research_result.json",
            )
        )
        blob = " ".join(
            f"{e.get('title', '')} {e.get('evidence', '')} {e.get('url', '')}"
            for e in reused["evidence"]
        ).lower()
        self.assertEqual(reused["evidence"], [])
        for marker in (
            "mortal shell",
            "flayed harbinger",
            "marrow keep",
            "2584270",
            "the forest",
            "assassin",
        ):
            self.assertNotIn(marker, blob)

    def test_b_ms2_beta_carry_over_still_accepts_on_topic(self) -> None:
        game = "Mortal Shell II"
        topic = "beta save / beta rewards / progress carry-over"
        source_query = "mortal shell 2 beta rewards"

        self.assertEqual(rr.resolve_steam_appids(game), ["2584270"])
        self.assertTrue(rr.is_mortal_shell_ii(game))
        self.assertFalse(rr.is_cross_game_contamination(MS2_SNIPPET, MS2_URL, game))

        item, reason = rr.evidence_item(
            "steam",
            "save transfer to full game",
            MS2_URL,
            MS2_SNIPPET,
            game,
            topic,
            source_scoped=True,
            match_on_excerpt=True,
            source_query=source_query,
        )
        self.assertIsNotNone(item, msg=f"expected MS2 evidence accepted, got reason={reason}")
        assert item is not None
        self.assertGreaterEqual(item["relevance"], 0.42)
        self.assertIn(item["discovered_topic"], {
            "prologue_skip",
            "beta_rewards",
            "full_save_transfer",
            "playtime_threshold",
            "inventory_reset",
            "claim_process",
        })
        self.assertTrue(rr.has_core_topic(MS2_SNIPPET, game, topic, source_query))

    def test_c_unknown_game_skips_steam_without_guessing(self) -> None:
        game = "Totally Unknown Indie Game XYZ"
        topic = "ps5"
        self.assertEqual(rr.resolve_steam_appids(game), [])

        items, counts = rr.collect_steam(game, topic, [])
        self.assertEqual(items, [])
        self.assertEqual(counts["raw"], 0)
        self.assertEqual(counts["valid"], 0)
        self.assertEqual(counts["filtered"].get("no_steam_appid"), 1)
        self.assertEqual(counts["filtered_total"], 1)

        # Explicitly ensure we never invent 2584270 for unknowns.
        self.assertNotIn("2584270", rr.resolve_steam_appids(game))


class TopicBindingTests(unittest.TestCase):
    def test_ms2_terms_not_applied_to_other_games(self) -> None:
        au_terms = rr.core_terms_for(
            "Approximately Up", "ps5", "approximately up ps5", ["ps5"]
        )
        self.assertIn("ps5", au_terms)
        self.assertNotIn("flayed harbinger", au_terms)
        self.assertNotIn("marrow keep", au_terms)

        ms2_terms = rr.core_terms_for(
            "Mortal Shell II",
            "beta save / beta rewards / progress carry-over",
            "mortal shell 2 beta rewards",
        )
        self.assertIn("flayed harbinger", ms2_terms)
        self.assertIn("carry over", ms2_terms)

    def test_reddit_queries_are_game_bound(self) -> None:
        au_q = rr.build_search_queries(
            "Approximately Up", "ps5", "approximately up ps5", ["ps5"]
        )
        blob = " ".join(au_q).lower()
        self.assertIn("approximately up", blob)
        self.assertNotIn("mortal shell", blob)

        ms2_q = rr.build_search_queries(
            "Mortal Shell II",
            "beta save / beta rewards / progress carry-over",
            "mortal shell 2 beta rewards",
        )
        self.assertTrue(any("mortal shell" in q.lower() for q in ms2_q))


if __name__ == "__main__":
    unittest.main()
