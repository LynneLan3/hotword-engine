#!/usr/bin/env python3
"""Fetch pending STEAM_CANDIDATE_RESEARCH jobs from Apps Script.

Writes:
  input/pending_steam_candidate_research_jobs.json
  input/steam_candidate_research_job.json

The fetcher is read-only with respect to Apps Script and selects only the
first pending job for local execution. Tests inject fetch_fn and never call
the real endpoint.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent
PENDING_STEAM_CANDIDATE_RESEARCH_JOBS_URL = (
    "https://script.google.com/macros/s/"
    "AKfycbwILJmfmk_PRtjgGffPzX1ZebnGTf9TzAbinkalMNBu5y4PsMbW4L_IdeJJTYGOuQzf"
    "/exec?action=pendingSteamCandidateResearchJobs"
)

CONTRACT_FIELDS = (
    "job_id",
    "job_type",
    "steam_app_id",
    "game_name",
    "steam_url",
    "research_cycle_date",
    "steam_signals",
    "manual_signals",
    "serp_queries",
    "requested_checks",
    "created_at",
)

FetchFn = Callable[[str], dict[str, Any]]


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def fetch_pending_steam_candidate_research_jobs(
    url: str = PENDING_STEAM_CANDIDATE_RESEARCH_JOBS_URL,
    fetch_fn: FetchFn | None = None,
) -> dict[str, Any]:
    if fetch_fn is not None:
        payload = fetch_fn(url)
    else:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "hotword-engine-fetch-steam-candidate-research/1"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read().decode("utf-8")
        except urllib.error.URLError as exc:
            raise SystemExit(f"Failed to fetch pending Steam candidate research jobs: {exc}") from exc
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"Steam candidate research API did not return JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise SystemExit("Steam candidate research API returned a non-object JSON payload")
    jobs = payload.get("jobs")
    if not isinstance(jobs, list):
        raise SystemExit("Steam candidate research API response missing a jobs array")
    return payload


def _is_non_empty(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


def _normalize_contract(job: dict[str, Any]) -> dict[str, Any]:
    out = {key: job.get(key) for key in CONTRACT_FIELDS}
    if not isinstance(out.get("steam_signals"), dict):
        out["steam_signals"] = {}
    if not isinstance(out.get("manual_signals"), dict):
        out["manual_signals"] = {}
    for field in ("serp_queries", "requested_checks"):
        if not isinstance(out.get(field), list):
            out[field] = []
    return out


def to_steam_candidate_research_job(job: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(job, dict):
        raise SystemExit("Selected Steam candidate research job is not an object")
    out = _normalize_contract(job)
    missing = [key for key in CONTRACT_FIELDS if not _is_non_empty(out.get(key))]
    if missing:
        raise SystemExit("Selected Steam candidate research job missing fields: " + ", ".join(missing))
    if str(out.get("job_type") or "").strip().upper() != "STEAM_CANDIDATE_RESEARCH":
        raise SystemExit("Selected job job_type is not STEAM_CANDIDATE_RESEARCH")
    if not out["serp_queries"]:
        raise SystemExit("Selected Steam candidate research job requires a brand SERP query")
    required_checks = {"GAME_WIDE_SOCIAL", "GOOGLE_ORGANIC_SERP"}
    if not required_checks.issubset({str(check).strip().upper() for check in out["requested_checks"]}):
        raise SystemExit("Selected Steam candidate research job requested_checks are incomplete")
    return out


def save_pending_steam_candidate_research_jobs(
    payload: dict[str, Any],
    *,
    root: Path = ROOT,
) -> tuple[int, dict[str, Any] | None]:
    pending_path = root / "input" / "pending_steam_candidate_research_jobs.json"
    job_path = root / "input" / "steam_candidate_research_job.json"
    write_json(pending_path, payload)

    jobs = payload.get("jobs") or []
    if not jobs:
        print(f"Wrote {pending_path}")
        print("jobs=0")
        print("No STEAM_CANDIDATE_RESEARCH job to write")
        return 0, None

    selected = to_steam_candidate_research_job(jobs[0])
    write_json(job_path, selected)
    print(f"Wrote {pending_path}")
    print(f"Wrote {job_path}")
    print(f"jobs={len(jobs)}")
    print(f"selected_job_id={selected['job_id']}")
    return len(jobs), selected


def main() -> int:
    payload = fetch_pending_steam_candidate_research_jobs()
    count, _selected = save_pending_steam_candidate_research_jobs(payload)
    return 0 if count else 1


if __name__ == "__main__":
    sys.exit(main())
