#!/usr/bin/env python3
"""Deterministic M7B recommendation layer for Steam pre-build candidates.

This module consumes only a M7A ``steam_candidate_research_result.json``
artifact.  It deliberately has no provider, network, spreadsheet, or LLM
dependencies and never writes a Candidate Decision.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

RECOMMEND_BUILD = "RECOMMEND_BUILD"
RECOMMEND_WATCH = "RECOMMEND_WATCH"
RECOMMEND_REJECT = "RECOMMEND_REJECT"
RECOMMENDATIONS = {RECOMMEND_BUILD, RECOMMEND_WATCH, RECOMMEND_REJECT}
RECOMMENDATION_SCOPE = "STEAM_PREBUILD_CANDIDATE"


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _count(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _normalize_type(value: Any) -> str:
    """Normalize existing 1B labels without creating a new Steam score."""
    compact = _text(value).replace(" ", "").replace("　", "")
    if compact in {"🔥趋势", "🔥趋势候选", "🌱Early", "🌱Early候选"}:
        return "STRONG"
    if compact == "⚪低优先级":
        return "WEAK"
    if compact in {"🏢大盘对照", "🏢大盘对照候选"}:
        return "BENCHMARK"
    return "UNKNOWN"


def _normalize_trends(value: Any) -> str:
    normalized = _text(value).upper()
    if normalized in {"强", "STRONG"}:
        return "STRONG"
    if normalized in {"中", "MEDIUM"}:
        return "MEDIUM"
    if normalized in {"弱", "无", "WEAK", "NONE"}:
        return "WEAK_OR_NONE"
    return "UNKNOWN"


def _normalize_keyword_opportunity(value: Any) -> str:
    normalized = _text(value).upper()
    if normalized in {"有", "YES"}:
        return "PRESENT"
    if normalized in {"无", "NO"}:
        return "NOT_FOUND"
    return "UNKNOWN"


def _signal_set(summary: Any) -> set[str]:
    summary_dict = _as_dict(summary)
    signals = summary_dict.get("signals")
    if not isinstance(signals, (list, tuple, set)):
        return set()
    return {_text(signal).upper() for signal in signals if _text(signal)}


def _guide_density(signals: set[str]) -> str:
    if "HIGH_GUIDE_DENSITY" in signals:
        return "HIGH"
    if "MODERATE_GUIDE_DENSITY" in signals:
        return "MODERATE"
    if "LOW_GUIDE_DENSITY" in signals:
        return "LOW"
    return "UNKNOWN"


def _append_missing(missing: list[str], value: str) -> None:
    if value not in missing:
        missing.append(value)


def _confidence(*, steam_known: bool, serp_available: bool, social_available: bool) -> str:
    if not steam_known:
        return "LOW"
    if serp_available and social_available:
        return "HIGH"
    if serp_available or social_available:
        return "MEDIUM"
    return "LOW"


def build_steam_candidate_recommendation(research_result: dict[str, Any]) -> dict[str, Any]:
    """Build a deterministic recommendation from one M7A research artifact."""
    if not isinstance(research_result, dict):
        raise ValueError("Steam candidate research result must be an object")

    steam_signals = _as_dict(research_result.get("steam_signals"))
    manual_signals = _as_dict(research_result.get("manual_signals"))
    social = _as_dict(research_result.get("social"))
    serp = _as_dict(research_result.get("serp"))
    competition_summary = _as_dict(serp.get("competition_summary"))

    steam_type = _normalize_type(steam_signals.get("first_round_type"))
    steam_known = steam_type != "UNKNOWN"
    social_available = _text(social.get("status")).upper() == "AVAILABLE"
    serp_available = _text(serp.get("status")).upper() == "AVAILABLE"
    actionable_social = _count(social.get("actionable_cluster_count")) >= 1
    social_evidence = _count(social.get("evidence_count"))

    serp_signals = _signal_set(competition_summary)
    low_guide_density = "LOW_GUIDE_DENSITY" in serp_signals
    high_guide_density = "HIGH_GUIDE_DENSITY" in serp_signals
    high_video_ugc = "HIGH_VIDEO_UGC_PRESENCE" in serp_signals
    serp_contamination = "SERP_CONTAMINATION_PRESENT" in serp_signals

    trends = _normalize_trends(manual_signals.get("trends_result"))
    keyword_opportunity = _normalize_keyword_opportunity(
        manual_signals.get("keyword_opportunity")
    )

    missing_evidence: list[str] = []
    if not social_available:
        _append_missing(missing_evidence, "SOCIAL_EVIDENCE_NOT_AVAILABLE")
    if not serp_available:
        _append_missing(missing_evidence, "SERP_EVIDENCE_NOT_AVAILABLE")
    elif not competition_summary:
        _append_missing(missing_evidence, "SERP_COMPETITION_SUMMARY_NOT_AVAILABLE")
    if trends == "UNKNOWN":
        _append_missing(missing_evidence, "TRENDS_NOT_AVAILABLE")
    if keyword_opportunity == "UNKNOWN":
        _append_missing(missing_evidence, "KEYWORD_OPPORTUNITY_NOT_AVAILABLE")

    build = (
        steam_type == "STRONG"
        and serp_available
        and low_guide_density
        and (actionable_social or high_video_ugc)
        and not high_guide_density
    )
    reject_weak_steam = (
        steam_type == "WEAK"
        and high_guide_density
        and not actionable_social
    )
    reject_manual = (
        trends == "WEAK_OR_NONE"
        and keyword_opportunity == "NOT_FOUND"
        and high_guide_density
        and not actionable_social
    )

    reasons: list[str] = []
    blocking_reasons: list[str] = []
    if build:
        reasons.append("STEAM_STRONG_EARLY_SIGNAL")
        if low_guide_density:
            reasons.append("LOW_GUIDE_DENSITY")
        if actionable_social:
            reasons.append("ACTIONABLE_SOCIAL_PROBLEMS")
        if high_video_ugc:
            reasons.append("HIGH_VIDEO_UGC_PRESENCE")
        if trends == "STRONG":
            reasons.append("TRENDS_STRONG")
        if keyword_opportunity == "PRESENT":
            reasons.append("KEYWORD_OPPORTUNITY_PRESENT")
        recommendation = RECOMMEND_BUILD
    elif reject_weak_steam or reject_manual:
        if reject_weak_steam:
            reasons.extend(["STEAM_WEAK_SIGNAL", "HIGH_GUIDE_DENSITY"])
        else:
            reasons.extend([
                "TRENDS_WEAK_OR_NONE",
                "KEYWORD_OPPORTUNITY_NOT_FOUND",
                "HIGH_GUIDE_DENSITY",
            ])
        reasons.append("NO_ACTIONABLE_SOCIAL_PROBLEMS")
        recommendation = RECOMMEND_REJECT
    else:
        recommendation = RECOMMEND_WATCH
        if not steam_known:
            blocking_reasons.append("STEAM_SIGNAL_UNKNOWN")
        elif steam_type != "STRONG":
            blocking_reasons.append("STEAM_SIGNAL_NOT_STRONG")
        if not serp_available:
            blocking_reasons.append("SERP_EVIDENCE_NOT_AVAILABLE")
        if high_guide_density:
            blocking_reasons.append("HIGH_GUIDE_DENSITY")
        if not actionable_social and not high_video_ugc:
            blocking_reasons.append("NO_ACTIONABLE_SOCIAL_PROBLEMS")
        if serp_contamination:
            blocking_reasons.append("SERP_CONTAMINATION_PRESENT")

    evidence_snapshot = {
        "first_round_type": steam_signals.get("first_round_type"),
        "first_round_priority": steam_signals.get("first_round_priority"),
        "followers": steam_signals.get("followers"),
        "followers_gain_7d": steam_signals.get("followers_gain_7d"),
        "growth_rate": steam_signals.get("growth_rate"),
        "review_count": steam_signals.get("review_count"),
        "social_available": social_available,
        "social_evidence_count": social_evidence,
        "social_cluster_count": _count(social.get("cluster_count")),
        "actionable_social_cluster_count": _count(
            social.get("actionable_cluster_count")
        ),
        "serp_available": serp_available,
        "serp_organic_count": serp.get("organic_count"),
        "serp_guide_density": _guide_density(serp_signals),
        "serp_high_video_ugc": high_video_ugc,
        "serp_contamination": serp_contamination,
        "trends_result": manual_signals.get("trends_result"),
        "keyword_opportunity": manual_signals.get("keyword_opportunity"),
    }
    return {
        "job_id": research_result.get("job_id"),
        "steam_app_id": research_result.get("steam_app_id"),
        "game_name": research_result.get("game_name"),
        "recommendation": recommendation,
        "recommendation_scope": RECOMMENDATION_SCOPE,
        "confidence": _confidence(
            steam_known=steam_known,
            serp_available=serp_available,
            social_available=social_available,
        ),
        "reasons": reasons,
        "blocking_reasons": blocking_reasons,
        "missing_evidence": missing_evidence,
        "evidence_snapshot": evidence_snapshot,
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("research_result", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)

    payload = json.loads(args.research_result.read_text(encoding="utf-8"))
    recommendation = build_steam_candidate_recommendation(payload)
    if args.output:
        _write_json(args.output, recommendation)
    else:
        print(json.dumps(recommendation, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
