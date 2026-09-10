#!/usr/bin/env python3
"""Focused cross-repo receipt contract checks."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import batch_receipt as br
import research_job_runner as rjr


class CrossRepoReceiptTests(unittest.TestCase):
    def test_callback_propagates_batch_and_scheduler_identity(self) -> None:
        with patch.dict(
            os.environ,
            {"GITHUB_RUN_ID": "123", "GITHUB_REPOSITORY": "LynneLan3/hotword-engine"},
            clear=False,
        ):
            body = rjr.build_callback_body(
                {"job_id": "job-1", "status": "REVIEW", "batch_id": "research-demo-crossrepo-001"},
                job={"batch_id": "research-demo-crossrepo-001"},
            )
        self.assertEqual(body["batch_id"], "research-demo-crossrepo-001")
        self.assertEqual(body["scheduler_run_id"], "123")
        self.assertNotIn("token", json.dumps(body).lower())

    def test_late_lower_stage_callback_does_not_downgrade(self) -> None:
        receipt = br.build_receipt(
            identifier="research-demo-crossrepo-001",
            started_at="2026-09-10T00:00:00+00:00",
            finished_at="2026-09-10T00:01:00+00:00",
            status="RESEARCH_PASS",
            stage="RESEARCH_PASS",
            research_jobs=[{"job_id": "job-1", "site": "Demo", "research_status": "REVIEW"}],
            evidence={"PASS": 1, "HOLD": 0, "FAIL": 0},
            site="Demo",
        )
        implemented = br.advance_receipt(receipt, {
            "status": "IMPLEMENTED",
            "implementation": {
                "status": "IMPLEMENTED", "site": "Demo", "action": "UPDATE",
                "changed_files": ["src/demo.md"], "canonical_urls": ["/demo/"],
            },
            "git": {"repo": "LynneLan3/demo", "commit_sha": "abc", "commit_url": "https://github.com/LynneLan3/demo/commit/abc"},
        })
        deployed = br.advance_receipt(implemented, {
            "status": "DEPLOYED",
            "deployment": {"status": "DEPLOYED", "production_url": "https://demo.test", "changed_urls": ["https://demo.test/demo/"]},
        })
        late = br.advance_receipt(deployed, {
            "status": "IMPLEMENTED",
            "implementation": {"status": "IMPLEMENTED", "canonical_urls": ["/demo/", "/demo/"]},
        })
        self.assertEqual(late["status"], "DEPLOYED")
        self.assertEqual(late["implementation"]["canonical_urls"], ["/demo/"])

    def test_machine_callback_entrypoint_advances_same_receipt(self) -> None:
        base = br.build_receipt(
            identifier="research-demo-crossrepo-001",
            started_at="2026-09-10T00:00:00+00:00",
            finished_at="2026-09-10T00:01:00+00:00",
            status="RESEARCH_PASS",
            stage="RESEARCH_PASS",
            research_jobs=[{"job_id": "job-1", "site": "Demo", "research_status": "REVIEW"}],
            evidence={"PASS": 1, "HOLD": 0, "FAIL": 0},
            site="Demo",
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            receipt = root / "jobs/batches/research-demo-crossrepo-001/batch_receipt.json"
            base_path = root / "base.json"
            patch_path = root / "patch.json"
            base_path.write_text(json.dumps(base), encoding="utf-8")
            patch_path.write_text(json.dumps({
                "batch_id": "research-demo-crossrepo-001", "status": "IMPLEMENTED",
                "implementation": {"status": "IMPLEMENTED", "site": "Demo", "action": "UPDATE", "changed_files": ["src/demo.md"], "canonical_urls": ["/demo/"]},
                "git": {"repo": "LynneLan3/demo", "commit_sha": "abc", "commit_url": "https://github.com/LynneLan3/demo/commit/abc"},
            }), encoding="utf-8")
            result = subprocess.run(
                ["python3", "apply_batch_receipt_callback.py", "--receipt", str(receipt), "--base-json", str(base_path), "--patch-json", str(patch_path)],
                cwd=Path(__file__).parent, text=True, capture_output=True, check=True,
            )
            saved = json.loads(receipt.read_text(encoding="utf-8"))
        self.assertEqual(saved["batch_id"], "research-demo-crossrepo-001")
        self.assertEqual(saved["status"], "IMPLEMENTED")
        self.assertIn("CONTENT IMPLEMENTED", result.stdout)

    def test_duplicate_callback_is_idempotent_and_failed_deploy_stays_failed(self) -> None:
        receipt = br.build_receipt(
            identifier="research-demo-crossrepo-001",
            started_at="2026-09-10T00:00:00+00:00",
            finished_at="2026-09-10T00:01:00+00:00",
            status="RESEARCH_PASS",
            stage="RESEARCH_PASS",
            research_jobs=[{"job_id": "job-1", "site": "Demo", "research_status": "REVIEW"}],
            evidence={"PASS": 1, "HOLD": 0, "FAIL": 0},
            site="Demo",
        )
        implementation = {
            "status": "IMPLEMENTED", "site": "Demo", "action": "UPDATE",
            "changed_files": ["src/demo.md"], "canonical_urls": ["/demo/", "/demo/"],
        }
        git = {"repo": "LynneLan3/demo", "commit_sha": "abc", "commit_url": "https://github.com/LynneLan3/demo/commit/abc"}
        first = br.advance_receipt(receipt, {"status": "IMPLEMENTED", "implementation": implementation, "git": git})
        duplicate = br.advance_receipt(first, {"status": "IMPLEMENTED", "implementation": implementation, "git": git})
        self.assertEqual(duplicate["implementation"]["canonical_urls"], ["/demo/"])
        failed = br.advance_receipt(duplicate, {
            "status": "FAILED", "failure_stage": "DEPLOYMENT", "failure_reason": "DEPLOY_FAILED",
        })
        self.assertEqual(failed["status"], "FAILED")
        with self.assertRaises(br.BatchReceiptTransitionError):
            br.advance_receipt(failed, {"status": "DEPLOYED"})


if __name__ == "__main__":
    unittest.main()
