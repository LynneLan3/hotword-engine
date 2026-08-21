"""Tests for game_wide_cross_run cross-run dedup."""

from __future__ import annotations

import unittest

from game_wide_cross_run import (
    _evidence_key,
    _normalize_url,
    accumulate_runs,
    aggregate_intent_families,
)


class EvidenceKeyTests(unittest.TestCase):
    def test_same_url_same_provider_same_key(self):
        k1 = _evidence_key("youtube", "https://youtu.be/abc?si=123")
        k2 = _evidence_key("youtube", "https://youtu.be/abc?si=456")
        self.assertEqual(k1, k2)

    def test_different_provider_different_key(self):
        k1 = _evidence_key("youtube", "https://youtu.be/abc")
        k2 = _evidence_key("reddit", "https://youtu.be/abc")
        self.assertNotEqual(k1, k2)

    def test_trailing_slash_normalized(self):
        k1 = _evidence_key("reddit", "https://reddit.com/r/x/123/")
        k2 = _evidence_key("reddit", "https://reddit.com/r/x/123")
        self.assertEqual(k1, k2)


class NormalizeUrlTests(unittest.TestCase):
    def test_strip_query(self):
        self.assertEqual(_normalize_url("https://x.com/a?b=1&c=2"), "https://x.com/a")

    def test_strip_trailing_slash(self):
        self.assertEqual(_normalize_url("https://x.com/a/"), "https://x.com/a")

    def test_youtube_video_ids_are_distinct(self):
        self.assertNotEqual(
            _normalize_url("https://youtube.com/watch?v=A"),
            _normalize_url("https://youtube.com/watch?v=B"),
        )

    def test_youtube_tracking_params_are_ignored(self):
        self.assertEqual(
            _normalize_url("https://youtube.com/watch?v=A&utm_source=x"),
            _normalize_url("https://youtube.com/watch?v=A"),
        )

    def test_short_youtube_url_matches_watch_url(self):
        self.assertEqual(
            _normalize_url("https://youtu.be/A?si=tracking"),
            _normalize_url("https://youtube.com/watch?v=A"),
        )


