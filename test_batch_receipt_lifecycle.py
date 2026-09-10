#!/usr/bin/env python3
"""Focused lifecycle checks; does not rerun the scheduler retry suite."""

from __future__ import annotations

import json
import unittest

import batch_receipt as br


def research_receipt() -> dict:
    return br.build_receipt(
        identifier="research-test-001",
        started_at="2026-09-10T00:00:00+00:00",
        finished_at="2026-09-10T00:01:00+00:00",
        status="RESEARCH_PASS",
        stage="RESEARCH_PASS",
        research_jobs=[{"job_id": "job-1", "site": "Test Game", "research_status": "REVIEW"}],
        evidence={"PASS": 3, "HOLD": 0, "FAIL": 0},
        site="Test Game",
    )


class BatchReceiptLifecycleTests(unittest.TestCase):
    def test_research_only_summary_cannot_claim_deployment(self) -> None:
        receipt = research_receipt()
        self.assertIn("RESEARCH BATCH research-test-001", receipt["summary"])
        self.assertIn("Result: RESEARCH_PASS", receipt["summary"])
        self.assertNotIn("AUTO CONTENT BATCH", receipt["summary"])
        self.assertIn("Implementation: NOT_RUN", receipt["summary"])
        self.assertIn("Production: NOT_RUN", receipt["summary"])

    def test_same_batch_advances_to_deployed_with_evidence(self) -> None:
        implemented = br.advance_receipt(
            research_receipt(),
            {
                "batch_id": "research-test-001",
                "status": "IMPLEMENTED",
                "implementation": {
                    "status": "IMPLEMENTED",
                    "site": "Test Game",
                    "action": "UPDATE",
                    "changed_files": ["src/content/docs/test.md"],
                    "canonical_urls": ["/test/"],
                },
                "git": {
                    "repo": "LynneLan3/test-site",
                    "commit_sha": "abc123",
                    "commit_url": "https://github.com/LynneLan3/test-site/commit/abc123",
                },
            },
        )
        deployed = br.advance_receipt(
            implemented,
            {
                "batch_id": "research-test-001",
                "status": "DEPLOYED",
                "deployment": {
                    "status": "DEPLOYED",
                    "production_url": "https://test.example",
                    "changed_urls": ["https://test.example/test/"],
                },
                "indexing": {"IndexNow": "PASS", "manual_request_indexing_urls": []},
            },
        )
        self.assertEqual(deployed["status"], "DEPLOYED")
        self.assertIn("AUTO CONTENT BATCH research-test-001", deployed["summary"])
        self.assertIn("Result: DEPLOYED", deployed["summary"])
        self.assertIn("UPDATE /test/", deployed["summary"])
        self.assertNotIn("callback-secret", json.dumps(deployed))
        checked = br.advance_receipt(
            deployed,
            {"status": "INDEXING_CHECKED", "indexing": {"URL Inspection": "PASS"}},
        )
        self.assertIn("AUTO CONTENT BATCH research-test-001", checked["summary"])
        self.assertIn("Result: INDEXING_CHECKED", checked["summary"])

    def test_failed_receipt_cannot_advance(self) -> None:
        failed = br.advance_receipt(
            research_receipt(),
            {"status": "FAILED", "failure_stage": "IMPLEMENTATION", "failure_reason": "WRITER_FAILED"},
        )
        self.assertEqual(failed["status"], "FAILED")
        with self.assertRaises(br.BatchReceiptTransitionError):
            br.advance_receipt(failed, {"status": "DEPLOYED"})


if __name__ == "__main__":
    unittest.main()
