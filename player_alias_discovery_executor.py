#!/usr/bin/env python3
"""Bounded executor for pending PLAYER_ALIAS_DISCOVERY jobs."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import fetch_pending_player_alias_discovery_jobs as fetcher
import player_alias_discovery_job_runner as job_runner

ROOT = Path(__file__).resolve().parent
MAX_JOBS_ENV = "PLAYER_ALIAS_DISCOVERY_MAX_JOBS"
TARGET_APP_IDS_ENV = "PLAYER_ALIAS_TARGET_APP_IDS"
CONTINUE_ON_CALLBACK_FAIL_ENV = "PLAYER_ALIAS_CONTINUE_ON_CALLBACK_FAIL"
DEFAULT_MAX_JOBS = 8


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def _positive_int(value: Any, name: str) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a non-negative integer") from exc
    if parsed < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return parsed


def _target_app_ids() -> set[str] | None:
    raw = os.environ.get(TARGET_APP_IDS_ENV, "").strip()
    if not raw:
        return None
    return {part.strip() for part in raw.split(",") if part.strip()}


def _continue_on_callback_fail() -> bool:
    return os.environ.get(CONTINUE_ON_CALLBACK_FAIL_ENV, "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def run_batch(
    *,
    max_jobs: int | None = None,
    dry_run: bool = False,
    root: Path = ROOT,
    fetch_fn: Any = None,
    post_fn: Any = None,
    collect_fn: Any = None,
) -> dict[str, Any]:
    limit = (
        _positive_int(max_jobs, "--max-jobs")
        if max_jobs is not None
        else _positive_int(
            os.environ.get(MAX_JOBS_ENV) or DEFAULT_MAX_JOBS,
            MAX_JOBS_ENV,
        )
    )
    payload = fetcher.fetch_pending_player_alias_discovery_jobs(fetch_fn=fetch_fn)
    fetcher.save_pending_player_alias_discovery_jobs(payload, root=root)
    jobs = [job for job in (payload.get("jobs") or []) if isinstance(job, dict)]
    targets = _target_app_ids()
    if targets:
        jobs = [job for job in jobs if _text(job.get("steam_app_id")) in targets]

    selected = jobs[:limit]
    results: list[dict[str, Any]] = []
    stopped_early = False

    for job in selected:
        validated = fetcher.to_player_alias_discovery_job(job)
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            path = Path(handle.name)
            handle.write(json.dumps(validated, ensure_ascii=False, indent=2))
        try:
            outcome = job_runner.run_job(
                path,
                collect_fn=collect_fn,
                post_fn=post_fn,
                root=root,
                dry_run=dry_run,
            )
        finally:
            try:
                path.unlink()
            except OSError:
                pass

        row = {
            "job_id": validated["job_id"],
            "steam_app_id": validated["steam_app_id"],
            "game_name": validated["game_name"],
            "alias": (outcome.get("result") or {}).get("alias")
            or (outcome.get("callback") or {}).get("alias"),
            "status": (outcome.get("result") or {}).get("status")
            or (outcome.get("callback") or {}).get("status"),
            "callback_ok": outcome.get("callback_ok"),
            "callback_error": outcome.get("callback_error"),
            "source_urls": ((outcome.get("result") or {}).get("source_urls") or [])[:5],
        }
        results.append(row)
        if not outcome.get("callback_ok") and not dry_run and not _continue_on_callback_fail():
            stopped_early = True
            break

    summary = {
        "run_date": _now_stamp(),
        "pending_count": len(jobs),
        "selected_count": len(selected),
        "processed_count": len(results),
        "stopped_early": stopped_early,
        "dry_run": dry_run,
        "results": results,
    }
    out_dir = root / "jobs" / f"daily-player-alias-discovery-{summary['run_date']}"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "daily_run_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Run pending player alias discovery jobs")
    parser.add_argument("--max-jobs", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    summary = run_batch(max_jobs=args.max_jobs, dry_run=args.dry_run)
    if summary.get("stopped_early"):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
