#!/usr/bin/env python3
"""GAME_WIDE_SOCIAL_DISCOVERY V1 runner.

This module adds a game-wide social discovery layer on top of the existing
DEMAND_DISCOVERY provider path. It writes local JSON artifacts only:
  jobs/<job_id>/job.json
  jobs/<job_id>/game_wide_social_result.json
  jobs/<job_id>/status.json

Uses the existing Research Job callback when configured; no scheduler or page creation.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import sys
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import demand_discovery_runner as ddr
import research_runner as rr
import research_job_runner as rjr

ROOT = Path(__file__).resolve().parent

JOB_TYPE = "GAME_WIDE_SOCIAL_DISCOVERY"
DECISIONS = ("NEW", "EXPAND", "WATCH", "IGNORE")
DEFAULT_PROVIDERS = ("reddit", "steam", "youtube")
DEFAULT_LOOKBACK_HOURS = 48
DEFAULT_COOLDOWN_DAYS = 7
DEFAULT_PROVIDER_TIMEOUT_SECONDS = 180
CONTENT_STAGES = ("PRE_LAUNCH", "LAUNCH", "GROWTH", "STABLE")
GAMEPLAY_INTENTS = {
    "BOSS",
    "BUILD",
    "ITEM_LOCATION",
    "MECHANIC",
    "PROGRESSION",
    "PUZZLE_STUCK",
    "QUEST_PROGRESSION",
    "SAVE_PROGRESS",
    "WALKTHROUGH",
}
PRE_LAUNCH_INTENTS = {"EDITION", "FEATURE", "PLATFORM", "RELEASE", "OFFICIAL_REFERENCE"}

TASK_VERBS = {
    "how",
    "where",
    "why",
    "what",
    "when",
    "which",
    "can",
    "does",
    "do",
    "is",
    "are",
    "will",
    "need",
    "help",
    "find",
    "get",
    "unlock",
    "fix",
    "skip",
    "travel",
    "retrieve",
    "recover",
    "lost",
    "stuck",
    "crash",
    "crashing",
    "farm",
}

GENERIC_DISCUSSION = {
    "amazing",
    "awesome",
    "great",
    "impressions",
    "impression",
    "review",
    "trailer",
    "discussion",
    "thoughts",
    "hype",
    "news",
    "hotfix",
    "live",
    "patch",
    "lore",
    "meme",
    "beta",
}

STOP_WORDS = ddr.STOP_WORDS | {
    "mortal",
    "shell",
    "ii",
    "2",
    "all",
    "still",
    "after",
    "with",
    "from",
    "into",
    "below",
    "above",
    "there",
    "this",
    "that",
    "new",
}

SYNONYMS = {
    "crashes": "crash",
    "crashed": "crash",
    "crashing": "crash",
    "hotfixes": "hotfix",
    "beacons": "beacon",
    "travelling": "travel",
    "traveling": "travel",
    "retrieving": "retrieve",
    "retrieved": "retrieve",
    "recovers": "recover",
    "recovery": "recover",
    "farming": "farm",
    "fragments": "fragment",
    "skipping": "skip",
    "glooms": "gloom",
}


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def now_iso() -> str:
    return rr.now_iso()


def log(msg: str) -> None:
    rr.log(msg)


def _clean_provider(provider: Any) -> str:
    p = str(provider or "").strip().lower()
    if p not in DEFAULT_PROVIDERS:
        raise ValueError(f"unsupported provider: {p}")
    return p


def validate_job(job: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(job, dict):
        raise ValueError("job json must be object")
    if str(job.get("job_type") or "").strip().upper() != JOB_TYPE:
        raise ValueError(f"job_type must be {JOB_TYPE}")
    for field in ("job_id", "site_key", "game_name"):
        if not str(job.get(field) or "").strip():
            raise ValueError(f"{JOB_TYPE} job missing field: {field}")

    aliases = job.get("aliases") or []
    if not isinstance(aliases, list):
        raise ValueError("aliases must be array")
    for field in ("gsc_queries", "existing_pages", "recent_interventions"):
        value = job.get(field)
        if value is None:
            job[field] = []
        elif not isinstance(value, list):
            raise ValueError(f"{field} must be array")

    providers = job.get("providers") or list(DEFAULT_PROVIDERS)
    if not isinstance(providers, list):
        raise ValueError("providers must be array")
    job["providers"] = list(dict.fromkeys(_clean_provider(p) for p in providers))
    job["lookback_hours"] = int(job.get("lookback_hours") or DEFAULT_LOOKBACK_HOURS)
    if job["lookback_hours"] <= 0:
        raise ValueError("lookback_hours must be positive")
    job["cooldown_days"] = int(job.get("cooldown_days") or DEFAULT_COOLDOWN_DAYS)
    job["provider_timeout_seconds"] = int(
        job.get("provider_timeout_seconds") or DEFAULT_PROVIDER_TIMEOUT_SECONDS
    )
    return job


def load_job(path: Path) -> dict[str, Any]:
    return validate_job(json.loads(path.read_text(encoding="utf-8")))


def seed_terms(job: dict[str, Any]) -> list[str]:
    raw = [str(job.get("game_name") or "").strip()]
    raw.extend(str(a or "").strip() for a in job.get("aliases") or [])
    seen: set[str] = set()
    out: list[str] = []
    for term in raw:
        key = ddr.normalize_text(term)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(term)
    return out


def _canonical_token(token: str) -> str:
    t = token.lower()
    t = SYNONYMS.get(t, t)
    if t.endswith("ies") and len(t) > 4:
        t = t[:-3] + "y"
    elif t.endswith("s") and len(t) > 4:
        t = t[:-1]
    return SYNONYMS.get(t, t)


def text_tokens(text: str, *, aliases: list[str] | None = None) -> set[str]:
    norm = ddr.normalize_text(text)
    for alias in aliases or []:
        alias_norm = ddr.normalize_text(alias)
        if alias_norm:
            norm = norm.replace(alias_norm, " ")
    out: set[str] = set()
    for raw in re.findall(r"[a-z0-9]+", norm):
        tok = _canonical_token(raw)
        if len(tok) < 2 or tok in STOP_WORDS:
            continue
        out.add(tok)
    return out


def meaningful_overlap(a: set[str], b: set[str]) -> set[str]:
    return {t for t in (a & b) if t not in {"gloom", "hotfix", "beta", "page", "guide"}}


def evidence_text(item: dict[str, Any]) -> str:
    return " ".join(
        str(item.get(k) or "")
        for k in ("player_question", "signal_text", "title", "excerpt", "text", "snippet")
    ).strip()


def is_generic_discussion(text: str) -> bool:
    toks = text_tokens(text)
    if not toks:
        return True
    if toks <= GENERIC_DISCUSSION:
        return True
    blob = ddr.normalize_text(text)
    generic_hits = sum(1 for t in GENERIC_DISCUSSION if t in toks)
    task_hits = sum(1 for t in TASK_VERBS if t in toks or t in blob)
    return generic_hits >= 2 and task_hits == 0 and "?" not in text


def infer_question(text: str) -> str:
    raw = re.sub(r"\s+", " ", str(text or "")).strip()
    questions = re.findall(r"[^.!?\n]{8,220}\?", raw)
    if questions:
        q = questions[0].strip()
        return q[:220]
    n = ddr.normalize_text(raw)
    if "beacon" in n and "travel" in n:
        return "Where are all the Beacons and how do I fast travel?"
    if "beacon" in n:
        return "Where are all the Beacons?"
    if "fast travel" in n or ("travel" in n and "beacon" in n):
        return "How do I fast travel?"
    if "gloom" in n and any(w in n for w in ("lost", "retrieve", "recover", "stuck", "cliff")):
        return "How do I retrieve lost Gloom?"
    if "crash" in n or "crashing" in n:
        return "Why does the game crash?"
    if "skip" in n and "prologue" in n:
        return "How do I skip the prologue?"
    if "map fragment" in n or "map fragments" in n:
        return "Where do I find Map Fragments?"
    if "slayer seal" in n:
        return "How does Slayer Seal difficulty work?"
    if "magdalena" in n:
        return "Where is Magdalena?"
    cleaned = raw.strip(" .")
    if not cleaned:
        return ""
    if any(t in text_tokens(cleaned) for t in TASK_VERBS):
        return cleaned[:160] + ("" if cleaned.endswith("?") else "?")
    return ""


def task_intent_score(question: str, items: list[dict[str, Any]]) -> int:
    blob = " ".join([question] + [evidence_text(i) for i in items])
    toks = text_tokens(blob)
    if is_generic_discussion(blob):
        return 0
    score = 0
    if question.endswith("?"):
        score += 35
    score += min(35, 7 * sum(1 for t in toks if t in TASK_VERBS))
    if any(t in toks for t in ("crash", "skip", "beacon", "travel", "retrieve", "recover", "lost", "farm", "find", "unlock")):
        score += 20
    if len(toks) >= 2:
        score += 10
    return min(100, score)


def engagement_value(item: dict[str, Any]) -> int:
    engagement = item.get("engagement") or {}
    if not isinstance(engagement, dict):
        return 0
    total = 0
    for key in ("score", "upvotes", "comments", "comment_count", "views", "likes", "replies"):
        try:
            total += int(float(engagement.get(key) or 0))
        except Exception:
            continue
    return total


def engagement_score(items: list[dict[str, Any]]) -> int:
    total = sum(engagement_value(i) for i in items)
    if total >= 5000:
        return 100
    if total >= 500:
        return 80
    if total >= 100:
        return 60
    if total >= 20:
        return 40
    if total > 0:
        return 20
    return min(30, len(items) * 8)


def freshness_score(items: list[dict[str, Any]]) -> int:
    dated = [str(i.get("published_at") or "").strip() for i in items if str(i.get("published_at") or "").strip()]
    if dated:
        return 90
    return 50


def evidence_item_for_output(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "provider": item.get("provider") or item.get("source") or "",
        "source_family": item.get("source_family") or ddr.provider_to_family(str(item.get("provider") or item.get("source") or "")),
        "title": item.get("title") or "",
        "text": item.get("text") or item.get("excerpt") or item.get("evidence") or "",
        "snippet": item.get("snippet") or item.get("excerpt") or item.get("evidence") or "",
        "url": item.get("url") or "",
        "id": item.get("id") or "",
        "published_at": item.get("published_at"),
        "engagement": item.get("engagement") if isinstance(item.get("engagement"), dict) else {},
        "player_question": item.get("player_question") or "",
        "relevance": item.get("relevance") or 0,
        "matched_seed_terms": item.get("matched_seed_terms") or [],
    }


def cluster_key(question: str, aliases: list[str]) -> tuple[str, ...]:
    toks = text_tokens(question, aliases=aliases)
    if {"beacon", "travel"} & toks:
        if "beacon" in toks or "travel" in toks:
            return ("beacon", "travel")
    if "gloom" in toks and {"lost", "retrieve", "recover", "stuck", "cliff"} & toks:
        return ("gloom", "lost", "recover")
    if "crash" in toks:
        return ("crash",)
    if {"skip", "prologue"} <= toks:
        return ("skip", "prologue")
    return tuple(sorted(toks)[:6]) or ("other",)


# ---------------------------------------------------------------------------
# Intent family classification
# ---------------------------------------------------------------------------

# Keyword → family mapping. Order matters: first match wins.
# Triggers are checked against cluster key tokens ONLY (not topic text)
# to avoid false positives from game name "Mortal Shell" etc.
_INTENT_FAMILY_RULES: list[tuple[str, set[str], str]] = [
    # weapon upgrade / acquisition / path to weapons
    ("weapon", {"weapon", "axatana", "upgrade-weapon", "weapon-upgrade"}, "WEAPON_PROGRESSION"),
    # build guides (playstyle, not pure weapon)
    ("build", {"build", "sariel"}, "BUILD"),
    # shell ranking / selection (must have "shell" + ranking intent)
    ("shell", {"shell", "ranked", "tier"}, "SHELL_SELECTION"),
    # boss fights
    ("boss", {"boss", "boss-fight"}, "BOSS"),
    # location / map / beacon
    ("location", {"beacon", "beacon-travel", "map", "location"}, "LOCATION"),
    # general progression
    ("progression", {"progression", "unlock"}, "PROGRESSION"),
    # bugs / crashes
    ("bug", {"crash", "crashing", "bug", "glitch"}, "BUG"),
    # save / new game / prologue
    ("save", {"save", "new-game", "prologue", "skip-prologue", "save-progress"}, "SAVE_PROGRESS"),
    # trophy / achievement
    ("trophy", {"trophy", "achievement"}, "TROPHY"),
]


def intent_family(cluster_key_tuple: tuple[str, ...], topic: str = "") -> str:
    """Classify a cluster key into a high-level intent family.

    Uses keyword rules on the cluster key tokens + topic tokens.
    Falls back to OTHER if no rule matches.
    """
    toks = set(cluster_key_tuple)
    # Also tokenize topic for additional signal
    if topic:
        toks = toks | set(text_tokens(topic))

    for _match_tok, trigger_set, family in _INTENT_FAMILY_RULES:
        if trigger_set & toks:
            return family
    return "OTHER"


def cluster_evidence(evidence: list[dict[str, Any]], aliases: list[str]) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    for item in evidence:
        question = str(item.get("player_question") or "").strip() or infer_question(evidence_text(item))
        if not question:
            question = str(item.get("title") or item.get("signal_text") or item.get("excerpt") or "").strip()[:160]
        key = cluster_key(question or evidence_text(item), aliases)
        placed = False
        toks = set(key)
        for g in groups:
            same_key = key == g["_key"]
            overlap = meaningful_overlap(toks, set(g["_key"]))
            if same_key or (overlap and len(overlap) >= 2):
                g["_items"].append(item)
                if len(question) > len(g["_question"]):
                    g["_question"] = question
                placed = True
                break
        if not placed:
            groups.append({"_key": key, "_question": question, "_items": [item]})

    out: list[dict[str, Any]] = []
    for idx, group in enumerate(groups, start=1):
        items = group["_items"]
        providers = sorted({str(i.get("provider") or i.get("source") or "") for i in items if i.get("provider") or i.get("source")})
        families = sorted({ddr.provider_to_family(p) for p in providers if p})
        question = group["_question"] or infer_question(" ".join(evidence_text(i) for i in items))
        t_score = task_intent_score(question, items)
        topic = question if t_score >= 25 else (str(items[0].get("title") or items[0].get("signal_text") or "")[:160])
        out.append(
            {
                "topic_key": "-".join(group["_key"]),
                "topic": topic,
                "intent_family": intent_family(group["_key"], topic),
                "intent": "TASK" if t_score >= 45 else "GENERIC",
                "representative_questions": [question] if question and t_score >= 25 else [],
                "evidence_count": len(items),
                "source_families": families,
                "providers": providers,
                "freshness_score": freshness_score(items),
                "engagement_score": engagement_score(items),
                "task_intent_score": t_score,
                "evidence": [evidence_item_for_output(i) for i in items[:10]],
            }
        )
    out.sort(key=lambda c: (c["task_intent_score"], len(c["source_families"]), c["evidence_count"]), reverse=True)
    return out


def match_gsc(cluster: dict[str, Any], gsc_queries: list[dict[str, Any]], aliases: list[str]) -> dict[str, Any]:
    cluster_tokens = text_tokens(cluster.get("topic") or "", aliases=aliases)
    best: dict[str, Any] | None = None
    best_score = 0
    for row in gsc_queries:
        query = str(row.get("query") or "")
        qtoks = text_tokens(query, aliases=aliases)
        overlap = meaningful_overlap(cluster_tokens, qtoks)
        score = len(overlap) * 2
        if "crash" in cluster_tokens and "crash" in qtoks:
            score += 4
        if {"skip", "prologue"} <= cluster_tokens and {"skip", "prologue"} <= qtoks:
            score += 4
        if score > best_score:
            best_score = score
            best = row
    if best_score >= 5:
        status = "PRESENT"
    elif best_score >= 2:
        status = "WEAK"
    else:
        status = "ABSENT"
    return {"status": status, "query": best or {}, "score": best_score}


def _page_tokens(page: dict[str, Any]) -> set[str]:
    return text_tokens(" ".join(str(page.get(k) or "") for k in ("url", "title", "topic")))


def match_existing_page(cluster: dict[str, Any], pages: list[dict[str, Any]], aliases: list[str]) -> dict[str, Any]:
    cluster_tokens = text_tokens(cluster.get("topic") or "", aliases=aliases)
    best: dict[str, Any] | None = None
    best_score = 0
    best_status = "NONE"
    for page in pages:
        ptoks = _page_tokens(page)
        overlap = meaningful_overlap(cluster_tokens, ptoks)
        score = len(overlap) * 2
        status = "NONE"
        url_title = ddr.normalize_text(f"{page.get('url') or ''} {page.get('title') or ''}")
        if "crash" in cluster_tokens and "crashing" in url_title:
            score += 6
            status = "MATCHED"
        elif {"skip", "prologue"} <= cluster_tokens and {"skip", "prologue"} <= ptoks:
            score += 6
            status = "MATCHED"
        elif "gloom" in cluster_tokens and "gloom" in ptoks:
            if {"lost", "retrieve", "recover", "stuck"} & cluster_tokens and "farm" in ptoks:
                score += 3
                status = "PARTIAL"
            elif len(overlap) >= 2:
                status = "MATCHED"
        elif score >= 5:
            status = "MATCHED"
        elif score >= 2:
            status = "PARTIAL"
        if status == "NONE" and score < best_score:
            continue
        if score > best_score or (status == "MATCHED" and best_status != "MATCHED"):
            best_score = score
            best = page
            best_status = status
    return {"status": best_status, "page": best or {}, "score": best_score}


def _parse_date(value: Any) -> date | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except Exception:
        return None


def reference_date(job: dict[str, Any]) -> date:
    for key in ("run_date", "created_at"):
        parsed = _parse_date(job.get(key))
        if parsed:
            return parsed
    return datetime.now(timezone.utc).date()


def match_intervention(
    page_match: dict[str, Any],
    interventions: list[dict[str, Any]],
    job: dict[str, Any],
) -> dict[str, Any]:
    page = page_match.get("page") or {}
    url = str(page.get("url") or "").strip()
    if not url:
        return {"recent_intervention": False}
    ref = reference_date(job)
    cooldown = int(job.get("cooldown_days") or DEFAULT_COOLDOWN_DAYS)
    for item in interventions:
        iurl = str(item.get("url") or "").strip()
        if not iurl or iurl != url:
            continue
        dt = _parse_date(item.get("date"))
        if not dt:
            continue
        age = (ref - dt).days
        if 0 <= age <= cooldown:
            return {
                "recent_intervention": True,
                "url": url,
                "date": dt.isoformat(),
                "type": item.get("type") or "",
                "cooldown_days": cooldown,
                "age_days": age,
            }
    return {"recent_intervention": False}


def decide_cluster(cluster: dict[str, Any]) -> tuple[str, str]:
    task_score = int(cluster.get("task_intent_score") or 0)
    evidence_n = int(cluster.get("evidence_count") or 0)
    family_n = len(cluster.get("source_families") or [])
    provider_n = len(cluster.get("providers") or [])
    engagement = int(cluster.get("engagement_score") or 0)
    page_status = str((cluster.get("existing_page_match") or {}).get("status") or "NONE")
    recent_intervention = bool((cluster.get("intervention_match") or {}).get("recent_intervention"))

    if task_score < 45 or str(cluster.get("intent")) != "TASK":
        return "IGNORE", "Generic discussion or non-task intent."
    if recent_intervention:
        return "WATCH", "Matched page has a recent intervention inside cooldown."
    if page_status == "NONE":
        if evidence_n >= 2 and (family_n >= 2 or provider_n >= 2):
            return "NEW", "Task intent is clear, no existing page matched, and multiple social providers agree."
        if family_n == 1 and evidence_n >= 2 and task_score >= 80 and engagement >= 40:
            return "NEW", "Single-family signal is high-engagement, repeated, and task intent is very clear."
        return "WATCH", "Social signal exists but needs stronger confirmation before NEW."
    if page_status in {"MATCHED", "PARTIAL"}:
        if family_n >= 2 and evidence_n >= 2 and task_score >= 70:
            return "EXPAND", f"Existing page is {page_status.lower()}, but social evidence shows an uncovered task subproblem."
        return "WATCH", f"Existing page is {page_status.lower()}, but signal is not strong enough to expand yet."
    return "WATCH", "Fallback decision."


def _bool_value(value: Any) -> bool | None:
    if value is True or value is False:
        return value
    raw = str(value or "").strip().lower()
    if raw in {"true", "yes", "1", "released", "playable", "live"}:
        return True
    if raw in {"false", "no", "0", "upcoming", "announced", "unreleased", "not_playable"}:
        return False
    return None


def _gsc_has_demand(job: dict[str, Any], cluster: dict[str, Any] | None = None) -> bool:
    context = job.get("gsc_context") if isinstance(job.get("gsc_context"), dict) else {}
    if _bool_value(context.get("demand_confirmed")) is True:
        return True
    for key in ("impressions", "clicks", "query_count", "queries"):
        value = context.get(key)
        if isinstance(value, list) and value:
            return True
        try:
            if float(value or 0) > 0:
                return True
        except (TypeError, ValueError):
            pass
    for row in job.get("gsc_queries") if isinstance(job.get("gsc_queries"), list) else []:
        if not isinstance(row, dict):
            continue
        if any(float(row.get(k) or 0) > 0 for k in ("impressions", "clicks")):
            return True
    if isinstance(cluster, dict):
        match = cluster.get("gsc_match") if isinstance(cluster.get("gsc_match"), dict) else {}
        if str(match.get("status") or "").upper() == "PRESENT":
            return True
        query = match.get("query") if isinstance(match.get("query"), dict) else {}
        return any(float(query.get(k) or 0) > 0 for k in ("impressions", "clicks"))
    return False


def resolve_content_stage(job: dict[str, Any], clusters: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Resolve stage from explicit playability, current evidence and GSC context.

    No calendar-day shortcut: absent playability evidence is deliberately safe.
    """
    facts = job.get("official_facts") if isinstance(job.get("official_facts"), dict) else {}
    playability = _bool_value(
        job.get("is_playable")
        if job.get("is_playable") is not None
        else facts.get("is_playable", job.get("playability"))
    )
    release_state = str(
        job.get("release_state") or facts.get("release_state") or job.get("release_status") or ""
    ).strip().upper().replace("-", "_")
    launch_signal = _bool_value(job.get("launch_signal", facts.get("launch_signal")))
    launch_window = _bool_value(job.get("launch_window", facts.get("launch_window")))
    lifecycle = str(job.get("site_lifecycle") or job.get("lifecycle") or "").strip().upper()
    clusters = clusters or []
    external_demand = any(int(c.get("evidence_count") or 0) > 0 for c in clusters)
    gsc_demand = _gsc_has_demand(job)

    if playability is False or release_state in {"PRE_RELEASE", "UPCOMING", "ANNOUNCED", "UNRELEASED"}:
        return {
            "content_stage": "PRE_LAUNCH",
            "reason": "Official or playability facts show the game is not formally playable yet.",
            "evidence": {"playability": playability, "release_state": release_state, "source": "official_facts"},
        }
    if playability is not True:
        return {
            "content_stage": "PRE_LAUNCH",
            "reason": "Playability is not verified; gameplay intent stays blocked until an official/playable fact is supplied.",
            "evidence": {"playability": playability, "release_state": release_state, "source": "missing_playability_fact"},
        }
    if launch_signal is True or launch_window is True or release_state in {"LAUNCH", "RELEASED_FIRST_WAVE"}:
        return {
            "content_stage": "LAUNCH",
            "reason": "Playable fact plus an explicit current launch/first-wave signal.",
            "evidence": {"playability": playability, "launch_signal": launch_signal, "launch_window": launch_window, "release_state": release_state},
        }
    if gsc_demand and external_demand:
        return {
            "content_stage": "GROWTH",
            "reason": "Real GSC demand and current external player demand support expansion or a distinct intent.",
            "evidence": {"playability": playability, "gsc_demand": gsc_demand, "external_demand": external_demand},
        }
    return {
        "content_stage": "STABLE",
        "reason": "Playable fact is present without a current launch or combined expansion signal; route maintenance and long-tail work.",
        "evidence": {"playability": playability, "site_lifecycle": lifecycle, "gsc_demand": gsc_demand, "external_demand": external_demand},
    }