class AccumulateRunsTests(unittest.TestCase):
    def _make_artifact(self, clusters):
        return {"clusters": clusters}

    def _make_cluster(self, topic_key, evidence, decision="WATCH", gsc="ABSENT", page="NONE"):
        return {
            "topic_key": topic_key,
            "topic": topic_key,
            "intent": "TASK",
            "decision": decision,
            "evidence_count": len(evidence),
            "providers": list({e["provider"] for e in evidence}),
            "source_families": list({e.get("source_family", "VIDEO") for e in evidence}),
            "gsc_match": {"status": gsc},
            "existing_page_match": {"status": page},
            "evidence": evidence,
        }

    def test_same_url_3_runs_unique_1(self):
        """Same URL across 3 runs → unique_evidence_count = 1."""
        ev = {"provider": "steam", "url": "https://steam.com/thread/1", "title": "help"}
        art1 = self._make_artifact([self._make_cluster("t1", [ev])])
        art2 = self._make_artifact([self._make_cluster("t1", [ev])])
        art3 = self._make_artifact([self._make_cluster("t1", [ev])])

        result = accumulate_runs([art1, art2, art3])
        self.assertEqual(len(result), 1)
        c = result[0]
        self.assertEqual(c["runs"], 3)
        self.assertEqual(c["raw_occurrences"], 3)
        self.assertEqual(c["unique_evidence_count"], 1)
        self.assertEqual(c["unique_urls_count"], 1)

    def test_different_urls_accumulate(self):
        """Different URLs → unique_evidence_count = number of URLs."""
        ev1 = {"provider": "youtube", "url": "https://youtu.be/abc", "title": "a"}
        ev2 = {"provider": "youtube", "url": "https://youtu.be/def", "title": "b"}
        art1 = self._make_artifact([self._make_cluster("t1", [ev1])])
        art2 = self._make_artifact([self._make_cluster("t1", [ev2])])

        result = accumulate_runs([art1, art2])
        c = result[0]
        self.assertEqual(c["runs"], 2)
        self.assertEqual(c["unique_evidence_count"], 2)
        self.assertEqual(c["unique_urls_count"], 2)

    def test_run_count_preserved(self):
        """run_count reflects how many runs the cluster appeared in."""
        ev = {"provider": "reddit", "url": "https://reddit.com/r/x/1", "title": "q"}
        art1 = self._make_artifact([self._make_cluster("t1", [ev])])
        art2 = self._make_artifact([self._make_cluster("t1", [ev])])

        result = accumulate_runs([art1, art2])
        c = result[0]
        self.assertEqual(c["runs"], 2)
        self.assertEqual(c["raw_occurrences"], 2)
        self.assertEqual(c["unique_evidence_count"], 1)

    def test_single_run_unchanged(self):
        """Single run: unique = raw, no dedup effect."""
        ev1 = {"provider": "youtube", "url": "https://youtu.be/abc", "title": "a"}
        ev2 = {"provider": "youtube", "url": "https://youtu.be/def", "title": "b"}
        art = self._make_artifact([self._make_cluster("t1", [ev1, ev2])])

        result = accumulate_runs([art])
        c = result[0]
        self.assertEqual(c["runs"], 1)
        self.assertEqual(c["unique_evidence_count"], 2)

    def test_mixed_unique_and_repeat(self):
        """Some URLs repeat, some are new."""
        ev1 = {"provider": "youtube", "url": "https://youtu.be/abc", "title": "a"}
        ev2 = {"provider": "youtube", "url": "https://youtu.be/def", "title": "b"}
        art1 = self._make_artifact([self._make_cluster("t1", [ev1])])
        art2 = self._make_artifact([self._make_cluster("t1", [ev1, ev2])])

        result = accumulate_runs([art1, art2])
        c = result[0]
        self.assertEqual(c["runs"], 2)
        self.assertEqual(c["raw_occurrences"], 3)  # ev1 twice + ev2 once
        self.assertEqual(c["unique_evidence_count"], 2)  # ev1 + ev2


class FamilyAggregationTests(unittest.TestCase):
    def test_weapon_topics_aggregate_and_keep_children(self):
        evidence = {
            "provider": "youtube",
            "source_family": "VIDEO",
            "author": "creator",
            "url": "https://youtu.be/A",
            "title": "weapon guide",
        }
        clusters = [
            {
                "topic_key": "upgrade-weapons",
                "topic": "How to Upgrade Weapons",
                "intent_family": "WEAPON_PROGRESSION",
                "evidence": [evidence],
            },
            {
                "topic_key": "all-weapons",
                "topic": "Fastest Path to All Weapons",
                "intent_family": "WEAPON_PROGRESSION",
                "evidence": [dict(evidence)],
            },
        ]
        family = aggregate_intent_families(accumulate_runs([{"clusters": clusters}]))[0]
        self.assertEqual(family["intent_family"], "WEAPON_PROGRESSION")
        self.assertEqual(family["child_topics"], ["all-weapons", "upgrade-weapons"])
        self.assertEqual(family["unique_evidence_count"], 1)
        self.assertEqual(family["unique_authors_count"], 1)
        self.assertEqual(family["unique_source_families"], ["VIDEO"])

    def test_empty_metadata_is_not_counted(self):
        cluster = {
            "topic_key": "empty",
            "topic": "Empty metadata",
            "intent_family": "OTHER",
            "evidence": [{"provider": "", "source_family": "", "author": "", "url": "https://x.com/a"}],
        }
        family = aggregate_intent_families(accumulate_runs([{"clusters": [cluster]}]))[0]
        self.assertEqual(family["unique_providers"], [])
        self.assertEqual(family["unique_source_families"], [])
        self.assertEqual(family["unique_authors_count"], 0)


if __name__ == "__main__":
    unittest.main()
