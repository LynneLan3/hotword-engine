#!/usr/bin/env python3
"""Minimal Research Job wrapper around research_runner.py.

Takes one GSC content-opportunity job, runs the existing Research Runner,
and leaves artifacts for human review.

No Apps Script, database, queue, API, dashboard, site edits, or articles.

Usage:
  python3 research_job_runner.py input/research_job.json
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path
from typing import Any

import research_runner as rr

ROOT = Path(__file__).resolve().parent
REQUIRED_FIELDS = (
    "job_id",
    "game",
    "topic",
    "existing_page",
    "opportunity_level",
    "recommended_action",
    "source_query",
    "created_at",
)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_job(path: Path) -> dict[str, Any]:
    job = json.loads(path.read_text(encoding="utf-8"))
    missing = [k for k in REQUIRED_FIELDS if not str(job.get(k) or "").strip()]
    if missing:
        raise ValueError("research_job.json missing fields: " + ", ".join(missing))
    return job


def status_payload(
    job: dict[str, Any],
    *,
    status: str,
    started_at: str | None,
    finished_at: str | None = None,
    recommendation: dict[str, Any] | None = None,
    evidence_count: int | None = None,
    result_path: str | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "job_id": job["job_id"],
        "status": status,
        "started_at": started_at,
        "finished_at": finished_at,
        "recommendation": recommendation,
        "evidence_count": evidence_count,
        "result_path": result_path,
    }
    if error:
        out["error"] = error
    return out


def run_job(job_path: Path) -> dict[str, Any]:
    job = load_job(job_path)
    job_id = str(job["job_id"]).strip()
    job_dir = ROOT / "jobs" / job_id
    result_path = job_dir / "research_result.json"
    status_path = job_dir / "status.json"
    job_copy_path = job_dir / "job.json"

    write_json(job_copy_path, job)
    started_at = rr.now_iso()
    write_json(
        status_path,
        status_payload(
            job,
            status="RUNNING",
            started_at=started_at,
            result_path=None,
        ),
    )
    rr.log(f"Research Job     {job_id}")
    rr.log(f"Status           RUNNING")
    rr.log(f"Job dir          {job_dir}")

    try:
        args = argparse.Namespace(
            game=job["game"],
            topic=job["topic"],
            existing_page=job["existing_page"],
            steam_appid=None,
            out=str(result_path),
            reuse=None,
        )
        result = rr.run(args)
        write_json(result_path, result)
        finished = status_payload(
            job,
            status="REVIEW",
            started_at=started_at,
            finished_at=rr.now_iso(),
            recommendation=result.get("recommendation"),
            evidence_count=len(result.get("evidence") or []),
            result_path=str(result_path),
        )
        write_json(status_path, finished)
        rr.log("Status           REVIEW  (waiting for human review)")
        rr.log(f"Wrote            {result_path}")
        return finished
    except Exception as exc:
        err = str(exc).strip() or exc.__class__.__name__
        rr.log(f"Research Job failed: {err}")
        traceback.print_exc()
        failed = status_payload(
            job,
            status="FAILED",
            started_at=started_at,
            finished_at=rr.now_iso(),
            result_path=str(result_path) if result_path.exists() else None,
            error=err[:300],
        )
        write_json(status_path, failed)
        return failed


def main() -> int:
    parser = argparse.ArgumentParser(description="M1 Research Job runner")
    parser.add_argument(
        "job_file",
        nargs="?",
        default=str(ROOT / "input" / "research_job.json"),
        help="Path to research_job.json",
    )
    args = parser.parse_args()
    job_path = Path(args.job_file)
    if not job_path.is_absolute():
        job_path = (Path.cwd() / job_path).resolve()
    if not job_path.exists():
        rr.log(f"Job file not found: {job_path}")
        return 2
    status = run_job(job_path)
    return 0 if status["status"] == "REVIEW" else 1


if __name__ == "__main__":
    sys.exit(main())