def _intent_type(cluster: dict[str, Any]) -> str:
    family = str(cluster.get("intent_family") or "").upper()
    mapped = {
        "LOCATION": "ITEM_LOCATION",
        "PROGRESSION": "QUEST_PROGRESSION",
        "SAVE_PROGRESS": "SAVE_PROGRESS",
        "BUG": "MECHANIC",
    }.get(family)
    if mapped:
        return mapped
    if family in GAMEPLAY_INTENTS or family in PRE_LAUNCH_INTENTS:
        return family
    return "MECHANIC"


def build_content_routing_receipt(job: dict[str, Any], cluster: dict[str, Any], stage: dict[str, Any]) -> dict[str, Any]:
    content_stage = stage["content_stage"]
    intent = _intent_type(cluster)
    decision = str(cluster.get("decision") or "WATCH").upper()
    evidence_count = int(cluster.get("evidence_count") or 0)
    source_families = set(cluster.get("source_families") or [])
    official = bool(cluster.get("official_evidence")) or bool(
        (job.get("official_facts") or {}).get("verified") if isinstance(job.get("official_facts"), dict) else False
    )
    gap = ""
    evidence_pass = False
    if content_stage == "PRE_LAUNCH":
        evidence_pass = intent not in GAMEPLAY_INTENTS and official and evidence_count > 0
        if intent in GAMEPLAY_INTENTS:
            gap = "PRE_LAUNCH cannot release an unverified gameplay guide; wait for playable evidence."
        elif not evidence_pass:
            gap = "Need an official release/platform/edition or public-feature source."
    elif content_stage == "LAUNCH":
        evidence_pass = evidence_count >= 2 and bool(source_families)
        if not evidence_pass:
            gap = "Need at least two current evidence items for the launch player problem."
    elif content_stage == "GROWTH":
        evidence_pass = _gsc_has_demand(job, cluster) and evidence_count > 0
        if not evidence_pass:
            gap = "Need real GSC query/impression evidence plus current external demand."
    else:
        freshness = job.get("gsc_context") if isinstance(job.get("gsc_context"), dict) else {}
        maintenance = any(freshness.get(k) for k in ("stale_answer", "freshness_signal", "ctr_signal", "long_tail"))
        evidence_pass = (_gsc_has_demand(job, cluster) or maintenance) and evidence_count > 0
        if not evidence_pass:
            gap = "Need GSC maintenance/freshness/CTR/long-tail context and supporting evidence."

    media = str(cluster.get("media_state") or job.get("media_state") or "MISSING").strip().upper()
    publish_state = "READY_FOR_WRITER" if evidence_pass else "RESEARCH_REQUIRED"
    if decision == "WATCH":
        publish_state = "RESEARCH_REQUIRED"
        gap = gap or "WATCH is not implementation-eligible; keep the scope under observation."
    article_class = {
        "PRE_LAUNCH": "OFFICIAL_REFERENCE",
        "LAUNCH": "PROBLEM_SOLVING_GUIDE",
        "GROWTH": "EXPANSION_GUIDE",
        "STABLE": "MAINTENANCE_REFRESH",
    }[content_stage]
    reason = stage["reason"]
    if gap:
        reason += " " + gap
    elif media in {"PARTIAL", "MISSING"}:
        reason += " Reliable answer evidence passes; media is " + media + " and does not block Writer readiness."
    primary = "CREATE_NEW_PAGE" if decision == "NEW" else "EXPAND_EXISTING" if decision == "EXPAND" else "WATCH"
    decision_id = f"content-decision-{job.get('job_id')}-{cluster.get('topic_key') or 'scope'}"
    return {
        "site_lifecycle": str(job.get("site_lifecycle") or job.get("lifecycle") or "").strip(),
        "content_stage": content_stage,
        "intent_type": intent,
        "article_class": article_class,
        "evidence_requirement": "PASS" if evidence_pass else "RESEARCH_REQUIRED",
        "media_requirement": media,
        "writer_mode": "SHARED_ARTICLE_WRITER" if publish_state == "READY_FOR_WRITER" else "BLOCKED",
        "publish_state": publish_state,
        "routing_reason": reason,
        "decision_id": decision_id,
        "primary_decision": primary,
        "confidence": "HIGH" if evidence_pass and decision in {"NEW", "EXPAND"} else "LOW",
        "page_path": str((cluster.get("existing_page_match") or {}).get("page", {}).get("url") or "").strip(),
    }


