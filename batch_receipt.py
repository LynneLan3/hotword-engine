"""Machine-readable and human-readable receipt for one scheduler batch."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = "LynneLan3/hotword-engine"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def batch_id(started_at: str) -> str:
    run_id = str(os.environ.get("GITHUB_RUN_ID") or "").strip()
    if run_id:
        attempt = str(os.environ.get("GITHUB_RUN_ATTEMPT") or "1").strip()
        return f"research-{run_id}-attempt-{attempt}"
    compact = started_at.replace("+00:00", "Z").replace("-", "").replace(":", "")
    return f"research-local-{compact}"


def trigger() -> str:
    event = str(os.environ.get("GITHUB_EVENT_NAME") or "local").strip()
    return {"schedule": "SCHEDULE", "workflow_dispatch": "WORKFLOW_DISPATCH"}.get(event, event.upper())


def scheduler_run_url() -> str | None:
    run_id = str(os.environ.get("GITHUB_RUN_ID") or "").strip()
    if not run_id:
        return None
    repo = str(os.environ.get("GITHUB_REPOSITORY") or REPO).strip()
    return f"https://github.com/{repo}/actions/runs/{run_id}"


def _commit_url() -> str | None:
    sha = str(os.environ.get("GITHUB_SHA") or "").strip()
    repo = str(os.environ.get("GITHUB_REPOSITORY") or REPO).strip()
    return f"https://github.com/{repo}/commit/{sha}" if sha else None


def receipt_path(root: Path, identifier: str) -> Path:
    return root / "jobs" / "batches" / identifier / "batch_receipt.json"


def research_row(job: dict[str, Any], research_status: str, evidence_count: int | None) -> dict[str, Any]:
    return {
        "job_id": str(job.get("job_id") or ""),
        "site": str(job.get("game") or ""),
        "query/opportunity": str(job.get("source_query") or job.get("topic") or ""),
        "research_status": research_status,
        "evidence_count": int(evidence_count or 0),
    }


def build_receipt(
    *,
    identifier: str,
    started_at: str,
    finished_at: str,
    status: str,
    stage: str,
    research_jobs: list[dict[str, Any]] | None = None,
    evidence: dict[str, int] | None = None,
    site: str = "",
    job_id: str | None = None,
    failure_stage: str | None = None,
    failure_reason: str | None = None,
    failure_detail: str | None = None,
    research_artifact: str | None = None,
) -> dict[str, Any]:
    jobs = list(research_jobs or [])
    run_url = scheduler_run_url()
    commit_sha = str(os.environ.get("GITHUB_SHA") or "").strip() or None
    production_url = None
    receipt_ref = f"jobs/batches/{identifier}/batch_receipt.json"
    receipt = {
        "schema_version": "batch-receipt.v1",
        "batch_id": identifier,
        "status": status,
        "stage": stage,
        "started_at": started_at,
        "finished_at": finished_at,
        "trigger": trigger(),
        "job_id": job_id,
        "research_jobs": jobs,
        "evidence": evidence or {"PASS": 0, "HOLD": 0, "FAIL": 0},
        "implementation": {
            "status": "NOT_RUN",
            "site": site,
            "action": "NOT_RUN",
            "canonical_urls": [],
            "changed_files": [],
        },
        "git": {
            "repo": REPO,
            "commit_sha": commit_sha,
            "commit_url": _commit_url(),
        },
        "deployment": {
            "status": "NOT_RUN",
            "production_url": production_url,
            "changed_urls": [],
        },
        "indexing": {
            "sitemap": "NOT_RUN",
            "IndexNow": "NOT_RUN",
            "URL Inspection": "NOT_RUN",
            "manual_request_indexing_urls": [],
        },
        "links": {
            "scheduler_run": run_url,
            "research_artifact": research_artifact,
            "receipt": receipt_ref,
            "artifact": f"{run_url}/artifacts" if run_url else None,
            "commit": _commit_url(),
            "production": production_url,
        },
    }
    if failure_stage:
        receipt["failure_stage"] = failure_stage
    if failure_reason:
        receipt["failure_reason"] = failure_reason
    if failure_detail:
        receipt["failure_detail"] = failure_detail[:300]
    receipt["summary"] = human_summary(receipt)
    return receipt


def human_summary(receipt: dict[str, Any]) -> str:
    jobs = receipt.get("research_jobs") or []
    site = str((jobs[0] if jobs else {}).get("site") or receipt.get("implementation", {}).get("site") or "UNKNOWN")
    evidence = receipt.get("evidence") or {}
    links = receipt.get("links") or {}
    return "\n".join(
        [
            f"AUTO CONTENT BATCH {receipt.get('batch_id')}",
            f"Site: {site}",
            f"Result: {receipt.get('status')}",
            f"Research: {len(jobs)} jobs / {evidence.get('PASS', 0)} Evidence PASS",
            f"Implemented: {len(receipt.get('implementation', {}).get('canonical_urls') or [])} URLs",
            f"Receipt: {links.get('receipt')}",
            f"GitHub Run: {links.get('scheduler_run') or 'NOT_AVAILABLE'}",
            f"Commit: {links.get('commit') or 'NOT_AVAILABLE'}",
            f"Production: {links.get('production') or 'NOT_RUN'}",
        ]
    )


def write_receipt(path: Path, receipt: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
