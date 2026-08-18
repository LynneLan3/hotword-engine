#!/usr/bin/env python3
"""Offline tests for R3B SEARCH_DEMAND loader + runner."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import fetch_pending_search_demand_jobs as fpsj
import search_demand_providers as sdp
import search_demand_runner as sdr

GAME = "Agefield High: Rock the School"
SEED_GAME = GAME
SEED_ANCHOR = f"{GAME} classes"
RADAR_ID = "agefield|/agefield-high-rock-the-school/classes/|QUERY_BLIND_SPOT"


def _job(**overrides) -> dict:
    job = {
        "job_id": "search-test-1",
        "research_type": "SEARCH_DEMAND",
        "site": "Agefield High",
        "game": GAME,
        "radar_id": RADAR_ID,
        "trigger_type": "QUERY_BLIND_SPOT",
        "anchor_page": "/agefield-high-rock-the-school/classes/",
        "source_signal_summary": "Query coverage is thin on the classes page.",
        "discovery_scope": {"page_topic": "classes"},
        "seed_terms": [SEED_GAME, SEED_ANCHOR],
        "search_sources_requested": [
            sdp.SOURCE_GOOGLE_AUTOCOMPLETE,
            sdp.SOURCE_GOOGLE_PAA,
            sdp.SOURCE_GOOGLE_RELATED,
            sdp.SOURCE_BING_AUTOCOMPLETE,
        ],
        "search_cycle_date": "2026-08-18",
        "created_at": "2026-08-18T21:00:00+08:00",
    }
    for key, value in overrides.items():
        job[key] = value
    return job


def _osjson(query: str, suggestions: list[str]) -> str:
    return json.dumps([query, suggestions])


class RecordingFetch:
    def __init__(
        self,
        *,
        google: list[str] | None = None,
        bing: list[str] | None = None,
        google_error: bool = False,
        bing_error: bool = False,
        google_raise: bool = False,
        bing_raise: bool = False,
    ) -> None:
        self.google = google if google is not None else ["Agefield High walkthrough"]
        self.bing = bing if bing is not None else ["Agefield High review"]
        self.google_error = google_error
        self.bing_error = bing_error
        self.google_raise = google_raise
        self.bing_raise = bing_raise
        self.urls: list[str] = []

    def __call__(self, url: str, headers=None) -> dict:
        self.urls.append(url)
        if "google.com/search" in url:
            raise AssertionError(f"SERP must not be requested: {url}")
        q = ""
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
        q = (qs.get("q") or qs.get("query") or [""])[0]
        if "suggestqueries.google.com" in url:
            if self.google_raise:
                raise ConnectionError("google down")
            if self.google_error:
                return {"ok": False, "status": 500, "body": "nope", "url": url, "error": "HTTPError 500"}
            return {"ok": True, "status": 200, "body": _osjson(q, self.google), "url": url, "error": None}
        if "api.bing.com" in url:
            if self.bing_raise:
                raise ConnectionError("bing down")
            if self.bing_error:
                return {"ok": False, "status": 503, "body": "nope", "url": url, "error": "HTTPError 503"}
            return {"ok": True, "status": 200, "body": _osjson(q, self.bing), "url": url, "error": None}
        raise AssertionError(f"unexpected url: {url}")


class LoaderContractTests(unittest.TestCase):
    def test_1_search_demand_job_schema(self) -> None:
        parsed = fpsj.to_search_demand_job(_job())
        self.assertEqual(parsed["research_type"], "SEARCH_DEMAND")
        self.assertEqual(parsed["search_cycle_date"], "2026-08-18")
        self.assertIn("search_sources_requested", parsed)
        self.assertNotIn("source_families_requested", parsed)
        self.assertNotIn("topic", parsed)
        self.assertNotIn("source_query", parsed)
        sdr._validate_job(parsed)

        with self.assertRaises(SystemExit):
            fpsj.to_search_demand_job(_job(research_type="DEMAND_DISCOVERY"))

    def test_loader_empty_queue_reports_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            count, selected = fpsj.save_pending_search_demand_jobs({"jobs": []}, root=root)
            self.assertEqual(count, 0)
            self.assertIsNone(selected)
            self.assertTrue((root / "input" / "pending_search_demand_jobs.json").exists())
            self.assertFalse((root / "input" / "search_demand_job.json").exists())

    def test_loader_selects_only_first_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = _job(job_id="search-first")
            second = _job(job_id="search-second")
            count, selected = fpsj.save_pending_search_demand_jobs(
                {"jobs": [first, second]},
                root=root,
            )
            self.assertEqual(count, 2)
            self.assertEqual(selected["job_id"], "search-first")
            written = json.loads((root / "input" / "search_demand_job.json").read_text())
            self.assertEqual(written["job_id"], "search-first")


class SeedRoleTests(unittest.TestCase):
    def test_2_game_wide_and_anchor_seed_roles(self) -> None:
        self.assertEqual(sdr.determine_seed_role(SEED_GAME, GAME), sdr.SCOPE_GAME_WIDE)
        self.assertEqual(sdr.determine_seed_role(SEED_ANCHOR, GAME), sdr.SCOPE_ANCHOR)


class AnchorGateTests(unittest.TestCase):
    def test_3_classes_suggestion_is_anchor(self) -> None:
        job = _job()
        self.assertTrue(sdr.is_anchor_relevant_suggestion("Agefield High classes", job))

    def test_4_walkthrough_is_background(self) -> None:
        job = _job()
        self.assertFalse(sdr.is_anchor_relevant_suggestion("Agefield High walkthrough", job))
        self.assertFalse(sdr.is_anchor_relevant_suggestion("Agefield gameplay", job))
        self.assertFalse(sdr.is_anchor_relevant_suggestion("Agefield High review", job))
        self.assertFalse(sdr.is_anchor_relevant_suggestion("Agefield High release date", job))
        self.assertFalse(sdr.is_anchor_relevant_suggestion("Agefield High ps5", job))
        self.assertFalse(sdr.is_anchor_relevant_suggestion("Agefield High xbox", job))
        self.assertFalse(sdr.is_anchor_relevant_suggestion("Agefield High romance", job))

    def test_5_singular_plural_class_classes(self) -> None:
        job = _job()
        self.assertTrue(sdr.is_anchor_relevant_suggestion("Agefield High class", job))
        self.assertTrue(sdr.is_anchor_relevant_suggestion("Agefield High classes", job))


class RunnerSemanticsTests(unittest.TestCase):
    def test_6_google_bing_same_suggestion_deduped_with_provenance(self) -> None:
        fetch = RecordingFetch(
            google=["Agefield High classes"],
            bing=["Agefield High classes"],
        )
        result = sdr.run_search_demand(_job(), fetch_fn=fetch)
        self.assertEqual(result["all_evidence_count"], 1)
        row = result["all_evidence"][0]
        self.assertEqual(
            row["sources"],
            [sdp.SOURCE_GOOGLE_AUTOCOMPLETE, sdp.SOURCE_BING_AUTOCOMPLETE],
        )
        self.assertEqual(row["source_family"], sdr.SOURCE_FAMILY_SEARCH)
        self.assertTrue(row["anchor_relevant"])
        self.assertEqual(result["search_sources"], [
            sdp.SOURCE_GOOGLE_AUTOCOMPLETE,
            sdp.SOURCE_BING_AUTOCOMPLETE,
        ])

    def test_7_google_only_anchor_is_confirmed(self) -> None:
        fetch = RecordingFetch(
            google=["Agefield High classes"],
            bing=["Agefield High walkthrough"],
        )
        result = sdr.run_search_demand(_job(), fetch_fn=fetch)
        self.assertEqual(result["execution_status"], sdr.EXEC_COMPLETED)
        self.assertEqual(result["search_demand_status"], sdr.STATUS_CONFIRMED)
        self.assertEqual(result["search_sources"], [sdp.SOURCE_GOOGLE_AUTOCOMPLETE])
        self.assertEqual(result["search_evidence_count"], result["anchor_evidence_count"])
        self.assertEqual(result["search_evidence_count"], 1)
        self.assertEqual(result["top_questions"], [])

    def test_8_bing_only_anchor_is_confirmed(self) -> None:
        fetch = RecordingFetch(
            google=["Agefield High walkthrough"],
            bing=["Agefield High classes"],
        )
        result = sdr.run_search_demand(_job(), fetch_fn=fetch)
        self.assertEqual(result["search_demand_status"], sdr.STATUS_CONFIRMED)
        self.assertEqual(result["search_sources"], [sdp.SOURCE_BING_AUTOCOMPLETE])
        self.assertEqual(result["search_evidence_count"], 1)

    def test_9_both_success_generic_is_completed_no_signal(self) -> None:
        fetch = RecordingFetch(
            google=["Agefield High walkthrough", "Agefield High ps5"],
            bing=["Agefield High review", "Agefield High xbox"],
        )
        result = sdr.run_search_demand(_job(), fetch_fn=fetch)
        self.assertEqual(result["execution_status"], sdr.EXEC_COMPLETED)
        self.assertEqual(result["search_demand_status"], sdr.STATUS_NO_SIGNAL)
        self.assertGreater(result["all_evidence_count"], 0)
        self.assertEqual(result["anchor_evidence_count"], 0)
        self.assertGreater(result["background_evidence_count"], 0)
        self.assertEqual(result["search_sources"], [])
        self.assertEqual(result["search_evidence_count"], 0)
        self.assertEqual(result["matched_queries"], [])

    def test_10_google_fail_bing_generic_is_completed_no_signal(self) -> None:
        fetch = RecordingFetch(google_error=True, bing=["Agefield High romance"])
        result = sdr.run_search_demand(_job(), fetch_fn=fetch)
        self.assertEqual(result["execution_status"], sdr.EXEC_COMPLETED)
        self.assertEqual(result["search_demand_status"], sdr.STATUS_NO_SIGNAL)
        self.assertEqual(
            result["provider_status"][sdp.SOURCE_GOOGLE_AUTOCOMPLETE]["status"],
            sdp.STATUS_UNAVAILABLE,
        )
        self.assertEqual(
            result["provider_status"][sdp.SOURCE_BING_AUTOCOMPLETE]["status"],
            sdp.STATUS_BEST_EFFORT,
        )
        self.assertEqual(result["search_sources"], [])

    def test_11_google_fail_bing_anchor_is_completed_confirmed(self) -> None:
        fetch = RecordingFetch(google_error=True, bing=["Agefield High classes"])
        result = sdr.run_search_demand(_job(), fetch_fn=fetch)
        self.assertEqual(result["execution_status"], sdr.EXEC_COMPLETED)
        self.assertEqual(result["search_demand_status"], sdr.STATUS_CONFIRMED)
        self.assertEqual(result["search_sources"], [sdp.SOURCE_BING_AUTOCOMPLETE])

    def test_12_google_and_bing_fail_is_failed(self) -> None:
        fetch = RecordingFetch(google_error=True, bing_error=True)
        result = sdr.run_search_demand(_job(), fetch_fn=fetch)
        self.assertEqual(result["execution_status"], sdr.EXEC_FAILED)
        self.assertEqual(result["search_demand_status"], sdr.STATUS_NO_SIGNAL)
        self.assertEqual(result["all_evidence_count"], 0)
        self.assertEqual(result["search_sources"], [])

    def test_13_paa_related_unavailable_and_no_serp_request(self) -> None:
        fetch = RecordingFetch(google=["Agefield High walkthrough"], bing=["Agefield High review"])
        result = sdr.run_search_demand(_job(), fetch_fn=fetch)
        self.assertEqual(
            result["provider_status"][sdp.SOURCE_GOOGLE_PAA]["status"],
            sdp.STATUS_UNAVAILABLE,
        )
        self.assertEqual(
            result["provider_status"][sdp.SOURCE_GOOGLE_RELATED]["status"],
            sdp.STATUS_UNAVAILABLE,
        )
        self.assertEqual(result["provider_status"][sdp.SOURCE_GOOGLE_PAA]["error"], sdr.DISABLED_REASON)
        self.assertTrue(result["top_questions"] == [])
        self.assertTrue(fetch.urls)
        self.assertFalse(any("google.com/search" in u for u in fetch.urls))
        self.assertTrue(any("suggestqueries.google.com" in u for u in fetch.urls))
        self.assertTrue(any("api.bing.com" in u for u in fetch.urls))

    def test_14_game_wide_evidence_does_not_fill_search_sources(self) -> None:
        fetch = RecordingFetch(
            google=["Agefield High classes", "Agefield High walkthrough"],
            bing=["Agefield High class"],
        )
        job = _job(
            discovery_scope={},
            seed_terms=[SEED_GAME],
        )
        result = sdr.run_search_demand(job, fetch_fn=fetch)
        self.assertEqual(result["discovery_scope"], sdr.SCOPE_GAME_WIDE)
        self.assertEqual(result["search_demand_status"], sdr.STATUS_NO_SIGNAL)
        self.assertGreater(result["all_evidence_count"], 0)
        self.assertEqual(result["anchor_evidence_count"], 0)
        self.assertEqual(result["search_sources"], [])
        self.assertEqual(result["search_evidence_count"], 0)
        self.assertEqual(result["matched_queries"], [])

    def test_15_search_evidence_count_equals_anchor_count(self) -> None:
        fetch = RecordingFetch(
            google=["Agefield High classes", "Agefield High class answers"],
            bing=["Agefield High walkthrough"],
        )
        result = sdr.run_search_demand(_job(), fetch_fn=fetch)
        self.assertEqual(result["search_evidence_count"], result["anchor_evidence_count"])
        self.assertGreater(result["anchor_evidence_count"], 0)
        self.assertNotEqual(result["search_evidence_count"], result["all_evidence_count"])

    def test_16_provider_failure_does_not_crash_runner(self) -> None:
        fetch = RecordingFetch(google_raise=True, bing=["Agefield High classes"])
        result = sdr.run_search_demand(_job(), fetch_fn=fetch)
        self.assertEqual(result["execution_status"], sdr.EXEC_COMPLETED)
        self.assertEqual(result["search_demand_status"], sdr.STATUS_CONFIRMED)
        self.assertEqual(
            result["provider_status"][sdp.SOURCE_GOOGLE_AUTOCOMPLETE]["status"],
            sdp.STATUS_UNAVAILABLE,
        )


class ArtifactAndCallbackTests(unittest.TestCase):
    def test_callback_ready_contract_and_status_json(self) -> None:
        fetch = RecordingFetch(
            google=["Agefield High walkthrough"],
            bing=["Agefield High review"],
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job_path = root / "input" / "search_demand_job.json"
            fpsj.write_json(job_path, _job())
            outcome = sdr.run_job(job_path, fetch_fn=fetch, root=root)
            job_dir = root / "jobs" / "search-test-1"
            self.assertTrue((job_dir / "job.json").exists())
            self.assertTrue((job_dir / "search_demand_result.json").exists())
            status = json.loads((job_dir / "status.json").read_text())
            self.assertEqual(status["research_type"], "SEARCH_DEMAND")
            self.assertEqual(status["status"], sdr.EXEC_COMPLETED)
            self.assertEqual(status["search_demand_status"], sdr.STATUS_NO_SIGNAL)
            self.assertIsNone(status["callback_ok"])
            self.assertIn("finished_at", status)
            cb = sdr.to_callback_fields(outcome["result"])
            self.assertEqual(
                set(cb),
                {
                    "job_id",
                    "research_type",
                    "radar_id",
                    "search_cycle_date",
                    "discovery_scope",
                    "search_demand_status",
                    "all_evidence_count",
                    "anchor_evidence_count",
                    "background_evidence_count",
                    "search_evidence_count",
                    "search_sources",
                    "matched_queries",
                    "top_questions",
                    "provider_status",
                    "result_path",
                },
            )
            self.assertEqual(cb["research_type"], "SEARCH_DEMAND")
            self.assertEqual(cb["top_questions"], [])

    def test_failed_status_json_callback_ok_is_null(self) -> None:
        fetch = RecordingFetch(google_error=True, bing_error=True)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job_path = root / "job.json"
            fpsj.write_json(job_path, _job())
            outcome = sdr.run_job(job_path, fetch_fn=fetch, root=root)
            self.assertEqual(outcome["status"], sdr.EXEC_FAILED)
            status = json.loads((root / "jobs" / "search-test-1" / "status.json").read_text())
            self.assertEqual(status["status"], sdr.EXEC_FAILED)
            self.assertIsNone(status["callback_ok"])
            self.assertNotEqual(status["callback_ok"], False)


if __name__ == "__main__":
    unittest.main()
