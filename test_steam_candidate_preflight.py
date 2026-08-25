"""Offline regression tests for automatic Steam Candidate preflight."""

from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

from steam_candidate_preflight import (
    AUTO_REJECT,
    MANUAL_REVIEW,
    PREFLIGHT_ERROR,
    WATCH,
    run_preflight,
)


def _job(name: str, **state):
    release_date = state.pop("release_date", None)
    steam_signals = {"release_stage": state.pop("release_stage", "Released")}
    if release_date:
        steam_signals["release_date"] = release_date
    return {
        "job_id": f"job-{name.lower().replace(' ', '-')}",
        "steam_app_id": "12345",
        "game_name": name,
        "research_cycle_date": "2026-08-25",
        "steam_signals": steam_signals,
        "candidate_state": state,
    }


def _autocomplete(name: str, *, guide: bool = True):
    def probe(_source: str, query: str):
        items = [{"text": f"{name} guide"}] if guide else []
        return {"status": "BEST_EFFORT", "items": items}

    return probe


def _serp(name: str, domains: list[str], *, irrelevant: int = 0, guide_count: int | None = None):
    def probe(query: str):
        count = guide_count if guide_count is not None else len(domains)
        items = [
            {
                "position": index + 1,
                "title": f"{name} guide {index + 1}",
                "domain": domain,
                "url": f"https://{domain}/{index + 1}",
                "snippet": f"{name} walkthrough and tips",
            }
            for index, domain in enumerate(domains[:count])
        ]
        items.extend(
            {
                "position": len(items) + 1,
                "title": "Water fire extinguisher ordinary language result",
                "domain": "example.com",
                "url": "https://example.com/result",
                "snippet": "water and fire extinguisher information",
            }
            for _ in range(irrelevant)
        )
        return {"status": "SUPPORTED", "items": items}

    return probe


