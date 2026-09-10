"""Machine-readable and human-readable receipt for one scheduler batch."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = "LynneLan3/hotword-engine"
LIFECYCLE_STAGES = (
    "RESEARCH_PENDING",
    "RESEARCH_PASS",
    "IMPLEMENTATION_PENDING",
    "IMPLEMENTED",
    "DEPLOYMENT_PENDING",
    "DEPLOYED",
    "INDEXING_CHECKED",
    "FAILED",
)
ACTION_ALIASES = {
    "UPDATE_PAGE": "UPDATE",
    "CREATE_PAGE": "NEW",
    "EXPAND_PAGE": "EXPAND",
}


class BatchReceiptTransitionError(ValueError):
    pass


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


def _normalized_action(value: Any) -> str:
    action = str(value or "").strip().upper()
    return ACTION_ALIASES.get(action, action)


def _has_implementation_evidence(receipt: dict[str, Any]) -> bool:
    implementation = receipt.get("implementation") or {}
    return (
        str(implementation.get("status") or "").upper() == "IMPLEMENTED"
        and _normalized_action(implementation.get("action")) in {"UPDATE", "NEW", "EXPAND"}
        and bool(implementation.get("changed_files"))
        and bool(implementation.get("canonical_urls"))
    )


def _has_deployment_evidence(receipt: dict[str, Any]) -> bool:
    deployment = receipt.get("deployment") or {}
    git = receipt.get("git") or {}
    return (
        str(deployment.get("status") or "").upper() in {"DEPLOYED", "PASS"}
        and bool(deployment.get("production_url"))
        and bool(deployment.get("changed_urls"))
        and bool(git.get("commit_sha"))
        and bool(git.get("commit_url"))
    )


def _has_indexing_evidence(receipt: dict[str, Any]) -> bool:
    indexing = receipt.get("indexing") or {}
    return any(
        str(indexing.get(field) or "").upper() not in {"", "NOT_RUN"}
        for field in ("sitemap", "indexnow", "IndexNow", "url_inspection", "URL Inspection")
    )


def advance_receipt(receipt: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    """Apply one evidence-backed lifecycle transition to an existing receipt."""
    if not isinstance(receipt, dict) or not isinstance(update, dict):
        raise BatchReceiptTransitionError("receipt and update must be objects")
    if update.get("batch_id") and update.get("batch_id") != receipt.get("batch_id"):
        raise BatchReceiptTransitionError("batch_id does not match receipt")

    current = str(receipt.get("status") or "").strip().upper()
    target = str(update.get("status") or current).strip().upper()
    if target not in LIFECYCLE_STAGES and target != "NO_PENDING_JOB":
        raise BatchReceiptTransitionError(f"unsupported lifecycle status: {target}")
    if current == "FAILED" and target != "FAILED":
        raise BatchReceiptTransitionError("FAILED receipt is terminal")
    allowed = {
        ("RESEARCH_PASS", "IMPLEMENTATION_PENDING"),
        ("RESEARCH_PASS", "IMPLEMENTED"),
        ("IMPLEMENTATION_PENDING", "IMPLEMENTED"),
        ("IMPLEMENTED", "DEPLOYMENT_PENDING"),
        ("IMPLEMENTED", "DEPLOYED"),
        ("DEPLOYMENT_PENDING", "DEPLOYED"),
        ("DEPLOYED", "INDEXING_CHECKED"),
    }
    if target != current and target != "FAILED" and (current, target) not in allowed:
        raise BatchReceiptTransitionError(f"invalid lifecycle transition: {current} -> {target}")

    merged = dict(receipt)
    for section in ("implementation", "git", "deployment", "indexing", "links"):
        if isinstance(update.get(section), dict):
            merged[section] = {**(merged.get(section) or {}), **update[section]}
    merged["status"] = target
    merged["stage"] = target
    if target == "IMPLEMENTED" and not _has_implementation_evidence(merged):
        raise BatchReceiptTransitionError("IMPLEMENTED requires changed_files, canonical_urls, and action evidence")
    if target == "DEPLOYED" and not (_has_implementation_evidence(merged) and _has_deployment_evidence(merged)):
        raise BatchReceiptTransitionError("DEPLOYED requires implementation, commit, production_url, and changed_urls evidence")
    if target == "INDEXING_CHECKED" and not (_has_deployment_evidence(merged) and _has_indexing_evidence(merged)):
        raise BatchReceiptTransitionError("INDEXING_CHECKED requires deployment and indexing evidence")
    if target == "FAILED":
        failure_reason = str(update.get("failure_reason") or "").strip()
        if not failure_reason:
            raise BatchReceiptTransitionError("FAILED requires failure_reason")
        merged["failure_stage"] = str(update.get("failure_stage") or current or "UNKNOWN")
        merged["failure_reason"] = failure_reason
        if update.get("failure_detail"):
            merged["failure_detail"] = str(update["failure_detail"])[:300]
    if merged.get("deployment", {}).get("production_url"):
        merged.setdefault("links", {})["production"] = merged["deployment"]["production_url"]
    if merged.get("git", {}).get("commit_url"):
        merged.setdefault("links", {})["commit"] = merged["git"]["commit_url"]
    merged["summary"] = human_summary(merged)
    return merged


def human_summary(receipt: dict[str, Any]) -> str:
    jobs = receipt.get("research_jobs") or []
    site = str((jobs[0] if jobs else {}).get("site") or receipt.get("implementation", {}).get("site") or "UNKNOWN")
    evidence = receipt.get("evidence") or {}
    links = receipt.get("links") or {}
    if str(receipt.get("status") or "").upper() in {"DEPLOYED", "INDEXING_CHECKED"} and _has_implementation_evidence(receipt) and _has_deployment_evidence(receipt):
        implementation = receipt.get("implementation") or {}
        indexing = receipt.get("indexing") or {}
        action = _normalized_action(implementation.get("action"))
        urls = implementation.get("canonical_urls") or []
        lines = [
            f"AUTO CONTENT BATCH {receipt.get('batch_id')}",
            f"Site: {site}",
            f"Result: {receipt.get('status')}",
            f"Research: {len(jobs)} jobs / {evidence.get('PASS', 0)} Evidence PASS",
            "",
            "Implemented:",
            *[f"- {action} {url}" for url in urls],
            "",
            f"Commit: {(receipt.get('git') or {}).get('commit_url') or 'NOT_AVAILABLE'}",
            f"Production: {(receipt.get('deployment') or {}).get('production_url') or 'NOT_AVAILABLE'}",
            f"Indexing: {indexing.get('IndexNow') or indexing.get('indexnow') or 'NOT_RUN'}",
            f"Manual GSC Request Indexing: {', '.join(indexing.get('manual_request_indexing_urls') or []) or 'NONE'}",
            f"Receipt: {links.get('receipt')}",
            f"GitHub Run: {links.get('scheduler_run') or 'NOT_AVAILABLE'}",
        ]
        return "\n".join(lines)
    return "\n".join(
        [
            f"RESEARCH BATCH {receipt.get('batch_id')}",
            f"Site: {site}",
            f"Result: {receipt.get('status')}",
            f"Evidence PASS: {evidence.get('PASS', 0)}",
            "Implementation: NOT_RUN" if str(receipt.get("status") or "").upper() in {"RESEARCH_PENDING", "RESEARCH_PASS", "NO_PENDING_JOB"} else f"Implementation: {(receipt.get('implementation') or {}).get('status') or 'UNKNOWN'}",
            f"Production: {(receipt.get('deployment') or {}).get('status') or 'NOT_RUN'}",
            f"Receipt: {links.get('receipt')}",
            f"GitHub Run: {links.get('scheduler_run') or 'NOT_AVAILABLE'}",
        ]
    )


def write_receipt(path: Path, receipt: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
