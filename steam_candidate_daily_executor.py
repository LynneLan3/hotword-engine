#!/usr/bin/env python3
"""Bounded daily executor for Steam Candidate Research jobs.

This module owns only queue selection, budget accounting, and circuit-breaker
behavior. Each selected job is executed by the machine research executor.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import fetch_pending_steam_candidate_research_jobs as fetcher
import steam_candidate_research_job_runner as runner
import steam_candidate_preflight as preflight
import steam_candidate_machine_research_executor as machine_research_executor

ROOT = Path(__file__).resolve().parent
JOB_LIMIT_ENV = "STEAM_CANDIDATE_RESEARCH_DAILY_JOB_LIMIT"
PAID_BUDGET_ENV = "STEAM_CANDIDATE_RESEARCH_DAILY_PAID_ATTEMPT_BUDGET"
DEFAULT_MAX_TOTAL_JOBS = 5
DEFAULT_PAID_ATTEMPT_BUDGET = 3
INJECTED_FETCH_URL = "https://steam.example/exec?action=pendingSteamCandidateResearchJobs"

RunFn = Callable[..., dict[str, Any]]


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _positive_int(value: Any, name: str) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a non-negative integer") from exc
    if parsed < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return parsed


def _configured_limit(env_name: str, default: int) -> int:
    raw = os.environ.get(env_name)
    if raw is None or not raw.strip():
        return default
    return _positive_int(raw, env_name)


def resolve_limits(
    max_jobs: int | None = None,
    paid_attempt_budget: int | None = None,
) -> tuple[int, int]:
    job_limit = (
        _positive_int(max_jobs, "--max-jobs")
        if max_jobs is not None
        else _configured_limit(JOB_LIMIT_ENV, DEFAULT_MAX_TOTAL_JOBS)
    )
    paid_budget = (
        _positive_int(paid_attempt_budget, "--paid-attempt-budget")
        if paid_attempt_budget is not None
        else _configured_limit(PAID_BUDGET_ENV, DEFAULT_PAID_ATTEMPT_BUDGET)
    )
    return job_limit, paid_budget


def _priority(first_round_type: Any) -> int:
    value = _text(first_round_type).replace(" ", "")
    if value.startswith("🔥") or "趋势" in value:
        return 0
    if value.startswith("🌱") or "Early" in value or "early" in value:
        return 1
    if value.startswith("🏢") or "大盘" in value or "对照" in value:
        return 2
    if value.startswith("⚪") or "低优先级" in value:
        return 3
    return 4


def normalize_and_sort_jobs(payload: dict[str, Any]) -> tuple[int, list[dict[str, Any]]]:
    jobs = payload.get("jobs")
    if not isinstance(jobs, list):
        raise ValueError("Steam candidate research API response missing a jobs array")

    unique: list[dict[str, Any]] = []
    seen_app_ids: set[str] = set()
    for raw_job in jobs:
        job = fetcher.to_steam_candidate_research_job(raw_job)
        state = preflight._decision_state(job)
        status = _text(state.get("status")).upper()
        if state.get("one_a_excluded") or status in {"REJECT", "BUILD"}:
            continue
        if status == "WATCH" and state.get("next_review_date"):
            try:
                review_date = str(state["next_review_date"])[:10]
                if datetime.now().strftime("%Y-%m-%d") < review_date:
                    continue
            except (TypeError, ValueError):
                pass
        app_id = _text(job.get("steam_app_id"))
        if app_id in seen_app_ids:
            continue
        seen_app_ids.add(app_id)
        unique.append(job)

    # Python's sort is stable, so queue order is preserved within each type.
    return len(jobs), sorted(
        unique,
        key=lambda job: _priority((job.get("steam_signals") or {}).get("first_round_type")),
    )


def _completed_artifact(root: Path, job: dict[str, Any]) -> bool:
    job_id = _text(job.get("job_id"))
    preflight_path = root / "jobs" / job_id / "steam_candidate_preflight.json"
    if preflight_path.exists():
        try:
            result = json.loads(preflight_path.read_text(encoding="utf-8"))
            if _text(result.get("preflight_verdict")) in {"AUTO_REJECT", "WATCH", "MANUAL_REVIEW"}:
                return True
        except (OSError, ValueError):
            pass
    path = root / "jobs" / job_id / "steam_candidate_research_result.json"
    if not path.exists():
        return False
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return runner._valid_completed_result(result, job)


def _selection(
    jobs: list[dict[str, Any]],
    *,
    max_jobs: int,
    paid_attempt_budget: int,
    root: Path,
) -> tuple[list[dict[str, Any]], int, int]:
    selected: list[dict[str, Any]] = []
    fresh_attempts = 0
    reuses = 0
    for job in jobs:
        if len(selected) >= max_jobs:
            break
        reused = _completed_artifact(root, job)
        if not reused and fresh_attempts >= paid_attempt_budget:
            continue
        selected.append(job)
        if reused:
            reuses += 1
        else:
            fresh_attempts += 1
    return selected, fresh_attempts, reuses


def _result_summary(job: dict[str, Any], outcome: dict[str, Any], reused: bool) -> dict[str, Any]:
    callback_payload = outcome.get("callback_payload") if isinstance(outcome, dict) else {}
    callback_payload = callback_payload if isinstance(callback_payload, dict) else {}
    return {
        "job_id": _text(job.get("job_id")),
        "steam_app_id": _text(job.get("steam_app_id")),
        "game_name": _text(job.get("game_name")),
        "first_round_type": _text((job.get("steam_signals") or {}).get("first_round_type")),
        "fresh_paid_attempt": not reused,
        "reused_artifact": reused,
        "execution_status": _text(outcome.get("execution_status")).upper(),
        "callback_ok": outcome.get("callback_ok"),
        "recommendation": _text(callback_payload.get("recommendation")) or None,
        "preflight_verdict": _text(outcome.get("preflight_verdict")) or None,
    }


def _fresh_serp_unavailable(root: Path, job: dict[str, Any]) -> bool:
    path = root / "jobs" / _text(job.get("job_id")) / "steam_candidate_research_result.json"
    if not path.exists():
        return False
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    serp = result.get("serp") if isinstance(result, dict) else None
    if not isinstance(serp, dict):
        return False
    if _text(serp.get("status")).upper() == "UNAVAILABLE":
        return True
    queries = serp.get("queries")
    if not isinstance(queries, list) or not queries:
        return False
    return not any(
        _text(query.get("status")).upper() in {"SUPPORTED", "AVAILABLE"}
        for query in queries
        if isinstance(query, dict)
    )


def _summary_path(root: Path, run_date: str) -> Path:
    return root / "jobs" / f"daily-steam-candidate-research-{run_date}" / "daily_run_summary.json"


def _write_summary(path: Path, summary: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run_daily_executor(
    *,
    root: Path = ROOT,
    fetch_fn: Callable[[str], dict[str, Any]] | None = None,
    run_fn: RunFn | None = None,
    max_jobs: int | None = None,
    paid_attempt_budget: int | None = None,
    dry_run: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    job_limit, paid_budget = resolve_limits(max_jobs, paid_attempt_budget)
    run_fn = run_fn or machine_research_executor.run_job
    started = now or datetime.now()
    run_date = started.strftime("%Y%m%d")
    if fetch_fn is None:
        payload = fetcher.fetch_pending_steam_candidate_research_jobs()
    else:
        payload = fetcher.fetch_pending_steam_candidate_research_jobs(
            url=INJECTED_FETCH_URL,
            fetch_fn=fetch_fn,
        )
    pending_fetched, jobs = normalize_and_sort_jobs(payload)
    selected, fresh_attempts, reuses = _selection(
        jobs,
        max_jobs=job_limit,
        paid_attempt_budget=paid_budget,
        root=root,
    )

    summary: dict[str, Any] = {
        "run_date": started.strftime("%Y-%m-%d"),
        "pending_fetched": pending_fetched,
        "unique_candidates": len(jobs),
        "job_limit": job_limit,
        "paid_attempt_budget": paid_budget,
        "processed": 0,
        "fresh_paid_attempts": 0,
        "artifact_reuses": 0,
        "results": [],
        "stopped_early": False,
        "stop_reason": None,
        "dry_run": bool(dry_run),
        "started_at": started.isoformat(timespec="seconds"),
        "finished_at": None,
    }

    if dry_run:
        summary["planned_candidates"] = [
            {
                "game_name": _text(job.get("game_name")),
                "steam_app_id": _text(job.get("steam_app_id")),
                "job_id": _text(job.get("job_id")),
                "first_round_type": _text((job.get("steam_signals") or {}).get("first_round_type")),
                "fresh_paid_attempt": not _completed_artifact(root, job),
                "reused_artifact": _completed_artifact(root, job),
            }
            for job in selected
        ]
        summary["planned_fresh_paid_attempts"] = fresh_attempts
        summary["planned_artifact_reuses"] = reuses
        summary["finished_at"] = datetime.now().isoformat(timespec="seconds")
        _write_summary(_summary_path(root, run_date), summary)
        return summary

    with tempfile.TemporaryDirectory(prefix="steam-candidate-daily-") as temp_dir:
        temp_root = Path(temp_dir)
        for job in selected:
            reused = _completed_artifact(root, job)
            job_path = temp_root / f"{_text(job.get('job_id'))}.json"
            job_path.write_text(json.dumps(job, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            outcome = run_fn(job_path, root=root)
            if not isinstance(outcome, dict):
                outcome = {"execution_status": "FAILED", "callback_ok": False}
            summary["processed"] += 1
            summary["fresh_paid_attempts"] += 0 if reused else 1
            summary["artifact_reuses"] += 1 if reused else 0
            summary["results"].append(_result_summary(job, outcome, reused))

            if outcome.get("callback_ok") is False:
                summary["stopped_early"] = True
                summary["stop_reason"] = "callback_failed"
                break
            if _text(outcome.get("execution_status")).upper() == runner.EXEC_FAILED:
                summary["stopped_early"] = True
                summary["stop_reason"] = "execution_failed"
                break
            if not reused and _fresh_serp_unavailable(root, job):
                summary["stopped_early"] = True
                summary["stop_reason"] = "fresh_serp_unavailable"
                break

    summary["finished_at"] = datetime.now().isoformat(timespec="seconds")
    _write_summary(_summary_path(root, run_date), summary)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bounded Steam Candidate daily executor")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-jobs", type=int, default=None)
    parser.add_argument("--paid-attempt-budget", type=int, default=None)
    args = parser.parse_args(argv)
    try:
        summary = run_daily_executor(
            max_jobs=args.max_jobs,
            paid_attempt_budget=args.paid_attempt_budget,
            dry_run=args.dry_run,
        )
    except (SystemExit, ValueError) as exc:
        print(f"ERROR: {exc}")
        return 2
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 1 if summary.get("stopped_early") else 0


if __name__ == "__main__":
    raise SystemExit(main())
