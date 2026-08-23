#!/usr/bin/env python3
"""Deterministic research recommendation from existing local evidence.

This module is intentionally downstream-only. It does not call providers, scan
jobs, write callbacks, or make Candidate Decisions. Its only recommendation
values are RECOMMEND_ADVANCE, RECOMMEND_WATCH, and RECOMMEND_REJECT.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

RECOMMEND_ADVANCE = "RECOMMEND_ADVANCE"
RECOMMEND_WATCH = "RECOMMEND_WATCH"
RECOMMEND_REJECT = "RECOMMEND_REJECT"
RECOMMENDATIONS = (RECOMMEND_ADVANCE, RECOMMEND_WATCH, RECOMMEND_REJECT)

SCOPE_ORDINARY_CONTENT_OPPORTUNITY = "ORDINARY_CONTENT_OPPORTUNITY"

CONFIDENCE_HIGH = "HIGH"
CONFIDENCE_MEDIUM = "MEDIUM"
CONFIDENCE_LOW = "LOW"

SEARCH_STATUS_CONFIRMED = "CONFIRMED"
EXECUTION_COMPLETED = "COMPLETED"

SIGNAL_LOW_GUIDE_DENSITY = "LOW_GUIDE_DENSITY"
SIGNAL_MODERATE_GUIDE_DENSITY = "MODERATE_GUIDE_DENSITY"
SIGNAL_HIGH_GUIDE_DENSITY = "HIGH_GUIDE_DENSITY"
SIGNAL_HIGH_VIDEO_UGC_PRESENCE = "HIGH_VIDEO_UGC_PRESENCE"
SIGNAL_SERP_CONTAMINATION_PRESENT = "SERP_CONTAMINATION_PRESENT"

SOCIAL_ACTIONABLE_DECISIONS = frozenset({"NEW", "EXPAND"})
SOCIAL_WATCH_DECISION = "WATCH"


def _as_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _status(value: Any) -> str:
    return str(value or "").strip().upper()


def _summary_list(search_result: dict[str, Any]) -> list[dict[str, Any]]:
    summaries = search_result.get("serp_competition_summaries")
    if not isinstance(summaries, list):
        return []
    return [summary for summary in summaries if isinstance(summary, dict)]


def _signals(summary: dict[str, Any]) -> set[str]:
    raw = summary.get("signals") or []
    if not isinstance(raw, list):
        return set()
    return {_status(signal) for signal in raw if str(signal or "").strip()}


def _social_counts(social_result: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize social cluster decisions without promoting them to decisions."""
    if not isinstance(social_result, dict):
        return {
            "available": False,
            "evidence_count": 0,
            "cluster_count": 0,
            "actionable_cluster_count": 0,
            "watch_cluster_count": 0,
        }

    clusters = social_result.get("clusters")
    decision_counts = social_result.get("decision_counts")
    if isinstance(clusters, list):
        decisions = [_status(cluster.get("decision")) for cluster in clusters if isinstance(cluster, dict)]
        cluster_count = len(clusters)
        actionable_count = sum(decision in SOCIAL_ACTIONABLE_DECISIONS for decision in decisions)
        watch_count = sum(decision == SOCIAL_WATCH_DECISION for decision in decisions)
    else:
        counts = decision_counts if isinstance(decision_counts, dict) else {}
        actionable_count = sum(_as_int(counts.get(decision)) for decision in SOCIAL_ACTIONABLE_DECISIONS)
        watch_count = _as_int(counts.get(SOCIAL_WATCH_DECISION))
        cluster_count = sum(_as_int(value) for value in counts.values())

    return {
        "available": True,
        "evidence_count": _as_int(social_result.get("evidence_count")),
        "cluster_count": cluster_count,
        "actionable_cluster_count": actionable_count,
        "watch_cluster_count": watch_count,
    }


def _add_reason(reasons: list[str], reason: str) -> None:
    if reason not in reasons:
        reasons.append(reason)


def _serp_snapshot(summaries: list[dict[str, Any]]) -> dict[str, int]:
    counts = {
        "low": 0,
        "moderate": 0,
        "high": 0,
        "contamination": 0,
        "high_video_ugc": 0,
    }
    for summary in summaries:
        signals = _signals(summary)
        if SIGNAL_LOW_GUIDE_DENSITY in signals:
            counts["low"] += 1
        if SIGNAL_MODERATE_GUIDE_DENSITY in signals:
            counts["moderate"] += 1
        if SIGNAL_HIGH_GUIDE_DENSITY in signals:
            counts["high"] += 1
        if SIGNAL_SERP_CONTAMINATION_PRESENT in signals:
            counts["contamination"] += 1
        if SIGNAL_HIGH_VIDEO_UGC_PRESENCE in signals:
            counts["high_video_ugc"] += 1
    return counts


def _confidence(
    *,
    execution_completed: bool,
    has_serp: bool,
    social_available: bool,
) -> str:
    if not execution_completed or not has_serp:
        return CONFIDENCE_LOW
    if social_available:
        return CONFIDENCE_HIGH
    return CONFIDENCE_MEDIUM


