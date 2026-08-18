#!/usr/bin/env python3
"""Fetch pending DEMAND_DISCOVERY jobs from Apps Script endpoint.

Writes:
  input/pending_demand_discovery_jobs.json
  input/demand_discovery_job.json

This loader is independent from fetch_pending_jobs.py and does not require
legacy content-opportunity fields (topic/source_query/recommended_action/etc.).
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
PENDING_DISCOVERY_JOBS_URL = (
    "https://script.google.com/macros/s/"
    "AKfycbwILJmfmk_PRtjgGffPzX1ZebnGTf9TzAbinkalMNBu5y4PsMbW4L_IdeJJTYGOuQzf"
    "/exec?action=pendingDemandDiscoveryJobs"
)

CONTRACT_FIELDS = (
    "job_id",
    "research_type",
    "site",
    "game",
    "radar_id",
    "trigger_type",
    "anchor_page",
    "source_signal_summary",
    "discovery_scope",
    "seed_terms",
    "source_families_requested",
    "discovery_cycle_date",
    "created_at",
)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def fetch_pending_demand_discovery_jobs(
    url: str = PENDING_DISCOVERY_JOBS_URL,
) -> dict[str, Any]:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "hotword-engine-fetch-demand-discovery/1"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.URLError as exc:
        raise SystemExit(f"Failed to fetch pending demand discovery jobs: {exc}") from exc
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Demand discovery API did not return JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise SystemExit("Demand discovery API returned a non-object JSON payload")
    jobs = payload.get("jobs")
    if not isinstance(jobs, list):
        raise SystemExit("Demand discovery API response missing a jobs array")
    return payload


def _is_non_empty(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


def _normalize_contract(job: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in CONTRACT_FIELDS:
        out[key] = job.get(key)
    if not isinstance(out.get("discovery_scope"), dict):
        out["discovery_scope"] = {}
    if not isinstance(out.get("seed_terms"), list):
        out["seed_terms"] = []
    if not isinstance(out.get("source_families_requested"), list):
        out["source_families_requested"] = []
    return out


def to_demand_discovery_job(job: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(job, dict):
        raise SystemExit("Selected demand discovery job is not an object")
    out = _normalize_contract(job)
    missing = [k for k in CONTRACT_FIELDS if not _is_non_empty(out.get(k))]
    if missing:
        raise SystemExit("Selected demand discovery job missing fields: " + ", ".join(missing))
    if str(out.get("research_type") or "").strip().upper() != "DEMAND_DISCOVERY":
        raise SystemExit("Selected job research_type is not DEMAND_DISCOVERY")
    return out


def main() -> int:
    payload = fetch_pending_demand_discovery_jobs()
    pending_path = ROOT / "input" / "pending_demand_discovery_jobs.json"
    job_path = ROOT / "input" / "demand_discovery_job.json"
    write_json(pending_path, payload)

    jobs = payload.get("jobs") or []
    if not jobs:
        print(f"Wrote {pending_path}")
        print("jobs=0")
        print("No DEMAND_DISCOVERY job to write")
        return 1

    selected = to_demand_discovery_job(jobs[0])
    write_json(job_path, selected)
    print(f"Wrote {pending_path}")
    print(f"Wrote {job_path}")
    print(f"jobs={len(jobs)}")
    print(f"selected_job_id={selected['job_id']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

