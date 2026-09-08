#!/usr/bin/env python3
"""Derive machine-written Today Action fields from M7A/preflight artifacts."""

from __future__ import annotations

from typing import Any

RECOMMEND_BUILD = "RECOMMEND_BUILD"
RECOMMEND_WATCH = "RECOMMEND_WATCH"
RECOMMEND_REJECT = "RECOMMEND_REJECT"

DISPLAY_BUILD = "BUILD"
DISPLAY_WATCH = "WATCH"
DISPLAY_REJECT = "REJECT"
DISPLAY_SKIP = "SKIP"
DISPLAY_ALREADY_BUILT = "ALREADY_BUILT"

# Candidate master-table enum for 机器推荐 / 人工决定.
MASTER_RECOMMENDATIONS = {DISPLAY_BUILD, DISPLAY_WATCH, DISPLAY_SKIP}


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _count(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def normalize_machine_recommendation_display(value: Any) -> str:
    """Legacy Today Action / 候选决策 display: BUILD / WATCH / REJECT / ALREADY_BUILT."""
    normalized = _text(value).upper()
    if normalized in {DISPLAY_ALREADY_BUILT, "ALREADY_BUILT"}:
        return DISPLAY_ALREADY_BUILT
    if normalized in {RECOMMEND_BUILD, DISPLAY_BUILD}:
        return DISPLAY_BUILD
    if normalized in {RECOMMEND_WATCH, DISPLAY_WATCH}:
        return DISPLAY_WATCH
    if normalized in {RECOMMEND_REJECT, DISPLAY_REJECT, DISPLAY_SKIP, "INSUFFICIENT_EVIDENCE"}:
        return DISPLAY_REJECT
    return ""


def normalize_master_machine_recommendation(value: Any) -> str:
    """Candidate master-table 机器推荐: BUILD / WATCH / SKIP only."""
    display = normalize_machine_recommendation_display(value)
    if display == DISPLAY_BUILD:
        return DISPLAY_BUILD
    if display == DISPLAY_WATCH:
        return DISPLAY_WATCH
    if display in {DISPLAY_REJECT, DISPLAY_ALREADY_BUILT}:
        return DISPLAY_SKIP
    normalized = _text(value).upper()
    if normalized == DISPLAY_SKIP:
        return DISPLAY_SKIP
    return ""


def build_master_outcome_machine_fields(
    *,
    social: dict[str, Any] | None = None,
    preflight_result: dict[str, Any] | None = None,
    machine: dict[str, Any] | None = None,
    recommendation: Any = None,
    confidence: Any = None,
) -> dict[str, Any]:
    """Machine-writable subset for 候选主表 outcome columns (no human fields)."""
    resolved = dict(machine or {})
    if not resolved:
        resolved = build_machine_fields(
            social=_as_dict(social),
            preflight_result=preflight_result,
        )
    return {
        "social_result": _text(resolved.get("social_result") or resolved.get("social_verdict")),
        "serp_competition": _text(resolved.get("serp_competition")),
        "machine_recommendation": normalize_master_machine_recommendation(
            recommendation if recommendation is not None else resolved.get("machine_recommendation")
        ),
        "machine_confidence": _text(confidence if confidence is not None else resolved.get("machine_confidence")),
    }


def compute_social_verdict(social: dict[str, Any]) -> tuple[str, str]:
    """Return (强/中/弱/无, one-line summary) for Today Action display."""
    if _text(social.get("status")).upper() != "AVAILABLE":
        error = _text(social.get("error"))
        return "无", error or "社媒证据不可用"

    evidence_count = _count(social.get("evidence_count"))
    actionable = _count(social.get("actionable_cluster_count"))
    watch = _count(social.get("watch_cluster_count"))
    clusters = social.get("top_clusters")
    clusters = clusters if isinstance(clusters, list) else []

    providers: set[str] = set()
    topics: list[str] = []
    for cluster in clusters[:5]:
        if not isinstance(cluster, dict):
            continue
        topic = _text(cluster.get("topic")) or _text(cluster.get("topic_key"))
        if topic and topic not in topics:
            topics.append(topic)
        for provider in cluster.get("providers") or cluster.get("source_families") or []:
            label = _text(provider)
            if label:
                providers.add(label)

    if actionable >= 2 or (actionable >= 1 and evidence_count >= 8):
        verdict = "强"
    elif actionable >= 1 or watch >= 2 or evidence_count >= 4:
        verdict = "中"
    elif evidence_count >= 1:
        verdict = "弱"
    else:
        verdict = "无"

    source_bits: list[str] = []
    provider_text = "/".join(sorted(providers)[:3])
    if provider_text:
        source_bits.append(provider_text)
    if topics:
        source_bits.append("主题:" + "、".join(topics[:3]))
    if actionable:
        source_bits.append(f"可行动话题{actionable}个")
    elif evidence_count:
        source_bits.append(f"证据{evidence_count}条")

    one_liner = "；".join(source_bits) if source_bits else "未发现明确玩家讨论"
    return verdict, one_liner


def compute_serp_competition(preflight_result: dict[str, Any] | None) -> str:
    preflight = _as_dict(preflight_result)
    serp = _as_dict(preflight.get("serp"))
    if "queries" in serp and not serp.get("queries"):
        return "未检查"
    dedicated = len(serp.get("dedicated_guide_domains") or [])
    densities = [
        _text(serp.get("brand_serp_guide_density")).upper(),
        _text(serp.get("guide_query_guide_density")).upper(),
        _text(serp.get("wiki_query_guide_density")).upper(),
    ]
    if dedicated >= 2 or "HIGH" in densities or serp.get("high_guide_density"):
        return "高"
    if dedicated >= 1 or "MODERATE" in densities:
        return "中"
    if serp:
        return "低"
    return "未检查"


def compute_keyword_opportunity(preflight_result: dict[str, Any] | None) -> str:
    preflight = _as_dict(preflight_result)
    autocomplete = _as_dict(preflight.get("autocomplete"))
    if autocomplete.get("guide_intent"):
        return "有"
    if _text(autocomplete.get("status")).upper() == "AVAILABLE":
        return "无"
    return "未检查"


def build_machine_fields(
    *,
    social: dict[str, Any],
    preflight_result: dict[str, Any] | None,
) -> dict[str, Any]:
    social_verdict, social_one_liner = compute_social_verdict(social)
    serp = _as_dict(_as_dict(preflight_result).get("serp"))
    dedicated_domains = serp.get("dedicated_guide_domains")
    dedicated_domains = dedicated_domains if isinstance(dedicated_domains, list) else []
    query_clusters: list[str] = []
    autocomplete = _as_dict(_as_dict(preflight_result).get("autocomplete"))
    for item in (autocomplete.get("items") or [])[:8]:
        if not isinstance(item, dict):
            continue
        text = _text(item.get("text"))
        if text and item.get("guide_intent"):
            query_clusters.append(text)
    for cluster in (social.get("top_clusters") or [])[:5]:
        if not isinstance(cluster, dict):
            continue
        for question in (cluster.get("representative_questions") or [])[:2]:
            text = _text(question)
            if text and text not in query_clusters:
                query_clusters.append(text)

    preflight = _as_dict(preflight_result)
    paid_state = _text(
        preflight.get("trends_provider_state") or preflight.get("paid_provider_state")
    ).upper()
    paid_reason = _text(
        preflight.get("trends_provider_reason")
        or preflight.get("paid_provider_reason")
        or preflight.get("trends_error")
    )

    return {
        "trends_result": _text(preflight.get("trends_result")) or "未检查",
        "social_result": social_verdict,
        "social_verdict": social_verdict,
        "social_one_liner": social_one_liner,
        "serp_competition": compute_serp_competition(preflight_result),
        "keyword_opportunity": compute_keyword_opportunity(preflight_result),
        "dedicated_wiki_domains": dedicated_domains[:5],
        "query_clusters": query_clusters[:8],
        "provider_terminal_state": paid_state or None,
        "provider_terminal_reason": paid_reason or None,
        "provider_usage": preflight.get("paid_provider_usage"),
        "machine_research_terminal": bool(paid_state),
    }