class PreflightRegressionTests(unittest.TestCase):
    def test_persisted_reject_suppresses_before_network(self):
        calls = []

        def forbidden(*_args):
            calls.append(True)
            raise AssertionError("persisted REJECT must not call providers")

        result = run_preflight(_job("Rejected", decision="REJECT"), autocomplete_fn=forbidden, serp_fn=forbidden)
        self.assertEqual(result["preflight_verdict"], AUTO_REJECT)
        self.assertEqual(result["preflight_reason"], "PERSISTED_DECISION_REJECT")
        self.assertEqual(calls, [])

    def test_build_and_watch_before_date_are_suppressed(self):
        for status, expected in (("BUILD", AUTO_REJECT), ("WATCH", WATCH)):
            calls = []

            def forbidden(*_args):
                calls.append(True)
                raise AssertionError("persisted state must not call providers")

            result = run_preflight(
                _job("State", decision=status, next_recheck_date="2026-09-03"),
                autocomplete_fn=forbidden,
                serp_fn=forbidden,
                now=date(2026, 8, 25),
            )
            self.assertEqual(result["preflight_verdict"], expected)
            self.assertEqual(calls, [])

    def test_due_watch_reenters_and_one_site_low_density_is_manual(self):
        result = run_preflight(
            _job(
                "BRIGANDINE ABYSS",
                decision="WATCH",
                next_recheck_date="2026-08-25",
                current_7d_gain=1300,
                last_checked_7d_gain=1000,
            ),
            autocomplete_fn=_autocomplete("BRIGANDINE ABYSS"),
            serp_fn=_serp("BRIGANDINE ABYSS", ["brigandineabyss.com"]),
            now=date(2026, 8, 25),
        )
        self.assertEqual(result["preflight_verdict"], MANUAL_REVIEW)
        self.assertEqual(result["next_action"], "Google Trends")

    def test_due_watch_below_growth_threshold_continues_without_provider(self):
        calls = []

        def forbidden(*_args):
            calls.append(True)
            raise AssertionError("WATCH without new signal must not call providers")

        result = run_preflight(
            _job(
                "The Sinking City 2",
                decision="WATCH",
                next_recheck_date="2026-08-24",
                current_7d_gain=6986,
                last_checked_7d_gain=6289,
                trends_result="中",
                keyword_opportunity="未检查",
            ),
            autocomplete_fn=forbidden,
            serp_fn=forbidden,
            now=date(2026, 8, 25),
        )
        self.assertEqual(result["preflight_verdict"], WATCH)
        self.assertEqual(result["preflight_reason"], "WATCH_CONTINUE_NO_NEW_SIGNAL")
        self.assertFalse(result["eligible_for_today_action"])
        self.assertEqual(calls, [])

    def test_due_watch_growth_at_threshold_allows_recheck(self):
        result = run_preflight(
            _job(
                "Growth Candidate",
                decision="WATCH",
                next_recheck_date="2026-08-24",
                current_7d_gain=1300,
                last_checked_7d_gain=1000,
            ),
            autocomplete_fn=_autocomplete("Growth Candidate"),
            serp_fn=_serp("Growth Candidate", []),
            now=date(2026, 8, 25),
        )
        self.assertEqual(result["preflight_verdict"], MANUAL_REVIEW)
        self.assertEqual(result["next_action"], "Google Trends")

    def test_due_watch_new_external_signal_allows_recheck(self):
        result = run_preflight(
            _job(
                "External Signal Candidate",
                decision="WATCH",
                next_recheck_date="2026-08-24",
                current_7d_gain=1050,
                last_checked_7d_gain=1000,
                external_signal="GOOGLE_TRENDS",
                external_signal_is_new=True,
            ),
            autocomplete_fn=_autocomplete("External Signal Candidate"),
            serp_fn=_serp("External Signal Candidate", []),
            now=date(2026, 8, 25),
        )
        self.assertEqual(result["preflight_verdict"], MANUAL_REVIEW)
        self.assertEqual(result["next_action"], "Google Trends")

    def test_existing_weak_trends_without_signal_becomes_watch_without_provider(self):
        calls = []

        def forbidden(*_args):
            calls.append(True)
            raise AssertionError("weak existing Trends must not call providers")

        result = run_preflight(
            _job(
                "Brigador Killers",
                trends_result="弱",
                keyword_opportunity="未检查",
            ),
            autocomplete_fn=forbidden,
            serp_fn=forbidden,
        )
        self.assertEqual(result["preflight_verdict"], WATCH)
        self.assertEqual(result["preflight_reason"], "EXISTING_MANUAL_EVIDENCE_WATCH")
        self.assertIsNone(result["next_action"])
        self.assertEqual(calls, [])

    def test_existing_trends_resumes_next_incomplete_stage_without_provider(self):
        calls = []

        def forbidden(*_args):
            calls.append(True)
            raise AssertionError("existing Trends must resume manually without providers")

        result = run_preflight(
            _job(
                "Entropy",
                trends_result="中",
                serp_competition="高",
                keyword_opportunity="无",
            ),
            autocomplete_fn=forbidden,
            serp_fn=forbidden,
        )
        self.assertEqual(result["preflight_verdict"], WATCH)
        self.assertEqual(result["preflight_reason"], "EXISTING_MANUAL_EVIDENCE_WATCH")
        self.assertIsNone(result["next_action"])
        self.assertFalse(result["eligible_for_today_action"])
        self.assertEqual(calls, [])

    def test_no_manual_evidence_still_needs_trends(self):
        result = run_preflight(
            _job("No Manual Evidence"),
            autocomplete_fn=_autocomplete("No Manual Evidence"),
            serp_fn=_serp("No Manual Evidence", []),
        )
        self.assertEqual(result["preflight_verdict"], MANUAL_REVIEW)
        self.assertEqual(result["next_action"], "Google Trends")

    def test_existing_trends_with_missing_keyword_routes_to_semrush_stage(self):
        calls = []

        def forbidden(*_args):
            calls.append(True)
            raise AssertionError("existing Trends must not call providers")

        result = run_preflight(
            _job(
                "Keyword Stage Candidate",
                trends_result="中",
                serp_competition="高",
                keyword_opportunity="未检查",
            ),
            autocomplete_fn=forbidden,
            serp_fn=forbidden,
        )
        self.assertEqual(result["preflight_verdict"], MANUAL_REVIEW)
        self.assertEqual(result["preflight_reason"], "EXISTING_MANUAL_EVIDENCE_RESUME")
        self.assertEqual(result["next_action"], "Keyword Research")
        self.assertEqual(calls, [])

    def test_dedicated_domains_saturate(self):
        result = run_preflight(
            _job("Soul's Remnant"),
            autocomplete_fn=_autocomplete("Soul's Remnant"),
            serp_fn=_serp("Soul's Remnant", ["soulsremnant.com", "soulsremnantwiki.com"]),
        )
        self.assertEqual(result["preflight_verdict"], AUTO_REJECT)
        self.assertEqual(result["serp"]["dedicated_guide_domain_count"], 2)

    def test_single_site_high_density_saturates_but_entity_punctuation_stays_strict(self):
        result = run_preflight(
            _job("CICADAMATA"),
            autocomplete_fn=_autocomplete("CICADAMATA"),
            serp_fn=_serp("CICADAMATA", ["cicadamatawiki.com"] * 5),
        )
        self.assertEqual(result["preflight_verdict"], AUTO_REJECT)
        self.assertEqual(result["preflight_reason"], "COMPETITION_SATURATED_HIGH_GUIDE_DENSITY")

    def test_entity_contamination_is_hard_reject_only_when_clear(self):
        result = run_preflight(
            _job("Water You Doing?"),
            autocomplete_fn=_autocomplete("Water You Doing?", guide=False),
            serp_fn=_serp("Water You Doing?", [], irrelevant=8),
        )
        self.assertEqual(result["preflight_verdict"], AUTO_REJECT)
        self.assertEqual(result["preflight_reason"], "ENTITY_SEARCH_INTENT_CONTAMINATION")

    def test_upcoming_immature_demand_is_watch(self):
        result = run_preflight(
            _job("BOMBANANA!", release_stage="Upcoming", release_date="2026-09-02"),
            autocomplete_fn=_autocomplete("BOMBANANA!", guide=False),
            serp_fn=_serp("BOMBANANA!", []),
        )
        self.assertEqual(result["preflight_verdict"], WATCH)
        self.assertEqual(result["next_review_date"], "2026-09-03")
        self.assertFalse(result["eligible_for_today_action"])

    def test_serp_budget_and_same_day_cache_reuse(self):
        calls = []

        def ac(source, query):
            calls.append(("autocomplete", source, query))
            return {"status": "BEST_EFFORT", "items": [{"text": "Example Game guide"}]}

        def serp(query):
            calls.append(("serp", query))
            return {"status": "SUPPORTED", "items": []}

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job = _job("Example Game")
            first = run_preflight(job, autocomplete_fn=ac, serp_fn=serp, cache_dir=root, now=date(2026, 8, 25))
            second = run_preflight(job, autocomplete_fn=ac, serp_fn=serp, cache_dir=root, now=date(2026, 8, 25))
        self.assertEqual(first["searchapi_queries_used"], 3)
        self.assertEqual(first["searchapi_queries_reused"], 0)
        self.assertEqual(second["searchapi_queries_reused"], 3)
        self.assertEqual(len([call for call in calls if call[0] == "serp"]), 3)

    def test_provider_failure_is_not_reject(self):
        def failed(*_args):
            raise RuntimeError("provider down")

        result = run_preflight(_job("Provider Down"), autocomplete_fn=failed, serp_fn=failed)
        self.assertEqual(result["preflight_verdict"], PREFLIGHT_ERROR)
        self.assertNotEqual(result["preflight_verdict"], AUTO_REJECT)


if __name__ == "__main__":
    unittest.main()
