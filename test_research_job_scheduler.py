#!/usr/bin/env python3
"""Small offline checks for scheduler retries and batch receipts."""

from __future__ import annotations

import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

import fetch_pending_jobs as fpj
import run_next_research_job as rnj
import upstream_http
import research_job_runner as rjr


class _Response:
    def __init__(self, body: bytes, code: int = 200) -> None:
        self._body = body
        self._code = code
        self.headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def getcode(self):
        return self._code

    def read(self):
        return self._body


def _job() -> dict[str, str]:
    return {
        "job_id": "test-research-job",
        "game": "Test Game",
        "topic": "test topic",
        "existing_page": "/test/",
        "opportunity_level": "HIGH",
        "recommended_action": "RESEARCH",
        "source_query": "test query",
        "created_at": "2026-09-10T00:00:00Z",
    }


class SchedulerRetryTests(unittest.TestCase):
    def test_timeout_retries_then_returns_payload(self) -> None:
        calls = 0

        def opener(_request, timeout):
            nonlocal calls
            calls += 1
            self.assertEqual(timeout, 30)
            if calls < 3:
                raise TimeoutError("read operation timed out")
            return _Response(b'{"jobs": []}')

        with patch.object(upstream_http.time, "sleep") as sleep:
            with patch.object(fpj.urllib.request, "urlopen", opener):
                payload = fpj.fetch_pending_jobs()
        self.assertEqual(payload, {"jobs": []})
        self.assertEqual(calls, 3)
        self.assertEqual(sleep.call_args_list[0].args, (1,))
        self.assertEqual(sleep.call_args_list[1].args, (2,))

    def test_timeout_exhaustion_is_not_no_pending(self) -> None:
        with patch.object(upstream_http.time, "sleep"):
            with patch.object(fpj.urllib.request, "urlopen", side_effect=TimeoutError("timed out")):
                with self.assertRaises(fpj.PendingJobsFetchError) as caught:
                    fpj.fetch_pending_jobs()
        self.assertEqual(caught.exception.reason, "UPSTREAM_TIMEOUT")
        self.assertEqual(caught.exception.attempts, 3)

    def test_explicit_4xx_is_not_retried(self) -> None:
        error = urllib.error.HTTPError("https://example.test", 400, "bad request", {}, None)
        with patch.object(fpj.urllib.request, "urlopen", side_effect=error) as opener:
            with self.assertRaises(fpj.PendingJobsFetchError) as caught:
                fpj.fetch_pending_jobs()
        self.assertEqual(caught.exception.reason, "UPSTREAM_HTTP_ERROR")
        self.assertEqual(opener.call_count, 1)

    def test_callback_timeout_retries_without_logging_token(self) -> None:
        calls = 0

        class Opener:
            def open(self, _request, timeout):
                nonlocal calls
                calls += 1
                if calls < 3:
                    raise TimeoutError("read operation timed out")
                return _Response(b'{"ok": true}')

        with (
            patch.dict("os.environ", {"RESEARCH_CALLBACK_URL": "https://example.test/callback", "RESEARCH_CALLBACK_TOKEN": "callback-secret"}, clear=False),
            patch.object(rjr.urllib.request, "build_opener", return_value=Opener()),
            patch.object(upstream_http.time, "sleep"),
            patch.object(rjr.rr, "log") as log,
        ):
            self.assertTrue(rjr.post_callback_body({"job_id": "test", "status": "REVIEW"}))
        self.assertEqual(calls, 3)
        self.assertNotIn("callback-secret", " ".join(str(call) for call in log.call_args_list))


class BatchReceiptTests(unittest.TestCase):
    def _run(self, payload, outcome):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (
                patch.object(rnj.fpj, "ROOT", root),
                patch.object(rnj.fpj, "fetch_pending_jobs", return_value=payload),
                patch.object(rnj.rjr, "run_job", return_value=outcome),
                patch.dict("os.environ", {"GITHUB_RUN_ID": "34420915837", "GITHUB_SHA": "abc123"}, clear=False),
            ):
                code = rnj.main()
            receipts = list((root / "jobs" / "batches").glob("*/batch_receipt.json"))
            self.assertEqual(len(receipts), 1)
            return code, json.loads(receipts[0].read_text(encoding="utf-8"))

    def test_no_pending_job_has_explicit_status(self) -> None:
        code, receipt = self._run({"jobs": []}, {})
        self.assertEqual(code, 0)
        self.assertEqual(receipt["status"], "NO_PENDING_JOB")
        self.assertEqual(receipt["stage"], "NO_PENDING_JOB")
        self.assertEqual(receipt["research_jobs"], [])

    def test_success_receipt_keeps_research_separate_from_production(self) -> None:
        job = _job()
        code, receipt = self._run(
            {"jobs": [{**job, "status": "PENDING"}]},
            {"status": "REVIEW", "callback_ok": True, "evidence_count": 2},
        )
        self.assertEqual(code, 0)
        self.assertEqual(receipt["status"], "SUCCESS")
        self.assertEqual(receipt["stage"], "RESEARCH_PASS")
        self.assertEqual(receipt["research_jobs"][0]["job_id"], job["job_id"])
        self.assertEqual(receipt["deployment"]["status"], "NOT_RUN")
        self.assertEqual(receipt["evidence"]["PASS"], 2)

    def test_failed_receipt_contains_reason_and_no_secret(self) -> None:
        job = _job()
        code, receipt = self._run(
            {"jobs": [{**job, "status": "PENDING"}]},
            {"status": "FAILED", "callback_ok": False, "error": "research boom"},
        )
        text = json.dumps(receipt, ensure_ascii=False)
        self.assertEqual(code, 1)
        self.assertEqual(receipt["status"], "FAILED")
        self.assertEqual(receipt["failure_reason"], "RESEARCH_FAILED")
        self.assertEqual(receipt["job_id"], job["job_id"])
        self.assertNotIn("callback-secret", text)


if __name__ == "__main__":
    unittest.main()
