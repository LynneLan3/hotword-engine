#!/usr/bin/env python3
"""Daily Twitch Top Games raw observation → Historical Raw Ledger.

Fact collection only: no opportunity score, thresholds, auto BUILD, or Steam
1A/1B weight changes. Does not write production 候选主表.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from opportunity_discovery.sources.twitch import (
    TWITCH_MAX_LIMIT,
    TwitchTopGamesAdapter,
    append_twitch_run,
    write_twitch_artifact,
)
from opportunity_discovery.twitch_historical_raw_ledger import (
    TWITCH_LEDGER_SPREADSHEET_NAME,
    build_ledger_payload,
    map_run_statuses,
)
import twitch_historical_ledger_callback as ledger_callback

ROOT = Path(__file__).resolve().parent
REQUESTED_LIMIT_ENV = "TWITCH_DAILY_REQUESTED_LIMIT"
DEFAULT_REQUESTED_LIMIT = TWITCH_MAX_LIMIT
CollectFn = Callable[[], dict[str, Any]]
PostFn = ledger_callback.PostFn


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _positive_int(value: Any, name: str) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if parsed < 1:
        raise ValueError(f"{name} must be a positive integer")
    return parsed


def resolve_requested_limit(limit: int | None = None) -> int:
    if limit is not None:
        return min(_positive_int(limit, "--requested-limit"), TWITCH_MAX_LIMIT)
    raw = os.environ.get(REQUESTED_LIMIT_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_REQUESTED_LIMIT
    return min(_positive_int(raw, REQUESTED_LIMIT_ENV), TWITCH_MAX_LIMIT)


def _run_dir(root: Path, run_date: str) -> Path:
    return root / "jobs" / f"daily-twitch-raw-observation-{run_date}"


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run_daily_twitch_raw_observation(
    *,
    root: Path = ROOT,
    collect_fn: CollectFn | None = None,
    post_fn: PostFn | None = None,
    requested_limit: int | None = None,
    dry_run: bool = False,
    run_type: str = "SCHEDULED_DAILY",
    trigger_type: str = "SCHEDULED_TRIGGER",
    now: datetime | None = None,
) -> dict[str, Any]:
    started = now or datetime.now(timezone.utc)
    run_date = started.strftime("%Y%m%d")
    run_dir = _run_dir(root, run_date)
    limit = resolve_requested_limit(requested_limit)
    history_path = root / "jobs" / "twitch-historical-raw-ledger" / "history.json"

    summary: dict[str, Any] = {
        "run_date": started.strftime("%Y-%m-%d"),
        "requested_limit": limit,
        "dry_run": bool(dry_run),
        "started_at": started.isoformat(timespec="seconds"),
        "finished_at": None,
        "run_id": None,
        "run_status": None,
        "final_status": None,
        "discovery_completeness": None,
        "rows_returned": 0,
        "pages_completed": 0,
        "ledger_spreadsheet_name": TWITCH_LEDGER_SPREADSHEET_NAME,
        "ledger_ok": None,
        "ledger_error": None,
        "canonical_run_id": None,
        "wrote_candidate_master": False,
        "artifact_paths": {},
    }

    artifact = collect_fn() if collect_fn is not None else TwitchTopGamesAdapter(requested_limit=limit).collect()
    if not isinstance(artifact, dict):
        raise ValueError("Twitch collect() must return a dict artifact")

    metadata = artifact.get("run_metadata") if isinstance(artifact.get("run_metadata"), dict) else {}
    run_status = _text(metadata.get("run_status")) or "UNKNOWN"
    final_status, discovery = map_run_statuses(run_status)
    finished = datetime.now(timezone.utc)
    summary.update(
        {
            "run_id": _text(artifact.get("run_id")),
            "run_status": run_status,
            "final_status": final_status,
            "discovery_completeness": discovery,
            "rows_returned": int(metadata.get("rows_returned") or 0),
            "pages_completed": int(metadata.get("pages_completed") or 0),
        }
    )

    twitch_path = run_dir / "twitch_observation.json"
    run_dir.mkdir(parents=True, exist_ok=True)
    history_path.parent.mkdir(parents=True, exist_ok=True)
    write_twitch_artifact(artifact, twitch_path)
    append_twitch_run(history_path, artifact)
    summary["artifact_paths"] = {
        "twitch_observation": str(twitch_path.relative_to(root)),
        "history": str(history_path.relative_to(root)),
    }

    payload = build_ledger_payload(
        artifact,
        run_type=run_type,
        trigger_type=trigger_type,
        started_at=started.isoformat(timespec="seconds"),
        finished_at=finished.isoformat(timespec="seconds"),
    )
    summary_path = run_dir / "daily_run_summary.json"

    if dry_run:
        summary["planned_ledger"] = {
            "job_type": payload["job_type"],
            "raw_rows": len(payload["raw_observation_rows"]),
            "run_id": payload["run_id"],
        }
        summary["finished_at"] = finished.isoformat(timespec="seconds")
        if final_status == "SUCCESS" and discovery == "COMPLETE":
            summary["canonical_run_id"] = payload["run_id"]
        _write_json(summary_path, summary)
        return summary

    ok, error, response = ledger_callback.post_twitch_historical_ledger(payload, post_fn=post_fn)
    summary["ledger_ok"] = ok
    summary["ledger_error"] = error
    if isinstance(response, dict):
        summary["ledger_response"] = {
            key: response.get(key)
            for key in (
                "spreadsheetId",
                "spreadsheetUrl",
                "spreadsheetName",
                "rawAppended",
                "runLedgerAppended",
                "canonicalRunId",
                "tabs",
            )
            if key in response
        }
        summary["canonical_run_id"] = response.get("canonicalRunId")
    elif final_status == "SUCCESS" and discovery == "COMPLETE":
        summary["canonical_run_id"] = payload["run_id"]

    summary["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _write_json(summary_path, summary)
    summary["artifact_paths"]["daily_run_summary"] = str(summary_path.relative_to(root))
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Daily Twitch Top Games raw observation ledger run")
    parser.add_argument("--requested-limit", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--run-type",
        default="SCHEDULED_DAILY",
        choices=["SCHEDULED_DAILY", "MANUAL_PRODUCTION", "TEST"],
    )
    parser.add_argument(
        "--trigger-type",
        default="SCHEDULED_TRIGGER",
        choices=["SCHEDULED_TRIGGER", "MANUAL"],
    )
    args = parser.parse_args(argv)
    try:
        summary = run_daily_twitch_raw_observation(
            requested_limit=args.requested_limit,
            dry_run=args.dry_run,
            run_type=args.run_type,
            trigger_type=args.trigger_type,
        )
    except ValueError as exc:
        print(f"ERROR: {exc}")
        return 2
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    # Auth / hard failures are non-zero; PARTIAL still persists rows and exits 0.
    if summary.get("run_status") in {"AUTH_MISSING", "AUTH_FAILED", "HTTP_ERROR"} and not summary.get("rows_returned"):
        return 1
    if summary.get("ledger_ok") is False and not args.dry_run:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