class ProviderTimeout(BaseException):
    pass


class provider_deadline:
    def __init__(self, seconds: int, label: str) -> None:
        self.seconds = max(1, int(seconds or DEFAULT_PROVIDER_TIMEOUT_SECONDS))
        self.label = label
        self.previous_handler: Any = None

    def _handle(self, _signum: int, _frame: Any) -> None:
        raise ProviderTimeout(f"{self.label} timed out after {self.seconds}s")

    def __enter__(self) -> None:
        self.previous_handler = signal.getsignal(signal.SIGALRM)
        signal.signal(signal.SIGALRM, self._handle)
        signal.alarm(self.seconds)

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        signal.alarm(0)
        if self.previous_handler is not None:
            signal.signal(signal.SIGALRM, self.previous_handler)
        return False


def collect_provider_for_seed(
    provider: str,
    seed: str,
    job: dict[str, Any],
    appids: list[str],
) -> tuple[list[dict[str, Any]], int]:
    game = str(job.get("game_name") or "")
    seed_role = ddr.determine_seed_role(seed, game)
    timeout_s = int(job.get("provider_timeout_seconds") or DEFAULT_PROVIDER_TIMEOUT_SECONDS)
    with provider_deadline(timeout_s, provider):
        if provider == "youtube":
            raw_items, _ = rr.collect_youtube(game=game, topic=seed)
        elif provider == "reddit":
            raw_items, _ = rr.collect_reddit(game=game, topic=seed)
        elif provider == "steam":
            raw_items, _ = rr.collect_steam(game=game, topic=seed, appids=appids)
        else:
            raise ValueError(f"unsupported provider: {provider}")
    items = [ddr.to_discovery_evidence_item(raw, seed, seed_role) for raw in raw_items]
    return items, len(raw_items)


