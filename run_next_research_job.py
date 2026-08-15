#!/usr/bin/env python3
"""Consume at most one PENDING Research Job from Google Sheet.

Flow:
  Pending Job API → first PENDING → input/research_job.json
  → research_job_runner → REVIEW/FAILED → Sheet callback

Reuses fetch_pending_jobs.py and research_job_runner.py.
Does not batch, schedule, or invent Research logic.

Usage:
  RESEARCH_CALLBACK_URL=... RESEARCH_CALLBACK_TOKEN=... \\
    python3 run_next_research_job.py
"""

from __future__ import annotations

import sys

import fetch_pending_jobs as fpj
import research_job_runner as rjr


def main() -> int:
    payload = fpj.fetch_pending_jobs()
    pending_path = fpj.ROOT / "input" / "pending_jobs.json"
    job_path = fpj.ROOT / "input" / "research_job.json"
    fpj.write_json(pending_path, payload)

    jobs = payload.get("jobs") or []
    pending = [job for job in jobs if fpj.is_pending(job)]
    if not pending:
        print("No pending research jobs")
        return 0

    research_job = fpj.to_research_job(pending[0])
    fpj.write_json(job_path, research_job)

    print(f"selected_job_id={research_job['job_id']}")
    print(f"selected_topic={research_job['topic']}")
    print(f"Wrote {pending_path}")
    print(f"Wrote {job_path}")

    status = rjr.run_job(job_path)
    return 0 if status.get("status") in {"REVIEW", "WATCH"} else 1


if __name__ == "__main__":
    sys.exit(main())
