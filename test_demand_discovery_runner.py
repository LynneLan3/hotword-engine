#!/usr/bin/env python3
"""Deterministic tests for R2B demand discovery loader + runner."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import demand_discovery_runner as ddr
import fetch_pending_demand_discovery_jobs as fddj


def _job() -> dict:
    return {
        "job_id": "demand-agefield-classes-query-blind-spot-20260818",
        "research_type": "DEMAND_DISCOVERY",
        "site": "Agefield High",
        "game": "Agefield High: Rock the School",
        "radar_id": "agefield|/agefield-high-rock-the-school/classes/|QUERY_BLIND_SPOT",
        "trigger_type": "QUERY_BLIND_SPOT",
        "anchor_page": "/agefield-high-rock-the-school/classes/",
        "source_signal_summary": "Page 7D has traffic but visible query coverage is low.",
        "discovery_scope": {"page_topic": "classes"},
        "seed_terms": [
            "Agefield High: Rock the School",
            "Agefield High: Rock the School classes",
        ],
        "source_families_requested": ["COMMUNITY", "VIDEO"],
        "discovery_cycle_date": "2026-08-18",
        "created_at": "2026-08-18T10:00:00+08:00",
    }


def _ev(provider: str, title: str, url: str, excerpt: str, question: str = "",
        seed_term: str = "Agefield High: Rock the School",
        seed_role: str = "GAME_WIDE") -> dict:
    st = question or title or excerpt
    return {
        "provider": provider,
        "source_family": ddr.provider_to_family(provider),
        "title": title,
        "url": url,
        "excerpt": excerpt,
        "player_question": question,
        "signal_text": st,
        "seed_term": seed_term,
        "seed_role": seed_role,
        "matched_seed_terms": [seed_term],
        "matched_seed_roles": [seed_role],
        "relevance": 0.8,
        "normalized_signal": ddr.normalize_signal(st),
    }


class LoaderContractTests(unittest.TestCase):
    def test_case1_contract_accepts_r2a_fields_without_legacy_fields(self) -> None:
        job = _job()
        # no topic/source_query/opportunity/recommended_action/existing_page
        parsed = fddj.to_demand_discovery_job(job)
        self.assertEqual(parsed["research_type"], "DEMAND_DISCOVERY")
        self.assertEqual(parsed["job_id"], job["job_id"])
        self.assertEqual(parsed["discovery_cycle_date"], "2026-08-18")
        self.assertNotIn("topic", parsed)
        self.assertNotIn("source_query", parsed)


class ClusterRulesTests(unittest.TestCase):
    def test_case2_reddit_steam_same_topic_is_single_family(self) -> None:
        items = [
            _ev(
                "reddit",
                "German class answers?",
                "https://reddit.com/r/a/comments/1",
                "What are the german class answers?",
                "What are the german class answers?",
            ),
            _ev(
                "steam",
                "German class answers thread",
                "https://steamcommunity.com/app/3562580/discussions/0/100",
                "Need help with german class answers",
                "Need help with german class answers?",
            ),
        ]
        clusters = ddr.cluster_demand_signals(items)
        self.assertEqual(len(clusters), 1)
        c = clusters[0]
        self.assertEqual(c["independent_source_family_count"], 1)
        self.assertEqual(c["source_families"], ["COMMUNITY"])
        self.assertFalse(c["cross_validated"])

    def test_case3_reddit_youtube_same_topic_is_cross_validated(self) -> None:
        items = [
            _ev(
                "reddit",
                "German class answers?",
                "https://reddit.com/r/a/comments/1",
                "What are the german class answers?",
                "What are the german class answers?",
            ),
            _ev(
                "youtube",
                "Agefield High German Class Answers Guide",
                "https://www.youtube.com/watch?v=abc",
                "Agefield High german class answers guide",
            ),
        ]
        clusters = ddr.cluster_demand_signals(items)
        self.assertEqual(len(clusters), 1)
        c = clusters[0]
        self.assertEqual(c["independent_source_family_count"], 2)
        self.assertEqual(c["source_families"], ["COMMUNITY", "VIDEO"])
        self.assertTrue(c["cross_validated"])

    def test_case4_different_topics_must_not_fake_cross_validation(self) -> None:
        items = [
            _ev(
                "reddit",
                "Steam Deck performance",
                "https://reddit.com/r/a/comments/steamdeck",
                "Does it work on steam deck?",
                "Does it work on steam deck?",
            ),
            _ev(
                "youtube",
                "Chloe romance ending guide",
                "https://www.youtube.com/watch?v=romance",
                "Chloe romance ending choices",
            ),
        ]
        clusters = ddr.cluster_demand_signals(items)
        self.assertEqual(len(clusters), 2)
        self.assertEqual(sum(1 for c in clusters if c["cross_validated"]), 0)
        status = ddr.derive_discovery_status(clusters)
        self.assertNotEqual(status, ddr.DISCOVERY_STATUS_CROSS_VALIDATED)

    def test_case5_many_single_family_evidence_never_cross_validated(self) -> None:
        items = []
        for i in range(12):
            items.append(
                _ev(
                    "reddit",
                    "German class answers",
                    f"https://reddit.com/r/a/comments/{i}",
                    "German class answers help",
                    "German class answers help?",
                )
            )
        clusters = ddr.cluster_demand_signals(items)
        self.assertEqual(len(clusters), 1)
        c = clusters[0]
        self.assertEqual(c["evidence_count"], 12)
        self.assertEqual(c["independent_source_family_count"], 1)
        self.assertFalse(c["cross_validated"])

    def test_case6_duplicate_url_across_seeds_should_dedupe(self) -> None:
        url = "https://www.youtube.com/watch?v=dup1"
        a = _ev("youtube", "same video", url, "German class answers video")
        b = dict(a)
        b["seed_term"] = "Agefield High: Rock the School classes"
        deduped = ddr.dedupe_discovery_evidence([a, b])
        self.assertEqual(len(deduped), 1)

    def test_case7_agefield_classes_fixture_cross_validated(self) -> None:
        items = [
            _ev(
                "reddit",
                "What are the German class answers?",
                "https://reddit.com/r/agefield/comments/cls1",
                "What are the german class answers?",
                "What are the german class answers?",
            ),
            _ev(
                "youtube",
                "Agefield High German Class Answers Guide",
                "https://www.youtube.com/watch?v=agefield-classes",
                "Agefield High german class answers guide",
            ),
        ]
        clusters = ddr.cluster_demand_signals(items)
        self.assertEqual(len(clusters), 1)
        self.assertTrue(clusters[0]["cross_validated"])

    def test_case8_agefield_different_topics_must_not_merge(self) -> None:
        items = [
            _ev(
                "reddit",
                "How do classes work?",
                "https://reddit.com/r/agefield/comments/cls2",
                "How do classes work?",
                "How do classes work?",
            ),
            _ev(
                "youtube",
                "Chloe romance ending",
                "https://www.youtube.com/watch?v=chloe-ending",
                "Chloe romance ending guide",
            ),
        ]
        clusters = ddr.cluster_demand_signals(items)
        self.assertEqual(len(clusters), 2)

    def test_case9_discovery_status_mapping(self) -> None:
        self.assertEqual(ddr.derive_discovery_status([]), ddr.DISCOVERY_STATUS_NO_SIGNAL)
        discovered = [
            {
                "cross_validated": False,
                "independent_source_family_count": 1,
            }
        ]
        self.assertEqual(
            ddr.derive_discovery_status(discovered),
            ddr.DISCOVERY_STATUS_DISCOVERED,
        )
        cross = [
            {
                "cross_validated": True,
                "independent_source_family_count": 2,
            }
        ]
        self.assertEqual(
            ddr.derive_discovery_status(cross),
            ddr.DISCOVERY_STATUS_CROSS_VALIDATED,
        )


class AnchorRelevanceGateTests(unittest.TestCase):
    """R2B.1 anchor relevance gate tests."""

    def test_caseA_game_wide_noise_no_signal(self) -> None:
        """Generic game noise for Agefield /classes/ → NO_SIGNAL."""
        job = _job()
        evidence = [
            _ev("reddit", "Agefield trailer", "https://reddit.com/r/a/1", "Agefield trailer"),
            _ev("youtube", "Agefield full walkthrough", "https://youtube.com/watch?v=w1", "Agefield full walkthrough"),
            _ev("steam", "Agefield review", "https://steamcommunity.com/app/3562580/discussions/0/rev", "Agefield review"),
        ]
        anchor = [e for e in evidence if ddr.is_anchor_qualified(e, job)]
        bg = [e for e in evidence if not ddr.is_anchor_qualified(e, job)]
        self.assertEqual(len(anchor), 0)
        self.assertEqual(len(bg), 3)
        clusters = ddr.cluster_demand_signals(anchor)
        self.assertEqual(len(clusters), 0)
        self.assertEqual(ddr.derive_discovery_status(clusters), ddr.DISCOVERY_STATUS_NO_SIGNAL)

    def test_caseB_anchor_seed_hit(self) -> None:
        """Evidence from ANCHOR seed is anchor-qualified."""
        job = _job()
        ev = _ev("reddit", "German class answers", "https://reddit.com/r/a/2",
                 "German class answers", seed_term="Agefield High: Rock the School classes",
                 seed_role="ANCHOR")
        self.assertTrue(ddr.is_anchor_qualified(ev, job))

    def test_caseC_game_wide_with_anchor_token(self) -> None:
        """GAME_WIDE evidence containing anchor token is anchor-qualified."""
        job = _job()
        ev = _ev("reddit", "How do classes work in Agefield High?",
                 "https://reddit.com/r/a/3",
                 "How do classes work in Agefield High?")
        self.assertEqual(ev["matched_seed_roles"], ["GAME_WIDE"])
        self.assertTrue(ddr.is_anchor_qualified(ev, job))

    def test_caseD_dedupe_merges_seed_provenance(self) -> None:
        """Same URL from GAME_WIDE and ANCHOR → merged roles, count=1."""
        url = "https://www.youtube.com/watch?v=merge1"
        a = _ev("youtube", "Agefield classes guide", url, "classes guide",
                seed_term="Agefield High: Rock the School", seed_role="GAME_WIDE")
        b = _ev("youtube", "Agefield classes guide", url, "classes guide",
                seed_term="Agefield High: Rock the School classes", seed_role="ANCHOR")
        deduped = ddr.dedupe_discovery_evidence([a, b])
        self.assertEqual(len(deduped), 1)
        self.assertIn("GAME_WIDE", deduped[0]["matched_seed_roles"])
        self.assertIn("ANCHOR", deduped[0]["matched_seed_roles"])
        job = _job()
        self.assertTrue(ddr.is_anchor_qualified(deduped[0], job))

    def test_caseE_different_clusters_no_fake_cross(self) -> None:
        """Different topics across families must not cross-validate (unchanged R2B rule)."""
        job = _job()
        items = [
            _ev("reddit", "Steam Deck performance", "https://reddit.com/r/a/sd",
                "Does it work on steam deck?", "Does it work on steam deck?",
                seed_role="ANCHOR", seed_term="Agefield High: Rock the School classes"),
            _ev("youtube", "Chloe romance ending guide", "https://youtube.com/watch?v=romance",
                "Chloe romance ending choices",
                seed_role="ANCHOR", seed_term="Agefield High: Rock the School classes"),
        ]
        clusters = ddr.cluster_demand_signals(items)
        self.assertEqual(len(clusters), 2)
        self.assertEqual(sum(1 for c in clusters if c["cross_validated"]), 0)

    def test_caseF_anchor_cross_family_cross_validated(self) -> None:
        """Anchor-qualified evidence from Reddit+YouTube same cluster → CROSS_VALIDATED."""
        job = _job()
        items = [
            _ev("reddit", "German class answers?", "https://reddit.com/r/a/cls1",
                "What are the german class answers?", "What are the german class answers?",
                seed_role="ANCHOR", seed_term="Agefield High: Rock the School classes"),
            _ev("youtube", "Agefield High German Class Answers Guide",
                "https://youtube.com/watch?v=agefield-cls",
                "Agefield High german class answers guide",
                seed_role="ANCHOR", seed_term="Agefield High: Rock the School classes"),
        ]
        for e in items:
            self.assertTrue(ddr.is_anchor_qualified(e, job))
        clusters = ddr.cluster_demand_signals(items)
        self.assertEqual(len(clusters), 1)
        self.assertTrue(clusters[0]["cross_validated"])
        self.assertEqual(ddr.derive_discovery_status(clusters), ddr.DISCOVERY_STATUS_CROSS_VALIDATED)

    def test_caseG_background_cross_family_no_effect(self) -> None:
        """Background evidence across families must not affect discovery_status."""
        job = _job()
        bg_items = [
            _ev("reddit", "Agefield review", "https://reddit.com/r/a/rev", "Agefield review"),
            _ev("youtube", "Agefield walkthrough", "https://youtube.com/watch?v=walk", "Agefield walkthrough"),
        ]
        for e in bg_items:
            self.assertFalse(ddr.is_anchor_qualified(e, job))
        anchor = [e for e in bg_items if ddr.is_anchor_qualified(e, job)]
        clusters = ddr.cluster_demand_signals(anchor)
        self.assertEqual(len(clusters), 0)
        self.assertEqual(ddr.derive_discovery_status(clusters), ddr.DISCOVERY_STATUS_NO_SIGNAL)


class RunnerArtifactTests(unittest.TestCase):
    def test_run_job_writes_local_artifacts_only(self) -> None:
        job = _job()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            job_path = tmp_root / "input" / "demand_discovery_job.json"
            job_path.parent.mkdir(parents=True, exist_ok=True)
            job_path.write_text(__import__("json").dumps(job), encoding="utf-8")

            # Avoid network in this deterministic test.
            original_collect = ddr._collect_for_seed

            def fake_collect(seed_term: str, _jobx: dict, providers: list[str]):
                del seed_term, providers
                ev = [
                    _ev(
                        "reddit",
                        "What are the german class answers?",
                        "https://reddit.com/r/agefield/comments/seed",
                        "What are the german class answers?",
                        "What are the german class answers?",
                    )
                ]
                return ev, {"reddit": 1, "steam": 0, "youtube": 0}, {}

            ddr._collect_for_seed = fake_collect
            try:
                original_root = ddr.ROOT
                ddr.ROOT = tmp_root
                status = ddr.run_job(job_path)
            finally:
                ddr._collect_for_seed = original_collect
                ddr.ROOT = original_root

            self.assertTrue(status["ok"])
            job_dir = tmp_root / "jobs" / job["job_id"]
            self.assertTrue((job_dir / "job.json").exists())
            self.assertTrue((job_dir / "status.json").exists())
            self.assertTrue((job_dir / "demand_discovery_result.json").exists())


if __name__ == "__main__":
    unittest.main()

