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
