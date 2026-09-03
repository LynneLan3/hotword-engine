#!/usr/bin/env python3
"""Fetch pending PLAYER_ALIAS_DISCOVERY jobs from Steam Apps Script.

Writes:
  input/pending_player_alias_discovery_jobs.json
  input/player_alias_discovery_job.json
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent
STEAM_CANDIDATE_RESEARCH_API_URL_ENV = "STEAM_CANDIDATE_RESEARCH_API_URL"
PENDING_ACTION = "pendingPlayerAliasDiscoveryJobs"
JOB_TYPE = "PLAYER_ALIAS_DISCOVERY"

CONTRACT_FIELDS = (
    "job_id",
    "job_type",
    "steam_app_id",
    "game_name",
    "steam_url",
    "research_cycle_date",
    "created_at",
)

FetchFn = Callable[[str], dict[str, Any]]


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def configured_api_url() -> str:
    base_url = str(os.environ.get(STEAM_CANDIDATE_RESEARCH_API_URL_ENV) or "").strip()
    if not base_url:
        raise SystemExit(
            f"{STEAM_CANDIDATE_RESEARCH_API_URL_ENV} is not set; "
            "refusing to use a fallback endpoint"
        )
    separator = "&" if "?" in base_url else "?"
    return f"{base_url}{separator}action={PENDING_ACTION}"


def fetch_pending_player_alias_discovery_jobs(
    url: str | None = None,
    fetch_fn: FetchFn | None = None,
) -> dict[str, Any]:
    url = url or configured_api_url()
    if fetch_fn is not None:
        payload = fetch_fn(url)
    else:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "hotword-engine-fetch-player-alias-discovery/1"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                body = resp.read().decode("utf-8")
        except urllib.error.URLError as exc:
            raise SystemExit(f"Failed to fetch pending player alias jobs: {exc}") from exc
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"Player alias API did not return JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise SystemExit("Player alias API returned a non-object JSON payload")
    jobs = payload.get("jobs")
    if not isinstance(jobs, list):
        raise SystemExit("Player alias API response missing a jobs array")
    return payload


def _is_non_empty(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


def to_player_alias_discovery_job(job: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(job, dict):
        raise SystemExit("Selected player alias job is not an object")
    out = {key: job.get(key) for key in CONTRACT_FIELDS}
    missing = [key for key in CONTRACT_FIELDS if not _is_non_empty(out.get(key))]
    if missing:
        raise SystemExit("Selected player alias job missing fields: " + ", ".join(missing))
    if str(out.get("job_type") or "").strip().upper() != JOB_TYPE:
        raise SystemExit("Selected job job_type is not PLAYER_ALIAS_DISCOVERY")
    return out


def save_pending_player_alias_discovery_jobs(
    payload: dict[str, Any],
    *,
    root: Path = ROOT,
) -> tuple[int, dict[str, Any] | None]:
    pending_path = root / "input" / "pending_player_alias_discovery_jobs.json"
    job_path = root / "input" / "player_alias_discovery_job.json"
    write_json(pending_path, payload)

    jobs = payload.get("jobs") or []
    if not jobs:
        print(f"Wrote {pending_path}")
        print("jobs=0")
        print("No PLAYER_ALIAS_DISCOVERY job to write")
        return 0, None

    selected = to_player_alias_discovery_job(jobs[0])
    write_json(job_path, selected)
    print(f"Wrote {pending_path}")
    print(f"Wrote {job_path}")
    print(f"jobs={len(jobs)}")
    print(f"selected_job_id={selected['job_id']}")
    return len(jobs), selected


def main() -> int:
    payload = fetch_pending_player_alias_discovery_jobs()
    count, _selected = save_pending_player_alias_discovery_jobs(payload)
    return 0 if count else 1


if __name__ == "__main__":
    sys.exit(main())
