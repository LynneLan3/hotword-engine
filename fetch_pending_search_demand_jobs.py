#!/usr/bin/env python3
"""Fetch pending SEARCH_DEMAND jobs from Apps Script endpoint.

Writes:
  input/pending_search_demand_jobs.json
  input/search_demand_job.json

Independent from fetch_pending_jobs.py and fetch_pending_demand_discovery_jobs.py.
Does not run Search Demand, send callbacks, or change job status.

This stage does not need a live production Search Job. Tests use fixtures.

Usage:
  python3 fetch_pending_search_demand_jobs.py
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent
PENDING_SEARCH_DEMAND_JOBS_URL = (
    "https://script.google.com/macros/s/"
    "AKfycbwILJmfmk_PRtjgGffPzX1ZebnGTf9TzAbinkalMNBu5y4PsMbW4L_IdeJJTYGOuQzf"
    "/exec?action=pendingSearchDemandJobs"
)

CONTRACT_FIELDS = (
    "job_id",
    "research_type",
    "site",
    "game",
    "radar_id",
    "trigger_type",
    "anchor_page",
    "discovery_scope",
    "seed_terms",
    "search_sources_requested",
    "source_signal_summary",
    "search_cycle_date",
    "created_at",
)

FetchFn = Callable[[str], dict[str, Any]]


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def fetch_pending_search_demand_jobs(
    url: str = PENDING_SEARCH_DEMAND_JOBS_URL,
    fetch_fn: FetchFn | None = None,
) -> dict[str, Any]:
    if fetch_fn is not None:
        payload = fetch_fn(url)
    else:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "hotword-engine-fetch-search-demand/1"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read().decode("utf-8")
        except urllib.error.URLError as exc:
            raise SystemExit(f"Failed to fetch pending search demand jobs: {exc}") from exc
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"Search demand API did not return JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise SystemExit("Search demand API returned a non-object JSON payload")
    jobs = payload.get("jobs")
    if not isinstance(jobs, list):
        raise SystemExit("Search demand API response missing a jobs array")
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
    if not isinstance(out.get("search_sources_requested"), list):
        out["search_sources_requested"] = []
    return out


def to_search_demand_job(job: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(job, dict):
        raise SystemExit("Selected search demand job is not an object")
    out = _normalize_contract(job)
    missing = [k for k in CONTRACT_FIELDS if not _is_non_empty(out.get(k))]
    if missing:
        raise SystemExit("Selected search demand job missing fields: " + ", ".join(missing))
    if str(out.get("research_type") or "").strip().upper() != "SEARCH_DEMAND":
        raise SystemExit("Selected job research_type is not SEARCH_DEMAND")
    return out


def save_pending_search_demand_jobs(
    payload: dict[str, Any],
    *,
    root: Path = ROOT,
) -> tuple[int, dict[str, Any] | None]:
    """Write pending payload and optionally the first SEARCH_DEMAND job.

    Returns (job_count, selected_job_or_none). Does not walk the whole queue.
    """
    pending_path = root / "input" / "pending_search_demand_jobs.json"
    job_path = root / "input" / "search_demand_job.json"
    write_json(pending_path, payload)

    jobs = payload.get("jobs") or []
    if not jobs:
        print(f"Wrote {pending_path}")
        print("jobs=0")
        print("No SEARCH_DEMAND job to write")
        return 0, None

    selected = to_search_demand_job(jobs[0])
    write_json(job_path, selected)
    print(f"Wrote {pending_path}")
    print(f"Wrote {job_path}")
    print(f"jobs={len(jobs)}")
    print(f"selected_job_id={selected['job_id']}")
    return len(jobs), selected


def main() -> int:
    payload = fetch_pending_search_demand_jobs()
    count, _selected = save_pending_search_demand_jobs(payload)
    return 0 if count else 1


if __name__ == "__main__":
    sys.exit(main())
