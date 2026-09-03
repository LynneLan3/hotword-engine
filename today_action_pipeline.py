#!/usr/bin/env python3
"""Daily today-action selection pipeline for Steam candidates.

Fixed order:

    raw candidates
    → existing-site exclusion
    → hard reject
    → research freshness
    → BUILD eligibility
    → ranking
    → user action queue

Already-built games cannot occupy the BUILD queue. A day may end with
``NO_BUILD_TODAY``.
"""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

import build_eligibility
import existing_site_exclusion as exclusion
import steam_candidate_preflight as preflight

ROOT = Path(__file__).resolve().parent
DEFAULT_REGISTRY_SITES = (
    ROOT.parent / "hotword-control-center" / "registry" / "sites.yaml"
)
DEFAULT_REGISTRY_GAMES = (
    ROOT.parent / "hotword-control-center" / "registry" / "games.yaml"
)


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _decision(candidate: dict[str, Any]) -> str:
    return _text(
        _as_dict(candidate.get("candidate_state")).get("decision")
        or candidate.get("decision")
        or candidate.get("machine_recommendation")
    ).upper().replace("RECOMMEND_", "")


def _is_hard_reject(candidate: dict[str, Any]) -> tuple[bool, str | None]:
    state = _as_dict(candidate.get("candidate_state"))
    if state.get("one_a_excluded"):
        return True, "EXISTING_1A_EXCLUSION"
    if _decision(candidate) == "REJECT":
        return True, "PERSISTED_DECISION_REJECT"
    verdict = _text(
        _as_dict(candidate.get("preflight") or candidate.get("preflight_result")).get(
            "preflight_verdict"
        )
        or state.get("preflight_verdict")
    ).upper()
    if verdict == preflight.AUTO_REJECT:
        reason = _text(
            _as_dict(candidate.get("preflight") or candidate.get("preflight_result")).get(
                "preflight_reason"
            )
        )
        return True, reason or "HARD_REJECT_PREFLIGHT"
    return False, None


