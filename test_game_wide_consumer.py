import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import run_next_game_wide_discovery_job as consumer


def game(job_id: str, *, status=None, job_type=consumer.JOB_TYPE) -> dict:
    job = {
        "job_id": job_id,
        "job_type": job_type,
        "site_key": "example",
        "game_name": "Example Game",
        "discovery_cycle_date": "2026-09-08",
        "native_field": {"keep": True},
    }
    if status is not None:
        job["status"] = status
    return job


class GameWideConsumerTest(unittest.TestCase):
    def test_pending_contract_and_queue_isolation(self):
        self.assertTrue(consumer.is_pending_game_wide_job(game("g036")))
        self.assertTrue(consumer.is_pending_game_wide_job(game("g036-pending", status="PENDING")))
        self.assertFalse(consumer.is_pending_game_wide_job(game("action", job_type="ACTION_RESEARCH")))

    def test_historical_job_is_not_selectable(self):
        old = game("old")
        old["discovery_cycle_date"] = "2026-08-23"
        self.assertIsNone(consumer.select_pending_game_wide_job([old]))

    def test_new_flow_job_is_selectable(self):
        selected = consumer.select_pending_game_wide_job([game("new")])
        self.assertEqual(selected["job_id"], "new")

    def test_new_flow_wins_over_historical_backlog(self):
        old = game("old")
        old["discovery_cycle_date"] = "2026-08-23"
        self.assertEqual(consumer.select_pending_game_wide_job([old, game("new")])["job_id"], "new")

    def test_latest_cycle_then_created_at_wins(self):
        earlier = game("earlier")
        earlier["created_at"] = "2026-09-08T13:51:50+08:00"
        later = game("later")
        later["created_at"] = "2026-09-08T13:57:11+08:00"
        newest_cycle = game("newest-cycle")
        newest_cycle["discovery_cycle_date"] = "2026-09-09"
        newest_cycle["created_at"] = "2026-01-01T00:00:00Z"
        selected = consumer.select_pending_game_wide_job([earlier, later, newest_cycle])
        self.assertEqual(selected["job_id"], "newest-cycle")
        self.assertEqual(
            consumer.select_pending_game_wide_job([earlier, later])["job_id"],
            "later",
        )

    def test_duplicate_job_id_is_deduped_before_selection(self):
        first = game("same")
        first["created_at"] = "2026-09-08T13:51:50+08:00"
        second = game("same")
        second["created_at"] = "2026-09-08T13:57:11+08:00"
        selected = consumer.select_pending_game_wide_job([first, second])
        self.assertEqual(selected["created_at"], second["created_at"])

    def test_no_pending_is_noop(self):
        payload = {"jobs": [game("done", status="COMPLETED"), game("action", job_type="ACTION_RESEARCH")]}
        with mock.patch.object(consumer, "fetch_pending_game_wide_jobs", return_value=payload), mock.patch.object(
            consumer.gws, "run_job"
        ) as run_job, tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(consumer, "ROOT", Path(tmp)):
                self.assertEqual(consumer.main(), 0)
        run_job.assert_not_called()

    def test_multiple_pending_runs_only_first_and_preserves_payload(self):
        payload = {"jobs": [game("first"), game("second")]}
        with mock.patch.object(consumer, "fetch_pending_game_wide_jobs", return_value=payload), mock.patch.object(
            consumer.gws, "run_job", return_value={"ok": True}
        ) as run_job, tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.object(consumer, "ROOT", root):
                self.assertEqual(consumer.main(), 0)
            selected = json.loads((root / "input" / "game_wide_discovery_job.json").read_text())
        self.assertEqual(selected, payload["jobs"][0])
        run_job.assert_called_once_with(root / "input" / "game_wide_discovery_job.json")

    def test_native_payload_reaches_runner_validation(self):
        native = game("native", status=None)
        with mock.patch.object(consumer, "fetch_pending_game_wide_jobs", return_value={"jobs": [native]}), mock.patch.object(
            consumer.gws, "run_job", side_effect=lambda path: {"ok": consumer.gws.load_job(path)["native_field"]["keep"]}
        ) as run_job, tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(consumer, "ROOT", Path(tmp)):
                self.assertEqual(consumer.main(), 0)
        run_job.assert_called_once()


if __name__ == "__main__":
    unittest.main()
