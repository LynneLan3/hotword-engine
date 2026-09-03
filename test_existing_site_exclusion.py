#!/usr/bin/env python3
"""Regression tests for Existing Site Exclusion + today-action pipeline."""

from __future__ import annotations

import json
import unittest
from datetime import date
from pathlib import Path

import build_eligibility
import existing_site_exclusion as exclusion
import today_action_pipeline as pipeline

ROOT = Path(__file__).resolve().parent
FIXTURES = ROOT / "tests" / "fixtures" / "existing_site_exclusion"
REGISTRY_SITES = ROOT.parent / "hotword-control-center" / "registry" / "sites.yaml"
REGISTRY_GAMES = ROOT.parent / "hotword-control-center" / "registry" / "games.yaml"


def _snapshot() -> dict:
    return json.loads((FIXTURES / "2026-09-03-existing-sites.json").read_text(encoding="utf-8"))


def _raw_candidates() -> list[dict]:
    return json.loads((FIXTURES / "2026-09-03-raw-candidates.json").read_text(encoding="utf-8"))[
        "candidates"
    ]


def _index() -> exclusion.ExistingSiteIndex:
    snapshot = _snapshot()
    return exclusion.load_existing_site_index(
        registry_sites_path=REGISTRY_SITES,
        registry_games_path=REGISTRY_GAMES,
        gsc_rows=snapshot["gscSiteRows"],
        site_pool_rows=snapshot["steamSitePoolRows"],
    )


class ExistingSiteExclusionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not REGISTRY_SITES.exists():
            raise unittest.SkipTest("hotword-control-center registry/sites.yaml unavailable")
        cls.index = _index()

    def test_sucker_already_built_despite_site_pool_gap(self) -> None:
        candidate = {
            "steam_app_id": "3848900",
            "game_name": "Sucker for Love: Crush Landing",
            "candidate_state": {"decision": "BUILD"},
        }
        result = exclusion.evaluate_existing_site(candidate, self.index)
        self.assertTrue(result["existingSite"])
        self.assertFalse(result["eligibleForNewSite"])
        self.assertEqual(result["reconciled_decision"], exclusion.ALREADY_BUILT)
        self.assertIn(exclusion.SOURCE_REGISTRY, result["existingSiteSource"])
        self.assertIn(exclusion.SOURCE_GSC, result["existingSiteSource"])
        self.assertNotIn(exclusion.SOURCE_SITE_POOL, result["existingSiteSource"])
        self.assertTrue(result["stateSyncGap"])
        self.assertEqual(result["existingSiteStatus"], "PRODUCTION")
        self.assertEqual(result["next_action"], "GSC monitoring / create-guide")

    def test_scarlet_preview_ready_blocks_build(self) -> None:
        candidate = {
            "steam_app_id": "4513480",
            "game_name": "Scarlet Skips",
            "candidate_state": {"decision": "BUILD"},
        }
        result = exclusion.evaluate_existing_site(candidate, self.index)
        self.assertTrue(result["existingSite"])
        self.assertEqual(result["reconciled_decision"], exclusion.ALREADY_BUILT)
        self.assertEqual(result["existingSiteStatus"], "PREVIEW_READY")
        self.assertEqual(result["next_action"], "audit/publish")

    def test_shipshaper_not_excluded(self) -> None:
        candidate = {
            "steam_app_id": "4339280",
            "game_name": "ShipShaper: Falconeer Chronicles",
            "candidate_state": {"decision": ""},
        }
        result = exclusion.evaluate_existing_site(candidate, self.index)
        self.assertFalse(result["existingSite"])
        self.assertTrue(result["eligibleForNewSite"])

    def test_steam_app_id_beats_name_and_forbids_name_only_when_present(self) -> None:
        # Same name as BOMBANANA registry/site pool, but a different App ID must not
        # be excluded by string matching when an App ID is present.
        candidate = {
            "steam_app_id": "9990001",
            "game_name": "BOMBANANA!",
            "candidate_state": {"decision": "BUILD"},
        }
        result = exclusion.evaluate_existing_site(candidate, self.index)
        self.assertFalse(result["existingSite"])

    def test_bombanana_punctuation_name_match_without_app_id(self) -> None:
        candidate = {
            "steam_app_id": "",
            "game_name": "BOMBANANA",
            "candidate_state": {"decision": "BUILD"},
        }
        result = exclusion.evaluate_existing_site(candidate, self.index)
        self.assertTrue(result["existingSite"])
        self.assertEqual(result["reconciled_decision"], exclusion.ALREADY_BUILT)

    def test_fixture_does_not_block_real_game(self) -> None:
        candidate = {
            "steam_app_id": "1111111",
            "game_name": "Real Game Near Fixture",
        }
        result = exclusion.evaluate_existing_site(candidate, self.index)
        self.assertFalse(result["existingSite"])

    def test_normalize_handles_colon_trademark_and_bang(self) -> None:
        self.assertEqual(
            exclusion.normalize_game_name("Sucker for Love: Crush Landing™"),
            exclusion.normalize_game_name("Sucker for Love Crush Landing"),
        )
        self.assertEqual(
            exclusion.compact_identity("BOMBANANA!"),
            exclusion.compact_identity("BOMBANANA"),
        )


class TodayActionPipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not REGISTRY_SITES.exists():
            raise unittest.SkipTest("hotword-control-center registry/sites.yaml unavailable")
        cls.index = _index()

    def test_pipeline_excludes_built_sites_and_allows_shipshaper_research(self) -> None:
        result = pipeline.run_today_action_pipeline(
            _raw_candidates(),
            existing_site_index=self.index,
            today=date(2026, 9, 3),
        )
        summary = result["summary"]
        excluded_ids = {row["steam_app_id"] for row in result["existing_sites_excluded"]}
        self.assertIn("3848900", excluded_ids)
        self.assertIn("4513480", excluded_ids)
        self.assertNotIn("4339280", excluded_ids)

        build_ids = {row["steam_app_id"] for row in result["build_eligible"]}
        self.assertNotIn("3848900", build_ids)
        self.assertNotIn("4513480", build_ids)

        remaining_ids = {row["steam_app_id"] for row in result["remaining_candidates"]}
        self.assertIn("4339280", remaining_ids)
        self.assertIn("1111111", remaining_ids)

        already = {row["steam_app_id"]: row for row in result["already_built"]}
        self.assertEqual(already["3848900"]["action_type"], exclusion.ALREADY_BUILT)
        self.assertEqual(already["4513480"]["action_type"], exclusion.ALREADY_BUILT)
        self.assertTrue(already["3848900"]["stateSyncGap"])

        self.assertEqual(summary["ExistingSitesExcluded"], len(result["existing_sites_excluded"]))
        self.assertIn("RawCandidates", summary)
        self.assertIn("NoBuildToday", summary)
        self.assertIn("BuildEligible", summary)
        self.assertIn("TopCandidate", summary)

    def test_no_build_today_when_nothing_eligible(self) -> None:
        only_built = [
            {
                "steam_app_id": "3848900",
                "game_name": "Sucker for Love: Crush Landing",
                "candidate_state": {"decision": "BUILD"},
                "steam_signals": {"first_round_type": "🔥 趋势候选", "followers_gain_7d": 9999},
            }
        ]
        result = pipeline.run_today_action_pipeline(
            only_built,
            existing_site_index=self.index,
            today=date(2026, 9, 3),
        )
        self.assertTrue(result["no_build_today"])
        self.assertEqual(result["no_build_today_code"], build_eligibility.NO_BUILD_TODAY)
        self.assertEqual(result["summary"]["BuildEligible"], 0)
        self.assertIsNone(result["summary"]["TopCandidate"])


