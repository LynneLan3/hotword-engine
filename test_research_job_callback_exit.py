#!/usr/bin/env python3
"""Callback failure must fail the runner without deleting artifacts."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import research_job_runner as rjr
import run_next_research_job as rnj


def _job() -> dict:
    return {
        "job_id": "asset-ms2-callback-exit",
        "game": "Mortal Shell II",
        "topic": "carry over",
        "existing_page": "/mortal-shell-ii/beta-progress-carry-over/",
        "opportunity_level": "HIGH",
        "recommended_action": "RESEARCH_EXPAND_EXISTING",
        "source_query": "mortal shell 2 beta progress carry over rewards",
        "created_at": "2026-08-17T00:00:00+08:00",
    }


def _result(*, evidence_count: int, action: str) -> dict:
    evidence = [
        {
            "source": "steam",
            "title": "carry",
            "url": f"https://example.test/{i}",
            "evidence": "carry over",
            "excerpt": "carry over",
            "relevance": 0.8,
        }
        for i in range(evidence_count)
    ]
    return {
        "evidence": evidence,
        "recommendation": {"action": action, "reason": "test"},
        "review_summary": f"{action}: test",
    }


class ExitCodeFromRunTests(unittest.TestCase):
    def test_review_callback_ok_success(self) -> None:
        self.assertEqual(
            rjr.exit_code_from_run({"status": "REVIEW", "callback_ok": True}),
            0,
        )

    def test_review_callback_false_failure(self) -> None:
        self.assertEqual(
            rjr.exit_code_from_run({"status": "REVIEW", "callback_ok": False}),
            1,
        )

    def test_watch_callback_ok_success(self) -> None:
        self.assertEqual(
            rjr.exit_code_from_run({"status": "WATCH", "callback_ok": True}),
            0,
        )

    def test_watch_callback_false_failure(self) -> None:
        self.assertEqual(
            rjr.exit_code_from_run({"status": "WATCH", "callback_ok": False}),
            1,
        )

    def test_failed_callback_false_still_failure(self) -> None:
        self.assertEqual(
            rjr.exit_code_from_run({"status": "FAILED", "callback_ok": False}),
            1,
        )

    def test_failed_callback_ok_still_failure(self) -> None:
        self.assertEqual(
            rjr.exit_code_from_run({"status": "FAILED", "callback_ok": True}),
            1,
        )


class RunJobCallbackExitTests(unittest.TestCase):
    def _run(self, *, callback_ok: bool, evidence_count: int, action: str):
        job = _job()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            job_path = tmp_path / "research_job.json"
            job_path.write_text(json.dumps(job), encoding="utf-8")
            job_dir = tmp_path / "jobs" / job["job_id"]

            with (
                patch.object(rjr, "ROOT", tmp_path),
                patch.object(rjr.rr, "run", return_value=_result(evidence_count=evidence_count, action=action)),
                patch.object(rjr, "post_research_callback", return_value=callback_ok) as cb,
            ):
                status = rjr.run_job(job_path)
                code = rjr.exit_code_from_run(status)
                next_code = None
                with (
                    patch.object(rnj.fpj, "fetch_pending_jobs", return_value={"jobs": [{**job, "status": "PENDING"}]}),
                    patch.object(rnj.fpj, "is_pending", return_value=True),
                    patch.object(rnj.fpj, "to_research_job", return_value=job),
                    patch.object(rnj.fpj, "write_json"),
                    patch.object(rnj.fpj, "ROOT", tmp_path),
                    patch.object(rnj.rjr, "run_job", return_value=status),
                ):
                    next_code = rnj.main()

            files = {
                "job.json": (job_dir / "job.json").exists(),
                "status.json": (job_dir / "status.json").exists(),
                "research_result.json": (job_dir / "research_result.json").exists(),
            }
            saved = json.loads((job_dir / "status.json").read_text(encoding="utf-8"))
            return status, code, next_code, files, saved, cb.call_count

    def test_review_callback_ok_keeps_artifacts(self) -> None:
        status, code, next_code, files, saved, calls = self._run(
            callback_ok=True, evidence_count=2, action="EXPAND_EXISTING"
        )
        self.assertEqual(status["status"], "REVIEW")
        self.assertTrue(status["callback_ok"])
        self.assertEqual(code, 0)
        self.assertEqual(next_code, 0)
        self.assertEqual(calls, 1)
        self.assertTrue(all(files.values()))
        self.assertNotIn("callback_ok", saved)

    def test_review_callback_false_fails_but_keeps_artifacts(self) -> None:
        status, code, next_code, files, saved, calls = self._run(
            callback_ok=False, evidence_count=2, action="EXPAND_EXISTING"
        )
        self.assertEqual(status["status"], "REVIEW")
        self.assertFalse(status["callback_ok"])
        self.assertEqual(code, 1)
        self.assertEqual(next_code, 1)
        self.assertEqual(calls, 1)
        self.assertTrue(all(files.values()))
        self.assertEqual(saved["status"], "REVIEW")
        self.assertNotIn("callback_ok", saved)

    def test_watch_callback_ok_succeeds_and_keeps_artifacts(self) -> None:
        status, code, next_code, files, saved, calls = self._run(
            callback_ok=True, evidence_count=0, action="WATCH"
        )
        self.assertEqual(status["status"], "WATCH")
        self.assertTrue(status["callback_ok"])
        self.assertEqual(code, 0)
        self.assertEqual(next_code, 0)
        self.assertEqual(calls, 1)
        self.assertTrue(all(files.values()))
        self.assertEqual(saved["status"], "WATCH")
        self.assertNotIn("callback_ok", saved)

    def test_watch_callback_false_fails_but_keeps_artifacts(self) -> None:
        status, code, next_code, files, saved, _ = self._run(
            callback_ok=False, evidence_count=0, action="WATCH"
        )
        self.assertEqual(status["status"], "WATCH")
        self.assertEqual(code, 1)
        self.assertEqual(next_code, 1)
        self.assertTrue(all(files.values()))
        self.assertEqual(saved["status"], "WATCH")

    def test_failed_callback_false_fails_but_keeps_job_and_status(self) -> None:
        job = _job()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            job_path = tmp_path / "research_job.json"
            job_path.write_text(json.dumps(job), encoding="utf-8")
            job_dir = tmp_path / "jobs" / job["job_id"]
            with (
                patch.object(rjr, "ROOT", tmp_path),
                patch.object(rjr.rr, "run", side_effect=RuntimeError("research boom")),
                patch.object(rjr, "post_research_callback", return_value=False) as cb,
            ):
                status = rjr.run_job(job_path)
                code = rjr.exit_code_from_run(status)
            self.assertEqual(status["status"], "FAILED")
            self.assertFalse(status["callback_ok"])
            self.assertEqual(code, 1)
            self.assertEqual(cb.call_count, 1)
            self.assertTrue((job_dir / "job.json").exists())
            self.assertTrue((job_dir / "status.json").exists())
            self.assertFalse((job_dir / "research_result.json").exists())
            saved = json.loads((job_dir / "status.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["status"], "FAILED")
            self.assertNotIn("callback_ok", saved)


class WorkflowStillUploadsOnFailureTests(unittest.TestCase):
    def test_workflow_preserves_artifact_upload_on_research_failure(self) -> None:
        yml = Path(__file__).resolve().parent / ".github/workflows/research-job-scheduler.yml"
        text = yml.read_text(encoding="utf-8")
        self.assertIn("continue-on-error: true", text)
        self.assertIn("Upload research results artifact", text)
        self.assertIn("Propagate research job failure", text)
        self.assertIn("steps.research_job.outcome == 'failure'", text)


if __name__ == "__main__":
    unittest.main()
