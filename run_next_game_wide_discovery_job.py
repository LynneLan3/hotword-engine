#!/usr/bin/env python3
"""Consume at most one pending GAME_WIDE discovery job."""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import game_wide_social_runner as gws

ROOT = Path(__file__).resolve().parent
PENDING_GAME_WIDE_JOBS_URL = (
    "https://script.google.com/macros/s/"
    "AKfycbwILJmfmk_PRtjgGffPzX1ZebnGTf9TzAbinkalMNBu5y4PsMbW4L_IdeJJTYGOuQzf"
    "/exec?action=pendingGameWideDiscoveryJobs"
)
JOB_TYPE = gws.JOB_TYPE


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def fetch_pending_game_wide_jobs() -> dict[str, Any]:
    request = urllib.request.Request(
        PENDING_GAME_WIDE_JOBS_URL,
        headers={"User-Agent": "hotword-engine-fetch-pending-game-wide-jobs/1"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Failed to fetch pending GAME_WIDE jobs: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
        raise SystemExit("Pending GAME_WIDE jobs API response missing a jobs array")
    return payload


def is_pending_game_wide_job(job: Any) -> bool:
    if not isinstance(job, dict):
        return False
    if str(job.get("job_type") or "").strip().upper() != JOB_TYPE:
        return False
    return str(job.get("status") or "").strip().upper() in {"", "PENDING"}


def main() -> int:
    payload = fetch_pending_game_wide_jobs()
    pending_path = ROOT / "input" / "pending_game_wide_discovery_jobs.json"
    job_path = ROOT / "input" / "game_wide_discovery_job.json"
    write_json(pending_path, payload)

    pending = [job for job in payload["jobs"] if is_pending_game_wide_job(job)]
    if not pending:
        print("No pending GAME_WIDE discovery jobs")
        return 0

    selected = pending[0]
    write_json(job_path, selected)
    print(f"selected_job_id={selected.get('job_id')}")
    print(f"Wrote {pending_path}")
    print(f"Wrote {job_path}")

    outcome = gws.run_job(job_path)
    return 0 if outcome.get("ok", True) else 1


if __name__ == "__main__":
    sys.exit(main())
