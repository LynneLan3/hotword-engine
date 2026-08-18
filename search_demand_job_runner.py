#!/usr/bin/env python3
"""Local SEARCH_DEMAND job runner.

Loads one local Search Demand job, runs search_demand_runner, and writes
jobs/<job_id>/{job.json,search_demand_result.json,status.json}.

No callback POST. No Apps Script writeback. No scheduler.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import search_demand_runner as sdr

ROOT = Path(__file__).resolve().parent


def main() -> int:
    parser = argparse.ArgumentParser(description="Local SEARCH_DEMAND job runner (no callback)")
    parser.add_argument(
        "job_file",
        nargs="?",
        default=str(ROOT / "input" / "search_demand_job.json"),
        help="Path to search_demand_job.json",
    )
    args = parser.parse_args()
    job_path = Path(args.job_file)
    if not job_path.is_absolute():
        job_path = (Path.cwd() / job_path).resolve()
    if not job_path.exists():
        sdr.log(f"Job file not found: {job_path}")
        return 2
    outcome = sdr.run_job(job_path)
    result = outcome["result"]
    cb = sdr.to_callback_fields(result)
    sdr.log(
        "local SEARCH_DEMAND done job_id={job} execution={ex} "
        "search_demand_status={sds} search_sources={src} "
        "search_evidence_count={sec} callback_ok=null".format(
            job=outcome["job_id"],
            ex=outcome["status"],
            sds=cb.get("search_demand_status"),
            src=cb.get("search_sources"),
            sec=cb.get("search_evidence_count"),
        )
    )
    return 0 if outcome["status"] != sdr.EXEC_FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
