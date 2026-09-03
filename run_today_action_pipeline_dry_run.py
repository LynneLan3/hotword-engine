#!/usr/bin/env python3
"""Production-equivalent today-action dry-run using live Existing Site sources.

Defaults to live Control Center registry + live GSC bindings + live site-pool
API/snapshot. Test fixtures are opt-in only via ``--use-regression-fixtures``
and are never used for production judgments.
"""

from __future__ import annotations

import argparse
import json
import os
import urllib.request
from datetime import date
from pathlib import Path
from typing import Any

import existing_site_exclusion as exclusion
import existing_site_live_sources as live_sources
import today_action_pipeline as pipeline

ROOT = Path(__file__).resolve().parent
STEAM_API_ENV = "STEAM_CANDIDATE_RESEARCH_API_URL"
FOCUS_CASES = (
    ("3848900", "Sucker for Love: Crush Landing"),
    ("4513480", "Scarlet Skips"),
    ("4339280", "ShipShaper: Falconeer Chronicles"),
)


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _fetch_pending_jobs() -> list[dict[str, Any]]:
    base = _text(os.environ.get(STEAM_API_ENV))
    if not base:
        return []
    separator = "&" if "?" in base else "?"
    url = f"{base}{separator}action=pendingSteamCandidateResearchJobs"
    req = urllib.request.Request(url, headers={"User-Agent": "hotword-engine-live-dry-run/1"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    jobs = payload.get("jobs") if isinstance(payload, dict) else None
    return [job for job in jobs or [] if isinstance(job, dict)]


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _candidates_from_payload(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        for key in ("candidates", "jobs", "raw_candidates"):
            rows = payload.get(key)
            if isinstance(rows, list):
                return [row for row in rows if isinstance(row, dict)]
    return []


def _ensure_focus_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_app = {_text(row.get("steam_app_id")): row for row in candidates}
    out = list(candidates)
    for app_id, game_name in FOCUS_CASES:
        if app_id in by_app:
            continue
        decision = "BUILD" if app_id in {"3848900", "4513480"} else ""
        out.append(
            {
                "job_id": f"live-focus-{app_id}-20260903",
                "steam_app_id": app_id,
                "game_name": game_name,
                "steam_signals": {"first_round_type": "🔥 趋势候选", "followers_gain_7d": 1},
                "candidate_state": {"decision": decision, "preflight_verdict": "PENDING"},
            }
        )
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--today", default="2026-09-03")
    parser.add_argument("--candidates", type=Path, default=None)
    parser.add_argument("--no-pending", action="store_true")
    parser.add_argument(
        "--use-regression-fixtures",
        action="store_true",
        help="Opt-in only. Never the production default.",
    )
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)

    if args.use_regression_fixtures:
        fixture = (
            ROOT
            / "tests"
            / "fixtures"
            / "existing_site_exclusion"
            / "2026-09-03-existing-sites.json"
        )
        snapshot = _load_json(fixture)
        index = exclusion.load_existing_site_index(
            registry_sites_path=live_sources.DEFAULT_REGISTRY_SITES,
            registry_games_path=live_sources.DEFAULT_REGISTRY_GAMES,
            gsc_rows=snapshot.get("gscSiteRows") or [],
            site_pool_rows=snapshot.get("steamSitePoolRows") or [],
        )
        source_meta = {"mode": "regression_fixtures_opt_in", "fixture": str(fixture)}
    else:
        index, source_meta = live_sources.load_production_existing_site_index()

    candidates: list[dict[str, Any]] = []
    if args.candidates:
        candidates.extend(_candidates_from_payload(_load_json(args.candidates)))
    if not args.no_pending:
        candidates.extend(_fetch_pending_jobs())
    candidates = _ensure_focus_candidates(candidates)

    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in candidates:
        key = _text(row.get("steam_app_id")) or f"name:{exclusion.normalize_game_name(row.get('game_name'))}"
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(row)

    result = pipeline.run_today_action_pipeline(
        deduped,
        existing_site_index=index,
        today=date.fromisoformat(args.today),
    )
    focus = {}
    for app_id, name in FOCUS_CASES:
        decision = "BUILD" if app_id in {"3848900", "4513480"} else ""
        focus[app_id] = exclusion.evaluate_existing_site(
            {
                "steam_app_id": app_id,
                "game_name": name,
                "candidate_state": {"decision": decision},
            },
            index,
        )
    payload = {
        "mode": "production_equivalent_live_dry_run",
        "existing_site_sources": source_meta,
        "focus_cases": focus,
        "pipeline": result,
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    output = args.output or (
        ROOT
        / "jobs"
        / f"daily-today-action-{args.today.replace('-', '')}"
        / "today_action_live_dry_run.json"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text, encoding="utf-8")
    print(text)
    print(f"Wrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