def build_research_recommendation(
    search_result: dict[str, Any],
    social_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a transparent recommendation from existing result artifacts."""
    if not isinstance(search_result, dict):
        search_result = {}

    summaries = _summary_list(search_result)
    serp_counts = _serp_snapshot(summaries)
    social = _social_counts(social_result)
    execution_completed = _status(search_result.get("execution_status")) == EXECUTION_COMPLETED
    demand_confirmed = _status(search_result.get("search_demand_status")) == SEARCH_STATUS_CONFIRMED
    all_serp_high_guide = bool(summaries) and all(
        SIGNAL_HIGH_GUIDE_DENSITY in _signals(summary) for summary in summaries
    )

    can_advance = (
        demand_confirmed
        and serp_counts["low"] > 0
        and (social["actionable_cluster_count"] > 0 or serp_counts["high_video_ugc"] > 0)
        and not all_serp_high_guide
    )
    can_reject = (
        bool(summaries)
        and all_serp_high_guide
        and not demand_confirmed
        and social["actionable_cluster_count"] == 0
        and social["available"]
    )
    recommendation = RECOMMEND_ADVANCE if can_advance else RECOMMEND_REJECT if can_reject else RECOMMEND_WATCH

    reasons: list[str] = []
    blocking_reasons: list[str] = []
    missing_evidence: list[str] = []

    if demand_confirmed:
        _add_reason(reasons, "SEARCH_DEMAND_CONFIRMED")
    else:
        _add_reason(blocking_reasons, "SEARCH_DEMAND_NOT_CONFIRMED")
    if serp_counts["low"] > 0:
        _add_reason(reasons, SIGNAL_LOW_GUIDE_DENSITY)
    if serp_counts["moderate"] > 0:
        _add_reason(reasons, SIGNAL_MODERATE_GUIDE_DENSITY)
    if serp_counts["high"] > 0:
        _add_reason(reasons, SIGNAL_HIGH_GUIDE_DENSITY)
    if serp_counts["high_video_ugc"] > 0:
        _add_reason(reasons, SIGNAL_HIGH_VIDEO_UGC_PRESENCE)
    if serp_counts["contamination"] > 0:
        _add_reason(reasons, SIGNAL_SERP_CONTAMINATION_PRESENT)
    if social["actionable_cluster_count"] > 0:
        _add_reason(reasons, "ACTIONABLE_SOCIAL_PROBLEMS")
    if social["watch_cluster_count"] > 0:
        _add_reason(reasons, "WATCH_SOCIAL_PROBLEMS")

    if not social["available"]:
        missing_evidence.append("SOCIAL_EVIDENCE_NOT_AVAILABLE")
    if not summaries:
        missing_evidence.append("SERP_COMPETITION_EVIDENCE_NOT_AVAILABLE")
    if not execution_completed:
        missing_evidence.append("SEARCH_DEMAND_EXECUTION_NOT_COMPLETED")

    if recommendation == RECOMMEND_REJECT:
        blocking_reasons = [
            SIGNAL_HIGH_GUIDE_DENSITY,
            "SEARCH_DEMAND_NOT_CONFIRMED",
            "NO_ACTIONABLE_SOCIAL_PROBLEMS",
        ]
    elif recommendation == RECOMMEND_WATCH:
        if not summaries:
            _add_reason(blocking_reasons, "SERP_COMPETITION_EVIDENCE_NOT_AVAILABLE")
        if summaries and all_serp_high_guide:
            _add_reason(blocking_reasons, SIGNAL_HIGH_GUIDE_DENSITY)

    snapshot = {
        "search_demand_status": _status(search_result.get("search_demand_status")),
        "search_evidence_count": _as_int(search_result.get("search_evidence_count")),
        "serp_query_count": len(summaries),
        "serp_low_guide_density_queries": serp_counts["low"],
        "serp_moderate_guide_density_queries": serp_counts["moderate"],
        "serp_high_guide_density_queries": serp_counts["high"],
        "serp_contamination_queries": serp_counts["contamination"],
        "serp_high_video_ugc_queries": serp_counts["high_video_ugc"],
        "social_available": social["available"],
        "social_evidence_count": social["evidence_count"],
        "social_cluster_count": social["cluster_count"],
        "actionable_social_cluster_count": social["actionable_cluster_count"],
        "watch_social_cluster_count": social["watch_cluster_count"],
    }
    return {
        "recommendation": recommendation,
        "recommendation_scope": SCOPE_ORDINARY_CONTENT_OPPORTUNITY,
        "confidence": _confidence(
            execution_completed=execution_completed,
            has_serp=bool(summaries),
            social_available=social["available"],
        ),
        "reasons": reasons,
        "blocking_reasons": blocking_reasons,
        "missing_evidence": missing_evidence,
        "evidence_snapshot": snapshot,
    }


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON object required: {path}")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a local research recommendation from existing artifacts")
    parser.add_argument("--search-result", required=True, type=Path)
    parser.add_argument("--social-result", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)

    search_result = _read_json(args.search_result)
    social_result = _read_json(args.social_result) if args.social_result else None
    output = json.dumps(
        build_research_recommendation(search_result, social_result),
        ensure_ascii=False,
        indent=2,
    ) + "\n"
    if args.output:
        args.output.write_text(output, encoding="utf-8")
    else:
        print(output, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
