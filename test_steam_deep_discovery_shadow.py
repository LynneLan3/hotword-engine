"""Focused offline tests for the G002 Steam deep-discovery shadow."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

from opportunity_discovery.shadow import ShadowClassification, run_steam_shadow, write_shadow_artifact
from opportunity_discovery.sources.steam import (
    POPULAR_NEW_RELEASES,
    POPULAR_UPCOMING,
    SteamShadowAdapter,
    build_steam_page_url,
    parse_steam_search_results,
)


OBSERVED_AT = "2026-08-26T00:00:00+00:00"


def _row(app_id: str, name: str, release: str, *, reviews: int | None = None, rating: int = 90) -> str:
    review_html = ""
    if reviews is not None:
        review_html = (
            f'<span class="search_review_summary" '
            f'data-tooltip-html="{rating}% of the {reviews:,} user reviews">x</span>'
        )
    return (
        f'<a class="search_result_row ds_collapse_flag" '
        f'href="https://store.steampowered.com/app/{app_id}/?snr=1" '
        f'data-ds-appid="{app_id}">'
        f'<span class="title">{name}</span>'
        f'<div class="search_released responsive_secondrow">{release}</div>'
        f"{review_html}</a>"
    )


class SteamShadowTests(unittest.TestCase):
    def test_page_parameter_correctness(self):
        url = build_steam_page_url(POPULAR_UPCOMING, 3)
        query = parse_qs(urlparse(url).query)
        self.assertEqual(query["start"], ["100"])
        self.assertEqual(query["count"], ["50"])

    def test_source_parsing_appid_rank_release_and_review(self):
        html = _row("123", "A &amp; B", "26 Aug, 2026", reviews=1_234, rating=95)
        parsed = parse_steam_search_results(html, POPULAR_UPCOMING, 2, OBSERVED_AT)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0].steam_app_id, "123")
        self.assertEqual(parsed[0].game_name, "A & B")
        self.assertEqual(parsed[0].rank_on_page, 1)
        self.assertEqual(parsed[0].release_date, "2026-08-26")
        self.assertEqual(parsed[0].review_count, 1234)
        self.assertEqual(parsed[0].review_rating, 0.95)

    def test_duplicate_pages_and_sources_reuse_canonical_ids(self):
        first = parse_steam_search_results(_row("123", "Same Game", "26 Aug, 2026"), POPULAR_UPCOMING, 1, OBSERVED_AT)[0]
        duplicate_page = parse_steam_search_results(_row("123", "Renamed Same Game", "26 Aug, 2026"), POPULAR_UPCOMING, 2, OBSERVED_AT)[0]
        duplicate_source = parse_steam_search_results(_row("123", "Same Game", "26 Aug, 2026", reviews=30), POPULAR_NEW_RELEASES, 1, OBSERVED_AT)[0]
        adapter = SteamShadowAdapter(1, 2, observed_at=OBSERVED_AT, request_interval_seconds=0, sources=(POPULAR_UPCOMING, POPULAR_NEW_RELEASES))
        normalized = [adapter.normalize(item.__dict__) for item in (first, duplicate_page, duplicate_source)]
        self.assertEqual({record.game_entities[0].game_entity_id for record in normalized}, {normalized[0].game_entities[0].game_entity_id})
        self.assertEqual({record.platform_listings[0].platform_listing_id for record in normalized}, {normalized[0].platform_listings[0].platform_listing_id})
        self.assertEqual({record.events[0].event_id for record in normalized}, {normalized[0].events[0].event_id})

    def test_baseline_deep_incremental_and_by_page(self):
        pages = {
            (POPULAR_UPCOMING, 1): _row("100", "Baseline", "28 Aug, 2026"),
            (POPULAR_UPCOMING, 2): _row("200", "Deep Two", "28 Aug, 2026") + _row("100", "Baseline", "28 Aug, 2026"),
            (POPULAR_UPCOMING, 3): _row("200", "Deep Two", "28 Aug, 2026"),
            (POPULAR_NEW_RELEASES, 1): _row("100", "Baseline", "25 Aug, 2026", reviews=30),
            (POPULAR_NEW_RELEASES, 2): _row("300", "Deep Three", "25 Aug, 2026", reviews=30),
            (POPULAR_NEW_RELEASES, 4): _row("400", "Deep Four", "25 Aug, 2026", reviews=30),
        }

        def fetch(url: str, _timeout: float):
            query = parse_qs(urlparse(url).query)
            page = int(query["start"][0]) // 50 + 1
            source = POPULAR_NEW_RELEASES if "filter=popularnew" in url else POPULAR_UPCOMING
            return SimpleNamespace(status=200, body=pages.get((source, page), ""))

        adapter = SteamShadowAdapter(1, 4, observed_at=OBSERVED_AT, fetcher=fetch, request_interval_seconds=0, max_attempts=1)
        artifact = run_steam_shadow(adapter)
        self.assertEqual(artifact["baseline"]["unique_games"], 1)
        self.assertEqual(artifact["deep"]["unique_games"], 4)
        self.assertEqual(artifact["deep"]["incremental_unique_games"], 3)
        self.assertEqual(artifact["deep"]["incremental_by_page"], {"p2": 2, "p3": 0, "p4": 1})
        self.assertEqual(artifact["deep"]["incremental_by_source"], {POPULAR_UPCOMING: 1, POPULAR_NEW_RELEASES: 2})
        self.assertEqual(
            artifact["deep"]["incremental_by_source_page"],
            {
                f"{POPULAR_UPCOMING} page 2": 1,
                f"{POPULAR_UPCOMING} page 3": 0,
                f"{POPULAR_UPCOMING} page 4": 0,
                f"{POPULAR_NEW_RELEASES} page 2": 1,
                f"{POPULAR_NEW_RELEASES} page 3": 0,
                f"{POPULAR_NEW_RELEASES} page 4": 1,
            },
        )
        self.assertEqual(artifact["canonical_record_counts"]["game_entities"], 4)

    def test_missing_history_needs_history_and_cheap_reject_is_deterministic(self):
        pages = {
            (POPULAR_UPCOMING, 1): "",
            (POPULAR_UPCOMING, 2): _row("201", "Needs History", "28 Aug, 2026") + _row("202", "Too Late", "30 Sep, 2026"),
            (POPULAR_NEW_RELEASES, 1): "",
            (POPULAR_NEW_RELEASES, 2): _row("203", "Low Reviews", "25 Aug, 2026", reviews=3),
        }

        def fetch(url: str, _timeout: float):
            query = parse_qs(urlparse(url).query)
            page = int(query["start"][0]) // 50 + 1
            source = POPULAR_NEW_RELEASES if "filter=popularnew" in url else POPULAR_UPCOMING
            return SimpleNamespace(status=200, body=pages.get((source, page), ""))

        adapter = SteamShadowAdapter(1, 2, observed_at=OBSERVED_AT, fetcher=fetch, request_interval_seconds=0, max_attempts=1)
        artifact = run_steam_shadow(adapter)
        self.assertEqual(artifact["classification"]["needs_history"], 1)
        self.assertEqual(artifact["classification"]["cheap_reject"], 2)
        self.assertEqual(artifact["classification"]["potential"], 1)
        sample = {item["steam_app_id"]: item["classification"] for item in artifact["incremental_candidate_sample"]}
        self.assertEqual(sample["201"], ShadowClassification.NEEDS_HISTORY.value)
        self.assertEqual(sample["202"], ShadowClassification.CHEAP_REJECT.value)
        self.assertEqual(sample["203"], ShadowClassification.CHEAP_REJECT.value)

    def test_existing_history_becomes_eligible(self):
        pages = {
            (POPULAR_UPCOMING, 2): _row("301", "History Game", "28 Aug, 2026"),
        }

        def fetch(url: str, _timeout: float):
            query = parse_qs(urlparse(url).query)
            page = int(query["start"][0]) // 50 + 1
            source = POPULAR_NEW_RELEASES if "filter=popularnew" in url else POPULAR_UPCOMING
            return SimpleNamespace(status=200, body=pages.get((source, page), ""))

        adapter = SteamShadowAdapter(1, 2, observed_at=OBSERVED_AT, fetcher=fetch, request_interval_seconds=0, max_attempts=1)
        artifact = run_steam_shadow(adapter, history_by_app_id={"301": {"days": 5, "followers": 400}})
        self.assertEqual(artifact["classification"]["existing_history"], 1)
        self.assertEqual(artifact["classification"]["needs_history"], 0)
        self.assertEqual(artifact["classification"]["potential"], 1)

    def test_partial_page_failure_and_artifact_json_serialization(self):
        def fetch(url: str, _timeout: float):
            if "start=50" in url:
                return SimpleNamespace(status=429, body="rate limited")
            return SimpleNamespace(status=200, body=_row("401", "Safe Page", "28 Aug, 2026"))

        adapter = SteamShadowAdapter(1, 2, observed_at=OBSERVED_AT, fetcher=fetch, request_interval_seconds=0, max_attempts=1)
        artifact = run_steam_shadow(adapter)
        self.assertEqual(artifact["http_result"], "PARTIAL")
        self.assertEqual(artifact["http"]["successful_pages"], 2)
        self.assertTrue(any(page["status"] == "FAIL" and page["http_status"] == 429 for page in artifact["http"]["pages"]))
        with TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "shadow.json"
            write_shadow_artifact(artifact, output)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8")), artifact)


if __name__ == "__main__":
    unittest.main()
