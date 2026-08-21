#!/usr/bin/env python3
"""Fetch pending GAME_WIDE scope DEMAND_DISCOVERY jobs from Apps Script endpoint.

Writes:
  input/pending_game_wide_jobs.json
  input/game_wide_job.json

Independent from fetch_pending_demand_discovery_jobs.py.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
PENDING_GAME_WIDE_JOBS_URL = (
    "https://script.google.com/macros/s/"
    "AKfycbwILJmfmk_PRtjgGffPzX1ZebnGTf9TzAbinkalMNBu5y4PsMbW4L_IdeJJTYGOuQzf"
    "/exec?action=pendingGameWideDiscoveryJobs"
)

CONTRACT_FIELDS = (
    "job_id",
    "job_type",
    "research_type",
    "site",
    "game",
    "discovery_scope",
    "seed_terms",
    "source_families_requested",
    "created_at",
)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def fetch_pending_game_wide_jobs(
    url: str = PENDING_GAME_WIDE_JOBS_URL,
) -> dict[str, Any]:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "hotword-engine-fetch-game-wide/1"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.URLError as exc:
        raise SystemExit(f"Failed to fetch pending game wide jobs: {exc}") from exc
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Game wide API did not return JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise SystemExit("Game wide API returned a non-object JSON payload")
    jobs = payload.get("jobs")
    if not isinstance(jobs, list):
        raise SystemExit("Game wide API response missing a jobs array")
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
    for field in ("seed_terms", "source_families_requested"):
        if not isinstance(out.get(field), list):
            out[field] = []
    # job_type may not be stored in Sheet; runner requires it
    if not out.get("job_type"):
        out["job_type"] = "GAME_WIDE_SOCIAL_DISCOVERY"
    # Runner expects site_key / game_name; derive from contract fields
    if not out.get("site_key"):
        out["site_key"] = out.get("site", "")
    if not out.get("game_name"):
        out["game_name"] = out.get("game", "")
    return out


def to_game_wide_job(job: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(job, dict):
        raise SystemExit("Selected game wide job is not an object")
    out = _normalize_contract(job)
    missing = [k for k in CONTRACT_FIELDS if not _is_non_empty(out.get(k))]
    if missing:
        raise SystemExit("Selected game wide job missing fields: " + ", ".join(missing))
    if str(out.get("research_type") or "").strip().upper() != "DEMAND_DISCOVERY":
        raise SystemExit("Selected job research_type is not DEMAND_DISCOVERY")
    scope = out.get("discovery_scope") or {}
    if str(scope.get("scope") or "").strip().upper() != "GAME_WIDE":
        raise SystemExit("Selected job discovery_scope.scope is not GAME_WIDE")
    return out


def main() -> int:
    payload = fetch_pending_game_wide_jobs()
    pending_path = ROOT / "input" / "pending_game_wide_jobs.json"
    job_path = ROOT / "input" / "game_wide_job.json"
    write_json(pending_path, payload)

    jobs = payload.get("jobs") or []
    if not jobs:
        print(f"Wrote {pending_path}")
        print("jobs=0")
        print("No GAME_WIDE DEMAND_DISCOVERY job to write")
        return 1

    selected = to_game_wide_job(jobs[0])
    write_json(job_path, selected)
    print(f"Wrote {pending_path}")
    print(f"Wrote {job_path}")
    print(f"jobs={len(jobs)}")
    print(f"selected_job_id={selected['job_id']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
