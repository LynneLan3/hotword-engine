"""Offline tests for the bounded M7E Steam Candidate daily executor."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

import steam_candidate_daily_executor as executor


TYPES = ["🔥趋势", "🌱Early", "🏢大盘对照", "⚪低优先级"]


def _job(app_id: str, job_id: str | None = None, first_round_type: str = "🔥趋势") -> dict:
    return {
        "job_id": job_id or f"steam-research-{app_id}-20260824",
        "job_type": "STEAM_CANDIDATE_RESEARCH",
        "steam_app_id": app_id,
        "game_name": f"Game {app_id}",
        "steam_url": f"https://store.steampowered.com/app/{app_id}/",
        "research_cycle_date": "2026-08-24",
        "steam_signals": {"first_round_type": first_round_type, "first_round_priority": "P1"},
        "manual_signals": {"trends_result": "", "keyword_opportunity": ""},
        "serp_queries": [f"Game {app_id}"],
        "requested_checks": ["GAME_WIDE_SOCIAL", "GOOGLE_ORGANIC_SERP"],
        "created_at": "2026-08-24T01:00:00Z",
    }


def _fetch_fn(jobs: list[dict]):
    def fetch(_url: str) -> dict:
        return {"jobs": jobs}

    return fetch


def _write_completed(root: Path, job: dict) -> None:
    path = root / "jobs" / job["job_id"] / "steam_candidate_research_result.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "job_id": job["job_id"],
                "job_type": "STEAM_CANDIDATE_RESEARCH",
                "steam_app_id": job["steam_app_id"],
                "research_status": "COMPLETED",
            }
        ),
        encoding="utf-8",
    )


def _run_success(
    calls: list[str], *, serp_status: str = "AVAILABLE", serp_error: str | None = None
):
    def run(job_path: Path, *, root: Path) -> dict:
        job = json.loads(job_path.read_text(encoding="utf-8"))
        calls.append(job["job_id"])
        result_path = root / "jobs" / job["job_id"] / "steam_candidate_research_result.json"
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(
            json.dumps(
                {
                    "job_id": job["job_id"],
                    "job_type": "STEAM_CANDIDATE_RESEARCH",
                    "steam_app_id": job["steam_app_id"],
                    "research_status": "COMPLETED",
                    "serp": {"status": serp_status, "error": serp_error},
                }
            ),
            encoding="utf-8",
        )
        return {
            "ok": True,
            "execution_status": "COMPLETED",
            "callback_ok": True,
            "callback_payload": {"recommendation": "RECOMMEND_WATCH"},
        }

    return run


class SteamCandidateDailyExecutorTests(unittest.TestCase):
    def test_preliminary_rank_uses_free_signals_and_excludes_controls(self) -> None:
        jobs = [
            _job("9100", first_round_type="🏢大盘对照"),
            _job("9101", first_round_type="🔥趋势"),
            _job("9102", first_round_type="🌱Early"),
        ]
        artifacts = {
            "9101": {
                "preflight": {
                    "checked_at": "2026-09-08T10:00:00+08:00",
                    "autocomplete": {"status": "AVAILABLE", "guide_intent": True, "relevant_ratio": 0.8},
                },
                "social": {"evidence_count": 6, "actionable_cluster_count": 1},
            },
            "9102": {
                "preflight": {"autocomplete": {"status": "AVAILABLE", "guide_intent": False}},
                "social": {"evidence_count": 1},
            },
        }
        ranked = executor.preliminary_rank_candidates(jobs, artifacts)
        self.assertEqual(ranked[0]["steam_app_id"], "9101")
        self.assertTrue(ranked[0]["paid_eligible"])
        control = next(row for row in ranked if row["steam_app_id"] == "9100")
        self.assertFalse(control["paid_eligible"])
        self.assertIn("CONTROL_EXCLUDED", control["reasons"])

    def test_paid_gate_counts_candidate_games_and_reuses_cache(self) -> None:
        ranked = [
            {"steam_app_id": str(9200 + index), "paid_eligible": True, "preliminary_score": 10 - index}
            for index in range(5)
        ]
        gate = executor.select_paid_top_candidates(ranked, paid_cache={"9201"}, daily_candidate_budget=3)
        self.assertEqual([row["steam_app_id"] for row in gate["selected"]], ["9200", "9201", "9202"])
        self.assertEqual(gate["new_paid_candidates"], ["9200", "9202"])
        self.assertTrue(gate["selected"][1]["paid_cache_hit"])
        self.assertTrue(gate["non_top3_paid_forbidden"])

    def test_fresh_jobs_are_capped_by_paid_budget(self) -> None:
        jobs = [_job(str(1000 + index), first_round_type=TYPES[index % 4]) for index in range(10)]
        calls: list[str] = []
        with tempfile.TemporaryDirectory() as tmp:
            summary = executor.run_daily_executor(
                root=Path(tmp),
                fetch_fn=_fetch_fn(jobs),
                run_fn=_run_success(calls),
                max_jobs=10,
                paid_attempt_budget=3,
            )
        self.assertEqual(summary["processed"], 3)
        self.assertEqual(summary["fresh_paid_attempts"], 3)
        self.assertEqual(len(calls), 3)

    def test_reuses_do_not_consume_paid_budget(self) -> None:
        jobs = [_job(str(2000 + index)) for index in range(5)]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_completed(root, jobs[0])
            _write_completed(root, jobs[1])
            calls: list[str] = []
            summary = executor.run_daily_executor(
                root=root,
                fetch_fn=_fetch_fn(jobs),
                run_fn=_run_success(calls),
                max_jobs=5,
                paid_attempt_budget=3,
            )
        self.assertEqual(summary["processed"], 5)
        self.assertEqual(summary["fresh_paid_attempts"], 3)
        self.assertEqual(summary["artifact_reuses"], 2)
        self.assertEqual(len(calls), 5)

    def test_duplicate_app_id_keeps_first_queue_entry(self) -> None:
        first = _job("3000", job_id="first")
        second = _job("3000", job_id="second")
        fetched, jobs, excluded = executor.normalize_and_sort_jobs({"jobs": [first, second]})
        self.assertEqual(fetched, 2)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["job_id"], "first")
        self.assertEqual(excluded, [])

    def test_priority_and_stable_queue_order(self) -> None:
        jobs = [
            _job("4000", first_round_type="unknown"),
            _job("4001", first_round_type="⚪低优先级"),
            _job("4002", first_round_type="🔥趋势"),
            _job("4003", first_round_type="🌱Early"),
            _job("4004", first_round_type="🏢大盘对照"),
            _job("4005", first_round_type="🔥趋势"),
        ]
        _fetched, sorted_jobs, excluded = executor.normalize_and_sort_jobs({"jobs": jobs})
        self.assertEqual([job["steam_app_id"] for job in sorted_jobs], ["4002", "4005", "4003", "4004", "4001", "4000"])
        self.assertEqual(excluded, [])

    def test_callback_failure_stops_batch(self) -> None:
        jobs = [_job("5000"), _job("5001")]
        calls: list[str] = []

        def run(job_path: Path, *, root: Path) -> dict:
            calls.append(json.loads(job_path.read_text())["job_id"])
            return {"execution_status": "COMPLETED", "callback_ok": False}

        with tempfile.TemporaryDirectory() as tmp:
            summary = executor.run_daily_executor(root=Path(tmp), fetch_fn=_fetch_fn(jobs), run_fn=run)
        self.assertEqual(calls, [jobs[0]["job_id"]])
        self.assertTrue(summary["stopped_early"])
        self.assertEqual(summary["stop_reason"], "callback_failed")

    def test_execution_failure_stops_batch(self) -> None:
        jobs = [_job("5500"), _job("5501")]
        calls: list[str] = []

        def run(job_path: Path, *, root: Path) -> dict:
            calls.append(json.loads(job_path.read_text())["job_id"])
            return {"execution_status": "FAILED", "callback_ok": True}

        with tempfile.TemporaryDirectory() as tmp:
            summary = executor.run_daily_executor(root=Path(tmp), fetch_fn=_fetch_fn(jobs), run_fn=run)
        self.assertEqual(calls, [jobs[0]["job_id"]])
        self.assertEqual(summary["stop_reason"], "execution_failed")

    def test_serp_transport_or_http_failure_stops_batch(self) -> None:
        for error in ("searchapi_transport_error", "searchapi_http_error"):
            jobs = [_job("6000"), _job("6001")]
            calls: list[str] = []
            with tempfile.TemporaryDirectory() as tmp:
                summary = executor.run_daily_executor(
                    root=Path(tmp),
                    fetch_fn=_fetch_fn(jobs),
                    run_fn=_run_success(calls, serp_status="UNAVAILABLE", serp_error=error),
                )
            self.assertEqual(calls, [jobs[0]["job_id"]])
            self.assertEqual(summary["stop_reason"], "fresh_serp_unavailable")

    def test_social_unavailable_serp_available_and_watch_continue(self) -> None:
        jobs = [_job("7000"), _job("7001")]
        calls: list[str] = []
        with tempfile.TemporaryDirectory() as tmp:
            summary = executor.run_daily_executor(
                root=Path(tmp), fetch_fn=_fetch_fn(jobs), run_fn=_run_success(calls, serp_status="AVAILABLE")
            )
        self.assertEqual(summary["processed"], 2)
        self.assertFalse(summary["stopped_early"])
        self.assertEqual(len(calls), 2)

    def test_dry_run_never_calls_runner_or_provider(self) -> None:
        jobs = [_job("8000"), _job("8001"), _job("8002")]
        runner_calls: list[str] = []
        provider_calls: list[str] = []

        def run(*_args, **_kwargs):
            runner_calls.append("runner")
            provider_calls.append("provider")
            raise AssertionError("dry-run must not call runner/provider")

        with tempfile.TemporaryDirectory() as tmp:
            summary = executor.run_daily_executor(
                root=Path(tmp), fetch_fn=_fetch_fn(jobs), run_fn=run, dry_run=True,
                now=datetime(2026, 8, 24, 1, 0, 0),
            )
            summary_text = (Path(tmp) / "jobs" / "daily-steam-candidate-research-20260824" / "daily_run_summary.json").read_text()
        self.assertEqual(summary["pending_fetched"], 3)
        self.assertEqual(len(summary["planned_candidates"]), 3)
        self.assertEqual(runner_calls, [])
        self.assertEqual(provider_calls, [])
        self.assertNotIn("SEARCHAPI_API_KEY", summary_text)
        self.assertNotIn("Authorization", summary_text)
        self.assertNotIn("callback token", summary_text)

    def test_limits_cli_values_override_environment(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                executor.JOB_LIMIT_ENV: "9",
                executor.PAID_BUDGET_ENV: "8",
            },
        ):
            self.assertEqual(executor.resolve_limits(2, 1), (2, 1))
            self.assertEqual(executor.resolve_limits(), (9, 8))


if __name__ == "__main__":
    unittest.main()
