#!/usr/bin/env python3
"""Deterministic automatic preflight for Steam 1B candidates.

Preflight answers one question only: is this candidate worth the user's
manual Google Trends time?  It never creates a BUILD decision and it never
uses social evidence as a hard queue gate.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import search_demand_providers as providers

ROOT = Path(__file__).resolve().parent

AUTO_REJECT = "AUTO_REJECT"
WATCH = "WATCH"
MANUAL_REVIEW = "MANUAL_REVIEW"
PREFLIGHT_ERROR = "PREFLIGHT_ERROR"
VERDICTS = {AUTO_REJECT, WATCH, MANUAL_REVIEW, PREFLIGHT_ERROR}

DEFAULT_MAX_SERP_QUERIES = 3
RECHECK_GAIN_GROWTH_MIN = 0.30
DEDICATED_DOMAIN_REJECT_MIN = 2
HIGH_GUIDE_DENSITY_MIN = 5
MODERATE_GUIDE_DENSITY_MIN = 3

_PUNCTUATION_RE = re.compile(r"[^\w\s']+", re.UNICODE)
_SPACE_RE = re.compile(r"\s+")
_GUIDE_RE = re.compile(
    r"\b(?:guide|guides|wiki|walkthrough|walkthroughs|tips?|faq|how[- ]?to|"
    r"builds?|bosses?|maps?|items?|classes?|攻略|指南|wiki)\b",
    re.IGNORECASE,
)
_OFFICIAL_HOST_RE = re.compile(
    r"(?:^|\.)steam(?:community|powered)\.com$|(?:^|\.)store\.steampowered\.com$",
    re.IGNORECASE,
)
_SOCIAL_HOST_RE = re.compile(
    r"(?:^|\.)(?:reddit\.com|youtube\.com|youtu\.be|twitch\.tv|discord\.com|"
    r"x\.com|twitter\.com|facebook\.com|tiktok\.com)$",
    re.IGNORECASE,
)
_GENERIC_INTENT_RE = re.compile(
    r"\b(?:water|fire extinguisher|plumbing|ordinary language|meaning|definition|"
    r"weather|recipe|lyrics|news|shopping)\b",
    re.IGNORECASE,
)

REASON_TEXT = {
    "PERSISTED_DECISION_REJECT": "Persisted Candidate Decision is REJECT; suppress permanently.",
    "PERSISTED_DECISION_BUILD": "Persisted Candidate Decision is BUILD; screening is finished.",
    "EXISTING_1A_EXCLUSION": "Existing 1A exclusion; candidate is not eligible for preflight.",
    "WATCH_RECHECK_NOT_DUE": "WATCH recheck date has not arrived; suppress until the explicit date.",
    "WATCH_CONTINUE_NO_NEW_SIGNAL": "WATCH continues: no 30% 7d Gain growth or new ExternalSignal since the last check.",
    "EXISTING_MANUAL_EVIDENCE_WATCH": "Existing weak/negative Trends evidence has no new recheck signal; keep WATCH without another Trends task.",
    "EXISTING_MANUAL_EVIDENCE_RESUME": "Existing manual evidence is reused; continue from the next incomplete research stage.",
    "COMPETITION_SATURATED_DEDICATED_DOMAINS": "Competition saturated: at least two dedicated game guide/wiki domains are present.",
    "COMPETITION_SATURATED_HIGH_GUIDE_DENSITY": "Competition saturated: dedicated game coverage has high guide density.",
    "ENTITY_SEARCH_INTENT_CONTAMINATION": "Entity/search intent is dominated by ordinary-language or unrelated results.",
    "SEARCH_DEMAND_IMMATURE": "Search demand is immature; wait for stronger guide intent or post-release demand.",
    "UPCOMING_SEARCH_DEMAND_IMMATURE": "Upcoming release has immature guide demand; recheck after release.",
    "ALL_PREFLIGHT_PROVIDERS_UNAVAILABLE": "Preflight providers failed; retry later without converting the error to REJECT.",
    "PASSED_AUTOMATIC_NOISE_AND_COMPETITION_FILTERS": "Passed automatic noise and competition filters; manual Google Trends review is warranted.",
}

AutocompleteFn = Callable[[str, str], dict[str, Any]]
SerpFn = Callable[[str], dict[str, Any]]


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _iso_date(value: Any) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    raw = _text(value)
    if raw:
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).date().isoformat()
        except ValueError:
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
                return raw
    return date.today().isoformat()


def normalize_game_name(value: Any) -> str:
    """Normalize official punctuation without broadening to arbitrary tokens."""
    text = unicodedata.normalize("NFKC", _text(value))
    text = text.replace("™", " ").replace("®", " ").replace("©", " ")
    text = text.replace("’", "'").replace("‘", "'").replace("–", "-")
    text = text.casefold()
    text = _PUNCTUATION_RE.sub(" ", text)
    return _SPACE_RE.sub(" ", text).strip()


def _slug(value: Any) -> str:
    return normalize_game_name(value).replace(" ", "")


def _tokens(value: Any) -> list[str]:
    return [token for token in normalize_game_name(value).split() if len(token) > 1]


def _brand_match(game_name: str, text: Any) -> bool:
    brand = normalize_game_name(game_name)
    haystack = normalize_game_name(text)
    if not brand or not haystack:
        return False
    if brand in haystack:
        return True
    if "'" in brand:
        return False
    compact_brand = brand.replace(" ", "")
    compact_haystack = haystack.replace(" ", "")
    return len(compact_brand) >= 6 and compact_brand in compact_haystack


def _domain(value: Any) -> str:
    raw = _text(value).lower()
    host = urlparse(raw).netloc if "://" in raw else raw.split("/")[0].split(":")[0]
    return host.removeprefix("www.")


def _domain_matches_brand(domain: str, game_name: str) -> bool:
    host = _domain(domain)
    if not host or _OFFICIAL_HOST_RE.search(host) or _SOCIAL_HOST_RE.search(host):
        return False
    brand_slug = _slug(game_name).replace("'", "")
    if len(brand_slug) >= 6 and brand_slug in host.replace("-", ""):
        return True
    brand_tokens = _tokens(game_name)
    if not brand_tokens:
        return False
    host_key = re.sub(r"[^a-z0-9]", "", host)
    strong_tokens = [token.replace("'", "") for token in brand_tokens if len(token.replace("'", "")) >= 5]
    return bool(strong_tokens) and all(token in host_key for token in strong_tokens)


def _is_excluded_domain(domain: str) -> bool:
    host = _domain(domain)
    return bool(_OFFICIAL_HOST_RE.search(host) or _SOCIAL_HOST_RE.search(host))


def _result_text(result: dict[str, Any]) -> str:
    return " ".join(
        _text(result.get(key)) for key in ("title", "text", "snippet", "url", "domain")
    ).strip()


def _guide_like(result: dict[str, Any]) -> bool:
    return bool(_GUIDE_RE.search(_result_text(result)))


def _relevant_result(game_name: str, result: dict[str, Any]) -> bool:
    text = _result_text(result)
    if _brand_match(game_name, text):
        return True
    tokens = _tokens(game_name)
    if len(tokens) >= 2:
        normalized = normalize_game_name(text)
        return sum(token in normalized.split() for token in tokens) >= min(2, len(tokens))
    return False


def _density(count: int) -> str:
    if count >= HIGH_GUIDE_DENSITY_MIN:
        return "HIGH"
    if count >= MODERATE_GUIDE_DENSITY_MIN:
        return "MODERATE"
    return "LOW"


def summarize_serp_query(game_name: str, query: str, payload: dict[str, Any]) -> dict[str, Any]:
    raw_items = payload.get("items") if isinstance(payload, dict) else []
    if not isinstance(raw_items, list):
        raw_items = []
    items = [item for item in raw_items if isinstance(item, dict)]
    relevant = [item for item in items if _relevant_result(game_name, item)]
    guide_items = [item for item in relevant if _guide_like(item)]
    dedicated_domains = sorted(
        {
            _domain(item.get("domain") or item.get("url"))
            for item in guide_items
            if _domain_matches_brand(_domain(item.get("domain") or item.get("url")), game_name)
        }
    )
    irrelevant = len(items) - len(relevant)
    return {
        "query": query,
        "status": _text(payload.get("status")).upper() if isinstance(payload, dict) else "UNAVAILABLE",
        "error": payload.get("error") if isinstance(payload, dict) else "invalid_serp_payload",
        "provider_state": payload.get("provider_state") if isinstance(payload, dict) else None,
        "provider_reason": payload.get("provider_reason") if isinstance(payload, dict) else None,
        "provider_metadata": payload.get("metadata") if isinstance(payload, dict) else {},
        "organic_count": len(items),
        "relevant_count": len(relevant),
        "irrelevant_count": irrelevant,
        "relevant_ratio": round(len(relevant) / len(items), 4) if items else 0.0,
        "guide_count": len(guide_items),
        "guide_density": _density(len(guide_items)),
        "dedicated_guide_domains": dedicated_domains,
        "dedicated_guide_domain_count": len(dedicated_domains),
        "result_classifications": [
            {
                "position": item.get("position"),
                "domain": _domain(item.get("domain") or item.get("url")),
                "relevant": _relevant_result(game_name, item),
                "guide_like": _guide_like(item),
                "dedicated_guide": _domain_matches_brand(
                    _domain(item.get("domain") or item.get("url")), game_name
                ),
            }
            for item in items
        ],
    }


def _autocomplete_summary(game_name: str, runs: list[dict[str, Any]]) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    errors: list[str] = []
    available = 0
    for run in runs:
        if _text(run.get("status")).upper() in {providers.STATUS_BEST_EFFORT, providers.STATUS_SUPPORTED}:
            available += 1
        if run.get("error"):
            errors.append(_text(run.get("error")))
        raw_items = run.get("items")
        if isinstance(raw_items, list):
            items.extend(item for item in raw_items if isinstance(item, dict))
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        text = _text(item.get("text"))
        key = normalize_game_name(text)
        if not key or key in seen:
            continue
        seen.add(key)
        item = dict(item)
        item["relevant"] = _brand_match(game_name, text)
        item["guide_intent"] = bool(_GUIDE_RE.search(text))
        unique.append(item)
    relevant = [item for item in unique if item["relevant"]]
    guide = [item for item in relevant if item["guide_intent"]]
    return {
        "status": "AVAILABLE" if available else "UNAVAILABLE",
        "run_count": len(runs),
        "suggestion_count": len(unique),
        "relevant_count": len(relevant),
        "relevant_ratio": round(len(relevant) / len(unique), 4) if unique else 0.0,
        "guide_intent_count": len(guide),
        "guide_intent": bool(guide),
        "items": unique,
        "errors": errors,
    }


def _decision_state(job: dict[str, Any]) -> dict[str, Any]:
    state = _as_dict(job.get("candidate_state"))
    if not state:
        state = _as_dict(job.get("decision_state"))
    decision = state.get("decision") or state.get("Decision") or job.get("Decision") or job.get("decision")
    status = _text(decision or state.get("status") or job.get("status")).upper()
    next_review = state.get("next_recheck_date") or state.get("下次复查日") or job.get("next_recheck_date")
    one_a_excluded = bool(
        state.get("one_a_excluded")
        or state.get("excluded_1a")
        or _text(state.get("one_a_result")).upper() in {"REJECT", "EXCLUDED", "排除"}
    )
    manual = _as_dict(state.get("manual_evidence"))
    manual_signals = _as_dict(job.get("manual_signals"))
    def field(name: str, *aliases: str) -> Any:
        for source in (manual, manual_signals, state):
            for key in (name,) + aliases:
                if key in source and source.get(key) not in (None, ""):
                    return source.get(key)
        return ""
    steam = _as_dict(job.get("steam_signals"))
    current_gain = field("current_7d_gain", "followers_gain_7d")
    if current_gain in (None, ""):
        current_gain = steam.get("followers_gain_7d", "")
    return {
        "status": status,
        "next_review_date": next_review,
        "one_a_excluded": one_a_excluded,
        "current_7d_gain": current_gain,
        "last_checked_7d_gain": field("last_checked_7d_gain", "last_gain"),
        "last_checked_date": field("last_checked_date", "last_manual_check_date"),
        "trends_result": field("trends_result", "Google Trends结果"),
        "social_result": field("social_result", "Social结果"),
        "serp_competition": field("serp_competition", "SERP竞争"),
        "keyword_opportunity": field("keyword_opportunity", "关键词机会"),
        "trend_last_checked": field("trend_last_checked", "TrendLastChecked"),
        "external_signal": field("external_signal", "ExternalSignal"),
        "external_signal_is_new": bool(state.get("external_signal_is_new") or manual.get("external_signal_is_new")),
        "final_research_stage": field("final_research_stage", "FinalResearchStage"),
    }


def _completed_manual_value(value: Any) -> bool:
    text = _text(value)
    return bool(text) and text != "未检查"


def _optional_date(value: Any) -> date | None:
    raw = _text(value)
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(raw[:10])
        except ValueError:
            return None


def _external_signal_is_new(state: dict[str, Any]) -> bool:
    if bool(state.get("external_signal_is_new")):
        return True
    if not _text(state.get("external_signal")):
        return False
    signal_date = _optional_date(state.get("trend_last_checked"))
    last_checked = _optional_date(state.get("last_checked_date"))
    return bool(signal_date and (last_checked is None or signal_date > last_checked))


def _gain_growth_reached(state: dict[str, Any]) -> bool:
    try:
        current = float(state.get("current_7d_gain"))
        previous = float(state.get("last_checked_7d_gain"))
    except (TypeError, ValueError):
        return False
    if current < 0 or previous < 0:
        return False
    return current > 0 if previous == 0 else current >= previous * (1 + RECHECK_GAIN_GROWTH_MIN)


def _manual_next_action(job: dict[str, Any], state: dict[str, Any], *, allow_weak_trend_recheck: bool = False) -> str:
    trends = _text(state.get("trends_result"))
    if not _completed_manual_value(trends):
        return "Google Trends"
    weak = trends in {"弱", "无"}
    if weak and not allow_weak_trend_recheck:
        return "Recheck"
    if weak and allow_weak_trend_recheck:
        return "Google Trends"
    steam = _as_dict(job.get("steam_signals"))
    if steam.get("first_round_type") == "🌱 Early候选" and not _completed_manual_value(state.get("social_result")):
        return "Social验证"
    stage = _text(state.get("final_research_stage")).upper()
    if stage == "SERP_PROBE" and not _completed_manual_value(state.get("serp_competition")):
        return "SERP检查"
    if stage == "KEYWORD_RESEARCH" and not _completed_manual_value(state.get("keyword_opportunity")):
        return "Keyword Research"
    if not _completed_manual_value(state.get("keyword_opportunity")):
        return "Keyword Research"
    if not _completed_manual_value(state.get("serp_competition")):
        return "SERP检查"
    return "Recheck"


def _manual_evidence_gate(job: dict[str, Any], state: dict[str, Any], *, allow_weak_trend_recheck: bool = False) -> dict[str, Any] | None:
    trends = _text(state.get("trends_result"))
    if not _completed_manual_value(trends):
        return None
    action = _manual_next_action(job, state, allow_weak_trend_recheck=allow_weak_trend_recheck)
    if action == "Recheck":
        return _suppressed_result(job, WATCH, "EXISTING_MANUAL_EVIDENCE_WATCH", state=state, next_action=None)
    return _suppressed_result(
        job,
        MANUAL_REVIEW,
        "EXISTING_MANUAL_EVIDENCE_RESUME",
        state=state,
        next_action=action,
        eligible_for_today_action=True,
    )


def _suppressed_result(
    job: dict[str, Any],
    verdict: str,
    reason: str,
    *,
    state: dict[str, Any],
    next_action: str | None = None,
    eligible_for_today_action: bool = False,
) -> dict[str, Any]:
    return {
        "job_id": job.get("job_id"),
        "steam_app_id": job.get("steam_app_id"),
        "game_name": job.get("game_name"),
        "preflight_verdict": verdict,
        "preflight_reason": reason,
        "preflight_reason_text": REASON_TEXT.get(reason, reason),
        "reason": reason,
        "eligible_for_today_action": eligible_for_today_action,
        "suppressed_by_persisted_state": True,
        "recheck_due": False,
        "candidate_state": state,
        "next_action": next_action,
        "checked_at": datetime.now().isoformat(timespec="seconds"),
    }


def _cache_path(cache_dir: Path, app_id: str, query: str, run_date: str, source: str) -> Path:
    digest = hashlib.sha256(f"{app_id}|{run_date}|{source}|{normalize_game_name(query)}".encode()).hexdigest()[:16]
    return cache_dir / f"{run_date}-{app_id}-{digest}.json"


def _cached_probe(
    *,
    cache_dir: Path | None,
    app_id: str,
    query: str,
    run_date: str,
    source: str,
    probe: Callable[[], dict[str, Any]],
) -> tuple[dict[str, Any], bool]:
    path = _cache_path(cache_dir, app_id, query, run_date, source) if cache_dir else None
    if path and path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, dict) and isinstance(payload.get("result"), dict):
                return payload["result"], True
        except (OSError, ValueError):
            pass
    try:
        result = probe()
    except Exception as exc:
        result = {"status": "UNAVAILABLE", "items": [], "error": f"{type(exc).__name__}: {exc}"}
    if not isinstance(result, dict):
        result = {"status": "UNAVAILABLE", "items": [], "error": "invalid_provider_result"}
    if path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"app_id": app_id, "query": query, "source": source, "result": result}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return result, False


def _default_autocomplete(source: str, query: str, game_name: str, fetch_fn: Any) -> dict[str, Any]:
    fn = providers.probe_google_autocomplete if source == providers.SOURCE_GOOGLE_AUTOCOMPLETE else providers.probe_bing_autocomplete
    return fn(query, seed_terms=[game_name], anchor_topic="", fetch_fn=fetch_fn)


def _default_serp(query: str, game_name: str, fetch_fn: Any) -> dict[str, Any]:
    return providers.probe_searchapi_google_organic(
        query,
        seed_terms=[game_name],
        anchor_topic="",
        fetch_fn=fetch_fn,
        hl="en",
        gl="us",
        device="desktop",
    )


def _quality_reason(job: dict[str, Any], autocomplete: dict[str, Any], serp: dict[str, Any]) -> str | None:
    steam = _as_dict(job.get("steam_signals"))
    release_stage = _text(steam.get("release_stage") or steam.get("发布阶段")).lower()
    if not autocomplete.get("guide_intent") and not serp.get("relevant_count"):
        return "SEARCH_DEMAND_IMMATURE"
    if release_stage in {"upcoming", "未发售", "early", "即将发售"} and not autocomplete.get("guide_intent"):
        return "UPCOMING_SEARCH_DEMAND_IMMATURE"
    return None


def _watch_review_date(job: dict[str, Any], checked_at: str) -> str:
    steam = _as_dict(job.get("steam_signals"))
    raw_release = _text(steam.get("release_date") or steam.get("Steam 发布日期"))
    if raw_release:
        try:
            release = datetime.fromisoformat(raw_release.replace("Z", "+00:00")).date()
            if release >= date.fromisoformat(checked_at[:10]):
                return (release + timedelta(days=1)).isoformat()
        except ValueError:
            pass
    return (date.fromisoformat(checked_at[:10]) + timedelta(days=7)).isoformat()


def evaluate_preflight(
    job: dict[str, Any],
    *,
    autocomplete_runs: list[dict[str, Any]],
    serp_queries: list[dict[str, Any]],
    checked_at: str | None = None,
) -> dict[str, Any]:
    """Pure verdict builder used by replay and provider-backed execution."""
    game_name = _text(job.get("game_name"))
    autocomplete = _autocomplete_summary(game_name, autocomplete_runs)
    dedicated_domains = sorted(
        {domain for query in serp_queries for domain in query.get("dedicated_guide_domains", [])}
    )
    guide_queries = [query for query in serp_queries if _GUIDE_RE.search(_text(query.get("query")))]
    wiki_queries = [query for query in serp_queries if "wiki" in normalize_game_name(query.get("query"))]
    all_results = sum(int(query.get("organic_count") or 0) for query in serp_queries)
    relevant_results = sum(int(query.get("relevant_count") or 0) for query in serp_queries)
    irrelevant_results = sum(int(query.get("irrelevant_count") or 0) for query in serp_queries)
    guide_counts = [int(query.get("guide_count") or 0) for query in serp_queries]
    high_density = any(_text(query.get("guide_density")).upper() == "HIGH" for query in serp_queries)
    serp_available = any(_text(query.get("status")).upper() in {"SUPPORTED", "AVAILABLE"} for query in serp_queries)
    autocomplete_available = autocomplete["status"] == "AVAILABLE"
    provider_errors = list(autocomplete.get("errors") or []) + [
        _text(query.get("error")) for query in serp_queries if query.get("error")
    ]
    paid_states = [
        _text(query.get("provider_state")).upper()
        for query in serp_queries
        if _text(query.get("provider_state"))
    ]
    generic_intent = any(_GENERIC_INTENT_RE.search(_result_text(item)) for query in serp_queries for item in query.get("_items", []))
    contamination = (
        all_results >= 4
        and relevant_results / all_results <= 0.25
        and (generic_intent or autocomplete.get("relevant_ratio", 0.0) <= 0.25)
    )
    if contamination:
        verdict, reason = AUTO_REJECT, "ENTITY_SEARCH_INTENT_CONTAMINATION"
    elif len(dedicated_domains) >= DEDICATED_DOMAIN_REJECT_MIN:
        verdict, reason = AUTO_REJECT, "COMPETITION_SATURATED_DEDICATED_DOMAINS"
    elif dedicated_domains and high_density:
        verdict, reason = AUTO_REJECT, "COMPETITION_SATURATED_HIGH_GUIDE_DENSITY"
    elif not (autocomplete_available or serp_available):
        verdict, reason = PREFLIGHT_ERROR, "ALL_PREFLIGHT_PROVIDERS_UNAVAILABLE"
    else:
        weak_reason = _quality_reason(job, autocomplete, {"relevant_count": relevant_results})
        if weak_reason:
            verdict, reason = WATCH, weak_reason
        else:
            verdict, reason = MANUAL_REVIEW, "PASSED_AUTOMATIC_NOISE_AND_COMPETITION_FILTERS"

    eligible = verdict == MANUAL_REVIEW
    if verdict == WATCH and _text(job.get("recheck_due")).lower() == "true":
        eligible = True
    result = {
        "job_id": job.get("job_id"),
        "steam_app_id": job.get("steam_app_id"),
        "game_name": game_name,
        "preflight_verdict": verdict,
        "preflight_reason": reason,
        "preflight_reason_text": REASON_TEXT.get(reason, reason),
        "reason": reason,
        "eligible_for_today_action": eligible,
        "next_action": _manual_next_action(job, _decision_state(job)) if eligible else None,
        "next_review_date": _watch_review_date(job, checked_at or datetime.now().isoformat(timespec="seconds")) if verdict == WATCH else None,
        "checked_at": checked_at or datetime.now().isoformat(timespec="seconds"),
        "autocomplete": autocomplete,
        "serp": {
            "queries": [{key: value for key, value in query.items() if key != "_items"} for query in serp_queries],
            "query_count": len(serp_queries),
            "organic_count": all_results,
            "relevant_count": relevant_results,
            "irrelevant_count": irrelevant_results,
            "dedicated_guide_domain_count": len(dedicated_domains),
            "dedicated_guide_domains": dedicated_domains,
            "brand_serp_guide_density": _density(guide_counts[0] if guide_counts else 0),
            "guide_query_guide_density": _density(guide_counts[1] if len(guide_counts) > 1 else 0),
            "wiki_query_guide_density": _density(guide_counts[2] if len(guide_counts) > 2 else 0),
            "high_guide_density": high_density,
        },
        "entity": {
            "relevant_serp_ratio": round(relevant_results / all_results, 4) if all_results else 0.0,
            "autocomplete_relevant_ratio": autocomplete.get("relevant_ratio", 0.0),
            "contamination": contamination,
        },
        "provider_errors": provider_errors,
        "paid_provider_state": paid_states[0] if paid_states else None,
        "paid_provider_reason": next(
            (_text(query.get("provider_reason")) for query in serp_queries if query.get("provider_reason")),
            None,
        ),
        "paid_provider_usage": next(
            (
                query.get("provider_metadata", {}).get("account")
                for query in serp_queries
                if isinstance(query.get("provider_metadata"), dict)
                and isinstance(query.get("provider_metadata", {}).get("account"), dict)
            ),
            None,
        ),
        "searchapi_queries_used": len(serp_queries),
        "searchapi_queries_reused": sum(1 for query in serp_queries if query.get("cache_reused")),
        "social_supporting_evidence": _as_dict(job.get("social_supporting_evidence")),
    }
    return result


def run_preflight(
    job: dict[str, Any],
    *,
    autocomplete_fn: AutocompleteFn | None = None,
    serp_fn: SerpFn | None = None,
    provider_fetch_fn: Any = None,
    cache_dir: Path | None = None,
    now: date | datetime | None = None,
    max_serp_queries: int = DEFAULT_MAX_SERP_QUERIES,
    paid_serp_enabled: bool = True,
) -> dict[str, Any]:
    """Run preflight with bounded, same-day provider/cache semantics.

    ``paid_serp_enabled=False`` is the G040 free-first boundary. It preserves
    the autocomplete/social verdict path while making a paid SERP call
    impossible for that invocation.
    """
    if not isinstance(job, dict):
        raise ValueError("Steam preflight job must be an object")
    game_name = _text(job.get("game_name"))
    if not game_name:
        return {
            "preflight_verdict": PREFLIGHT_ERROR,
            "preflight_reason": "MISSING_GAME_NAME",
            "eligible_for_today_action": False,
        }
    state = _decision_state(job)
    status = state["status"]
    if state["one_a_excluded"]:
        return _suppressed_result(job, AUTO_REJECT, "EXISTING_1A_EXCLUSION", state=state)
    if status == "REJECT":
        return _suppressed_result(job, AUTO_REJECT, "PERSISTED_DECISION_REJECT", state=state)
    if status == "BUILD":
        return _suppressed_result(job, AUTO_REJECT, "PERSISTED_DECISION_BUILD", state=state)
    review_date = _iso_date(state["next_review_date"]) if state["next_review_date"] else ""
    run_date = _iso_date(now)
    if status == "WATCH" and review_date and run_date < review_date:
        result = _suppressed_result(job, WATCH, "WATCH_RECHECK_NOT_DUE", state=state)
        result["next_review_date"] = review_date
        return result
    allow_weak_trend_recheck = False
    if status == "WATCH" and review_date and run_date >= review_date:
        gain_growth = _gain_growth_reached(state)
        external_signal = _external_signal_is_new(state)
        if not (gain_growth or external_signal):
            result = _suppressed_result(job, WATCH, "WATCH_CONTINUE_NO_NEW_SIGNAL", state=state)
            result["next_review_date"] = _watch_review_date(job, datetime.now().isoformat(timespec="seconds"))
            return result
        allow_weak_trend_recheck = True
        job = dict(job)
        job["recheck_due"] = "true"

    manual_gate = _manual_evidence_gate(
        job,
        state,
        allow_weak_trend_recheck=allow_weak_trend_recheck or _external_signal_is_new(state),
    )
    if manual_gate is not None:
        if manual_gate.get("preflight_verdict") == WATCH:
            manual_gate["next_review_date"] = _watch_review_date(job, datetime.now().isoformat(timespec="seconds"))
        return manual_gate

    sources = (providers.SOURCE_GOOGLE_AUTOCOMPLETE, providers.SOURCE_BING_AUTOCOMPLETE)
    seeds = [game_name, f"{game_name} guide", f"{game_name} wiki", f"{game_name} how to"]
    autocomplete_runs: list[dict[str, Any]] = []
    for source in sources:
        for seed in seeds:
            result, reused = _cached_probe(
                cache_dir=cache_dir,
                app_id=_text(job.get("steam_app_id")),
                query=seed,
                run_date=run_date,
                source=source,
                probe=(lambda source=source, seed=seed: autocomplete_fn(source, seed) if autocomplete_fn else _default_autocomplete(source, seed, game_name, provider_fetch_fn)),
            )
            result = dict(result)
            result["cache_reused"] = reused
            autocomplete_runs.append(result)

    serp_queries: list[dict[str, Any]] = []
    if paid_serp_enabled:
        query_names = [game_name, f"{game_name} guide", f"{game_name} wiki"][: max(1, min(max_serp_queries, DEFAULT_MAX_SERP_QUERIES))]
        for index, query in enumerate(query_names):
            if index > 0 and serp_queries:
                first = serp_queries[0]
                if len(first.get("dedicated_guide_domains", [])) >= DEDICATED_DOMAIN_REJECT_MIN or first.get("guide_density") == "HIGH":
                    break
            raw, reused = _cached_probe(
                cache_dir=cache_dir,
                app_id=_text(job.get("steam_app_id")),
                query=query,
                run_date=run_date,
                source="SEARCHAPI_GOOGLE_ORGANIC",
                probe=(lambda query=query: serp_fn(query) if serp_fn else _default_serp(query, game_name, provider_fetch_fn)),
            )
            summary = summarize_serp_query(game_name, query, raw)
            summary["cache_reused"] = reused
            raw_items = raw.get("items") if isinstance(raw, dict) else []
            summary["_items"] = raw_items if isinstance(raw_items, list) else []
            serp_queries.append(summary)

    checked_at = now.isoformat(timespec="seconds") if isinstance(now, datetime) else (
        f"{now.isoformat()}T00:00:00" if isinstance(now, date) else None
    )
    result = evaluate_preflight(
        job,
        autocomplete_runs=autocomplete_runs,
        serp_queries=serp_queries,
        checked_at=checked_at,
    )
    result["paid_serp_enabled"] = bool(paid_serp_enabled)
    result["paid_verification_status"] = "COMPLETED" if paid_serp_enabled else "DEFERRED"
    return result


def build_preflight_result(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Compatibility alias for callers that use result-builder terminology."""
    return evaluate_preflight(*args, **kwargs)
