#!/usr/bin/env python3
"""M7A Steam Candidate Research runner.

This is a pre-build candidate evidence runner. It deliberately does not
produce a Candidate Decision or any BUILD/WATCH/REJECT recommendation.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable

import search_demand_providers as sdp
from datetime import datetime, timezone

try:
    import game_wide_social_runner as gws
except ModuleNotFoundError:
    # main deliberately does not carry the unrelated game-wide discovery
    # implementation. Steam preflight must remain usable without it.
    gws = None

ROOT = Path(__file__).resolve().parent
JOB_TYPE = "STEAM_CANDIDATE_RESEARCH"
SOCIAL_CHECK = "GAME_WIDE_SOCIAL"
SERP_CHECK = "GOOGLE_ORGANIC_SERP"
EXEC_COMPLETED = "COMPLETED"
EXEC_FAILED = "FAILED"
FetchFn = Callable[[str, dict[str, str] | None], dict[str, Any]]


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _non_empty(value: Any) -> bool:
    return value is not None and (not isinstance(value, str) or bool(value.strip()))


def validate_job(job: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(job, dict):
        raise ValueError("steam candidate research job must be an object")
    if str(job.get("job_type") or "").strip().upper() != JOB_TYPE:
        raise ValueError(f"job_type must be {JOB_TYPE}")
    required = (
        "job_id", "steam_app_id", "game_name", "steam_url", "research_cycle_date",
        "steam_signals", "manual_signals", "serp_queries", "requested_checks", "created_at",
    )
    missing = [key for key in required if not _non_empty(job.get(key))]
    if missing:
        raise ValueError("Steam candidate research job missing fields: " + ", ".join(missing))
    if not str(job["steam_app_id"]).strip().isdigit():
        raise ValueError("steam_app_id must be numeric")
    if not isinstance(job.get("steam_signals"), dict):
        raise ValueError("steam_signals must be an object")
    if not isinstance(job.get("manual_signals"), dict):
        raise ValueError("manual_signals must be an object")
    queries = job.get("serp_queries")
    if not isinstance(queries, list) or len(queries) != 1 or str(queries[0] or "").strip() != str(job["game_name"]).strip():
        raise ValueError("serp_queries must contain exactly the game_name brand query")
    checks = job.get("requested_checks")
    if not isinstance(checks, list):
        raise ValueError("requested_checks must be an array")
    normalized_checks = {str(check or "").strip().upper() for check in checks}
    if not {SOCIAL_CHECK, SERP_CHECK}.issubset(normalized_checks):
        raise ValueError("requested_checks must include GAME_WIDE_SOCIAL and GOOGLE_ORGANIC_SERP")
    return job


def _social_top_cluster(cluster: dict[str, Any]) -> dict[str, Any]:
    """Keep social evidence while omitting the existing runner's decisions."""
    return {
        "topic_key": cluster.get("topic_key"),
        "topic": cluster.get("topic"),
        "intent": cluster.get("intent"),
        "representative_questions": cluster.get("representative_questions") or [],
        "evidence_count": int(cluster.get("evidence_count") or 0),
        "source_families": cluster.get("source_families") or [],
        "providers": cluster.get("providers") or [],
        "freshness_score": cluster.get("freshness_score"),
        "engagement_score": cluster.get("engagement_score"),
        "task_intent_score": cluster.get("task_intent_score"),
        "evidence": cluster.get("evidence") or [],
    }


def _run_social(job: dict[str, Any]) -> dict[str, Any]:
    if gws is None:
        return {
            "status": "UNAVAILABLE",
            "evidence_count": 0,
            "cluster_count": 0,
            "actionable_cluster_count": 0,
            "watch_cluster_count": 0,
            "top_clusters": [],
            "source_failures": {},
            "error": "game_wide_social_module_not_present_on_main",
        }


    social_job = {
        "job_type": gws.JOB_TYPE,
        "job_id": f"{job['job_id']}-social",
        "site_key": f"steam:{job['steam_app_id']}",
        "game_name": job["game_name"],
        "aliases": [],
        "existing_pages": [],
        "gsc_queries": [],
        "recent_interventions": [],
        "created_at": job["created_at"],
    }
    try:
        raw = gws.run_game_wide_social_discovery(social_job)
        evidence_count = int(raw.get("evidence_count") or 0)
        clusters = raw.get("clusters") or []
        available = evidence_count > 0
        return {
            "status": "AVAILABLE" if available else "UNAVAILABLE",
            "evidence_count": evidence_count,
            "cluster_count": len(clusters),
            "actionable_cluster_count": sum(
                1 for cluster in clusters if str(cluster.get("decision") or "") in {"NEW", "EXPAND"}
            ),
            "watch_cluster_count": sum(
                1 for cluster in clusters if str(cluster.get("decision") or "") == "WATCH"
            ),
            "top_clusters": [_social_top_cluster(cluster) for cluster in clusters[:10]],
            "source_failures": raw.get("source_failures") or {},
            "source_states": raw.get("source_states") or {},
            "error": None if available else "social_evidence_unavailable",
        }
    except Exception as exc:
        return {
            "status": "UNAVAILABLE",
            "evidence_count": 0,
            "cluster_count": 0,
            "actionable_cluster_count": 0,
            "watch_cluster_count": 0,
            "top_clusters": [],
            "source_failures": {},
            "error": str(exc)[:300],
        }


