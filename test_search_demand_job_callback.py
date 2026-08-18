#!/usr/bin/env python3
"""Offline tests for R3C SEARCH_DEMAND callback sender."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

import research_job_runner as rjr
import search_demand_job_runner as sjr
import search_demand_providers as sdp
import search_demand_runner as sdr

GAME = "Agefield High: Rock the School"
RADAR_ID = "agefield|/agefield-high-rock-the-school/classes/|QUERY_BLIND_SPOT"
SECRET_TOKEN = "SECRET_R3C_TOKEN_TEST"


def _job(**overrides) -> dict:
    job = {
        "job_id": "search-test-callback-1",
        "research_type": "SEARCH_DEMAND",
        "site": "Agefield High",
        "game": GAME,
        "radar_id": RADAR_ID,
        "trigger_type": "QUERY_BLIND_SPOT",
        "anchor_page": "/agefield-high-rock-the-school/classes/",
        "source_signal_summary": "x",
        "discovery_scope": {"page_topic": "classes"},
        "seed_terms": [GAME, f"{GAME} classes"],
        "search_sources_requested": [
            sdp.SOURCE_GOOGLE_AUTOCOMPLETE,
            sdp.SOURCE_GOOGLE_PAA,
            sdp.SOURCE_GOOGLE_RELATED,
            sdp.SOURCE_BING_AUTOCOMPLETE,
        ],
        "search_cycle_date": "2026-08-18",
        "created_at": "2026-08-18T21:00:00+08:00",
    }
    job.update(overrides)
    return job


def _anchor_item(*sources: str, suggestion: str = "Agefield High classes") -> dict:
    src_list = list(sources) or [sdp.SOURCE_GOOGLE_AUTOCOMPLETE]
    return {
        "source": src_list[0],
        "sources": src_list,
        "source_family": "SEARCH",
        "suggestion": suggestion,
        "seed_term": f"{GAME} classes",
        "matched_seed_terms": [f"{GAME} classes"],
        "matched_seed_roles": ["ANCHOR"],
        "normalized_signal": suggestion.lower(),
        "anchor_relevant": True,
    }


def _background_item(source: str = sdp.SOURCE_GOOGLE_AUTOCOMPLETE) -> dict:
    return {
        "source": source,
        "sources": [source],
        "source_family": "SEARCH",
        "suggestion": "Agefield High walkthrough",
        "seed_term": GAME,
        "matched_seed_terms": [GAME],
        "matched_seed_roles": ["GAME_WIDE"],
        "normalized_signal": "agefield high walkthrough",
        "anchor_relevant": False,
    }


def _confirmed_result(**overrides) -> dict:
    anchor = [_anchor_item(sdp.SOURCE_GOOGLE_AUTOCOMPLETE, suggestion="Agefield High classes")]
    result = {
        "job_id": "search-test-callback-1",
        "research_type": "SEARCH_DEMAND",
        "radar_id": RADAR_ID,
        "search_cycle_date": "2026-08-18",
        "discovery_scope": sdr.SCOPE_ANCHOR,
        "search_demand_status": sdr.STATUS_CONFIRMED,
        "execution_status": sdr.EXEC_COMPLETED,
        "all_evidence_count": 3,
        "anchor_evidence_count": 1,
        "background_evidence_count": 2,
        "search_evidence_count": 1,
        "search_sources": [sdp.SOURCE_GOOGLE_AUTOCOMPLETE],
        "matched_queries": ["Agefield High classes"],
        "top_questions": [],
        "result_path": "jobs/search-test-callback-1/search_demand_result.json",
        "anchor_evidence": anchor,
        "background_evidence": [_background_item(), _background_item(sdp.SOURCE_BING_AUTOCOMPLETE)],
        "all_evidence": anchor + [_background_item()],
        "provider_status": {
            sdp.SOURCE_GOOGLE_AUTOCOMPLETE: {"status": "BEST_EFFORT", "error": None, "http_status": 200},
            sdp.SOURCE_BING_AUTOCOMPLETE: {"status": "BEST_EFFORT", "error": None, "http_status": 200},
        },
    }
    result.update(overrides)
    return result


def _no_signal_result(**overrides) -> dict:
    background = [_background_item() for _ in range(3)]
    result = {
        "job_id": "search-test-callback-1",
        "research_type": "SEARCH_DEMAND",
        "radar_id": RADAR_ID,
        "search_cycle_date": "2026-08-18",
        "discovery_scope": sdr.SCOPE_ANCHOR,
        "search_demand_status": sdr.STATUS_NO_SIGNAL,
        "execution_status": sdr.EXEC_COMPLETED,
        "all_evidence_count": 14,
        "anchor_evidence_count": 0,
        "background_evidence_count": 14,
        "search_evidence_count": 0,
        "search_sources": [],
        "matched_queries": [],
        "top_questions": [],
        "result_path": "jobs/search-test-callback-1/search_demand_result.json",
        "anchor_evidence": [],
        "background_evidence": background,
        "all_evidence": background,
        "provider_status": {
            sdp.SOURCE_GOOGLE_AUTOCOMPLETE: {"status": "BEST_EFFORT", "error": None, "http_status": 200},
            sdp.SOURCE_BING_AUTOCOMPLETE: {"status": "BEST_EFFORT", "error": None, "http_status": 200},
        },
    }
    result.update(overrides)
    return result


def _failed_result(**overrides) -> dict:
    result = {
        "job_id": "search-test-callback-1",
        "research_type": "SEARCH_DEMAND",
        "radar_id": RADAR_ID,
        "search_cycle_date": "2026-08-18",
        "discovery_scope": sdr.SCOPE_ANCHOR,
        "search_demand_status": sdr.STATUS_NO_SIGNAL,
        "execution_status": sdr.EXEC_FAILED,
        "all_evidence_count": 0,
        "anchor_evidence_count": 0,
        "background_evidence_count": 0,
        "search_evidence_count": 0,
        "search_sources": [],
        "matched_queries": [],
        "top_questions": [],
        "result_path": "jobs/search-test-callback-1/search_demand_result.json",
        "anchor_evidence": [],
        "background_evidence": [],
        "provider_status": {
            sdp.SOURCE_GOOGLE_AUTOCOMPLETE: {
                "status": "UNAVAILABLE",
                "error": "unavailable",
                "http_status": 500,
            },
            sdp.SOURCE_BING_AUTOCOMPLETE: {
                "status": "UNAVAILABLE",
                "error": "unavailable",
                "http_status": 503,
            },
        },
    }
    result.update(overrides)
    return result


class PayloadMappingTests(unittest.TestCase):
    def test_confirmed_payload_mapping(self) -> None:
        body = sjr.build_search_demand_completed_callback_body(_confirmed_result())
        sjr.validate_search_demand_callback_body(body)
        self.assertEqual(body["job_id"], "search-test-callback-1")
        self.assertEqual(body["research_type"], "SEARCH_DEMAND")
        self.assertEqual(body["radar_id"], RADAR_ID)
        self.assertEqual(body["search_cycle_date"], "2026-08-18")
        self.assertEqual(body["execution_status"], sdr.EXEC_COMPLETED)
        self.assertEqual(body["discovery_scope"], sdr.SCOPE_ANCHOR)
        self.assertEqual(body["search_demand_status"], sdr.STATUS_CONFIRMED)
        self.assertEqual(body["search_evidence_count"], 1)
        self.assertEqual(body["search_sources"], [sdp.SOURCE_GOOGLE_AUTOCOMPLETE])
        self.assertEqual(body["matched_queries"], ["Agefield High classes"])
        self.assertEqual(body["top_questions"], [])
        self.assertEqual(body["result_path"], "jobs/search-test-callback-1/search_demand_result.json")
        self.assertNotIn("token", body)
        self.assertNotIn("background_evidence", body)
        self.assertNotIn("all_evidence", body)

    def test_no_signal_payload_mapping(self) -> None:
        body = sjr.build_search_demand_completed_callback_body(_no_signal_result())
        sjr.validate_search_demand_callback_body(body)
        self.assertEqual(body["execution_status"], sdr.EXEC_COMPLETED)
        self.assertEqual(body["search_demand_status"], sdr.STATUS_NO_SIGNAL)
        self.assertEqual(body["search_evidence_count"], 0)
        self.assertEqual(body["search_sources"], [])
        self.assertEqual(body["matched_queries"], [])

    def test_search_evidence_count_aligns_to_anchor_count(self) -> None:
        result = _confirmed_result(
            all_evidence_count=14,
            background_evidence_count=13,
            search_evidence_count=99,
            anchor_evidence_count=2,
            anchor_evidence=[
                _anchor_item(sdp.SOURCE_GOOGLE_AUTOCOMPLETE, suggestion="Agefield High classes"),
                _anchor_item(sdp.SOURCE_BING_AUTOCOMPLETE, suggestion="Agefield High class"),
            ],
            search_sources=[sdp.SOURCE_GOOGLE_AUTOCOMPLETE, sdp.SOURCE_BING_AUTOCOMPLETE],
        )
        body = sjr.build_search_demand_completed_callback_body(result)
        self.assertEqual(body["search_evidence_count"], 2)
        self.assertEqual(body["search_evidence_count"], result["anchor_evidence_count"])
        self.assertNotEqual(body["search_evidence_count"], result["all_evidence_count"])
        self.assertNotEqual(body["search_evidence_count"], 99)

    def test_search_sources_whitelist(self) -> None:
        result = _confirmed_result(
            search_sources=[
                sdp.SOURCE_GOOGLE_AUTOCOMPLETE,
                "COMMUNITY",
                "SEARCH",
                "reddit",
                sdp.SOURCE_BING_AUTOCOMPLETE,
            ],
            anchor_evidence=[
                _anchor_item(sdp.SOURCE_GOOGLE_AUTOCOMPLETE, sdp.SOURCE_BING_AUTOCOMPLETE),
            ],
            anchor_evidence_count=1,
        )
        body = sjr.build_search_demand_completed_callback_body(result)
        self.assertEqual(
            body["search_sources"],
            [sdp.SOURCE_GOOGLE_AUTOCOMPLETE, sdp.SOURCE_BING_AUTOCOMPLETE],
        )
        self.assertNotIn("COMMUNITY", body["search_sources"])
        self.assertNotIn("SEARCH", body["search_sources"])

    def test_generic_background_evidence_not_in_callback_sources(self) -> None:
        result = _no_signal_result(
            all_evidence_count=14,
            background_evidence_count=14,
            background_evidence=[
                _background_item(sdp.SOURCE_GOOGLE_AUTOCOMPLETE),
                _background_item(sdp.SOURCE_BING_AUTOCOMPLETE),
            ],
            search_sources=[sdp.SOURCE_GOOGLE_AUTOCOMPLETE, sdp.SOURCE_BING_AUTOCOMPLETE],
        )
        body = sjr.build_search_demand_completed_callback_body(result)
        self.assertEqual(body["search_demand_status"], sdr.STATUS_NO_SIGNAL)
        self.assertEqual(body["search_evidence_count"], 0)
        self.assertEqual(body["search_sources"], [])

    def test_game_wide_cannot_forge_anchor_confirmed(self) -> None:
        result = _confirmed_result(
            discovery_scope=sdr.SCOPE_GAME_WIDE,
            search_demand_status=sdr.STATUS_CONFIRMED,
            anchor_evidence_count=2,
            search_sources=[sdp.SOURCE_GOOGLE_AUTOCOMPLETE],
            anchor_evidence=[],
            all_evidence=[_background_item()],
        )
        body = sjr.build_search_demand_completed_callback_body(result)
        with self.assertRaises(ValueError):
            sjr.validate_search_demand_callback_body(body)

    def test_confirmed_count_zero_rejected(self) -> None:
        result = _confirmed_result(
            anchor_evidence_count=0,
            search_evidence_count=0,
            search_sources=[sdp.SOURCE_GOOGLE_AUTOCOMPLETE],
            anchor_evidence=[],
        )
        body = sjr.build_search_demand_completed_callback_body(result)
        with self.assertRaises(ValueError):
            sjr.validate_search_demand_callback_body(body)

    def test_confirmed_empty_sources_rejected(self) -> None:
        result = _confirmed_result(
            search_sources=[],
            anchor_evidence=[
                {
                    "source": "COMMUNITY",
                    "sources": ["COMMUNITY"],
                    "suggestion": "Agefield High classes",
                    "anchor_relevant": True,
                }
            ],
            anchor_evidence_count=1,
        )
        body = sjr.build_search_demand_completed_callback_body(result)
        self.assertEqual(body["search_sources"], [])
        with self.assertRaises(ValueError):
            sjr.validate_search_demand_callback_body(body)

    def test_non_search_demand_result_rejected(self) -> None:
        result = _confirmed_result(research_type="DEMAND_DISCOVERY")
        body = sjr.build_search_demand_completed_callback_body(result)
        with self.assertRaises(ValueError):
            sjr.validate_search_demand_callback_body(body)

    def test_failed_execution_is_not_success_style(self) -> None:
        payload, is_success = sjr._payload_for_outcome(
            job=_job(),
            result=_failed_result(),
            execution_status=sdr.EXEC_FAILED,
        )
        self.assertFalse(is_success)
        self.assertEqual(payload["execution_status"], sdr.EXEC_FAILED)
        self.assertEqual(payload["research_type"], "SEARCH_DEMAND")
        self.assertNotEqual(payload.get("search_demand_status"), sdr.STATUS_NO_SIGNAL)
        self.assertNotIn("search_demand_status", payload)
        self.assertIn("error", payload)
        sjr.validate_search_demand_callback_body(payload)

    def test_no_signal_allows_empty_sources_and_zero_count(self) -> None:
        body = sjr.build_search_demand_completed_callback_body(_no_signal_result())
        sjr.validate_search_demand_callback_body(body)
        self.assertEqual(body["search_evidence_count"], 0)
        self.assertEqual(body["search_sources"], [])


class SenderExecutionTests(unittest.TestCase):
    def _prepare(self, tmp_root: Path, *, job: dict, result: dict, status: str) -> Path:
        job_dir = tmp_root / "jobs" / job["job_id"]
        job_dir.mkdir(parents=True, exist_ok=True)
        (job_dir / "job.json").write_text(json.dumps(job), encoding="utf-8")
        (job_dir / "search_demand_result.json").write_text(json.dumps(result), encoding="utf-8")
        (job_dir / "status.json").write_text(
            json.dumps(
                {
                    "job_id": job["job_id"],
                    "research_type": "SEARCH_DEMAND",
                    "status": status,
                    "search_demand_status": result.get("search_demand_status"),
                    "result_path": result.get("result_path"),
                    "callback_ok": None,
                }
            ),
            encoding="utf-8",
        )
        job_path = tmp_root / "input" / "search_demand_job.json"
        job_path.parent.mkdir(parents=True, exist_ok=True)
        job_path.write_text(json.dumps(job), encoding="utf-8")
        return job_path

    def _run_sender(
        self,
        *,
        result: dict,
        status: str,
        callback_ok: bool | None = True,
        runner_raises: bool = False,
        http: bool = False,
        http_code: int = 200,
        http_body: bytes = b'{"ok": true}',
        http_error: Exception | None = None,
        dry_run: bool = False,
        from_result: bool = False,
    ) -> tuple[dict, dict, list]:
        job = _job()
        logs: list[str] = []
        posted: list[dict] = []

        def capture_log(msg: str) -> None:
            logs.append(str(msg))

        def fake_http(url, *, method, data=None, headers=None):
            if http_error is not None:
                raise http_error
            if data:
                posted.append(json.loads(data.decode("utf-8")))
            headers = headers or {}
            self.assertNotIn("Authorization", headers)
            self.assertNotIn("authorization", {k.lower() for k in headers})
            return http_code, {}, http_body

        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            job_path = self._prepare(tmp_root, job=job, result=result, status=status)
            result_path = tmp_root / "jobs" / job["job_id"] / "search_demand_result.json"

            if runner_raises:
                run_patch = patch.object(sdr, "run_job", side_effect=RuntimeError("search boom"))
            else:
                run_patch = patch.object(
                    sdr,
                    "run_job",
                    return_value={
                        "ok": status == sdr.EXEC_COMPLETED,
                        "status": status,
                        "job_id": job["job_id"],
                        "result": result,
                    },
                )

            env = {
                "RESEARCH_CALLBACK_URL": "https://example.test/research-callback",
                "RESEARCH_CALLBACK_TOKEN": SECRET_TOKEN,
            }
            patches = [
                patch.object(sjr, "ROOT", tmp_root),
                run_patch,
                patch.object(sdr, "log", side_effect=capture_log),
                patch.object(rjr.rr, "log", side_effect=capture_log),
                patch.dict(os.environ, env, clear=False),
            ]
            if http:
                patches.append(patch.object(rjr, "_http_exchange", side_effect=fake_http))
            else:
                def fake_post(body):
                    posted.append(dict(body))
                    self.assertNotEqual(body.get("token"), SECRET_TOKEN)
                    return bool(callback_ok)

                patches.append(patch.object(rjr, "post_callback_body", side_effect=fake_post))

            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
                sender = sjr.run_job(
                    None if from_result else job_path,
                    from_result=result_path if from_result else None,
                    dry_run=dry_run,
                )
            artifacts = {
                "status": json.loads((tmp_root / "jobs" / job["job_id"] / "status.json").read_text()),
                "job": json.loads((tmp_root / "jobs" / job["job_id"] / "job.json").read_text()),
                "result": json.loads(result_path.read_text()),
            }
            blob = json.dumps(artifacts)
            self.assertNotIn(SECRET_TOKEN, blob)
            joined_logs = "\n".join(logs)
            self.assertNotIn(SECRET_TOKEN, joined_logs)
            sender["_posted"] = posted
            sender["_logs"] = logs
            return sender, artifacts, posted

    def test_failed_execution_does_not_send_success_callback(self) -> None:
        sender, _, posted = self._run_sender(
            result=_failed_result(),
            status=sdr.EXEC_FAILED,
            callback_ok=True,
        )
        self.assertEqual(sender["execution_status"], sdr.EXEC_FAILED)
        self.assertEqual(len(posted), 1)
        self.assertEqual(posted[0]["execution_status"], sdr.EXEC_FAILED)
        self.assertNotIn("search_demand_status", posted[0])
        self.assertFalse(sender.get("success_style"))
        self.assertEqual(sjr.exit_code_from_run(sender["execution_status"], sender["callback_ok"]), 1)

    def test_http_success_sets_callback_ok_true(self) -> None:
        sender, artifacts, posted = self._run_sender(
            result=_no_signal_result(),
            status=sdr.EXEC_COMPLETED,
            http=True,
            http_code=200,
            http_body=b'{"ok": true}',
        )
        self.assertTrue(sender["callback_ok"])
        self.assertTrue(artifacts["status"]["callback_ok"])
        self.assertEqual(posted[0]["search_demand_status"], sdr.STATUS_NO_SIGNAL)
        self.assertEqual(posted[0]["token"], SECRET_TOKEN)
        self.assertNotIn("token", sender["callback_payload"])
        self.assertEqual(
            sjr.exit_code_from_run(sender["execution_status"], sender["callback_ok"]),
            0,
        )

    def test_http_failure_sets_callback_ok_false(self) -> None:
        sender, artifacts, _ = self._run_sender(
            result=_confirmed_result(),
            status=sdr.EXEC_COMPLETED,
            http=True,
            http_code=500,
            http_body=b'{"ok": false, "error": "boom"}',
        )
        self.assertFalse(sender["callback_ok"])
        self.assertFalse(artifacts["status"]["callback_ok"])
        self.assertEqual(
            sjr.exit_code_from_run(sender["execution_status"], sender["callback_ok"]),
            1,
        )

    def test_network_exception_sets_callback_ok_false(self) -> None:
        sender, artifacts, posted = self._run_sender(
            result=_confirmed_result(),
            status=sdr.EXEC_COMPLETED,
            http=True,
            http_error=URLError("timed out"),
        )
        self.assertFalse(sender["callback_ok"])
        self.assertFalse(artifacts["status"]["callback_ok"])
        self.assertEqual(posted, [])

    def test_token_not_written_to_artifacts_or_logs(self) -> None:
        sender, artifacts, posted = self._run_sender(
            result=_confirmed_result(),
            status=sdr.EXEC_COMPLETED,
            http=True,
        )
        for name in ("status", "job", "result"):
            self.assertNotIn("token", artifacts[name])
            self.assertNotIn(SECRET_TOKEN, json.dumps(artifacts[name]))
        self.assertNotIn("token", sender["callback_payload"])
        self.assertEqual(posted[0]["token"], SECRET_TOKEN)
        self.assertTrue(artifacts["status"]["callback_ok"])

    def test_dry_run_from_result_does_not_post(self) -> None:
        sender, artifacts, posted = self._run_sender(
            result=_no_signal_result(),
            status=sdr.EXEC_COMPLETED,
            dry_run=True,
            from_result=True,
        )
        self.assertEqual(posted, [])
        self.assertFalse(sender["sent"])
        self.assertIsNone(sender["callback_ok"])
        self.assertIsNone(artifacts["status"]["callback_ok"])
        payload = sender["callback_payload"]
        self.assertEqual(payload["search_demand_status"], sdr.STATUS_NO_SIGNAL)
        self.assertEqual(payload["search_evidence_count"], 0)
        self.assertEqual(payload["search_sources"], [])

    def test_confirmed_validation_failure_does_not_post(self) -> None:
        sender, artifacts, posted = self._run_sender(
            result=_confirmed_result(
                discovery_scope=sdr.SCOPE_GAME_WIDE,
                search_demand_status=sdr.STATUS_CONFIRMED,
                anchor_evidence=[],
                search_sources=[sdp.SOURCE_GOOGLE_AUTOCOMPLETE],
            ),
            status=sdr.EXEC_COMPLETED,
            callback_ok=True,
        )
        self.assertEqual(posted, [])
        self.assertFalse(sender["sent"])
        self.assertFalse(sender["callback_ok"])
        self.assertFalse(artifacts["status"]["callback_ok"])
        self.assertIn("callback_validation_failed", artifacts["status"].get("error", ""))

    def test_runner_exception_sends_failed_not_no_signal(self) -> None:
        sender, _, posted = self._run_sender(
            result=_no_signal_result(),
            status=sdr.EXEC_COMPLETED,
            runner_raises=True,
            callback_ok=True,
        )
        self.assertEqual(sender["execution_status"], sdr.EXEC_FAILED)
        self.assertEqual(posted[0]["execution_status"], sdr.EXEC_FAILED)
        self.assertNotIn("search_demand_status", posted[0])
        self.assertIn("search boom", posted[0]["error"])


if __name__ == "__main__":
    unittest.main()
