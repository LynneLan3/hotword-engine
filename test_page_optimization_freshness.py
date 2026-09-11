"""Regression fixtures for PAGE_OPTIMIZATION first-party freshness handling."""

from __future__ import annotations

import argparse
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import research_job_runner as job_runner
import research_runner as runner


JOB = {
    "job_id": "ms2-crashing-pc-manual-verify-20260911",
    "game": "Mortal Shell II",
    "topic": "crashing on PC / Sep 5 crash fixes",
    "existing_page": "/mortal-shell-ii/crashing-pc/",
    "opportunity_level": "HIGH",
    "recommended_action": "RESEARCH_EXPAND_EXISTING",
    "source_query": "mortal shell 2 crashing pc",
    "related_queries": [],
    "research_type": "PAGE_OPTIMIZATION_RESEARCH",
    "source_action": "OPTIMIZE_EXISTING",
    "created_at": "2026-09-11T00:00:00+08:00",
}
SIGNAL = (
    "Official Sep 5 update announces several crash fixes and optimisations; "
    "current live page still frames Sep 1 Hotfix 3 as latest."
)
COUNTS = {"raw": 0, "valid": 0, "filtered": {}, "filtered_total": 0}


def _args(signal: str = "") -> argparse.Namespace:
    return argparse.Namespace(
        game=JOB["game"],
        topic=JOB["topic"],
        existing_page=JOB["existing_page"],
        steam_appid=None,
        source_query=JOB["source_query"],
        related_queries=[],
        context_queries=[],
        freshness_signal=signal,
        research_type=JOB["research_type"],
        out="/tmp/page-optimization-freshness.json",
        reuse=None,
    )


def _steam_news(contents: str) -> bytes:
    return json.dumps(
        {
            "appnews": {
                "newsitems": [
                    {
                        "gid": "sep-5",
                        "title": "Mortal Shell II September 5 Update",
                        "url": "https://steamcommunity.com/app/2584270/news/detail/sep-5",
                        "date": "2026-09-05",
                        "contents": contents,
                    }
                ]
            }
        }
    ).encode()


class PageOptimizationFreshnessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.previous_type = runner._ACTIVE_RESEARCH_TYPE

    def tearDown(self) -> None:
        runner._ACTIVE_RESEARCH_TYPE = self.previous_type

    def _run(self, signal: str, page: str, news: bytes):
        with (
            patch.object(runner, "collect_youtube", return_value=([], COUNTS)),
            patch.object(runner, "collect_reddit", return_value=([], COUNTS)),
            patch.object(runner, "collect_steam", return_value=([], COUNTS)),
            patch.object(runner, "fetch_existing_page", return_value=(page, "OK")),
            patch.object(runner, "http_get", return_value=(news, "https://api.steampowered.com")) as fetch,
        ):
            result = runner.run(_args(signal))
        return result, fetch

    def test_case_a_verified_newer_official_fact_expands_existing(self) -> None:
        result, _ = self._run(
            SIGNAL,
            "Mortal Shell II PC crashing page. Sep 1 Hotfix 3 is the latest update.",
            _steam_news("The Sep 5 update includes several crash fixes and optimisations."),
        )
        self.assertEqual(result["freshness"]["status"], "VERIFIED")
        self.assertEqual(result["recommendation"]["action"], "EXPAND_EXISTING")
        self.assertEqual(result["evidence"][0]["source"], "steam_official")
        self.assertIn("2026-09-05", result["evidence_summary"])
        self.assertIn("crash fixes", result["evidence_summary"])

        decision = job_runner.build_content_decision(JOB, result)
        self.assertEqual(decision["primary_decision"], "EXPAND_EXISTING")
        self.assertIn("stale", decision["decision_reason"])
        self.assertIn("newer official fact", decision["decision_reason"])
        self.assertIn("Latest update / crash fixes and optimisations", decision["recommended_sections"])
        self.assertIn("crash fixes", decision["evidence_summary"])

    def test_case_b_unverified_signal_does_not_force_expand(self) -> None:
        result, _ = self._run(
            SIGNAL,
            "Mortal Shell II PC crashing page. Sep 1 Hotfix 3 is the latest update.",
            _steam_news("The Sep 5 update adjusts menu spacing and audio settings."),
        )
        self.assertEqual(result["freshness"]["status"], "UNVERIFIED")
        self.assertEqual(result["recommendation"]["action"], "WATCH")
        self.assertEqual(result["evidence"], [])
        self.assertIn("freshness signal unverified", result["recommendation"]["reason"].lower())

        decision = job_runner.build_content_decision(JOB, result)
        self.assertEqual(decision["primary_decision"], "WATCH")

    def test_case_c_no_signal_keeps_existing_behavior_and_skips_fetch(self) -> None:
        result, fetch = self._run(
            "",
            "Mortal Shell II PC crashing page. Sep 1 Hotfix 3 is the latest update.",
            _steam_news("This fixture must not be fetched."),
        )
        self.assertEqual(result["freshness"]["status"], "NOT_REQUESTED")
        self.assertEqual(result["recommendation"]["action"], "WATCH")
        self.assertEqual(
            result["recommendation"]["reason"],
            "Too few on-topic evidence items to justify a content change.",
        )
        fetch.assert_not_called()

    def test_job_runner_passes_action_context_freshness_signal(self) -> None:
        job = {**JOB, "action_context": {"freshnessSignal": SIGNAL}}
        with tempfile.TemporaryDirectory() as tmp:
            job_path = Path(tmp) / "research_job.json"
            job_path.write_text(json.dumps(job), encoding="utf-8")
            with (
                patch.object(job_runner, "ROOT", Path(tmp)),
                patch.object(job_runner.rr, "run", return_value={"evidence": [], "recommendation": {"action": "WATCH"}}) as run,
                patch.object(job_runner, "post_research_callback", return_value=True),
            ):
                job_runner.run_job(job_path)
        self.assertEqual(run.call_args.args[0].freshness_signal, SIGNAL)


if __name__ == "__main__":
    unittest.main()
