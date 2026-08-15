#!/usr/bin/env python3
"""Fetch pending Research Jobs from Google Sheet API.

Writes:
  input/pending_jobs.json   full API response
  input/research_job.json   first PENDING job, mapped to the local contract

Does not run Research, write back to the Sheet, or change job status.

Usage:
  python3 fetch_pending_jobs.py
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
PENDING_JOBS_URL = (
    "https://script.google.com/macros/s/"
    "AKfycbwILJmfmk_PRtjgGffPzX1ZebnGTf9TzAbinkalMNBu5y4PsMbW4L_IdeJJTYGOuQzf"
    "/exec?action=pendingResearchJobs"
)
CONTRACT_FIELDS = (
    "job_id",
    "game",
    "topic",
    "existing_page",
    "opportunity_level",
    "recommended_action",
    "source_query",
    "created_at",
)
OPTIONAL_FIELDS = ("related_queries",)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def fetch_pending_jobs() -> dict[str, Any]:
    request = urllib.request.Request(
        PENDING_JOBS_URL,
        headers={"User-Agent": "hotword-engine-fetch-pending-jobs/1"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8")
    except urllib.error.URLError as exc:
        raise SystemExit(f"Failed to fetch pending jobs: {exc}") from exc
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Pending jobs API did not return JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise SystemExit("Pending jobs API returned a non-object JSON payload")
    jobs = payload.get("jobs")
    if not isinstance(jobs, list):
        raise SystemExit("Pending jobs API response missing a jobs array")
    return payload


def is_pending(job: Any) -> bool:
    if not isinstance(job, dict):
        return False
    status = str(job.get("status") or "PENDING").strip().upper()
    return status == "PENDING"


def to_research_job(job: dict[str, Any]) -> dict[str, Any]:
    missing = [k for k in CONTRACT_FIELDS if not str(job.get(k) or "").strip()]
    if missing:
        raise SystemExit("Selected job missing fields: " + ", ".join(missing))
    out: dict[str, Any] = {k: job[k] for k in CONTRACT_FIELDS}
    for key in OPTIONAL_FIELDS:
        if key in job:
            out[key] = job[key]
    return out


def main() -> int:
    payload = fetch_pending_jobs()
    pending_path = ROOT / "input" / "pending_jobs.json"
    job_path = ROOT / "input" / "research_job.json"
    write_json(pending_path, payload)

    jobs = payload.get("jobs") or []
    pending = [job for job in jobs if is_pending(job)]
    if not pending:
        print(f"Wrote {pending_path}")
        print(f"jobs={len(jobs)} pending=0")
        print("No PENDING job to write to research_job.json")
        return 1

    selected = pending[0]
    research_job = to_research_job(selected)
    write_json(job_path, research_job)

    print(f"Wrote {pending_path}")
    print(f"Wrote {job_path}")
    print(f"jobs={len(jobs)} pending={len(pending)}")
    print(f"selected_job_id={research_job['job_id']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
