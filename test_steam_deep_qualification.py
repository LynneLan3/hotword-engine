"""Focused offline tests for G002A provider and qualification semantics."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from urllib.parse import urlparse

from opportunity_discovery.games_popularity import (
    GamesPopularityClient,
    ProviderResponse,
    calculate_7d_growth,
    normalize_history_response,
    normalize_latest_response,
)
from opportunity_discovery.steam_qualification import (
    CONTROL,
    DATA_ERROR,
    EARLY,
    INSUFFICIENT_HISTORY,
    ONE_A_PASS,
    REJECT,
    TREND,
    QualificationCandidate,
    classify_one_a,
    classify_one_b,
    experiment_verdict,
    qualify_steam_deep_candidates,
)
from opportunity_discovery.sources.steam import (
    POPULAR_NEW_RELEASES,
    POPULAR_UPCOMING,
    SteamDiscoveryObservation,
    SteamPageResult,
    SteamShadowAdapter,
)


OBSERVED = datetime(2026, 8, 26, tzinfo=timezone.utc)
OBSERVED_TEXT = OBSERVED.isoformat()


def _history(current: float, baseline: float, days: int = 7) -> tuple[dict[str, object], ...]:
    return ({"followers": baseline, "added": (OBSERVED - timedelta(days=days)).isoformat()},)


def _observation(app_id: str, name: str, page: int, source: str, release: str, *, reviews: int | None = None, rating: float | None = None) -> SteamDiscoveryObservation:
    return SteamDiscoveryObservation(
        source=source,
        page=page,
        rank_on_page=1,
        observed_at=OBSERVED_TEXT,
        steam_app_id=app_id,
        game_name=name,
        store_url=f"https://store.steampowered.com/app/{app_id}/",
        release_date=release,
        release_date_raw=release,
        release_status="UPCOMING" if source == POPULAR_UPCOMING else "RELEASED",
        review_count=reviews,
        review_rating=rating,
    )


class GamesPopularityTests(unittest.TestCase):
    def test_response_normalization_and_7d_growth(self):
        latest = {"followers": {"followers": 2_000}}
        history = {"history": list(_history(2_000, 1_000))}
        self.assertEqual(normalize_latest_response(latest), 2_000)
        points = normalize_history_response(history)
        self.assertEqual(len(points), 1)
        growth = calculate_7d_growth(points, 2_000, OBSERVED)
        self.assertTrue(growth["ok"])
        self.assertEqual(growth["baseline_followers"], 1_000)
        self.assertEqual(growth["gain"], 1_000)
        self.assertAlmostEqual(growth["growth_rate"], 0.5)
        self.assertEqual(growth["coverage_days"], 7.0)

    def test_short_history_is_insufficient(self):
        points = normalize_history_response({"history": list(_history(2_000, 1_000, days=3))})
        result = calculate_7d_growth(points, 2_000, OBSERVED, min_history_days=5)
        self.assertFalse(result["ok"])
        self.assertEqual(result["coverage_days"], 3.0)

    def test_bounded_provider_normalizes_available_missing_and_failed(self):
        calls: list[str] = []

        def fetch(url: str, _timeout: float) -> ProviderResponse:
            calls.append(url)
            parsed = urlparse(url)
            app_id = parsed.path.rsplit("/", 1)[-1]
            if app_id == "1" and "/latest/" in parsed.path:
                return ProviderResponse(200, json.dumps({"followers": {"followers": 2_000}}))
            if app_id == "1" and "/followers/" in parsed.path:
                return ProviderResponse(200, json.dumps({"history": list(_history(2_000, 1_000))}))
            if app_id == "2":
                return ProviderResponse(404, "")
            return ProviderResponse(503, "")

        client = GamesPopularityClient("fixture-key", fetcher=fetch, max_workers=2, max_attempts=1, min_interval_seconds=0, sleep_fn=lambda _seconds: None)
        results = client.fetch_many(["1", "2", "3"])
        self.assertEqual(results["1"].status, "AVAILABLE")
        self.assertEqual(results["2"].status, "MISSING")
        self.assertEqual(results["3"].status, "FAILED")
        self.assertEqual(len(calls), 6)

    def test_malformed_history_is_failed_without_exposing_provider_details(self):
        def fetch(url: str, _timeout: float) -> ProviderResponse:
            if "/latest/7" in url:
                return ProviderResponse(200, json.dumps({"followers": 2_000}))
            return ProviderResponse(
                200,
                json.dumps(
                    {
                        "history": [
                            {"followers": 1_000, "added": (OBSERVED - timedelta(days=7)).isoformat()},
                            {"followers": "not-a-number", "added": "bad-date"},
                        ]
                    }
                ),
            )

        result = GamesPopularityClient("fixture-key", fetcher=fetch, max_attempts=1, min_interval_seconds=0).fetch_many(["7"])["7"]
        self.assertEqual(result.status, "FAILED")
        self.assertEqual(result.history_status, "FAILED")
        self.assertNotIn("fixture-key", result.error or "")


class SteamQualificationTests(unittest.TestCase):
    def test_known_production_1b_cases_match_business_categories(self):
        fixture_path = Path(__file__).parent / "tests" / "fixtures" / "steam_production_known_cases.json"
        for case in json.loads(fixture_path.read_text(encoding="utf-8")):
            candidate = QualificationCandidate("1", "known", [], 2, POPULAR_UPCOMING)
            candidate.one_a = ONE_A_PASS
            candidate.current_followers = case["followers"]
            candidate.gain_7d = case["gain_7d"]
            candidate.growth_rate = case["growth_rate"]
            classify_one_b(candidate)
            self.assertEqual(candidate.one_b, case["expected_1b"], case["name"])

    def test_1a_upcoming_released_control_and_data_boundaries(self):
        upcoming = QualificationCandidate(
            "1", "Upcoming", [_observation("1", "Upcoming", 2, POPULAR_UPCOMING, "2026-08-30")], 2, POPULAR_UPCOMING
        )
        upcoming.current_followers = 2_000
        self.assertEqual(classify_one_a(upcoming).one_a, ONE_A_PASS)

        released = QualificationCandidate(
            "2", "Released", [_observation("2", "Released", 2, POPULAR_NEW_RELEASES, "2026-08-24", reviews=100, rating=0.9)], 2, POPULAR_NEW_RELEASES
        )
        released.current_followers = 2_000
        self.assertEqual(classify_one_a(released).one_a, ONE_A_PASS)

        control = QualificationCandidate(
            "3", "Control", [_observation("3", "Control", 2, POPULAR_UPCOMING, "2026-08-30")], 2, POPULAR_UPCOMING
        )
        control.current_followers = 40_000
        self.assertEqual(classify_one_a(control).one_a, ONE_A_PASS)
        self.assertTrue(control.control_only)

        rejected = QualificationCandidate(
            "5", "Rejected", [_observation("5", "Rejected", 2, POPULAR_UPCOMING, "2026-08-30")], 2, POPULAR_UPCOMING
        )
        rejected.current_followers = 100
        self.assertEqual(classify_one_a(rejected).one_a, REJECT)

        missing = QualificationCandidate(
            "4", "Missing", [_observation("4", "Missing", 2, POPULAR_NEW_RELEASES, "2026-08-24", reviews=100, rating=0.9)], 2, POPULAR_NEW_RELEASES
        )
        self.assertEqual(classify_one_a(missing).one_a, DATA_ERROR)

    def test_page_source_aggregation_and_missing_provider_data(self):
        pages = [
            _observation("100", "Baseline", 1, POPULAR_UPCOMING, "2026-08-30"),
            _observation("101", "Trend", 2, POPULAR_UPCOMING, "2026-08-30"),
            _observation("102", "Early", 2, POPULAR_UPCOMING, "2026-08-30"),
            _observation("104", "Cheap Reject", 2, POPULAR_UPCOMING, "2026-09-30", reviews=3),
            _observation("103", "Control", 3, POPULAR_NEW_RELEASES, "2026-08-24", reviews=100, rating=0.9),
            _observation("105", "Provider Missing", 3, POPULAR_NEW_RELEASES, "2026-08-24", reviews=100, rating=0.9),
            _observation("106", "Short History", 3, POPULAR_NEW_RELEASES, "2026-08-24", reviews=100, rating=0.9),
        ]

        def steam_fetch(url: str, _timeout: float):
            return SimpleNamespace(status=200, body="")

        adapter = SteamShadowAdapter(1, 3, observed_at=OBSERVED_TEXT, fetcher=steam_fetch, request_interval_seconds=0, max_attempts=1)
        # The adapter page results are populated by a harmless mocked collection;
        # qualification itself consumes the explicit observation set.
        adapter.page_results = [
            SteamPageResult(source, page, "PASS", 200, 1)
            for source, page in ((POPULAR_UPCOMING, 1), (POPULAR_UPCOMING, 2), (POPULAR_NEW_RELEASES, 3))
        ]
        from opportunity_discovery.games_popularity import GamesPopularityEnrichment, FollowerPoint

        def enrichment(app_id: str, current: float, baseline: float, days: int = 7) -> GamesPopularityEnrichment:
            return GamesPopularityEnrichment(
                app_id, current, (FollowerPoint(baseline, OBSERVED - timedelta(days=days)),), "AVAILABLE", "AVAILABLE"
            )

        popularity = {
            "101": enrichment("101", 2_000, 1_000),
            "102": enrichment("102", 3_000, 2_200, days=6),
            "103": enrichment("103", 45_000, 43_000),
            "105": GamesPopularityEnrichment("105", None, (), "MISSING", "MISSING"),
            "106": enrichment("106", 2_000, 1_000, days=3),
        }
        artifact = qualify_steam_deep_candidates(adapter, pages, popularity, provider_requested=5)
        q = artifact["qualification"]
        self.assertEqual(q["incremental_total"], 6)
        self.assertEqual(q["cheap_reject_before_enrichment"], 1)
        self.assertEqual(q["enrichment_attempted"], 5)
        self.assertEqual(q["one_a"]["pass"], 3)
        self.assertEqual(q["one_a"]["insufficient_history"], 1)
        self.assertEqual(q["one_a"]["data_error"], 1)
        self.assertEqual(q["one_b"]["trend"], 1)
        self.assertEqual(q["one_b"]["early"], 1)
        self.assertEqual(q["one_b"]["control"], 1)
        self.assertEqual(q["qualified_high_priority"], 2)
        self.assertEqual(artifact["by_page"]["p2"]["qualified_high_priority"], 2)
        self.assertEqual(artifact["by_page"]["p3"]["qualified_high_priority"], 0)
        self.assertEqual(artifact["by_source"][POPULAR_UPCOMING]["trend"], 1)
        self.assertEqual(artifact["by_source"][POPULAR_NEW_RELEASES]["control"], 1)

    def test_verdict_serialization_is_deterministic(self):
        artifact = {
            "http_result": "COMPLETE",
            "qualification": {
                "enrichment_attempted": 3,
                "enrichment_failed": 0,
                "enrichment_missing": 0,
                "qualified_high_priority": 1,
            },
            "by_page": {"p2": {"qualified_high_priority": 0}, "p3": {"qualified_high_priority": 1}, "p4": {}, "p5": {}},
        }
        self.assertEqual(experiment_verdict(artifact)[0], "PROMOTE_DEEP_DISCOVERY")

    def test_partial_positive_evidence_stays_shadow_only(self):
        artifact = {
            "http_result": "COMPLETE",
            "qualification": {
                "enrichment_attempted": 3,
                "enrichment_failed": 0,
                "enrichment_missing": 1,
                "qualified_high_priority": 1,
            },
            "by_page": {"p2": {"qualified_high_priority": 1}},
        }
        self.assertEqual(experiment_verdict(artifact)[0], "KEEP_SHADOW")

    def test_missing_provider_result_is_terminal_data_error_and_inconclusive(self):
        observations = [
            _observation("900", "Baseline", 1, POPULAR_UPCOMING, "2026-08-30"),
            _observation("901", "Missing Provider", 2, POPULAR_UPCOMING, "2026-08-30"),
        ]
        adapter = SteamShadowAdapter(1, 2, observed_at=OBSERVED_TEXT, request_interval_seconds=0, max_attempts=1)
        artifact = qualify_steam_deep_candidates(adapter, observations, {}, provider_requested=0)
        self.assertEqual(artifact["qualification"]["enrichment_missing_results"], 1)
        self.assertEqual(artifact["candidate_results"][0]["1A"], DATA_ERROR)
        self.assertEqual(artifact["experiment_verdict"]["verdict"], "INCONCLUSIVE")


if __name__ == "__main__":
    unittest.main()