def run_candidate_social(job: dict[str, Any]) -> dict[str, Any]:
    """Run only the social stage for the Preflight V1 pipeline."""
    return _run_social(validate_job(dict(job)))


def _run_serp(job: dict[str, Any], fetch_fn: FetchFn | None) -> dict[str, Any]:
    query = str(job["game_name"]).strip()
    try:
        raw = sdp.probe_searchapi_google_organic(
            query,
            seed_terms=[query],
            anchor_topic="",
            fetch_fn=fetch_fn,
            hl="en",
            gl="us",
            device="desktop",
        )
        if raw.get("status") != sdp.STATUS_SUPPORTED:
            return {
                "status": "UNAVAILABLE",
                "query": query,
                "organic_count": 0,
                "competition_summary": None,
                "error": raw.get("error") or "searchapi_unavailable",
                "provider_metadata": raw.get("metadata") or {},
            }
        items = [item for item in raw.get("items") or [] if isinstance(item, dict)]
        summary = {
            "organic_count": len(items),
            "signals": [],
            "result_classifications": [
                {
                    "position": item.get("position"),
                    "title": item.get("title"),
                    "domain": item.get("domain"),
                    "url": item.get("url"),
                }
                for item in items
            ],
        }
        return {
            "status": "AVAILABLE",
            "query": query,
            "organic_count": len(items),
            "competition_summary": summary,
            "error": None,
            "provider_metadata": raw.get("metadata") or {},
        }
    except Exception as exc:
        return {
            "status": "UNAVAILABLE",
            "query": query,
            "organic_count": 0,
            "competition_summary": None,
            "error": str(exc)[:300],
            "provider_metadata": {},
        }


def run_candidate_research(
    job: dict[str, Any],
    fetch_fn: FetchFn | None = None,
) -> dict[str, Any]:
    job = validate_job(dict(job))
    social = _run_social(job) if SOCIAL_CHECK in {str(x).strip().upper() for x in job["requested_checks"]} else {
        "status": "UNAVAILABLE", "evidence_count": 0, "cluster_count": 0,
        "actionable_cluster_count": 0, "watch_cluster_count": 0, "top_clusters": [],
        "source_failures": {}, "error": "check_not_requested",
    }
    serp = _run_serp(job, fetch_fn) if SERP_CHECK in {str(x).strip().upper() for x in job["requested_checks"]} else {
        "status": "UNAVAILABLE", "query": job["game_name"], "organic_count": 0,
        "competition_summary": None, "error": "check_not_requested", "provider_metadata": {},
    }
    any_available = social["status"] == "AVAILABLE" or serp["status"] == "AVAILABLE"
    research_status = EXEC_COMPLETED if any_available else EXEC_FAILED
    return {
        "job_id": job["job_id"],
        "job_type": JOB_TYPE,
        "steam_app_id": job["steam_app_id"],
        "game_name": job["game_name"],
        "steam_signals": job["steam_signals"],
        "manual_signals": job["manual_signals"],
        "social": social,
        "serp": serp,
        "research_status": research_status,
        "generated_at": now_iso(),
    }


def run_job(
    job_path: Path,
    fetch_fn: FetchFn | None = None,
    *,
    root: Path | None = None,
) -> dict[str, Any]:
    base = root or ROOT
    job = validate_job(json.loads(job_path.read_text(encoding="utf-8")))
    job_id = str(job["job_id"]).strip()
    job_dir = base / "jobs" / job_id
    result_path = job_dir / "steam_candidate_research_result.json"
    status_path = job_dir / "status.json"
    write_json(job_dir / "job.json", job)
    write_json(status_path, {"job_id": job_id, "job_type": JOB_TYPE, "status": "RUNNING", "started_at": now_iso()})

    try:
        result = run_candidate_research(job, fetch_fn=fetch_fn)
        write_json(result_path, result)
        write_json(
            status_path,
            {
                "job_id": job_id,
                "job_type": JOB_TYPE,
                "status": result["research_status"],
                "finished_at": now_iso(),
                "result_path": f"jobs/{job_id}/steam_candidate_research_result.json",
            },
        )
        return {"ok": result["research_status"] == EXEC_COMPLETED, "status": result["research_status"], "job_id": job_id, "result": result}
    except Exception as exc:
        write_json(
            status_path,
            {"job_id": job_id, "job_type": JOB_TYPE, "status": EXEC_FAILED, "finished_at": now_iso(), "error": str(exc)[:300]},
        )
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description="M7A Steam Candidate Research Runner")
    parser.add_argument("job_file", nargs="?", default=str(ROOT / "input" / "steam_candidate_research_job.json"))
    args = parser.parse_args()
    outcome = run_job(Path(args.job_file))
    return 0 if outcome["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
