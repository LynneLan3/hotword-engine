#!/usr/bin/env python3
"""Replay saved Research Job evidence to the Google Sheet callback.

Reads an existing jobs/<job_id>/ directory (status.json + research_result.json)
and POSTs the current REVIEW callback payload — including evidence and
review_summary — without re-running research.

Does NOT:
  - call research_runner / Reddit / YouTube / Steam
  - create jobs
  - mutate local artifacts, cooldown, or content opportunities

Usage:
  RESEARCH_CALLBACK_URL=... RESEARCH_CALLBACK_TOKEN=... \\
    python3 backfill_review_evidence.py jobs/au-console-20260814
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import research_job_runner as rjr

ROOT = Path(__file__).resolve().parent


def resolve_job_dir(raw: str) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    if path.is_file() and path.name in {"status.json", "research_result.json", "job.json"}:
        path = path.parent
    if not path.is_dir():
        raise FileNotFoundError(f"job directory not found: {path}")
    return path


def load_saved_status(job_dir: Path) -> dict[str, Any]:
    status_path = job_dir / "status.json"
    if not status_path.exists():
        raise FileNotFoundError(f"missing {status_path}")
    status = json.loads(status_path.read_text(encoding="utf-8"))
    if not isinstance(status, dict) or not str(status.get("job_id") or "").strip():
        raise ValueError(f"invalid status.json in {job_dir}")
    return status


def load_saved_result(job_dir: Path, status: dict[str, Any]) -> dict[str, Any]:
    candidates: list[Path] = [job_dir / "research_result.json"]
    rel = str(status.get("result_path") or "").strip()
    if rel:
        candidates.insert(0, (ROOT / rel).resolve())
    for path in candidates:
        if path.exists():
            result = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(result, dict):
                raise ValueError(f"invalid research result: {path}")
            return result
    raise FileNotFoundError(
        f"missing research_result.json for job {status.get('job_id')} under {job_dir}"
    )


def backfill_status_for_callback(
    status: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    """Build REVIEW/WATCH status for callback reuse; never invents research."""
    job_id = str(status["job_id"]).strip()
    evidence = rjr.evidence_from_result(result)
    recommendation = status.get("recommendation")
    if recommendation is None:
        recommendation = result.get("recommendation")
    evidence_count = int(status.get("evidence_count") or len(evidence))
    # Prefer recomputing from the saved result so old REVIEW+WATCH artifacts
    # are corrected on replay.
    finished = rjr.resolve_finished_status(
        evidence_count=len(evidence),
        recommendation=recommendation,
    )
    return {
        "job_id": job_id,
        "status": finished,
        "started_at": status.get("started_at"),
        "finished_at": status.get("finished_at"),
        "recommendation": recommendation,
        "evidence_count": evidence_count if finished == "REVIEW" else len(evidence),
        "result_path": str(
            status.get("result_path") or rjr.relative_result_path(job_id)
        ),
    }


def backfill_job_dir(job_dir: Path) -> bool:
    status = load_saved_status(job_dir)
    result = load_saved_result(job_dir, status)
    job_id = str(status["job_id"]).strip()
    evidence = rjr.evidence_from_result(result)
    summary = rjr.review_summary_from_result(result)
    callback_status = backfill_status_for_callback(status, result)

    # Persist corrected status when replaying (e.g. old REVIEW → WATCH).
    corrected = dict(status)
    corrected["status"] = callback_status["status"]
    corrected["evidence_count"] = callback_status["evidence_count"]
    corrected["recommendation"] = callback_status["recommendation"]
    rjr.write_json(job_dir / "status.json", corrected)

    print(f"Backfill job     {job_id}")
    print(f"Job dir          {job_dir}")
    print(f"Artifact         research_result.json + status.json")
    print(f"Evidence count   {len(evidence)}")
    print(f"Status           {callback_status['status']}")
    print(f"Review summary   {(summary[:80] + '…') if len(summary) > 80 else summary or '(from recommendation.reason)'}")
    print("Action           replay callback only (no research)")

    ok = rjr.post_research_callback(callback_status, result=result)
    print(f"Callback         {'OK' if ok else 'FAILED'}")
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Replay saved Research Job evidence to Google Sheet callback"
    )
    parser.add_argument(
        "job_path",
        help="Path to jobs/<job_id>/ (or a file inside that directory)",
    )
    args = parser.parse_args()
    try:
        job_dir = resolve_job_dir(args.job_path)
        ok = backfill_job_dir(job_dir)
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
        print(f"Backfill error   {exc}", file=sys.stderr)
        return 2
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
