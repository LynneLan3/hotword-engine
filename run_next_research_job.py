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

import os
import sys

import batch_receipt as br
import fetch_pending_jobs as fpj
import research_job_runner as rjr


def main() -> int:
    started_at = br.now_iso()
    identifier = br.batch_id(started_at)
    receipt_file = br.receipt_path(fpj.ROOT, identifier)
    br.write_receipt(
        receipt_file,
        br.build_receipt(
            identifier=identifier,
            started_at=started_at,
            finished_at=started_at,
            status="RESEARCH_PENDING",
            stage="RESEARCH_PENDING",
        ),
    )

    def finish(receipt: dict[str, object], exit_code: int) -> int:
        br.write_receipt(receipt_file, receipt)
        github_output = os.environ.get("GITHUB_OUTPUT")
        if github_output:
            with open(github_output, "a", encoding="utf-8") as output:
                output.write(f"batch_id={identifier}\n")
                output.write(f"receipt_path={receipt_file.relative_to(fpj.ROOT)}\n")
        print(receipt["summary"])
        return exit_code

    try:
        payload = fpj.fetch_pending_jobs()
    except fpj.PendingJobsFetchError as exc:
        finished_at = br.now_iso()
        receipt = br.build_receipt(
            identifier=identifier,
            started_at=started_at,
            finished_at=finished_at,
            status="FAILED",
            stage="PENDING_JOB_FETCH",
            failure_stage="PENDING_JOB_FETCH",
            failure_reason=exc.reason,
            failure_detail=exc.detail,
        )
        print(f"Research job has_job=false final_failure_reason={exc.reason}")
        return finish(receipt, 1)
    pending_path = fpj.ROOT / "input" / "pending_jobs.json"
    job_path = fpj.ROOT / "input" / "research_job.json"
    fpj.write_json(pending_path, payload)

    jobs = payload.get("jobs") or []
    pending = [job for job in jobs if fpj.is_pending(job)]
    if not pending:
        receipt = br.build_receipt(
            identifier=identifier,
            started_at=started_at,
            finished_at=br.now_iso(),
            status="NO_PENDING_JOB",
            stage="RESEARCH_PENDING",
        )
        print("Research job has_job=false final_failure_reason=NO_PENDING_JOB")
        return finish(receipt, 0)

    selected = pending[0]
    job_id = str(selected.get("job_id") or "").strip() or None
    try:
        research_job = fpj.to_research_job(selected)
        research_job["batch_id"] = identifier
    except (SystemExit, ValueError) as exc:
        receipt = br.build_receipt(
            identifier=identifier,
            started_at=started_at,
            finished_at=br.now_iso(),
            status="FAILED",
            stage="RESEARCH",
            job_id=job_id,
            failure_stage="RESEARCH",
            failure_reason="RESEARCH_FAILED",
            failure_detail=str(exc),
        )
        print(f"Research job has_job=true job_id={job_id or 'UNKNOWN'} final_failure_reason=RESEARCH_FAILED")
        return finish(receipt, 1)
    fpj.write_json(job_path, research_job)

    print(f"has_job=true job_id={research_job['job_id']}")
    print(f"selected_topic={research_job['topic']}")
    print(f"Wrote {pending_path}")
    print(f"Wrote {job_path}")

    status = rjr.run_job(job_path)
    research_status = str(status.get("status") or "").strip().upper()
    evidence_count = int(status.get("evidence_count") or 0)
    callback_ok = status.get("callback_ok") is not False
    if research_status in {"REVIEW", "WATCH"} and callback_ok:
        receipt_status = "RESEARCH_PASS"
        stage = "RESEARCH_PASS"
        failure_stage = None
        failure_reason = None
    else:
        receipt_status = "FAILED"
        stage = "RESEARCH"
        failure_stage = "CALLBACK" if research_status in {"REVIEW", "WATCH"} else "RESEARCH"
        failure_reason = "RESEARCH_FAILED"
    research_row = br.research_row(research_job, research_status, evidence_count)
    receipt = br.build_receipt(
        identifier=identifier,
        started_at=started_at,
        finished_at=br.now_iso(),
        status=receipt_status,
        stage=stage,
        research_jobs=[research_row],
        evidence={
            "PASS": evidence_count if research_status in {"REVIEW", "WATCH"} and callback_ok else 0,
            "HOLD": 1 if research_status == "WATCH" else 0,
            "FAIL": 1 if receipt_status == "FAILED" else 0,
        },
        site=str(research_job.get("game") or ""),
        job_id=str(research_job["job_id"]),
        failure_stage=failure_stage,
        failure_reason=failure_reason,
        failure_detail=str(status.get("error") or "callback_failed") if failure_reason else None,
        research_artifact=f"jobs/{research_job['job_id']}/research_result.json",
    )
    if receipt_status == "FAILED":
        print(
            f"Research job has_job=true job_id={research_job['job_id']} "
            f"final_failure_reason={failure_reason}"
        )
    return finish(receipt, rjr.exit_code_from_run(status))


if __name__ == "__main__":
    sys.exit(main())
