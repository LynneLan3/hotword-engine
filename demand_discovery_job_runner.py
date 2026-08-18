#!/usr/bin/env python3
"""R2C-B DEMAND_DISCOVERY callback sender.

Loads one local DEMAND_DISCOVERY job, runs the existing R2B runner,
creates a compact DEMAND_DISCOVERY callback payload, and POSTs it to the
Apps Script receiver (same HTTP/token mechanism as research_job_runner.py).

No scheduler changes, no content recommendations, no Google Sheet writes
except the callback POST.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any

import demand_discovery_runner as ddr
import research_job_runner as rjr

ROOT = Path(__file__).resolve().parent

CALLBACK_ERROR_MAX_CHARS = 300


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _truncate_error(err: Any) -> str:
    s = str(err).strip()
    if not s:
        s = "demand_discovery_failed"
    return s[:CALLBACK_ERROR_MAX_CHARS]


def _stable_external_source_families_from_anchor_result(result: dict[str, Any]) -> list[str]:
    """P0: external_source_families are only from anchor-qualified evidence."""
    scope_label = str(result.get("discovery_scope_label") or "").strip().upper()
    if scope_label != "ANCHOR":
        return []

    anchor_evidence = result.get("anchor_evidence") or []
    if not isinstance(anchor_evidence, list):
        anchor_evidence = []

    if not result.get("anchor_evidence_count"):
        return []

    families: set[str] = set()
    for e in anchor_evidence:
        fam = str(e.get("source_family") or "").strip().upper()
        if fam:
            families.add(fam)

    # Only COMMMUNITY/VIDEO are allowed families in this stage.
    out: list[str] = []
    if "COMMUNITY" in families:
        out.append("COMMUNITY")
    if "VIDEO" in families:
        out.append("VIDEO")
    return out


def _compact_top_clusters(result: dict[str, Any], limit: int = 5) -> list[dict[str, Any]]:
    clusters = result.get("demand_clusters") or []
    if not isinstance(clusters, list):
        clusters = []

    out: list[dict[str, Any]] = []
    for c in clusters[:limit]:
        if not isinstance(c, dict):
            continue
        ex_urls = c.get("example_urls") or []
        if not isinstance(ex_urls, list):
            ex_urls = []
        ex_urls = ex_urls[:3]
        out.append(
            {
                "cluster_id": c.get("cluster_id"),
                "representative_signal": c.get("representative_signal") or "",
                "representative_question": c.get("representative_question") or "",
                "evidence_count": int(c.get("evidence_count") or 0),
                "providers": c.get("providers") or [],
                "source_families": c.get("source_families") or [],
                "independent_source_family_count": int(c.get("independent_source_family_count") or 0),
                "cross_validated": bool(c.get("cross_validated")),
                "example_urls": ex_urls,
            }
        )
    return out


def build_demand_discovery_completed_callback_body(job: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    discovery_scope = str(result.get("discovery_scope_label") or "").strip().upper()
    if discovery_scope not in {"ANCHOR", "GAME_WIDE"}:
        discovery_scope = "ANCHOR"  # conservative fallback

    external_families = _stable_external_source_families_from_anchor_result(result)

    body: dict[str, Any] = {
        "job_id": job["job_id"],
        "research_type": "DEMAND_DISCOVERY",
        "radar_id": job["radar_id"],
        "discovery_cycle_date": job["discovery_cycle_date"],
        "execution_status": "COMPLETED",
        "discovery_status": result.get("discovery_status"),
        "discovery_scope": discovery_scope,
        "anchor_evidence_count": int(result.get("anchor_evidence_count") or 0),
        "background_evidence_count": int(result.get("background_evidence_count") or 0),
        "external_source_families": external_families,
        "cross_validated_cluster_count": int(result.get("cross_validated_cluster_count") or 0),
        "top_clusters": _compact_top_clusters(result, limit=5),
        "result_path": f"jobs/{job['job_id']}/demand_discovery_result.json",
    }
    return body


def build_demand_discovery_failed_callback_body(job: dict[str, Any], error: Any) -> dict[str, Any]:
    return {
        "job_id": job["job_id"],
        "research_type": "DEMAND_DISCOVERY",
        "radar_id": job["radar_id"],
        "discovery_cycle_date": job["discovery_cycle_date"],
        "execution_status": "FAILED",
        "error": _truncate_error(error),
    }


def _update_status_json_with_callback_ok(job_id: str, callback_ok: bool) -> None:
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
    job = ddr.load_job(job_path)
    job_id = str(job["job_id"]).strip()

    execution_status = "COMPLETED"
    callback_payload: dict[str, Any]

    # The R2B runner writes local artifacts (job.json/result/status.json).
    result: dict[str, Any] | None = None
    try:
        rr = ddr.run_job(job_path)
        result = rr.get("result") if isinstance(rr, dict) else None
        if not isinstance(result, dict):
            raise RuntimeError("demand_discovery_runner returned no result")
        callback_payload = build_demand_discovery_completed_callback_body(job, result)
    except Exception as exc:
        execution_status = "FAILED"
        callback_payload = build_demand_discovery_failed_callback_body(job, exc)
        # best-effort keep local discovery artifacts (already created by runner if possible)

    callback_ok = rjr.post_callback_body(callback_payload)
    _update_status_json_with_callback_ok(job_id, callback_ok)

    return {
        "ok": True,
        "job_id": job_id,
        "execution_status": execution_status,
        "callback_ok": callback_ok,
        "callback_payload": callback_payload,  # for tests only; not persisted
    }


def exit_code_from_run(execution_status: str, callback_ok: bool) -> int:
    if execution_status == "FAILED":
        return 1
    # COMPLETED
    return 0 if callback_ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="R2C-B DEMAND_DISCOVERY callback sender")
    parser.add_argument(
        "job_file",
        nargs="?",
        default=str(ROOT / "input" / "demand_discovery_job.json"),
        help="Path to demand_discovery_job.json",
    )
    args = parser.parse_args()
    job_path = Path(args.job_file)
    if not job_path.is_absolute():
        job_path = (Path.cwd() / job_path).resolve()
    if not job_path.exists():
        rjr.rr.log(f"Job file not found: {job_path}")
        return 2

    run = run_job(job_path)
    return exit_code_from_run(str(run.get("execution_status") or ""), bool(run.get("callback_ok")))


if __name__ == "__main__":
    sys.exit(main())