def run_today_action_pipeline(
    raw_candidates: list[dict[str, Any]],
    *,
    existing_site_index: exclusion.ExistingSiteIndex | None = None,
    registry_sites_path: Path | None = DEFAULT_REGISTRY_SITES,
    registry_games_path: Path | None = DEFAULT_REGISTRY_GAMES,
    gsc_rows: list[dict[str, Any]] | None = None,
    site_pool_rows: list[dict[str, Any]] | None = None,
    gsc_snapshot_path: Path | None = None,
    site_pool_snapshot_path: Path | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """Run the fixed today-action generation chain and return queue + summary."""
    today = today or date.today()
    index = existing_site_index or exclusion.load_existing_site_index(
        registry_sites_path=registry_sites_path,
        registry_games_path=registry_games_path,
        gsc_rows=gsc_rows,
        site_pool_rows=site_pool_rows,
        gsc_snapshot_path=gsc_snapshot_path,
        site_pool_snapshot_path=site_pool_snapshot_path,
    )

    existing_excluded: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    stale_refresh: list[dict[str, Any]] = []
    watch: list[dict[str, Any]] = []
    build_eligible: list[dict[str, Any]] = []
    already_built: list[dict[str, Any]] = []
    remaining_for_rank: list[dict[str, Any]] = []

    for raw in raw_candidates:
        if not isinstance(raw, dict):
            continue
        annotated = exclusion.annotate_candidate(raw, index)
        existing = _as_dict(annotated.get("existing_site"))

        if existing.get("existingSite"):
            item = {
                **annotated,
                "pipeline_disposition": exclusion.ALREADY_BUILT,
                "action_type": exclusion.ALREADY_BUILT,
                "next_action": existing.get("next_action"),
            }
            existing_excluded.append(item)
            already_built.append(item)
            continue

        hard, hard_reason = _is_hard_reject(annotated)
        if hard:
            rejected.append(
                {
                    **annotated,
                    "pipeline_disposition": "REJECTED",
                    "action_type": "REJECTED",
                    "reject_reason": hard_reason,
                }
            )
            continue

        eligibility = build_eligibility.assess_build_eligibility(annotated, today=today)
        annotated["build_eligibility"] = eligibility
        freshness = _as_dict(eligibility.get("freshness"))
        if freshness.get("research_stale"):
            stale_refresh.append(
                {
                    **annotated,
                    "pipeline_disposition": build_eligibility.RESEARCH_STALE,
                    "action_type": build_eligibility.RESEARCH_STALE,
                    "next_action": "refresh research before BUILD eligibility",
                }
            )
            # Stale research may still appear as WATCH work, never BUILD.
            watch.append(
                {
                    **annotated,
                    "pipeline_disposition": "WATCH",
                    "action_type": "WATCH",
                    "watch_reason": build_eligibility.RESEARCH_STALE,
                }
            )
            continue

        if eligibility.get("eligibleForBuild"):
            item = {
                **annotated,
                "pipeline_disposition": "BUILD",
                "action_type": "BUILD",
            }
            build_eligible.append(item)
            remaining_for_rank.append(item)
            continue

        if eligibility.get("action_class") == "REJECT":
            rejected.append(
                {
                    **annotated,
                    "pipeline_disposition": "REJECTED",
                    "action_type": "REJECTED",
                    "reject_reason": ",".join(eligibility.get("blocking_reasons") or []),
                }
            )
            continue

        watch_item = {
            **annotated,
            "pipeline_disposition": "WATCH",
            "action_type": "WATCH",
            "watch_reason": ",".join(eligibility.get("blocking_reasons") or []) or "NOT_BUILD_ELIGIBLE",
        }
        watch.append(watch_item)
        remaining_for_rank.append(watch_item)

    ranked = build_eligibility.rank_build_candidates(remaining_for_rank)
    build_ranked = [item for item in ranked if item.get("action_type") == "BUILD"]
    no_build_today = len(build_ranked) == 0
    top_candidate = build_ranked[0] if build_ranked else None

    action_queue: list[dict[str, Any]] = []
    for item in build_ranked:
        action_queue.append(_queue_row(item, "BUILD"))
    for item in watch:
        if item.get("action_type") == build_eligibility.RESEARCH_STALE:
            continue
        action_queue.append(_queue_row(item, "WATCH"))
    for item in stale_refresh:
        action_queue.append(_queue_row(item, build_eligibility.RESEARCH_STALE))
    for item in already_built:
        action_queue.append(_queue_row(item, exclusion.ALREADY_BUILT))

    summary = {
        "RawCandidates": len(raw_candidates),
        "ExistingSitesExcluded": len(existing_excluded),
        "Rejected": len(rejected),
        "Watch": len(watch),
        "BuildEligible": len(build_ranked),
        "TopCandidate": _brief(top_candidate) if top_candidate else None,
        "NoBuildToday": no_build_today,
        "AlreadyBuilt": len(already_built),
        "ResearchStale": len(stale_refresh),
    }

    return {
        "run_date": today.isoformat(),
        "summary": summary,
        "no_build_today": no_build_today,
        "no_build_today_code": build_eligibility.NO_BUILD_TODAY if no_build_today else None,
        "existing_sites_excluded": [_brief(item) for item in existing_excluded],
        "rejected": [_brief(item) for item in rejected],
        "watch": [_brief(item) for item in watch],
        "build_eligible": [_brief(item) for item in build_ranked],
        "already_built": [_brief(item, include_existing=True) for item in already_built],
        "action_queue": action_queue,
        "remaining_candidates": [_brief(item) for item in ranked],
        "top_candidate": _brief(top_candidate, include_existing=True) if top_candidate else None,
    }


def _brief(item: dict[str, Any] | None, *, include_existing: bool = False) -> dict[str, Any] | None:
    if not item:
        return None
    existing = _as_dict(item.get("existing_site"))
    eligibility = _as_dict(item.get("build_eligibility"))
    payload = {
        "steam_app_id": _text(item.get("steam_app_id")),
        "game_name": _text(item.get("game_name")),
        "action_type": item.get("action_type") or item.get("pipeline_disposition"),
        "first_round_type": _text(_as_dict(item.get("steam_signals")).get("first_round_type")),
        "eligibleForBuild": bool(eligibility.get("eligibleForBuild")),
        "existingSite": bool(existing.get("existingSite") or item.get("existingSite")),
    }
    if include_existing or existing:
        payload.update(
            {
                "existingSiteSource": existing.get("existingSiteSource"),
                "existingSiteID": existing.get("existingSiteID"),
                "existingSiteStatus": existing.get("existingSiteStatus"),
                "stateSyncGap": existing.get("stateSyncGap"),
                "next_action": existing.get("next_action") or item.get("next_action"),
                "state_sync_codes": existing.get("state_sync_codes") or [],
            }
        )
    if eligibility:
        payload["blocking_reasons"] = eligibility.get("blocking_reasons") or []
        payload["recommendation"] = eligibility.get("recommendation")
    return payload


def _queue_row(item: dict[str, Any], action_type: str) -> dict[str, Any]:
    brief = _brief(item, include_existing=True) or {}
    brief["action_type"] = action_type
    return brief


def _load_candidates(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        for key in ("candidates", "jobs", "raw_candidates", "rows"):
            rows = payload.get(key)
            if isinstance(rows, list):
                return [row for row in rows if isinstance(row, dict)]
    raise ValueError(f"{path} must contain a candidate list or object with jobs/candidates")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", type=Path, required=True)
    parser.add_argument("--registry-sites", type=Path, default=DEFAULT_REGISTRY_SITES)
    parser.add_argument("--registry-games", type=Path, default=DEFAULT_REGISTRY_GAMES)
    parser.add_argument("--gsc-snapshot", type=Path, default=None)
    parser.add_argument("--site-pool-snapshot", type=Path, default=None)
    parser.add_argument("--today", type=str, default=None)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)

    today = date.fromisoformat(args.today) if args.today else date.today()
    result = run_today_action_pipeline(
        _load_candidates(args.candidates),
        registry_sites_path=args.registry_sites,
        registry_games_path=args.registry_games,
        gsc_snapshot_path=args.gsc_snapshot,
        site_pool_snapshot_path=args.site_pool_snapshot,
        today=today,
    )
    text = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