def collect_social_evidence(job: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, int], dict[str, str]]:
    providers = list(job.get("providers") or DEFAULT_PROVIDERS)
    appids = rr.resolve_steam_appids(str(job.get("game_name") or ""), None)
    evidence: list[dict[str, Any]] = []
    source_counts = Counter({"reddit": 0, "steam": 0, "youtube": 0})
    source_failures: dict[str, str] = {}
    for seed in seed_terms(job):
        for provider in providers:
            try:
                provider_evidence, count = collect_provider_for_seed(provider, seed, job, appids)
                evidence.extend(provider_evidence)
                source_counts[provider] += count
            except ProviderTimeout as exc:
                source_failures[provider] = str(exc)
                log(f"  {provider} failed: {exc}")
            except Exception as exc:
                source_failures[provider] = str(exc)
                log(f"  {provider} failed: {exc}")
    return ddr.dedupe_discovery_evidence(evidence), dict(source_counts), source_failures


def run_game_wide_social_discovery(job: dict[str, Any]) -> dict[str, Any]:
    job = validate_job(dict(job))
    aliases = seed_terms(job)
    log(f"{JOB_TYPE} job={job['job_id']} game={job['game_name']!r}")
    log(f"Providers: {job['providers']}")
    log(f"Seed terms: {aliases}")
    evidence, source_counts, source_failures = collect_social_evidence(job)
    clusters = cluster_evidence(evidence, aliases)
    for cluster in clusters:
        gsc = match_gsc(cluster, job.get("gsc_queries") or [], aliases)
        page = match_existing_page(cluster, job.get("existing_pages") or [], aliases)
        intervention = match_intervention(page, job.get("recent_interventions") or [], job)
        cluster["gsc_match"] = gsc
        cluster["existing_page_match"] = page
        cluster["intervention_match"] = intervention
        decision, reason = decide_cluster(cluster)
        cluster["decision"] = decision
        cluster["reason"] = reason

    stage = resolve_content_stage(job, clusters)
    routing_receipts = []
    for cluster in clusters:
        if str(cluster.get("decision") or "").upper() in {"NEW", "EXPAND", "WATCH"}:
            receipt = build_content_routing_receipt(job, cluster, stage)
            cluster["content_routing"] = receipt
            routing_receipts.append(receipt)

    decision_counts = {d: 0 for d in DECISIONS}
    for c in clusters:
        decision_counts[str(c.get("decision") or "WATCH")] += 1

    return {
        "job_id": job["job_id"],
        "job_type": JOB_TYPE,
        "site_key": job.get("site_key"),
        "game_name": job["game_name"],
        "aliases": job.get("aliases") or [],
        "lookback_hours": job["lookback_hours"],
        "status": "COMPLETED",
        "run_at": now_iso(),
        "source_counts": source_counts,
        "source_failures": source_failures,
        "evidence_count": len(evidence),
        "site_lifecycle": job.get("site_lifecycle") or job.get("lifecycle") or "",
        "content_stage": stage["content_stage"],
        "content_stage_reason": stage["reason"],
        "content_stage_evidence": stage["evidence"],
        "clusters": clusters,
        "content_routing_receipts": routing_receipts,
        "content_decisions": routing_receipts,
        "decision_counts": decision_counts,
    }


