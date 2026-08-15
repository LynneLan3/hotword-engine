#!/usr/bin/env python3
"""Status semantic tests: WATCH vs REVIEW vs FAILED callback payloads."""

from __future__ import annotations

import unittest

import research_job_runner as rjr


class FinishedStatusTests(unittest.TestCase):
    def test_a_watch_zero_evidence_not_review(self) -> None:
        status = rjr.resolve_finished_status(
            evidence_count=0,
            recommendation={"action": "WATCH", "reason": "too few"},
        )
        self.assertEqual(status, "WATCH")
        body = rjr.build_callback_body(
            {
                "job_id": "au-console-20260814",
                "status": "WATCH",
                "recommendation": {"action": "WATCH", "reason": "too few"},
                "evidence_count": 0,
                "result_path": "jobs/au-console-20260814/research_result.json",
            },
            result={
                "review_summary": "WATCH: too few. Evidence count: 0.",
                "evidence": [],
                "recommendation": {"action": "WATCH", "reason": "too few"},
            },
        )
        self.assertEqual(body["status"], "WATCH")
        self.assertEqual(body["recommendation"], "WATCH")
        self.assertEqual(body["evidence_count"], 0)
        self.assertIn("review_summary", body)
        self.assertNotIn("evidence", body)  # must not write 研究审核

    def test_b_review_with_evidence_keeps_behavior(self) -> None:
        status = rjr.resolve_finished_status(
            evidence_count=18,
            recommendation={"action": "EXPAND_EXISTING", "reason": "gaps"},
        )
        self.assertEqual(status, "REVIEW")
        evidence = [
            {
                "source": "steam",
                "title": "save",
                "url": "https://steamcommunity.com/app/2584270/discussions/0/1/",
                "evidence": "Flayed Harbinger unlock",
                "excerpt": "Flayed Harbinger unlock",
                "relevance": 0.8,
            }
        ]
        body = rjr.build_callback_body(
            {
                "job_id": "ms2-beta-progress-carry-over-20260814",
                "status": "REVIEW",
                "recommendation": {"action": "EXPAND_EXISTING"},
                "evidence_count": 18,
                "result_path": "jobs/ms2-beta-progress-carry-over-20260814/research_result.json",
            },
            result={
                "evidence": evidence,
                "recommendation": {"action": "EXPAND_EXISTING", "reason": "gaps"},
                "review_summary": "EXPAND_EXISTING: gaps",
            },
        )
        self.assertEqual(body["status"], "REVIEW")
        self.assertEqual(body["recommendation"], "EXPAND_EXISTING")
        self.assertIn("evidence", body)
        self.assertEqual(len(body["evidence"]), 1)

    def test_c_failed_unchanged(self) -> None:
        body = rjr.build_callback_body(
            {
                "job_id": "x",
                "status": "FAILED",
                "error": "boom",
            }
        )
        self.assertEqual(body, {"job_id": "x", "status": "FAILED", "error": "boom"})

    def test_d_legacy_review_callback_shape(self) -> None:
        """Old REVIEW callers that already set status=REVIEW stay compatible."""
        body = rjr.build_callback_body(
            {
                "job_id": "legacy",
                "status": "REVIEW",
                "recommendation": "EXPAND_EXISTING",
                "evidence_count": 2,
                "result_path": "jobs/legacy/research_result.json",
            },
            result={"evidence": [{"source": "reddit", "url": "u", "evidence": "x" * 40}],
                    "review_summary": "ok"},
        )
        self.assertEqual(body["status"], "REVIEW")
        self.assertEqual(body["evidence_count"], 2)
        self.assertIsInstance(body["evidence"], list)


if __name__ == "__main__":
    unittest.main()
