#!/usr/bin/env python3
"""GAME_WIDE scope DEMAND_DISCOVERY job runner with callback.

Runs game_wide_social_runner, builds callback payload, and POSTs to Apps Script.
Mirrors the pattern of demand_discovery_job_runner.py.

Usage:
  python run_game_wide_social_job.py [job_file]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import game_wide_social_runner as gwsr
import research_job_runner as rjr

ROOT = Path(__file__).resolve().parent


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _truncate_error(error: Any) -> str:
    return str(error or "")[:300]


def _compact_top_clusters(result: dict[str, Any], limit: int = 5) -> list[dict[str, Any]]:
    clusters = result.get("clusters") or []
    out: list[dict[str, Any]] = []
    for c in clusters[:limit]:
        evidence = c.get("evidence") or []
        example_urls = [e.get("url") or "" for e in evidence[:3]]
        out.append({
            "topic_key": c.get("topic_key", ""),
            "topic": c.get("topic", ""),
            "intent": c.get("intent", ""),
            "task_intent_score": c.get("task_intent_score", 0),
            "decision": c.get("decision", ""),
            "evidence_count": c.get("evidence_count", 0),
            "source_families": c.get("source_families", []),
            "providers": c.get("providers", []),
            "example_urls": example_urls,
        })
    return out


def build_game_wide_completed_callback_body(
    job: dict[str, Any], result: dict[str, Any]
) -> dict[str, Any]:
    return {
        "job_id": job["job_id"],
        "research_type": "DEMAND_DISCOVERY",
        "execution_status": "COMPLETED",
        "discovery_scope": {"scope": "GAME_WIDE"},
        "evidence_count": int(result.get("evidence_count") or 0),
        "cluster_count": len(result.get("clusters") or []),
        "decision_counts": result.get("decision_counts") or {},
        "top_clusters": _compact_top_clusters(result, limit=5),
        "result_path": f"jobs/{job['job_id']}/game_wide_social_result.json",
    }


def build_game_wide_failed_callback_body(
    job: dict[str, Any], error: Any
) -> dict[str, Any]:
    return {
        "job_id": job["job_id"],
        "research_type": "DEMAND_DISCOVERY",
        "execution_status": "FAILED",
        "discovery_scope": {"scope": "GAME_WIDE"},
        "error": _truncate_error(error),
    }


def _update_status_json(job_id: str, callback_ok: bool) -> None:
    status_path = ROOT / "jobs" / job_id / "status.json"
    if not status_path.exists():
        return
    try:
        current = json.loads(status_path.read_text(encoding="utf-8"))
    except Exception:
        return
    current["callback_ok"] = bool(callback_ok)
    _write_json(status_path, current)


def run_job(job_path: Path) -> dict[str, Any]:
    job = gwsr.load_job(job_path)
    job_id = str(job["job_id"]).strip()

    execution_status = "COMPLETED"
    callback_payload: dict[str, Any]

    result: dict[str, Any] | None = None
    try:
        raw = gwsr.run_job(job_path)
        if isinstance(raw, dict) and raw.get("result"):
            result = raw["result"]
        if not isinstance(result, dict):
            raise RuntimeError("game_wide_social_runner returned no result")
        callback_payload = build_game_wide_completed_callback_body(job, result)
    except Exception as exc:
        execution_status = "FAILED"
        callback_payload = build_game_wide_failed_callback_body(job, exc)

    callback_ok = rjr.post_callback_body(callback_payload)
    _update_status_json(job_id, callback_ok)

    return {
        "ok": True,
        "job_id": job_id,
        "execution_status": execution_status,
        "callback_ok": callback_ok,
        "callback_payload": callback_payload,
    }


def exit_code_from_run(execution_status: str, callback_ok: bool) -> int:
    if execution_status == "FAILED":
        return 1
    return 0 if callback_ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="GAME_WIDE scope DEMAND_DISCOVERY callback sender")
    parser.add_argument(
        "job_file",
        nargs="?",
        default=str(ROOT / "input" / "game_wide_job.json"),
        help="Path to game_wide_job.json",
    )
    args = parser.parse_args()
    job_path = Path(args.job_file)
    if not job_path.is_absolute():
        job_path = (Path.cwd() / job_path).resolve()
    if not job_path.exists():
        gwsr.log(f"Job file not found: {job_path}")
        return 2

    run = run_job(job_path)
    return exit_code_from_run(
        str(run.get("execution_status") or ""), bool(run.get("callback_ok"))
    )


if __name__ == "__main__":
    sys.exit(main())