def build_game_wide_callback_body(result: dict[str, Any], *, result_path: str = "") -> dict[str, Any]:
    receipts = result.get("content_routing_receipts") if isinstance(result.get("content_routing_receipts"), list) else []
    return {
        "job_id": result.get("job_id"),
        "job_type": JOB_TYPE,
        "research_type": "DEMAND_DISCOVERY",
        "execution_status": "COMPLETED",
        "discovery_scope": {"scope": "GAME_WIDE"},
        "evidence_count": int(result.get("evidence_count") or 0),
        "cluster_count": len(result.get("clusters") or []),
        "decision_counts": result.get("decision_counts") or {},
        "result_path": result_path,
        "site_lifecycle": result.get("site_lifecycle") or "",
        "content_stage": result.get("content_stage") or "",
        "content_stage_reason": result.get("content_stage_reason") or "",
        "content_stage_evidence": result.get("content_stage_evidence") or {},
        "content_routing_receipts": receipts,
        "content_decisions": receipts,
    }


def run_job(job_path: Path) -> dict[str, Any]:
    job = load_job(job_path)
    job_id = str(job["job_id"]).strip()
    job_dir = ROOT / "jobs" / job_id
    result_path = job_dir / "game_wide_social_result.json"
    status_path = job_dir / "status.json"
    job_copy_path = job_dir / "job.json"

    write_json(job_copy_path, job)
    write_json(status_path, {"job_id": job_id, "job_type": JOB_TYPE, "status": "RUNNING", "started_at": now_iso()})
    try:
        result = run_game_wide_social_discovery(job)
        write_json(result_path, result)
        callback_ok = None
        if os.environ.get("RESEARCH_CALLBACK_URL"):
            callback_ok = rjr.post_callback_body(
                build_game_wide_callback_body(
                    result,
                    result_path=f"jobs/{job_id}/game_wide_social_result.json",
                )
            )
        write_json(
            status_path,
            {
                "job_id": job_id,
                "job_type": JOB_TYPE,
                "status": "COMPLETED",
                "finished_at": now_iso(),
                "result_path": f"jobs/{job_id}/game_wide_social_result.json",
                "evidence_count": result.get("evidence_count", 0),
                "cluster_count": len(result.get("clusters") or []),
                "decision_counts": result.get("decision_counts") or {},
                "callback_ok": callback_ok,
            },
        )
        print_summary(result)
        return {"ok": callback_ok is not False, "status": "COMPLETED", "job_id": job_id, "result": result, "callback_ok": callback_ok}
    except Exception as exc:
        write_json(
            status_path,
            {
                "job_id": job_id,
                "job_type": JOB_TYPE,
                "status": "FAILED",
                "finished_at": now_iso(),
                "error": str(exc)[:300],
            },
        )
        raise


