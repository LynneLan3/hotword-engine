#!/usr/bin/env python3
"""Build eligibility and research freshness for daily BUILD recommendations.

Reuses existing research / preflight / Trends / SERP / social evidence.
Does not invent a second scoring system or force a daily BUILD.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

import steam_candidate_preflight as preflight
import steam_candidate_recommendation as recommendation

RESEARCH_STALE = "RESEARCH_STALE"
NO_BUILD_TODAY = "NO_BUILD_TODAY"
DISPLAY_BUILD = "BUILD"
DISPLAY_WATCH = "WATCH"
DISPLAY_REJECT = "REJECT"

# Prefer existing preflight thresholds rather than inventing new magic numbers.
DEDICATED_DOMAIN_BLOCK = preflight.DEDICATED_DOMAIN_REJECT_MIN
STALE_BEFORE_RELEASE_DAYS = 3
STALE_AFTER_RELEASE_DAYS = 2


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _count(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _parse_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raw = _text(value)
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except ValueError:
        if len(raw) >= 10 and raw[4] == "-" and raw[7] == "-":
            try:
                return date.fromisoformat(raw[:10])
            except ValueError:
                return None
    return None


def _research_observed_at(candidate: dict[str, Any]) -> date | None:
    research = _as_dict(candidate.get("research_result") or candidate.get("research"))
    serp = _as_dict(research.get("serp") or candidate.get("serp"))
    manual = _as_dict(
        research.get("manual_signals")
        or candidate.get("manual_signals")
        or _as_dict(candidate.get("candidate_state")).get("manual_evidence")
    )
    for value in (
        serp.get("checked_at"),
        serp.get("observed_at"),
        research.get("completed_at"),
        research.get("researched_at"),
        manual.get("trend_last_checked"),
        candidate.get("research_cycle_date"),
        _as_dict(candidate.get("candidate_state")).get("last_checked_date"),
    ):
        parsed = _parse_date(value)
        if parsed:
            return parsed
    return None


def assess_research_freshness(
    candidate: dict[str, Any],
    *,
    today: date | None = None,
) -> dict[str, Any]:
    """Mark RESEARCH_STALE when key SERP/Trends evidence predates release events."""
    today = today or date.today()
    steam = _as_dict(candidate.get("steam_signals"))
    release_date = _parse_date(steam.get("release_date") or candidate.get("release_date"))
    observed = _research_observed_at(candidate)
    reasons: list[str] = []
    stale = False

    if release_date and observed:
        if observed < release_date - timedelta(days=STALE_BEFORE_RELEASE_DAYS):
            # Pre-release SERP conclusions must not linger across the launch window.
            if today >= release_date - timedelta(days=1):
                stale = True
                reasons.append("SERP_OR_TRENDS_PREDATE_RELEASE_WINDOW")
        if (
            today >= release_date
            and observed < release_date
            and (today - release_date).days <= STALE_AFTER_RELEASE_DAYS
        ):
            stale = True
            reasons.append("POST_RELEASE_REQUIRES_FRESH_SERP_TRENDS")

    publish_event = _parse_date(
        candidate.get("recent_publish_at")
        or _as_dict(candidate.get("existing_site")).get("phase_entered_at")
    )
    if publish_event and observed and observed < publish_event:
        stale = True
        reasons.append("RESEARCH_OLDER_THAN_PUBLISH_EVENT")

    return {
        "research_stale": stale,
        "freshness_code": RESEARCH_STALE if stale else None,
        "research_observed_on": observed.isoformat() if observed else None,
        "release_date": release_date.isoformat() if release_date else None,
        "reasons": reasons,
        "requires_refresh_before_build": stale,
    }


def _dedicated_domain_count(candidate: dict[str, Any]) -> int:
    preflight_result = _as_dict(candidate.get("preflight") or candidate.get("preflight_result"))
    serp = _as_dict(preflight_result.get("serp"))
    domains = serp.get("dedicated_guide_domains")
    if isinstance(domains, list):
        return len(domains)
    research = _as_dict(candidate.get("research_result") or candidate.get("research"))
    competition = _as_dict(_as_dict(research.get("serp")).get("competition_summary"))
    signals = competition.get("signals")
    if isinstance(signals, list) and "HIGH_GUIDE_DENSITY" in {str(s).upper() for s in signals}:
        return DEDICATED_DOMAIN_BLOCK
    machine = _as_dict(candidate.get("machine_fields"))
    dedicated = machine.get("dedicated_wiki_domains")
    if isinstance(dedicated, list):
        return len(dedicated)
    return 0


def _recommendation_payload(candidate: dict[str, Any]) -> dict[str, Any]:
    existing = candidate.get("recommendation_payload")
    if isinstance(existing, dict) and existing.get("recommendation"):
        return existing
    research = candidate.get("research_result")
    if isinstance(research, dict) and research.get("steam_app_id"):
        try:
            return recommendation.build_steam_candidate_recommendation(research)
        except ValueError:
            pass
    # Fall back to a synthetic research-shaped object from attached fields.
    synthetic = {
        "job_id": candidate.get("job_id"),
        "steam_app_id": candidate.get("steam_app_id"),
        "game_name": candidate.get("game_name"),
        "steam_signals": _as_dict(candidate.get("steam_signals")),
        "manual_signals": _as_dict(candidate.get("manual_signals")),
        "social": _as_dict(candidate.get("social")),
        "serp": _as_dict(candidate.get("serp")),
    }
    if synthetic["steam_signals"] or synthetic["serp"] or synthetic["social"]:
        try:
            return recommendation.build_steam_candidate_recommendation(synthetic)
        except ValueError:
            return {}
    return {}


def assess_build_eligibility(
    candidate: dict[str, Any],
    *,
    today: date | None = None,
) -> dict[str, Any]:
    """Distinguish bestCandidate ranking from eligibleForBuild."""
    today = today or date.today()
    existing = _as_dict(candidate.get("existing_site"))
    freshness = assess_research_freshness(candidate, today=today)
    rec = _recommendation_payload(candidate)
    rec_code = _text(rec.get("recommendation")).upper()
    blocking: list[str] = []
    reasons: list[str] = list(rec.get("reasons") or [])

    if existing.get("existingSite") or candidate.get("existingSite") is True:
        blocking.append("EXISTING_SITE")
    if existing.get("eligibleForNewSite") is False or candidate.get("eligibleForNewSite") is False:
        blocking.append("NOT_ELIGIBLE_FOR_NEW_SITE")

    preflight_verdict = _text(
        _as_dict(candidate.get("preflight") or candidate.get("preflight_result")).get(
            "preflight_verdict"
        )
        or _as_dict(candidate.get("candidate_state")).get("preflight_verdict")
    ).upper()
    if preflight_verdict == preflight.AUTO_REJECT:
        blocking.append("HARD_REJECT_PREFLIGHT")

    decision = _text(
        _as_dict(candidate.get("candidate_state")).get("decision")
        or candidate.get("decision")
    ).upper()
    if decision == "REJECT":
        blocking.append("HARD_REJECT_DECISION")

    dedicated = _dedicated_domain_count(candidate)
    if dedicated >= DEDICATED_DOMAIN_BLOCK:
        blocking.append("DEDICATED_WIKI_DOMAINS_COVER_CORE_INTENT")

    manual = _as_dict(
        candidate.get("manual_signals")
        or _as_dict(candidate.get("candidate_state")).get("manual_evidence")
    )
    trends = recommendation._normalize_trends(manual.get("trends_result"))  # noqa: SLF001
    if trends == "WEAK_OR_NONE":
        blocking.append("TRENDS_SEARCH_DEMAND_WEAK")

    social = _as_dict(candidate.get("social") or _as_dict(candidate.get("research_result")).get("social"))
    actionable = _count(social.get("actionable_cluster_count"))
    if _text(social.get("status")).upper() == "AVAILABLE" and actionable < 1:
        # Only block when social evidence exists and shows no player problems.
        if rec_code == recommendation.RECOMMEND_BUILD:
            blocking.append("NO_ACTIONABLE_PLAYER_PROBLEMS")

    steam = _as_dict(candidate.get("steam_signals"))
    release_date = _parse_date(steam.get("release_date"))
    days_to_release = steam.get("days_to_release")
    try:
        days_value = int(days_to_release) if days_to_release is not None else None
    except (TypeError, ValueError):
        days_value = None
    if days_value is not None and days_value < -45:
        blocking.append("LIFECYCLE_WINDOW_MISSED")
    elif release_date and (today - release_date).days > 45:
        blocking.append("LIFECYCLE_WINDOW_MISSED")

    if freshness["research_stale"]:
        blocking.append(RESEARCH_STALE)

    missing = list(rec.get("missing_evidence") or [])
    if "SERP_EVIDENCE_NOT_AVAILABLE" in missing or "TRENDS_NOT_AVAILABLE" in missing:
        if rec_code == recommendation.RECOMMEND_BUILD:
            blocking.append("KEY_SERP_OR_TRENDS_EVIDENCE_MISSING")

    # High Steam momentum alone never forces BUILD.
    steam_type = recommendation._normalize_type(steam.get("first_round_type"))  # noqa: SLF001
    momentum_only = steam_type == "STRONG" and rec_code != recommendation.RECOMMEND_BUILD
    if momentum_only and not blocking:
        blocking.append("STEAM_MOMENTUM_INSUFFICIENT_WITHOUT_DEMAND_SERP_GUIDEABILITY")

    eligible = (
        not blocking
        and not existing.get("existingSite")
        and rec_code == recommendation.RECOMMEND_BUILD
    )
    watch = (
        not existing.get("existingSite")
        and not eligible
        and "HARD_REJECT_DECISION" not in blocking
        and "HARD_REJECT_PREFLIGHT" not in blocking
        and decision != "REJECT"
    )

    return {
        "eligibleForBuild": eligible,
        "bestCandidateScoreComponents": {
            "demand": trends,
            "lifecycle_timing": (
                "MISSED"
                if "LIFECYCLE_WINDOW_MISSED" in blocking
                else ("NEAR_RELEASE" if release_date else "UNKNOWN")
            ),
            "guideability": actionable,
            "serp_gap": _text(_as_dict(candidate.get("machine_fields")).get("serp_competition"))
            or _text(rec.get("evidence_snapshot", {}).get("serp_guide_density")),
            "steam_momentum": steam_type,
            "dedicated_site_competition": dedicated,
            "evidence_uncertainty": len(missing),
        },
        "recommendation": rec_code or None,
        "blocking_reasons": list(dict.fromkeys(blocking + list(rec.get("blocking_reasons") or []))),
        "reasons": reasons,
        "missing_evidence": missing,
        "freshness": freshness,
        "action_class": (
            DISPLAY_BUILD
            if eligible
            else (
                DISPLAY_REJECT
                if ("HARD_REJECT_DECISION" in blocking or "HARD_REJECT_PREFLIGHT" in blocking)
                else DISPLAY_WATCH
            )
        ),
        "watch_candidate": watch,
    }


def rank_build_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Order candidates for display without inventing a new weighted model.

    Preference order reuses existing labels:
    BUILD-eligible first, then stronger Steam type, then higher 7d gain,
    then lower dedicated-domain competition / evidence uncertainty.
    """
    type_rank = {"STRONG": 0, "UNKNOWN": 1, "BENCHMARK": 2, "WEAK": 3}

    def sort_key(item: dict[str, Any]) -> tuple:
        eligibility = _as_dict(item.get("build_eligibility"))
        components = _as_dict(eligibility.get("bestCandidateScoreComponents"))
        steam = _as_dict(item.get("steam_signals"))
        gain = _count(steam.get("followers_gain_7d"))
        return (
            0 if eligibility.get("eligibleForBuild") else 1,
            type_rank.get(_text(components.get("steam_momentum")), 9),
            -gain,
            _count(components.get("dedicated_site_competition")),
            _count(components.get("evidence_uncertainty")),
            _text(item.get("game_name")).casefold(),
        )

    return sorted(candidates, key=sort_key)
