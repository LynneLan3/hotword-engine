"""Tests for Twitch Historical Raw Ledger helpers and daily executor."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import twitch_raw_observation_daily_executor as executor
from opportunity_discovery.twitch_historical_raw_ledger import (
    CANONICAL_SELECTION_REASON,
    build_ledger_payload,
    select_daily_canonical_runs,
)


def _artifact(*, run_status: str = "COMPLETE", rows: int = 3, run_id: str = "20260903-010000") -> dict:
    observations = [
        {
            "run_id": run_id,
            "observed_at": "2026-09-03T01:00:00+00:00",
            "source": "TWITCH_HELIX_TOP_GAMES",
            "global_rank": index + 1,
            "api_page": 1,
            "page_rank": index + 1,
            "twitch_game_id": f"tw-{index}",
            "name": f"Game {index}",
            "igdb_id": None if index == 1 else f"ig-{index}",
            "box_art_url": "https://cdn.example/art.jpg",
            "raw_status": "OBSERVED",
        }
        for index in range(rows)
    ]
    return {
        "run_id": run_id,
        "observed_at": "2026-09-03T01:00:00+00:00",
        "source": "TWITCH_HELIX_TOP_GAMES",
        "observations": observations,
        "run_metadata": {
            "requested_limit": 300,
            "pages_completed": 1 if run_status != "COMPLETE" else 3,
            "rows_returned": rows,
            "unique_twitch_ids": rows,
            "duplicates": 0,
            "missing_igdb_count": 1 if rows > 1 else 0,
            "run_status": run_status,
            "source_errors": [{"api_page": 2, "code": "HTTP_ERROR"}] if run_status == "PARTIAL" else [],
        },
    }


class TwitchHistoricalRawLedgerTests(unittest.TestCase):
    def test_payload_contains_required_raw_fields(self) -> None:
        payload = build_ledger_payload(_artifact())
        self.assertEqual(payload["spreadsheet_name"], "Twitch Historical Raw Ledger V1")
        self.assertEqual(len(payload["raw_observation_rows"]), 3)
        headers = payload["headers"]["raw_observations"]
        for required in (
            "Run ID",
            "Observed At",
            "Source",
            "Global Rank",
            "API Page",
            "Page Rank",
            "Twitch Game ID",
            "Name",
            "IGDB ID",
            "Box Art URL",
            "Raw Status",
        ):
            self.assertIn(required, headers)
        row = dict(zip(headers, payload["raw_observation_rows"][0]))
        self.assertEqual(row["Twitch Game ID"], "tw-0")
        self.assertEqual(row["Raw Status"], "OBSERVED")

    def test_canonical_prefers_success_complete_production_latest(self) -> None:
        rows = [
            {
                "Run ID": "20260903-010000",
                "Run Date": "20260903",
                "Final Status": "SUCCESS",
                "Discovery Completeness": "COMPLETE",
                "Run Type": "SCHEDULED_DAILY",
                "Trigger Type": "SCHEDULED_TRIGGER",
                "Rows Returned": 300,
                "Finished At": "2026-09-03T01:00:00+00:00",
            },
            {
                "Run ID": "20260903-020000",
                "Run Date": "20260903",
                "Final Status": "SUCCESS",
                "Discovery Completeness": "COMPLETE",
                "Run Type": "MANUAL_PRODUCTION",
                "Trigger Type": "MANUAL",
                "Rows Returned": 300,
                "Finished At": "2026-09-03T02:00:00+00:00",
            },
            {
                "Run ID": "20260903-030000",
                "Run Date": "20260903",
                "Final Status": "PARTIAL",
                "Discovery Completeness": "PARTIAL",
                "Run Type": "SCHEDULED_DAILY",
                "Trigger Type": "SCHEDULED_TRIGGER",
                "Rows Returned": 100,
                "Finished At": "2026-09-03T03:00:00+00:00",
            },
            {
                "Run ID": "20260903-999999",
                "Run Date": "20260903",
                "Final Status": "SUCCESS",
                "Discovery Completeness": "COMPLETE",
                "Run Type": "TEST",
                "Trigger Type": "MANUAL",
                "Rows Returned": 300,
                "Finished At": "2026-09-03T09:00:00+00:00",
            },
        ]
        canonical = select_daily_canonical_runs(rows)
        self.assertEqual(len(canonical), 1)
        self.assertEqual(canonical[0][1], "20260903-020000")
        self.assertEqual(canonical[0][8], CANONICAL_SELECTION_REASON)


class TwitchDailyExecutorTests(unittest.TestCase):
    def test_dry_run_appends_history_without_candidate_master(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            summary = executor.run_daily_twitch_raw_observation(
                root=root,
                collect_fn=lambda: _artifact(),
                dry_run=True,
                now=datetime(2026, 9, 3, 1, 0, tzinfo=timezone.utc),
            )
            history = json.loads((root / "jobs" / "twitch-historical-raw-ledger" / "history.json").read_text())
        self.assertEqual(summary["rows_returned"], 3)
        self.assertEqual(summary["run_status"], "COMPLETE")
        self.assertFalse(summary["wrote_candidate_master"])
        self.assertEqual(len(history["runs"]), 1)
        self.assertEqual(summary["canonical_run_id"], "20260903-010000")

    def test_partial_run_keeps_rows_and_posts_ledger(self) -> None:
        sent: list[dict] = []

        def post(_url: str, body: dict) -> dict:
            sent.append(body)
            return {
                "ok": True,
                "spreadsheetId": "sheet-twitch",
                "spreadsheetName": "Twitch Historical Raw Ledger V1",
                "rawAppended": 1,
                "runLedgerAppended": 1,
                "canonicalRunId": None,
            }

        with tempfile.TemporaryDirectory() as tmp:
            summary = executor.run_daily_twitch_raw_observation(
                root=Path(tmp),
                collect_fn=lambda: _artifact(run_status="PARTIAL", rows=1),
                post_fn=post,
            )
        self.assertEqual(summary["rows_returned"], 1)
        self.assertEqual(summary["discovery_completeness"], "PARTIAL")
        self.assertTrue(summary["ledger_ok"])
        self.assertEqual(sent[0]["job_type"], "TWITCH_HISTORICAL_RAW_LEDGER_APPEND")
        self.assertFalse(summary["wrote_candidate_master"])

    def test_auth_missing_is_not_zero_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            summary = executor.run_daily_twitch_raw_observation(
                root=Path(tmp),
                collect_fn=lambda: _artifact(run_status="AUTH_MISSING", rows=0),
                dry_run=True,
            )
        self.assertEqual(summary["final_status"], "FAILED")
        self.assertEqual(summary["discovery_completeness"], "AUTH_MISSING")
        self.assertEqual(summary["rows_returned"], 0)


if __name__ == "__main__":
    unittest.main()