def print_summary(result: dict[str, Any], *, top_n: int = 10) -> None:
    counts = result.get("source_counts") or {}
    decisions = result.get("decision_counts") or {}
    log(
        f"Evidence={result.get('evidence_count', 0)} "
        f"reddit={counts.get('reddit', 0)} steam={counts.get('steam', 0)} youtube={counts.get('youtube', 0)}"
    )
    log(f"Clusters={len(result.get('clusters') or [])} decisions={decisions}")
    actionable = [c for c in result.get("clusters") or [] if c.get("decision") in {"NEW", "EXPAND", "WATCH"}]
    for idx, c in enumerate(actionable[:top_n], start=1):
        gsc = (c.get("gsc_match") or {}).get("status")
        page = (c.get("existing_page_match") or {}).get("status")
        log(f"{idx}. [{c.get('decision')}] {c.get('topic')} | gsc={gsc} page={page} | {c.get('reason')}")


def main() -> int:
    parser = argparse.ArgumentParser(description=f"{JOB_TYPE} Runner")
    parser.add_argument("job_file", help="Path to GAME_WIDE_SOCIAL_DISCOVERY job JSON")
    args = parser.parse_args()
    job_path = Path(args.job_file)
    if not job_path.is_absolute():
        job_path = (Path.cwd() / job_path).resolve()
    if not job_path.exists():
        log(f"Job file not found: {job_path}")
        return 2
    run_job(job_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