class BuildEligibilityFreshnessTests(unittest.TestCase):
    def test_stale_research_blocks_build(self) -> None:
        candidate = {
            "steam_app_id": "4339280",
            "game_name": "ShipShaper: Falconeer Chronicles",
            "steam_signals": {
                "first_round_type": "🔥 趋势候选",
                "release_date": "2026-09-02",
                "followers_gain_7d": 651,
            },
            "research_cycle_date": "2026-08-20",
            "manual_signals": {"trends_result": "强", "keyword_opportunity": "有"},
            "research_result": {
                "job_id": "x",
                "steam_app_id": "4339280",
                "game_name": "ShipShaper: Falconeer Chronicles",
                "steam_signals": {"first_round_type": "🔥 趋势候选"},
                "manual_signals": {"trends_result": "强", "keyword_opportunity": "有"},
                "social": {
                    "status": "AVAILABLE",
                    "actionable_cluster_count": 2,
                    "evidence_count": 8,
                },
                "serp": {
                    "status": "AVAILABLE",
                    "checked_at": "2026-08-20",
                    "competition_summary": {"signals": ["LOW_GUIDE_DENSITY"]},
                },
            },
        }
        freshness = build_eligibility.assess_research_freshness(
            candidate, today=date(2026, 9, 3)
        )
        self.assertTrue(freshness["research_stale"])
        eligibility = build_eligibility.assess_build_eligibility(
            candidate, today=date(2026, 9, 3)
        )
        self.assertFalse(eligibility["eligibleForBuild"])
        self.assertIn(build_eligibility.RESEARCH_STALE, eligibility["blocking_reasons"])

    def test_steam_momentum_alone_does_not_force_build(self) -> None:
        candidate = {
            "steam_app_id": "4339280",
            "game_name": "ShipShaper: Falconeer Chronicles",
            "steam_signals": {
                "first_round_type": "🔥 趋势候选",
                "followers_gain_7d": 5000,
                "release_date": "2026-09-02",
            },
            "manual_signals": {"trends_result": "弱", "keyword_opportunity": "无"},
            "research_result": {
                "job_id": "x",
                "steam_app_id": "4339280",
                "game_name": "ShipShaper: Falconeer Chronicles",
                "steam_signals": {"first_round_type": "🔥 趋势候选"},
                "manual_signals": {"trends_result": "弱", "keyword_opportunity": "无"},
                "social": {
                    "status": "AVAILABLE",
                    "actionable_cluster_count": 0,
                    "evidence_count": 1,
                },
                "serp": {
                    "status": "AVAILABLE",
                    "competition_summary": {"signals": ["HIGH_GUIDE_DENSITY"]},
                },
            },
        }
        eligibility = build_eligibility.assess_build_eligibility(
            candidate, today=date(2026, 9, 3)
        )
        self.assertFalse(eligibility["eligibleForBuild"])


class LiveSourceGuardTests(unittest.TestCase):
    def test_production_loader_rejects_fixture_paths(self) -> None:
        import existing_site_live_sources as live

        with self.assertRaises(ValueError):
            live.assert_production_source_path(
                FIXTURES / "2026-09-03-existing-sites.json",
                label="GSC snapshot",
            )

    def test_live_registry_marks_sucker_gsc_and_scarlet_registry(self) -> None:
        import existing_site_live_sources as live

        index, meta = live.load_production_existing_site_index()
        self.assertEqual(meta["mode"], "live_production")
        self.assertNotIn("fixtures", json.dumps(meta))
        sucker = exclusion.evaluate_existing_site(
            {
                "steam_app_id": "3848900",
                "game_name": "Sucker for Love: Crush Landing",
                "candidate_state": {"decision": "BUILD"},
            },
            index,
        )
        self.assertTrue(sucker["existingSite"])
        self.assertIn(exclusion.SOURCE_REGISTRY, sucker["existingSiteSource"])
        self.assertIn(exclusion.SOURCE_GSC, sucker["existingSiteSource"])
        self.assertNotIn(exclusion.SOURCE_SITE_POOL, sucker["existingSiteSource"])
        self.assertTrue(sucker["stateSyncGap"])
        self.assertEqual(sucker["reconciled_decision"], exclusion.ALREADY_BUILT)

        scarlet = exclusion.evaluate_existing_site(
            {
                "steam_app_id": "4513480",
                "game_name": "Scarlet Skips",
                "candidate_state": {"decision": "BUILD"},
            },
            index,
        )
        self.assertTrue(scarlet["existingSite"])
        self.assertEqual(scarlet["reconciled_decision"], exclusion.ALREADY_BUILT)

        ship = exclusion.evaluate_existing_site(
            {
                "steam_app_id": "4339280",
                "game_name": "ShipShaper: Falconeer Chronicles",
            },
            index,
        )
        self.assertFalse(ship["existingSite"])


if __name__ == "__main__":
    unittest.main()
