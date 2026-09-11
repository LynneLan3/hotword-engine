#!/usr/bin/env python3
"""One-shot Social Research Runner (M1 minimal loop).

Collects player discussion from YouTube, Reddit, and Steam Discussions for a
single GSC content opportunity, then writes research_result.json.

No articles, no site edits, no database, no dashboard.

Usage:
  python3 research_runner.py
  python3 research_runner.py --game "Mortal Shell II" \\
    --topic "beta save / beta rewards / progress carry-over" \\
    --existing-page "/mortal-shell-ii/beta-progress-carry-over/"
"""

from __future__ import annotations

import argparse
import html as html_lib
import json
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
OUT_DEFAULT = ROOT / "output" / "research_result.json"

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
SSL_CTX = ssl.create_default_context()
STEAM_COOKIE = (
    "birthtime=568022401; lastagecheckage=1-January-1988; "
    "wants_mature_content=1; mature_content=1"
)
YT_CLIENT = {"clientName": "WEB", "clientVersion": "2.20240815.00.00", "hl": "en", "gl": "US"}

# Only confirmed per-game Steam App IDs. Never guess; never fall back to another game.
STEAM_APP_IDS = {
    "mortal shell 2": ["2584270"],
}

GAME_ALIASES = {
    "mortal shell 2": ["mortal shell 2", "mortal shell ii", "mortalshell2"],
}

# Distinctive markers used to reject cross-game contamination.
# Keys are norm(game) owners; markers must not appear in other games' evidence.
GAME_DISTINCTIVE_MARKERS: dict[str, tuple[str, ...]] = {
    "mortal shell 2": (
        "mortal shell 2",
        "mortal shell ii",
        "mortalshell2",
        "flayed harbinger",
        "marrow keep",
        "2584270",
    ),
}

STEAM_APP_OWNERS = {
    "2584270": "mortal shell 2",
}

NOISE_ALIASES = (
    "mortal kombat",
    "mk1",
    "stellaris",
    "super mario",
    "ocarina of time",
    "stalker",
)

REDDIT_SKIP_SUBS = (
    "247videogame",
    "tekkens",
    "gearsofwars",
    "haloinfinity",
    "upgameshop",
    "radeon",
    "logitechgcloud",
    "erotica",
    "gametrade",
)

# Mortal Shell II specialty — only applied when researching that game.
MS2_CORE_TERMS = (
    "carry over",
    "carry-over",
    "carry on",
    "carried",
    "save transfer",
    "save file",
    "beta save",
    "cloud save",
    "progress transfer",
    "progress carry",
    "beta reward",
    "beta rewards",
    "beta bonus",
    "beta unlock",
    "flayed harbinger",
    "flayed",
    "uninstall",
    "prologue skip",
    "skip the prologue",
    "skip prologue",
    "marrow keep",
    "will not carry",
    "doesn't carry",
    "does not carry",
    "full reset",
    "reset at launch",
    "completion of the beta",
    "30 minutes",
    "30 minute",
)

MS2_STORE_BOILERPLATE = (
    "beta save progress will not carry over in full",
    "play the mortal shell ii open beta to unlock the flayed harbinger",
    "the mortal shell ii open beta grants access to the opening hours",
    "select progress carrying forward",
    "grants access to the opening hours",
    "play the opening three hours of the game",
)

MS2_DIRECT_ANSWER_PHRASES = (
    "will not carry",
    "does not carry",
    "doesn t carry",
    "doesnt carry",
    "reset at launch",
    "save data will not transfer",
    "progress does not carry",
    "skip the prologue",
    "skip prologue",
    "flayed harbinger",
    "30 minute",
    "carry over to the full",
    "carry over when",
    "save transfer",
    "save file carry",
    "full reset at release",
)

MS2_TOPIC_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("cloud_save", ("cloud save", "cloud saves", "no cloud", "cross-device", "another pc", "laptop")),
    ("uninstall_keep_rewards", ("uninstall", "delete the beta", "remove the beta", "lose our beta unlock")),
    ("completion_bonus", ("100 percent", "100%", "completion of the beta", "full completion")),
    ("playtime_threshold", ("30 minutes", "30 mins", "playtime", "play for at least")),
    ("prologue_skip", ("prologue", "marrow keep", "skip the prologue", "skip prologue")),
    ("beta_rewards", ("flayed harbinger", "harbinger", "cosmetic", "beta reward", "beta bonus", "unlock")),
    ("inventory_reset", ("currency", "weapons", "shells", "collectibles", "inventory", "items reset")),
    ("full_save_transfer", ("carry over", "carry on", "save transfer", "progress transfer", "save file", "beta save", "full reset", "start over")),
    ("claim_process", ("how to claim", "claim at launch", "claim the")),
    ("cross_platform", ("ps5", "xbox", "playstation", "cross platform", "cross-platform")),
]

MS2_PAGE_RELATED_TOPICS = {
    "full_save_transfer",
    "inventory_reset",
    "beta_rewards",
    "prologue_skip",
    "claim_process",
    "uninstall_keep_rewards",
    "completion_bonus",
    "playtime_threshold",
    "cross_platform",
}

MS2_EXISTING_PAGE_FALLBACK = """
Beta save progress does not carry over in full. Playing the Open Beta unlocks
The Flayed Harbinger cosmetic at launch. Progressing beyond the Marrow Keep
unlocks a Prologue-skip option. Currency, weapons, Shells, collectibles, and
other discovered items reset. The skip is optional. Magdalena is not the
stated requirement. Official copy does not publish a playtime threshold.
Claim The Flayed Harbinger in the full game at launch on August 20, 2026.
Tiel and other Shells reset. Save transfer is not the same as beta rewards.
"""

ACTION_RESEARCH_PAGE_OPTIMIZATION = "PAGE_OPTIMIZATION_RESEARCH"
_ACTIVE_RESEARCH_TYPE = ""
FRESHNESS_UPDATE_MARKERS = (
    "patch",
    "update",
    "release",
    "hotfix",
    "changed",
    "newer",
)
FRESHNESS_IMPACT_MARKERS = (
    "crash",
    "fix",
    "optimisation",
    "optimization",
    "bug",
    "performance",
    "status",
)
FRESHNESS_GENERIC_TERMS = {
    "official",
    "update",
    "patch",
    "release",
    "hotfix",
    "changed",
    "newer",
    "current",
    "live",
    "page",
    "latest",
    "still",
    "fact",
    "announces",
    "several",
    "officially",
}
LEGACY_BETA_INTENT_MARKERS = (
    "beta",
    "carry over",
    "save transfer",
    "save file",
    "progress carry",
    "beta reward",
    "beta bonus",
    "flayed harbinger",
    "prologue skip",
    "skip prologue",
)

QUESTION_CUES = (
    "does ",
    "do i",
    "will i",
    "will we",
    "can i",
    "how do",
    "how to",
    "what happens",
    "lose",
    "keep",
    "transfer",
    "carry",
    "reset",
    "unlock",
    "claim",
    "uninstall",
    "100%",
    "100 percent",
)


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def log(msg: str) -> None:
    print(msg, flush=True)


def strip_html(raw: str) -> str:
    text = re.sub(r"(?is)<script.*?>.*?</script>", " ", raw)
    text = re.sub(r"(?is)<style.*?>.*?</style>", " ", text)
    text = re.sub(r"(?is)<br\s*/?>", "\n", text)
    text = re.sub(r"(?is)</p>", "\n", text)
    text = re.sub(r"(?is)<[^>]+>", " ", text)
    text = html_lib.unescape(text)
    text = re.sub(r"&#\d+;", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def norm(text: str) -> str:
    text = text.lower()
    text = text.replace("mortal shell ii", "mortal shell 2")
    text = re.sub(r"[^\w%+\s]", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def tokens(text: str) -> set[str]:
    stop = {
        "the", "a", "an", "and", "or", "to", "of", "in", "on", "for", "is", "it",
        "do", "does", "will", "be", "at", "if", "my", "your", "our", "with",
        "this", "that", "from", "are", "we", "you", "me", "i",
    }
    return {t for t in norm(text).split() if len(t) > 1 and t not in stop}


def jaccard(a: str, b: str) -> float:
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def game_aliases(game: str) -> list[str]:
    key = norm(game)
    aliases = list(GAME_ALIASES.get(key, [key]))
    if key not in aliases:
        aliases.append(key)
    if " 2" in key:
        aliases.append(key.replace(" 2", " ii"))
    if " ii" in key:
        aliases.append(key.replace(" ii", " 2"))
    # unique, keep order
    seen: set[str] = set()
    out: list[str] = []
    for a in aliases:
        if a not in seen:
            seen.add(a)
            out.append(a)
    return out


def is_mortal_shell_ii(game: str) -> bool:
    key = norm(game)
    return key in {"mortal shell 2", "mortal shell ii", "mortalshell2"}


def is_page_optimization_research(research_type: str = "") -> bool:
    active = str(research_type or _ACTIVE_RESEARCH_TYPE or "").strip().upper()
    return active == ACTION_RESEARCH_PAGE_OPTIMIZATION


def is_legacy_beta_research(
    game: str,
    topic: str = "",
    source_query: str = "",
    related_queries: list[str] | None = None,
    existing_page: str = "",
) -> bool:
    if not is_mortal_shell_ii(game):
        return False
    blob = norm(
        " ".join(
            [
                str(topic or ""),
                str(source_query or ""),
                " ".join(str(q or "") for q in related_queries or []),
                str(existing_page or ""),
            ]
        )
    )
    return any(marker in blob for marker in LEGACY_BETA_INTENT_MARKERS)


def has_unbound_legacy_beta_signal(
    text: str,
    topic: str,
    source_query: str = "",
    related_queries: list[str] | None = None,
) -> bool:
    """Reject beta-only evidence unless the current request explicitly asks for it."""
    evidence_text = norm(text)
    request_text = norm(
        " ".join(
            [
                str(topic or ""),
                str(source_query or ""),
                " ".join(str(q or "") for q in related_queries or []),
            ]
        )
    )
    return any(marker in evidence_text and marker not in request_text for marker in LEGACY_BETA_INTENT_MARKERS)


def resolve_steam_appids(game: str, override: list[str] | None = None) -> list[str]:
    """Return confirmed Steam App IDs only. Empty means Steam must be skipped."""
    if override:
        return [str(a).strip() for a in override if str(a).strip()]
    return list(STEAM_APP_IDS.get(norm(game), []))


def _unique_terms(items: list[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        t = norm(item)
        if not t or t in seen:
            continue
        seen.add(t)
        out.append(t)
    return tuple(out)


def request_topic_terms(
    game: str,
    topic: str,
    source_query: str = "",
    related_queries: list[str] | None = None,
) -> tuple[str, ...]:
    """Topic / query terms bound to this research request (no other-game defaults)."""
    parts: list[str] = []
    for piece in re.split(r"[/,]| or ", topic or ""):
        piece = piece.strip()
        if piece:
            parts.append(piece)
    if source_query and str(source_query).strip():
        parts.append(str(source_query).strip())
    for q in related_queries or []:
        if str(q).strip():
            parts.append(str(q).strip())
    # Light platform expansion only when the request itself is about platforms.
    blob = norm(" ".join(parts))
    if any(x in blob for x in ("ps5", "playstation", "console", "xbox")):
        parts.extend(
            ["ps5", "playstation", "playstation 5", "console", "xbox", "xbox series"]
        )
    return _unique_terms(parts)


def core_terms_for(
    game: str,
    topic: str,
    source_query: str = "",
    related_queries: list[str] | None = None,
) -> tuple[str, ...]:
    terms = list(
        request_topic_terms(game, topic, source_query, related_queries)
    )
    if is_mortal_shell_ii(game) and not is_page_optimization_research():
        terms.extend(MS2_CORE_TERMS)
    return _unique_terms(terms)


def request_topic_overlap(
    text: str,
    game: str,
    topic: str,
    source_query: str = "",
    related_queries: list[str] | None = None,
) -> bool:
    """Deterministic token binding for Action Research evidence."""
    request_terms = request_topic_terms(game, topic, source_query, related_queries)
    if not request_terms:
        return False
    game_tokens: set[str] = set()
    for alias in game_aliases(game):
        game_tokens.update(tokens(alias))
    text_tokens = tokens(text)
    for term in request_terms:
        term_tokens = tokens(term) - game_tokens
        if not term_tokens:
            continue
        # One distinctive query/entity token is enough for a short intent such
        # as "difficulty"; phrases still match exactly through has_core_topic.
        if text_tokens & term_tokens:
            return True
    return False


def topic_rules_for(game: str, topic: str) -> list[tuple[str, tuple[str, ...]]]:
    if is_page_optimization_research():
        topic_terms = request_topic_terms(game, topic)
        return [("topic_match", topic_terms)] if topic_terms else []
    if is_mortal_shell_ii(game) and not is_page_optimization_research():
        return list(MS2_TOPIC_RULES)
    rules: list[tuple[str, tuple[str, ...]]] = []
    topic_terms = request_topic_terms(game, topic)
    if topic_terms:
        rules.append(("topic_match", topic_terms))
    blob = norm(topic)
    if any(x in blob for x in ("ps5", "playstation", "console", "xbox")):
        rules.append(
            (
                "cross_platform",
                ("ps5", "xbox", "playstation", "console", "cross platform", "cross-platform"),
            )
        )
    return rules


def page_related_topics_for(game: str) -> set[str]:
    if is_page_optimization_research():
        return {"topic_match", "cross_platform"}
    if is_mortal_shell_ii(game) and not is_page_optimization_research():
        return set(MS2_PAGE_RELATED_TOPICS)
    return {"topic_match", "cross_platform"}


def mentions_game(text: str, game: str) -> bool:
    n = norm(text)
    return any(alias in n for alias in game_aliases(game))


def is_cross_game_contamination(text: str, url: str, game: str) -> bool:
    """True when evidence clearly belongs to another known game."""
    blob = norm(f"{text} {url}")
    raw = f"{text} {url}".lower()
    current_norm = {norm(a) for a in game_aliases(game)}

    app_m = re.search(
        r"(?:steamcommunity\.com|store\.steampowered\.com)/app/(\d+)", raw
    )
    if app_m:
        owner = STEAM_APP_OWNERS.get(app_m.group(1))
        if owner and norm(owner) not in current_norm:
            return True

    for owner_key, markers in GAME_DISTINCTIVE_MARKERS.items():
        if owner_key in current_norm:
            continue
        if any(marker in blob or marker in raw for marker in markers):
            return True
    return False


def has_core_topic(
    text: str,
    game: str = "",
    topic: str = "",
    source_query: str = "",
    related_queries: list[str] | None = None,
) -> bool:
    n = norm(text)
    terms = core_terms_for(game, topic, source_query, related_queries)
    if not terms:
        return False
    if any(term in n for term in terms):
        return True
    return is_page_optimization_research() and request_topic_overlap(
        text, game, topic, source_query, related_queries
    )


def is_store_boilerplate(text: str, game: str = "") -> bool:
    if not is_mortal_shell_ii(game) or is_page_optimization_research():
        return False
    n = norm(text)
    return any(p in n for p in MS2_STORE_BOILERPLATE)


def is_player_voice(title: str, excerpt: str) -> bool:
    blob = f"{title} {excerpt}".strip()
    n = norm(blob)
    first_person = any(
        w in n
        for w in (
            "will i",
            "do i",
            "can i",
            "do we",
            "will we",
            "i lose",
            "we lose",
            "i uninstall",
            "bummed",
            "why not",
            "does your save",
            "does demo",
            "progress stay",
        )
    )
    if first_person:
        return True
    if "?" in blob and any(
        w in n for w in ("does ", "do i", "will i", "can i", "how do", "how to", "should i")
    ):
        return True
    return False


def is_on_topic_question(
    text: str,
    game: str = "",
    topic: str = "",
    source_query: str = "",
    related_queries: list[str] | None = None,
) -> bool:
    if has_core_topic(text, game, topic, source_query, related_queries):
        return True
    n = norm(text)
    if is_mortal_shell_ii(game) and not is_page_optimization_research():
        if "save" in n and "progress" in n:
            return True
        if "skip" in n and "prologue" in n:
            return True
        if "reward" in n and any(w in n for w in ("launch", "beta", "uninstall")):
            return True
        return False
    # Non-MS2: require overlap with this request's topic terms.
    req = request_topic_terms(game, topic, source_query, related_queries)
    return bool(req) and (
        any(term in n for term in req)
        or (
            is_page_optimization_research()
            and request_topic_overlap(text, game, topic, source_query, related_queries)
        )
    )


def is_direct_topic_answer(
    text: str,
    game: str = "",
    topic: str = "",
    source_query: str = "",
    related_queries: list[str] | None = None,
) -> bool:
    n = norm(text)
    if is_mortal_shell_ii(game) and not is_page_optimization_research():
        return any(p in n for p in MS2_DIRECT_ANSWER_PHRASES)
    req = request_topic_terms(game, topic, source_query, related_queries)
    return bool(req) and (
        any(term in n for term in req)
        or (
            is_page_optimization_research()
            and request_topic_overlap(text, game, topic, source_query, related_queries)
        )
    )


FILTER_STATS: dict[str, dict[str, int]] = {}
FILTER_EXAMPLES: list[dict[str, str]] = []


def reset_filter_log() -> None:
    FILTER_STATS.clear()
    FILTER_EXAMPLES.clear()


def record_filter(source: str, reason: str, excerpt: str, title: str = "") -> None:
    FILTER_STATS.setdefault(source, {})
    FILTER_STATS[source][reason] = FILTER_STATS[source].get(reason, 0) + 1
    if len(FILTER_EXAMPLES) < 16:
        FILTER_EXAMPLES.append(
            {
                "source": source,
                "reason": reason,
                "title": title[:160],
                "excerpt": re.sub(r"\s+", " ", excerpt).strip()[:220],
            }
        )


def new_source_bucket() -> dict[str, Any]:
    return {"raw": 0, "valid": 0, "filtered": {}, "seen": set()}


def candidate_key(source: str, url: str, excerpt: str) -> str:
    return f"{source}|{url}|{norm(excerpt)[:160]}"


def count_source_item(bucket: dict[str, Any], key: str, reason: str | None) -> None:
    """Each unique candidate is counted once: valid, or one filtered reason."""
    if key in bucket["seen"]:
        return
    bucket["seen"].add(key)
    bucket["raw"] += 1
    if reason is None:
        bucket["valid"] += 1
    else:
        bucket["filtered"][reason] = bucket["filtered"].get(reason, 0) + 1


def finalize_source_counts(bucket: dict[str, Any]) -> dict[str, Any]:
    filtered = dict(bucket["filtered"])
    return {
        "raw": bucket["raw"],
        "valid": bucket["valid"],
        "filtered": filtered,
        "filtered_total": sum(filtered.values()),
    }


def pack_source_counts(counts: dict[str, Any]) -> dict[str, Any]:
    filtered = dict(counts.get("filtered") or {})
    filtered_total = counts.get("filtered_total")
    if filtered_total is None:
        filtered_total = sum(filtered.values())
    return {
        "raw_candidates": int(counts.get("raw") or 0),
        "valid_evidence": int(counts.get("valid") or 0),
        "filtered": filtered,
        "filtered_total": int(filtered_total),
    }


def classify_invalid_evidence(
    excerpt: str,
    game: str = "",
    topic: str = "",
    source_query: str = "",
    related_queries: list[str] | None = None,
) -> str | None:
    """Body-only validity. Title relevance is not enough."""
    body = re.sub(r"\s+", " ", excerpt or "").strip()
    n = norm(body)
    if re.match(r"originally posted by\b", n):
        return "quote_shell"
    if len(body) < 28 and "?" not in body:
        return "low_info"
    if any(p in n for p in ("ok thanks", "pretty bummed", "hope so")):
        if not is_direct_topic_answer(
            body, game, topic, source_query, related_queries
        ) and not is_on_topic_question(
            body, game, topic, source_query, related_queries
        ):
            return "low_info"
    if any(p in n for p in ("linux", "proton", "compatdata")):
        return "adjacent_tech"
    if "cloud save" in n and not any(
        p in n for p in ("full game", "at launch", "carry over", "full release")
    ):
        if is_mortal_shell_ii(game) and not is_page_optimization_research():
            return "adjacent_tech"
    if any(p in n for p in ("fast travel", "beacon to beacon")):
        return "off_topic_chat"
    if any(
        p in n
        for p in (
            "i loved the beta",
            "beta is awesome",
            "keep it up",
            "lot of stuff to do",
            "i thought the game ends",
        )
    ) and not is_on_topic_question(
        body, game, topic, source_query, related_queries
    ) and not is_direct_topic_answer(
        body, game, topic, source_query, related_queries
    ):
        return "off_topic_chat"
    if not (
        has_core_topic(body, game, topic, source_query, related_queries)
        or is_on_topic_question(body, game, topic, source_query, related_queries)
        or is_direct_topic_answer(body, game, topic, source_query, related_queries)
    ):
        return "title_only"
    return None


def reddit_subreddit(url: str) -> str:
    m = re.search(r"reddit\.com/r/([^/]+)/", url)
    return (m.group(1) if m else "").lower()


def is_noise(
    text: str,
    game: str,
    url: str = "",
    topic: str = "",
    source_query: str = "",
    related_queries: list[str] | None = None,
) -> bool:
    n = norm(text)
    if "mortal kombat" in n and "mortal shell" not in n:
        return True
    if any(bad in n for bad in NOISE_ALIASES) and "mortal shell" not in n:
        return True
    sub = reddit_subreddit(url)
    if sub in REDDIT_SKIP_SUBS:
        return True
    if "ama with" in n and not has_core_topic(
        text, game, topic, source_query, related_queries
    ):
        return True
    if not mentions_game(text, game):
        # Steam forum pages are only treated as in-game when App ID was confirmed
        # for this game (caller sets source_scoped). Without a game mention they
        # are still noise for cross-posted / wrong-app text.
        return True
    return False


def topic_terms(
    topic: str,
    game: str = "",
    source_query: str = "",
    related_queries: list[str] | None = None,
) -> list[str]:
    return list(request_topic_terms(game, topic, source_query, related_queries))


def discover_topic(
    text: str,
    game: str = "",
    topic: str = "",
) -> str:
    n = norm(text)
    for name, kws in topic_rules_for(game, topic):
        if any(kw in n for kw in kws):
            return name
    if is_mortal_shell_ii(game) and not is_page_optimization_research():
        if "save" in n or "progress" in n or "carry" in n:
            return "full_save_transfer"
        if "reward" in n or "unlock" in n:
            return "beta_rewards"
    return "other"


def relevance_score(
    text: str,
    game: str,
    topic: str,
    source_scoped: bool = False,
    source_query: str = "",
    related_queries: list[str] | None = None,
) -> float:
    if not (
        has_core_topic(text, game, topic, source_query, related_queries)
        or is_on_topic_question(text, game, topic, source_query, related_queries)
        or is_direct_topic_answer(text, game, topic, source_query, related_queries)
    ):
        return 0.0
    n = norm(text)
    score = 0.0
    if mentions_game(text, game) or source_scoped:
        score += 0.35
    else:
        return 0.0
    terms = core_terms_for(game, topic, source_query, related_queries)
    hits = sum(1 for term in terms if term in n)
    score += min(0.45, hits * 0.09)
    if is_player_voice("", text):
        score += 0.15
    if is_store_boilerplate(text, game) and not is_player_voice("", text):
        score -= 0.05
    if any(bad in n for bad in ("performance", "fps", "frame gen", "steam machine")):
        score -= 0.2
    return max(0.0, min(1.0, round(score, 3)))


def clean_question_sentence(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip(" .")
    text = re.sub(
        r"^(also|so|but|and|wait|hey|ok|okay)[,:\s]+",
        "",
        text,
        flags=re.I,
    )
    text = re.sub(r"^i have a small question\.?\s*", "", text, flags=re.I)
    text = re.sub(r"^originally posted by[^:]+:\s*", "", text, flags=re.I)
    return text.strip()


def extract_question_sentences(
    body: str,
    game: str = "",
) -> list[str]:
    found: list[str] = []
    for piece in re.findall(r"[^.!?\n][^.!?\n]{8,}\?", body or ""):
        piece = clean_question_sentence(piece)
        if not (12 <= len(piece) <= 240):
            continue
        if is_store_boilerplate(piece, game):
            continue
        if piece.lower().startswith("originally posted"):
            continue
        found.append(piece if piece.endswith("?") else piece + "?")
    return found


def pick_best_question(questions: list[str]) -> str:
    def score(q: str) -> int:
        n = norm(q)
        pts = 0
        for term, w in (
            ("100 percent", 6),
            ("100%", 6),
            ("uninstall", 5),
            ("cloud save", 5),
            ("30 minute", 5),
            ("skip", 4),
            ("prologue", 3),
            ("save file", 4),
            ("carry over", 4),
            ("reward", 3),
            ("platform", 3),
            ("steam", 2),
            ("console", 2),
        ):
            if term in n:
                pts += w
        return pts

    return max(questions, key=score)


def compact_player_question(question: str, game: str = "") -> str:
    n = norm(question)
    game_label = game.strip() or "Game"
    if is_page_optimization_research():
        cleaned = clean_question_sentence(question)
        if cleaned and not cleaned.endswith("?"):
            cleaned += "?"
        return cleaned[:160]
    if re.search(r"100\s*%|100 percent|completion of the beta", n) and any(
        w in n for w in ("reward", "get anything", "bonus", "unlock", "at launch")
    ):
        return "Beta 100% completion 有额外 launch reward 吗？"
    if "uninstall" in n and any(
        w in n for w in ("reward", "unlock", "collect", "claim", "keep", "earn")
    ):
        return "卸载 beta 后 launch reward 还会在吗？"
    if "cloud save" in n:
        return f"{game_label} beta 有 cloud save 吗？"
    if any(w in n for w in ("skip the prologue", "skip prologue", "skipping the prologue")) and any(
        w in n for w in ("miss", "collect", "gloom", "coin", "item", "still play")
    ):
        return "Skip prologue 会错过序章物品或资源吗？"
    if any(w in n for w in ("30 minute", "30 min", "play for at least")):
        return "Beta 要玩 30 分钟才能拿 launch reward 吗？"
    if any(w in n for w in ("ps5", "xbox", "console", "platform", "on steam", "pc")) and any(
        w in n for w in ("reward", "skin", "harbinger", "claim", "cosmetic")
    ):
        return "Steam / 主机之间的 beta reward 能互通吗？"
    if any(
        w in n
        for w in (
            "carry over",
            "carry on",
            "save transfer",
            "save file",
            "save data",
            "progress transfer",
            "full reset",
            "start over",
        )
    ):
        return "Beta 存档/进度会带到正式版吗？"
    if "flayed" in n or ("cosmetic" in n and "launch" in n) or "beta reward" in n:
        return "正式版能拿到哪些 beta reward？"
    cleaned = clean_question_sentence(question)
    if cleaned and not cleaned.endswith("?"):
        cleaned += "?"
    return cleaned[:160]


def extract_question(
    title: str,
    excerpt: str,
    game: str = "",
    topic: str = "",
    source_query: str = "",
    related_queries: list[str] | None = None,
) -> str:
    """Extract a reviewable player question from evidence body only.

    Source titles are never spliced in. Unrelated questions are dropped.
    """
    del title  # kept in signature for call sites; do not use
    body = excerpt or ""
    questions = extract_question_sentences(body, game)
    on_topic = [
        q
        for q in questions
        if is_on_topic_question(q, game, topic, source_query, related_queries)
    ]
    if on_topic:
        return compact_player_question(pick_best_question(on_topic), game)
    if questions:
        return ""
    if is_player_voice("", body) and has_core_topic(
        body, game, topic, source_query, related_queries
    ):
        return compact_player_question(body[:240], game)
    return ""


def http_get(
    url: str,
    headers: dict[str, str] | None = None,
    timeout: int = 30,
    retries: int = 3,
) -> tuple[bytes, str]:
    h = {
        "User-Agent": UA,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if headers:
        h.update(headers)
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as resp:
                return resp.read(), resp.geturl()
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code in {429, 500, 502, 503, 504} and attempt < retries:
                wait = 8 if e.code == 429 else 1.5
                time.sleep(wait * (attempt + 1))
                continue
            raise
        except Exception as e:
            last_err = e
            if attempt < retries:
                time.sleep(1.2 * (attempt + 1))
                continue
            raise
    raise last_err or RuntimeError(url)


def http_post_json(url: str, payload: dict[str, Any], timeout: int = 30) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"User-Agent": UA, "Content-Type": "application/json", "Accept": "*/*"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as resp:
        return json.loads(resp.read().decode("utf-8"))


def freshness_signal_indicates_update(signal: str) -> bool:
    n = norm(signal)
    return bool(n) and any(marker in n for marker in FRESHNESS_UPDATE_MARKERS)


def _date_tuple(year: int, month: int, day: int) -> tuple[int, int, int] | None:
    try:
        datetime(year, month, day)
    except ValueError:
        return None
    return year, month, day


def _date_tuple_from_value(value: Any, default_year: int | None = None) -> tuple[int, int, int] | None:
    if isinstance(value, (int, float)) and value > 0:
        dt = datetime.fromtimestamp(value, tz=timezone.utc)
        return dt.year, dt.month, dt.day
    text = str(value or "").strip()
    iso = re.search(r"\b(20\d{2})[-/](\d{1,2})[-/](\d{1,2})\b", text)
    if iso:
        return _date_tuple(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
    month = re.search(
        r"\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
        r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|"
        r"Nov(?:ember)?|Dec(?:ember)?)\.?\s+(\d{1,2})(?:,?\s+(20\d{2}))?\b",
        text,
        re.IGNORECASE,
    )
    if not month or default_year is None and not month.group(3):
        return None
    months = {
        "jan": 1, "january": 1, "feb": 2, "february": 2,
        "mar": 3, "march": 3, "apr": 4, "april": 4,
        "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
        "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
        "oct": 10, "october": 10, "nov": 11, "november": 11,
        "dec": 12, "december": 12,
    }
    year = int(month.group(3) or default_year)
    return _date_tuple(year, months[month.group(1).lower().rstrip(".")], int(month.group(2)))


def _latest_reference_date(text: str, default_year: int | None = None) -> tuple[int, int, int] | None:
    dates = []
    for match in re.finditer(r"\b20\d{2}[-/]\d{1,2}[-/]\d{1,2}\b", text or ""):
        parsed = _date_tuple_from_value(match.group(0), default_year)
        if parsed:
            dates.append(parsed)
    for match in re.finditer(
        r"\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
        r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|"
        r"Nov(?:ember)?|Dec(?:ember)?)\.?\s+\d{1,2}(?:,?\s+20\d{2})?\b",
        text or "",
        re.IGNORECASE,
    ):
        parsed = _date_tuple_from_value(match.group(0), default_year)
        if parsed:
            dates.append(parsed)
    return max(dates) if dates else None


def _official_news_url(url: str, appid: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc.lower().split(":", 1)[0]
    path = parsed.path.rstrip("/")
    return (
        host.endswith("steamcommunity.com")
        and (
            f"/app/{appid}/news/" in path
            or f"/games/{appid}/announcements/" in path
        )
    ) or (
        host.endswith("steampowered.com")
        and f"/news/app/{appid}" in path
    )


def _official_news_text(value: Any) -> str:
    text = strip_html(str(value or ""))
    return re.sub(r"\[[^\]]+\]", " ", text).strip()


def _freshness_news_matches(text: str, topic: str, signal: str) -> bool:
    n = norm(text)
    if not any(marker in n for marker in FRESHNESS_IMPACT_MARKERS):
        return False
    target_terms = {
        token for token in tokens(f"{topic} {signal}")
        if token not in FRESHNESS_GENERIC_TERMS and not token.isdigit()
    }
    return bool(target_terms & set(n.split()))


def collect_first_party_freshness(
    game: str,
    topic: str,
    signal: str,
    page_text: str,
    appids: list[str],
) -> dict[str, Any]:
    """Treat freshnessSignal as a lead; verify it against official Steam news."""
    base: dict[str, Any] = {
        "signal": signal,
        "status": "NOT_REQUESTED" if not signal else "UNVERIFIED",
        "evidence": [],
        "evidence_summary": "",
        "recommended_sections": [],
    }
    if not signal:
        return base
    if not freshness_signal_indicates_update(signal):
        base["evidence_summary"] = "Freshness signal unverified: it did not identify a patch, update, release, or changed fact."
        return base
    if not appids:
        base["evidence_summary"] = "Freshness signal unverified: no confirmed first-party Steam source is configured for this game."
        return base

    candidates: list[dict[str, Any]] = []
    for appid in appids:
        api_url = "https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/?" + urllib.parse.urlencode(
            {"appid": appid, "count": 20, "maxlength": 10000}
        )
        try:
            raw, _ = http_get(api_url, {"Accept": "application/json"})
            payload = json.loads(raw.decode("utf-8", "ignore"))
        except Exception as exc:
            log(f"  Steam official news failed ({appid}): {exc}")
            continue
        items = ((payload.get("appnews") or {}).get("newsitems") or []) if isinstance(payload, dict) else []
        for item in items:
            if not isinstance(item, dict):
                continue
            title = _official_news_text(item.get("title"))
            contents = _official_news_text(item.get("contents"))
            url = str(item.get("url") or "").strip()
            if not url or not _official_news_url(url, appid):
                continue
            text = f"{title} {contents}".strip()
            if not _freshness_news_matches(text, topic, signal):
                continue
            published = _date_tuple_from_value(item.get("date")) or _latest_reference_date(text)
            if not published:
                continue
            candidates.append(
                {
                    "title": title or "Steam official update",
                    "url": url,
                    "text": contents or title,
                    "published": published,
                }
            )

    if not candidates:
        base["evidence_summary"] = "Freshness signal unverified: no matching official Steam update was found."
        return base

    default_year = max(item["published"][0] for item in candidates)
    page_date = _latest_reference_date(page_text, default_year)
    newer = [item for item in candidates if page_date and item["published"] > page_date]
    if not newer:
        base["evidence_summary"] = "Freshness signal unverified: matching official evidence was found, but it could not be verified as newer than the current page."
        return base

    newer.sort(key=lambda item: item["published"], reverse=True)
    verified: list[dict[str, Any]] = []
    for item in newer:
        year, month, day = item["published"]
        date_text = f"{year:04d}-{month:02d}-{day:02d}"
        verified.append(
            {
                "source": "steam_official",
                "evidence_type": "first_party_freshness",
                "title": item["title"],
                "url": item["url"],
                "evidence": item["text"][:600],
                "excerpt": item["text"][:280],
                "published_at": date_text,
                "fact_date": date_text,
                "player_question": "",
                "discovered_topic": "official freshness",
                "relevance": 1.0,
            }
        )
    summary = "Verified first-party Steam update dated " + verified[0]["fact_date"] + ": " + verified[0]["evidence"]
    section = "Latest official update facts"
    if any(marker in norm(summary) for marker in ("crash", "fix", "optimisation", "optimization")):
        section = "Latest update / crash fixes and optimisations"
    return {
        "signal": signal,
        "status": "VERIFIED",
        "page_date": "-".join(str(part) for part in page_date),
        "evidence": verified,
        "evidence_summary": summary,
        "recommended_sections": [section],
        "decision_reason": (
            f"Existing page fact is stale ({page_date[0]:04d}-{page_date[1]:02d}-{page_date[2]:02d}); "
            f"verified newer official fact is available ({verified[0]['fact_date']})."
        ),
    }


def evidence_item(
    source: str,
    title: str,
    url: str,
    excerpt: str,
    game: str,
    topic: str,
    source_scoped: bool = False,
    match_on_excerpt: bool = False,
    source_query: str = "",
    related_queries: list[str] | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    excerpt = re.sub(r"\s+", " ", excerpt).strip()
    title = re.sub(r"\s+", " ", title).strip()
    if len(excerpt) < 12 and len(title) < 12:
        return None, "low_info"
    if is_cross_game_contamination(f"{title} {excerpt}", url, game):
        record_filter(source, "cross_game", excerpt, title)
        return None, "cross_game"
    if is_page_optimization_research() and has_unbound_legacy_beta_signal(
        excerpt, topic, source_query, related_queries
    ):
        record_filter(source, "topic_not_relevant", excerpt, title)
        return None, "topic_not_relevant"
    invalid = classify_invalid_evidence(
        excerpt, game, topic, source_query, related_queries
    )
    if invalid:
        record_filter(source, invalid, excerpt, title)
        return None, invalid
    blob = excerpt if match_on_excerpt else f"{title} {excerpt}"
    if is_noise(
        blob, game, url, topic, source_query, related_queries
    ) and not source_scoped:
        record_filter(source, "noise", excerpt, title)
        return None, "noise"
    if not has_core_topic(
        excerpt, game, topic, source_query, related_queries
    ) and not is_on_topic_question(
        excerpt, game, topic, source_query, related_queries
    ) and not is_direct_topic_answer(
        excerpt, game, topic, source_query, related_queries
    ):
        record_filter(source, "title_only", excerpt, title)
        return None, "title_only"
    # match_on_excerpt only controls scoring text — never implies game scope.
    rel = relevance_score(
        excerpt if (match_on_excerpt or source_scoped) else blob,
        game,
        topic,
        source_scoped=source_scoped,
        source_query=source_query,
        related_queries=related_queries,
    )
    if rel < 0.42:
        record_filter(source, "low_relevance", excerpt, title)
        return None, "low_relevance"
    discovered = discover_topic(excerpt, game, topic)
    question = extract_question(
        title, excerpt, game, topic, source_query, related_queries
    )
    return {
        "source": source,
        "title": title[:240],
        "url": url,
        "evidence": excerpt[:600],
        "excerpt": excerpt[:280],
        "player_question": question,
        "discovered_topic": discovered,
        "relevance": rel,
    }, None


def take_evidence(
    bucket: dict[str, Any],
    evidence: list[dict[str, Any]],
    source: str,
    title: str,
    url: str,
    excerpt: str,
    game: str,
    topic: str,
    source_scoped: bool = False,
    match_on_excerpt: bool = False,
    source_query: str = "",
    related_queries: list[str] | None = None,
) -> None:
    key = candidate_key(source, url, excerpt)
    if key in bucket["seen"]:
        return
    item, reason = evidence_item(
        source,
        title,
        url,
        excerpt,
        game,
        topic,
        source_scoped=source_scoped,
        match_on_excerpt=match_on_excerpt,
        source_query=source_query,
        related_queries=related_queries,
    )
    count_source_item(bucket, key, reason)
    if item:
        evidence.append(item)


# ---------------------------------------------------------------------------
# YouTube
# ---------------------------------------------------------------------------

def walk_find(obj: Any, key: str) -> list[Any]:
    found: list[Any] = []

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            if key in node:
                found.append(node[key])
            for v in node.values():
                _walk(v)
        elif isinstance(node, list):
            for v in node:
                _walk(v)

    _walk(obj)
    return found


def yt_text(node: Any) -> str:
    if not isinstance(node, dict):
        return ""
    if "simpleText" in node:
        return str(node["simpleText"])
    if "runs" in node:
        return "".join(str(r.get("text", "")) for r in node["runs"])
    return ""


def youtube_search(query: str) -> list[dict[str, str]]:
    data = http_post_json(
        "https://www.youtube.com/youtubei/v1/search?prettyPrint=false",
        {"context": {"client": YT_CLIENT}, "query": query},
    )
    videos: list[dict[str, str]] = []
    seen: set[str] = set()
    for renderer in walk_find(data, "videoRenderer"):
        if not isinstance(renderer, dict):
            continue
        vid = renderer.get("videoId")
        if not vid or vid in seen:
            continue
        title = yt_text(renderer.get("title") or {})
        desc = ""
        snippets = renderer.get("detailedMetadataSnippets") or []
        if snippets:
            desc = yt_text((snippets[0] or {}).get("snippetText") or {})
        elif renderer.get("descriptionSnippet"):
            desc = yt_text(renderer.get("descriptionSnippet") or {})
        seen.add(vid)
        videos.append(
            {
                "id": vid,
                "title": title,
                "desc": desc,
                "url": f"https://www.youtube.com/watch?v={vid}",
            }
        )
    return videos


def youtube_player(video_id: str) -> tuple[str, str]:
    data = http_post_json(
        "https://www.youtube.com/youtubei/v1/player?prettyPrint=false",
        {"context": {"client": YT_CLIENT}, "videoId": video_id},
    )
    details = data.get("videoDetails") or {}
    return str(details.get("title") or ""), str(details.get("shortDescription") or "")


def youtube_comments(video_id: str, limit: int = 20) -> list[str]:
    nxt = http_post_json(
        "https://www.youtube.com/youtubei/v1/next?prettyPrint=false",
        {"context": {"client": YT_CLIENT}, "videoId": video_id},
    )
    token = None
    for cmd in walk_find(nxt, "continuationCommand"):
        if not isinstance(cmd, dict):
            continue
        tok = cmd.get("token") or ""
        if tok.startswith("Eg0") and "comments-section" in tok:
            token = tok
            break
    if not token:
        for cmd in walk_find(nxt, "continuationCommand"):
            if isinstance(cmd, dict):
                tok = cmd.get("token") or ""
                if tok.startswith("Eg0") and len(tok) >= 70:
                    token = tok
                    break
    if not token:
        return []
    data = http_post_json(
        "https://www.youtube.com/youtubei/v1/next?prettyPrint=false",
        {"context": {"client": YT_CLIENT}, "continuation": token},
    )
    comments: list[str] = []
    mutations = (
        ((data.get("frameworkUpdates") or {}).get("entityBatchUpdate") or {}).get("mutations")
        or []
    )
    for mut in mutations:
        payload = (mut or {}).get("payload") or {}
        entity = payload.get("commentEntityPayload") or {}
        content = ((entity.get("properties") or {}).get("content") or {}).get("content")
        if content:
            comments.append(str(content).strip())
        if len(comments) >= limit:
            break
    return comments


YT_UNIQUE_CLAIMS = (
    "30 minute",
    "30 min",
    "same platform",
    "same account",
    "will not appear",
    "no unlock notification",
    "uninstall",
    "100 percent",
    "100%",
    "cloud save",
)


def youtube_desc_has_unique_claim(desc: str) -> bool:
    n = norm(desc)
    return any(term in n for term in YT_UNIQUE_CLAIMS)


def classify_youtube_desc_noise(title: str, desc: str, game: str = "") -> str | None:
    blob = f"{title} {desc}".lower()
    n = norm(blob)
    if any(
        marker in blob
        for marker in ("streamlabs.com", "leave a tip", "leave a tip here")
    ):
        return "livestream_intro"
    if "livestream" in blob or "live stream" in blob:
        if is_store_boilerplate(desc, game) or "grants access to the opening hours" in n:
            return "livestream_intro"
        if "welcome to mortal shell" in n:
            return "livestream_intro"
    hashtags = len(re.findall(r"#\w+", title + " " + desc))
    stuffing = desc.lower().count("mortal shell") >= 4 and desc.lower().count("open beta") >= 2
    seo_phrases = "easy unlock" in n and "exclusive" in n and ("free reward" in n or "claim" in n)
    if hashtags >= 4 or stuffing or seo_phrases:
        return "seo_description"
    if (
        (is_store_boilerplate(desc, game) or "grants access to the opening hours" in n)
        and not youtube_desc_has_unique_claim(desc)
        and not is_player_voice("", desc)
    ):
        return "official_copy_no_question"
    return None


def build_search_queries(
    game: str,
    topic: str,
    source_query: str = "",
    related_queries: list[str] | None = None,
) -> list[str]:
    queries: list[str] = [f"{game} {topic}".strip()]
    if source_query and str(source_query).strip():
        queries.append(str(source_query).strip())
        queries.append(f"{game} {source_query}".strip())
    for q in related_queries or []:
        qs = str(q).strip()
        if qs:
            queries.append(f"{game} {qs}")
    if is_mortal_shell_ii(game) and not is_page_optimization_research():
        queries.extend(
            [
                f"{game} beta save",
                f"{game} beta rewards",
                f"{game} progress carry over",
                f"{game} save transfer",
                f"{game} open beta bonuses",
            ]
        )
    # de-dupe preserve order
    seen: set[str] = set()
    out: list[str] = []
    for q in queries:
        key = norm(q)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(q)
    return out


def collect_youtube(
    game: str,
    topic: str,
    source_query: str = "",
    related_queries: list[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    queries = build_search_queries(game, topic, source_query, related_queries)
    seen_ids: set[str] = set()
    candidates: list[dict[str, str]] = []
    for q in queries:
        try:
            found = youtube_search(q)
        except Exception as e:
            log(f"  YouTube search failed ({q}): {e}")
            continue
        for v in found:
            if v["id"] in seen_ids:
                continue
            seen_ids.add(v["id"])
            candidates.append(v)
        time.sleep(0.4)

    evidence: list[dict[str, Any]] = []
    bucket = new_source_bucket()
    ranked: list[tuple[float, dict[str, str]]] = []
    for v in candidates:
        blob = f"{v['title']} {v['desc']}"
        ranked.append(
            (
                relevance_score(
                    blob,
                    game,
                    topic,
                    source_query=source_query,
                    related_queries=related_queries,
                ),
                v,
            )
        )
    ranked.sort(key=lambda x: x[0], reverse=True)

    opened = 0
    for score, v in ranked:
        if opened >= 10:
            break
        title, desc = v["title"], v["desc"]
        try:
            ptitle, pdesc = youtube_player(v["id"])
            title = ptitle or title
            desc = pdesc or desc
        except Exception:
            pass
        blob = f"{title} {desc}"
        if not has_core_topic(
            blob, game, topic, source_query, related_queries
        ) and score < 0.42:
            continue
        opened += 1
        noise = classify_youtube_desc_noise(title, desc, game)
        if noise:
            count_source_item(
                bucket,
                candidate_key("youtube", v["url"], desc or title),
                noise,
            )
            log(f"  YouTube desc filtered ({noise}): {title[:80]}")
        elif has_core_topic(blob, game, topic, source_query, related_queries):
            take_evidence(
                bucket,
                evidence,
                "youtube",
                title,
                v["url"],
                desc or title,
                game,
                topic,
                source_query=source_query,
                related_queries=related_queries,
            )
        try:
            comments = youtube_comments(v["id"])
        except Exception as e:
            log(f"  YouTube comments failed ({v['id']}): {e}")
            comments = []
        for c in comments:
            # Comments are not game-scoped; require game/topic binding.
            take_evidence(
                bucket,
                evidence,
                "youtube",
                title,
                v["url"],
                c,
                game,
                topic,
                match_on_excerpt=True,
                source_query=source_query,
                related_queries=related_queries,
            )
        time.sleep(0.35)
    yt_counts = finalize_source_counts(bucket)
    log(
        "  YouTube filtered: "
        + ", ".join(f"{k}={v}" for k, v in yt_counts["filtered"].items())
    )
    return evidence, yt_counts


# ---------------------------------------------------------------------------
# Reddit
# ---------------------------------------------------------------------------

ATOM = {"a": "http://www.w3.org/2005/Atom"}


def parse_atom(xml_bytes: bytes) -> list[dict[str, str]]:
    root = ET.fromstring(xml_bytes)
    entries = root.findall("a:entry", ATOM) or root.findall("entry")
    out: list[dict[str, str]] = []
    for e in entries:
        title = e.findtext("a:title", default="", namespaces=ATOM) or e.findtext("title") or ""
        link = ""
        for node in e.findall("a:link", ATOM) or e.findall("link"):
            href = node.get("href") or ""
            if href:
                link = href
                break
        content = (
            e.findtext("a:content", default="", namespaces=ATOM)
            or e.findtext("a:summary", default="", namespaces=ATOM)
            or e.findtext("content")
            or e.findtext("summary")
            or ""
        )
        out.append({"title": title.strip(), "url": link, "content": strip_html(content)})
    return out


def reddit_search_rss(query: str) -> list[dict[str, str]]:
    url = "https://www.reddit.com/search.rss?" + urllib.parse.urlencode(
        {"q": query, "sort": "relevance", "t": "year"}
    )
    data, _ = http_get(url, {"Accept": "application/atom+xml,application/xml;q=0.9"})
    return parse_atom(data)


def reddit_post_rss(permalink_url: str) -> list[dict[str, str]]:
    if not permalink_url.endswith("/"):
        permalink_url += "/"
    rss_url = permalink_url + ".rss"
    data, _ = http_get(rss_url, {"Accept": "application/atom+xml,application/xml;q=0.9"})
    return parse_atom(data)


def collect_reddit(
    game: str,
    topic: str,
    source_query: str = "",
    related_queries: list[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    queries = build_search_queries(game, topic, source_query, related_queries)
    if is_mortal_shell_ii(game) and not is_page_optimization_research():
        queries.extend(
            [
                "mortal shell 2 beta save OR rewards OR carry OR cloud",
                "mortal shell ii beta progress carry over",
            ]
        )
        # de-dupe again
        seen_q: set[str] = set()
        deduped: list[str] = []
        for q in queries:
            key = norm(q)
            if key in seen_q:
                continue
            seen_q.add(key)
            deduped.append(q)
        queries = deduped

    posts: dict[str, dict[str, str]] = {}
    for q in queries:
        try:
            found = reddit_search_rss(q)
        except Exception as e:
            log(f"  Reddit search failed ({q}): {e}")
            time.sleep(6)
            continue
        for p in found:
            url = p["url"].split("?")[0]
            if "/comments/" not in url:
                continue
            if reddit_subreddit(url) in REDDIT_SKIP_SUBS:
                continue
            posts[url] = p
        time.sleep(4)

    evidence: list[dict[str, Any]] = []
    bucket = new_source_bucket()
    ranked = sorted(
        posts.values(),
        key=lambda p: relevance_score(
            f"{p['title']} {p['content']}",
            game,
            topic,
            source_query=source_query,
            related_queries=related_queries,
        ),
        reverse=True,
    )
    kept = 0
    for post in ranked:
        blob = f"{post['title']} {post['content']}"
        if not has_core_topic(blob, game, topic, source_query, related_queries):
            continue
        if (
            relevance_score(
                blob,
                game,
                topic,
                source_query=source_query,
                related_queries=related_queries,
            )
            < 0.42
        ):
            continue
        take_evidence(
            bucket,
            evidence,
            "reddit",
            post["title"],
            post["url"],
            post["content"] or post["title"],
            game,
            topic,
            match_on_excerpt=True,
            source_query=source_query,
            related_queries=related_queries,
        )
        kept += 1
        if kept >= 8:
            break
        try:
            thread = reddit_post_rss(post["url"])
        except Exception as e:
            log(f"  Reddit thread failed ({post['url']}): {e}")
            thread = []
        for entry in thread[1:]:
            take_evidence(
                bucket,
                evidence,
                "reddit",
                post["title"],
                entry["url"] or post["url"],
                entry["content"] or entry["title"],
                game,
                topic,
                match_on_excerpt=True,
                source_query=source_query,
                related_queries=related_queries,
            )
        time.sleep(2.5)
    return evidence, finalize_source_counts(bucket)


# ---------------------------------------------------------------------------
# Steam Discussions
# ---------------------------------------------------------------------------

TOPIC_HREF_RE = re.compile(
    r'href="(https://steamcommunity.com/app/(\d+)/discussions/(\d+)/([0-9]+)/?)"'
)


def steam_topic_urls(html: str, appid: str) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for m in TOPIC_HREF_RE.finditer(html):
        url, found_app = m.group(1), m.group(2)
        if found_app != appid:
            continue
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def parse_steam_topic(html: str, url: str) -> tuple[str, str, list[str]]:
    title_m = re.search(r"<title>(.*?)</title>", html, re.I | re.S)
    title = strip_html(title_m.group(1) if title_m else "")
    title = title.split("::")[0].strip()
    desc_m = re.search(
        r'<meta name="Description" content="([^"]+)"', html, re.I
    )
    op = html_lib.unescape(desc_m.group(1)) if desc_m else ""
    op_m = re.search(
        r'class="forum_op"[^>]*>([\s\S]*?)<div class="forum_op_footer"', html
    )
    if op_m:
        topic_m = re.search(r'<div class="topic">\s*(.*?)\s*</div>', op_m.group(1), re.S)
        content_m = re.search(r'<div class="content">\s*([\s\S]*?)</div>', op_m.group(1))
        topic_t = strip_html(topic_m.group(1)) if topic_m else ""
        content_t = strip_html(content_m.group(1)) if content_m else ""
        combined = " ".join(x for x in (topic_t, content_t) if x)
        if combined:
            op = combined
            if not title:
                title = topic_t or title
    comments = [
        strip_html(c)
        for c in re.findall(
            r'class="commentthread_comment_text"[^>]*>(.*?)</div>', html, re.S
        )
    ]
    comments = [c for c in comments if len(c) >= 12]
    return title, op, comments


def collect_steam(
    game: str,
    topic: str,
    appids: list[str],
    source_query: str = "",
    related_queries: list[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    if not appids:
        log("  Steam skipped: no confirmed Steam App ID for this game")
        return [], {
            "raw": 0,
            "valid": 0,
            "filtered": {"no_steam_appid": 1},
            "filtered_total": 1,
        }

    headers = {"Cookie": STEAM_COOKIE, "Referer": "https://steamcommunity.com/"}
    topic_urls: list[str] = []
    seen: set[str] = set()
    search_queries = list(
        request_topic_terms(game, topic, source_query, related_queries)
    )[:6]
    if is_mortal_shell_ii(game) and not is_page_optimization_research():
        search_queries.extend(
            [
                "beta save carry reward",
                "progress carried",
                "uninstall beta unlock",
                "cloud save",
                "100% beta",
            ]
        )
    if not search_queries:
        search_queries = [topic or game]
    # unique
    sq_seen: set[str] = set()
    sq_out: list[str] = []
    for q in search_queries:
        key = norm(q)
        if not key or key in sq_seen:
            continue
        sq_seen.add(key)
        sq_out.append(q)
    search_queries = sq_out
    list_paths: list[str] = []

    for appid in appids:
        for q in search_queries:
            url = f"https://steamcommunity.com/app/{appid}/discussions/search/?" + urllib.parse.urlencode(
                {"q": q}
            )
            try:
                html, _ = http_get(url, headers)
            except Exception as e:
                log(f"  Steam search failed ({q}): {e}")
                continue
            found = steam_topic_urls(html.decode("utf-8", "ignore"), appid)
            for u in found:
                if u not in seen:
                    seen.add(u)
                    topic_urls.append(u)
            time.sleep(0.4)
        for path in list_paths:
            url = f"https://steamcommunity.com/app/{appid}/{path}"
            try:
                html, final = http_get(url, headers)
            except Exception as e:
                log(f"  Steam list failed ({url}): {e}")
                continue
            if "store.steampowered.com" in final:
                continue
            text = html.decode("utf-8", "ignore")
            found = steam_topic_urls(text, appid)
            for u in found:
                if u not in seen:
                    seen.add(u)
                    topic_urls.append(u)
            time.sleep(0.2)

    evidence: list[dict[str, Any]] = []
    bucket = new_source_bucket()
    for url in topic_urls[:12]:
        try:
            html, final = http_get(url, headers)
        except Exception as e:
            log(f"  Steam topic failed ({url}): {e}")
            continue
        if "/discussions/" not in final or final.rstrip("/").endswith("/discussions"):
            continue
        title, op, comments = parse_steam_topic(html.decode("utf-8", "ignore"), final)
        if not has_core_topic(
            f"{title} {op}", game, topic, source_query, related_queries
        ):
            continue
        take_evidence(
            bucket,
            evidence,
            "steam",
            title,
            final,
            op or title,
            game,
            topic,
            source_scoped=True,
            match_on_excerpt=True,
            source_query=source_query,
            related_queries=related_queries,
        )
        for c in comments:
            take_evidence(
                bucket,
                evidence,
                "steam",
                title,
                final,
                c,
                game,
                topic,
                source_scoped=True,
                match_on_excerpt=True,
                source_query=source_query,
                related_queries=related_queries,
            )
        time.sleep(0.25)
    return evidence, finalize_source_counts(bucket)


# ---------------------------------------------------------------------------
# Existing page + clustering
# ---------------------------------------------------------------------------

def fetch_existing_page(
    path: str,
    game: str = "",
    research_type: str = "",
    allow_beta_fallback: bool | None = None,
    return_status: bool = False,
) -> str | tuple[str, str]:
    # Only MS2 has a known live host. The beta fallback is legacy-only.
    if allow_beta_fallback is None:
        allow_beta_fallback = is_legacy_beta_research(game, existing_page=path)
    page_mode = is_page_optimization_research(research_type)
    if is_mortal_shell_ii(game) or "/mortal-shell-ii/" in (path or ""):
        url = "https://mortal-shell-ii.vercel.app" + (
            path if path.startswith("/") else "/" + path
        )
        try:
            data, _ = http_get(url)
            text = strip_html(data.decode("utf-8", "ignore"))
            if len(text) > 200:
                result = (text, "OK")
                return result if return_status else result[0]
        except Exception as e:
            log(f"  Live page fetch failed: {e}")
        if allow_beta_fallback and not page_mode:
            result = (MS2_EXISTING_PAGE_FALLBACK, "FALLBACK_BETA")
        else:
            result = ("", "FAILED")
        return result if return_status else result[0]
    log("  Existing page fallback skipped (no game-specific host configured)")
    result = ("", "FAILED")
    return result if return_status else result[0]


def page_covers(
    topic_name: str,
    page_text: str,
    query_text: str = "",
    research_type: str = "",
) -> bool:
    n = norm(page_text)
    if is_page_optimization_research(research_type):
        query = query_text or topic_name
        qn = norm(query)
        if not qn:
            return False
        if qn in n:
            return True
        qtokens = tokens(qn)
        page_tokens = tokens(n)
        meaningful = {t for t in qtokens if len(t) >= 3}
        if not meaningful:
            return False
        overlap = len(meaningful & page_tokens)
        return overlap >= max(1, min(2, len(meaningful)))
    checks = {
        "full_save_transfer": ("carry over", "save progress", "will not carry"),
        "inventory_reset": ("currency", "weapons", "shells", "collectibles", "reset"),
        "beta_rewards": ("flayed harbinger", "cosmetic"),
        "prologue_skip": ("marrow keep", "skip the prologue", "prologue"),
        "claim_process": ("claim", "at launch"),
        "uninstall_keep_rewards": ("uninstall",),
        "completion_bonus": ("100 percent", "100%", "completion"),
        "playtime_threshold": ("playtime threshold", "30 minutes"),
        "cloud_save": ("cloud save", "cloud saves"),
        "cross_platform": ("playstation", "xbox", "same account"),
        "other": (),
    }
    kws = checks.get(topic_name, ())
    if not kws:
        return False
    hits = sum(1 for k in kws if k in n)
    need = 1 if topic_name in {"uninstall_keep_rewards", "completion_bonus", "cloud_save", "cross_platform"} else min(2, len(kws))
    return hits >= need


def question_key(q: str) -> str:
    return re.sub(r"\s+", " ", norm(q))


def cluster_questions(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    pool = [item for item in items if str(item.get("player_question") or "").strip()]
    groups: list[dict[str, Any]] = []
    for item in pool:
        q = item["player_question"]
        qk = question_key(q)
        placed = False
        for g in groups:
            same_question = qk == question_key(g["question"])
            same_topic = item["discovered_topic"] == g["discovered_topic"]
            if same_question or (same_topic and jaccard(q, g["question"]) >= 0.4):
                g["count"] += 1
                g["urls"].append(item["url"])
                g["sources"].append(item["source"])
                g["examples"].append(item["excerpt"])
                if item["relevance"] > g["max_relevance"]:
                    g["max_relevance"] = item["relevance"]
                    g["question"] = q
                if g["discovered_topic"] == "other" and item["discovered_topic"] != "other":
                    g["discovered_topic"] = item["discovered_topic"]
                placed = True
                break
        if not placed:
            groups.append(
                {
                    "question": q,
                    "discovered_topic": item["discovered_topic"],
                    "count": 1,
                    "urls": [item["url"]],
                    "sources": [item["source"]],
                    "examples": [item["excerpt"]],
                    "max_relevance": item["relevance"],
                }
            )
    groups.sort(key=lambda g: (g["count"], g["max_relevance"]), reverse=True)
    out = []
    for g in groups:
        out.append(
            {
                "question": g["question"],
                "discovered_topic": g["discovered_topic"],
                "count": g["count"],
                "sources": sorted(set(g["sources"])),
                "example_urls": list(dict.fromkeys(g["urls"]))[:5],
                "example_excerpt": g["examples"][0][:240],
            }
        )
    return out


def question_on_page(
    question: str,
    topic_name: str,
    page_text: str,
    research_type: str = "",
) -> bool:
    if is_page_optimization_research(research_type):
        return page_covers(topic_name, page_text, question, research_type)
    n = norm(page_text)
    qn = norm(question)
    if "uninstall" in qn or topic_name == "uninstall_keep_rewards":
        return "uninstall" in n
    if "cloud save" in qn or topic_name == "cloud_save":
        return "cloud save" in n
    if "100" in qn or "completion of the beta" in qn:
        return "100" in n or "completion reward" in n
    if any(w in qn for w in ("gloom", "miss out", "misses out", "still play the prologue")):
        return "gloom" in n and "optional" in n
    if "30 minute" in qn or "30 min" in qn or topic_name == "playtime_threshold":
        return "30 minute" in n
    if "platform" in qn or "on steam" in qn or topic_name == "cross_platform":
        return "same platform" in n or "playstation" in n and "xbox" in n
    return page_covers(topic_name, page_text)


def recommend(
    clusters: list[dict[str, Any]],
    page_text: str,
    evidence_n: int,
    game: str = "",
    research_type: str = "",
    topic_relevant_evidence_n: int | None = None,
    freshness: dict[str, Any] | None = None,
) -> dict[str, Any]:
    page_mode = is_page_optimization_research(research_type)
    relevant_n = evidence_n if topic_relevant_evidence_n is None else topic_relevant_evidence_n
    freshness_verified = page_mode and isinstance(freshness, dict) and freshness.get("status") == "VERIFIED"
    freshness_unverified = page_mode and isinstance(freshness, dict) and freshness.get("status") == "UNVERIFIED"
    if freshness_verified:
        return {
            "action": "EXPAND_EXISTING",
            "reason": str(freshness.get("decision_reason") or "Verified newer first-party facts require updating the existing page."),
            "content_gaps": [
                {
                    "player_question": "What changed in the latest official update?",
                    "discovered_topic": "official freshness",
                    "evidence_count": len(freshness.get("evidence") or []),
                    "why_missing": "The existing page's latest-update fact is stale.",
                    "kind": "related",
                }
            ],
            "related_gap_evidence": len(freshness.get("evidence") or []),
            "orthogonal_gap_evidence": 0,
        }
    if page_mode and evidence_n >= 3 and relevant_n < 3:
        reason = "Insufficient topic-specific evidence"
        if freshness_unverified:
            reason += " Freshness signal unverified; no reliable newer first-party fact was confirmed."
        return {
            "action": "WATCH",
            "reason": reason,
            "content_gaps": [],
            "related_gap_evidence": 0,
            "orthogonal_gap_evidence": 0,
        }
    gaps: list[dict[str, Any]] = []
    related_gap_n = 0
    orthogonal_gap_n = 0
    related_topics = page_related_topics_for(game)
    for c in clusters:
        covered = question_on_page(
            c["question"], c["discovered_topic"], page_text, research_type
        )
        if covered:
            continue
        kind = "related" if c["discovered_topic"] in related_topics else "orthogonal"
        if kind == "related":
            related_gap_n += c["count"]
        else:
            orthogonal_gap_n += c["count"]
        gaps.append(
            {
                "player_question": c["question"],
                "discovered_topic": c["discovered_topic"],
                "evidence_count": c["count"],
                "why_missing": (
                    "Existing carry-over page does not answer this player question."
                    if kind == "related" and is_mortal_shell_ii(game) and not page_mode
                    else (
                        "Existing page does not answer this player question."
                        if kind == "related"
                        else "This question sits next to the topic but is not the same page intent."
                    )
                ),
                "kind": kind,
            }
        )

    if evidence_n < 3:
        action = "WATCH"
        reason = "Too few on-topic evidence items to justify a content change."
        if freshness_unverified:
            reason += " Freshness signal unverified; no reliable newer first-party fact was confirmed."
    elif related_gap_n >= 2:
        action = "EXPAND_EXISTING"
        if is_mortal_shell_ii(game) and not page_mode:
            reason = (
                "Players are asking carry-over / rewards follow-ups that belong on the "
                "existing beta progress page but are not currently covered."
            )
        else:
            reason = (
                "Players are asking follow-ups that belong on the existing page "
                "but are not currently covered."
            )
    elif orthogonal_gap_n >= 4 and related_gap_n == 0:
        action = "NEW_CONTENT"
        reason = "Dominant unanswered questions are a separate topic from the existing page."
    elif not gaps:
        action = "WATCH"
        reason = "Collected questions are already covered by the existing page."
    else:
        action = "EXPAND_EXISTING"
        reason = (
            "There are unanswered follow-ups adjacent to the existing carry-over page."
            if is_mortal_shell_ii(game) and not page_mode
            else "There are unanswered follow-ups adjacent to the existing page."
        )

    if freshness_unverified and "Freshness signal unverified" not in reason:
        reason += " Freshness signal unverified; no reliable newer first-party fact was confirmed."

    return {
        "action": action,
        "reason": reason,
        "content_gaps": gaps,
        "related_gap_evidence": related_gap_n,
        "orthogonal_gap_evidence": orthogonal_gap_n,
    }


def dedupe_evidence(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        key = (item["url"], norm(item["excerpt"])[:160])
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    out.sort(key=lambda x: x["relevance"], reverse=True)
    return out


def make_review_summary(
    *,
    recommendation: dict[str, Any],
    evidence: list[dict[str, Any]],
    most_asked: list[str],
) -> str:
    """Short human-review blurb from existing recommendation + questions."""
    action = str(recommendation.get("action") or "").strip()
    reason = str(recommendation.get("reason") or "").strip()
    parts: list[str] = []
    if action and reason:
        parts.append(f"{action}: {reason}")
    elif reason:
        parts.append(reason)
    elif action:
        parts.append(action)
    asked = [str(q).strip() for q in most_asked if str(q).strip()][:3]
    if asked:
        parts.append("Top questions — " + " | ".join(asked))
    parts.append(f"Evidence count: {len(evidence)}.")
    return " ".join(parts)


def filter_final_evidence(
    items: list[dict[str, Any]],
    game: str,
    topic: str = "",
    source_query: str = "",
    related_queries: list[str] | None = None,
    research_type: str = "",
) -> list[dict[str, Any]]:
    """Hard safety net: bind evidence to the game and, for action research, topic."""
    page_mode = bool(research_type and is_page_optimization_research(research_type))
    kept: list[dict[str, Any]] = []
    confirmed_appids = set(resolve_steam_appids(game))
    for item in items:
        title = str(item.get("title") or "")
        evidence = str(item.get("evidence") or "")
        excerpt = str(item.get("excerpt") or "")
        url = str(item.get("url") or "")
        blob = f"{title} {evidence} {excerpt}"
        if is_cross_game_contamination(blob, url, game):
            record_filter(
                str(item.get("source") or "unknown"),
                "cross_game",
                blob,
                title,
            )
            continue
        on_confirmed_steam = any(f"/app/{appid}/" in url for appid in confirmed_appids)
        if not mentions_game(blob, game) and not on_confirmed_steam:
            record_filter(
                str(item.get("source") or "unknown"),
                "wrong_game",
                blob,
                title,
            )
            continue
        if page_mode:
            if has_unbound_legacy_beta_signal(
                blob, topic, source_query, related_queries
            ):
                record_filter(
                    str(item.get("source") or "unknown"),
                    "topic_not_relevant",
                    blob,
                    title,
                )
                continue
            topic_bound = (
                has_core_topic(blob, game, topic, source_query, related_queries)
                or is_on_topic_question(
                    blob, game, topic, source_query, related_queries
                )
                or is_direct_topic_answer(
                    blob, game, topic, source_query, related_queries
                )
            )
            if not topic_bound:
                record_filter(
                    str(item.get("source") or "unknown"),
                    "topic_not_relevant",
                    blob,
                    title,
                )
                continue
            item["topic_relevance"] = True
        kept.append(item)
    return kept


def run(args: argparse.Namespace) -> dict[str, Any]:
    global _ACTIVE_RESEARCH_TYPE
    game = args.game
    topic = args.topic
    existing_page = args.existing_page
    research_type = str(getattr(args, "research_type", None) or "").strip().upper()
    _ACTIVE_RESEARCH_TYPE = research_type
    source_query = str(getattr(args, "source_query", None) or "").strip()
    related_raw = getattr(args, "related_queries", None) or []
    if isinstance(related_raw, str):
        related_queries = [q.strip() for q in related_raw.split(",") if q.strip()]
    else:
        related_queries = [str(q).strip() for q in related_raw if str(q).strip()]
    context_raw = getattr(args, "context_queries", None) or []
    context_queries = [str(q).strip() for q in context_raw if str(q).strip()]
    freshness_signal = (
        str(getattr(args, "freshness_signal", None) or "").strip()
        if is_page_optimization_research(research_type)
        else ""
    )
    for query in context_queries:
        if query not in related_queries:
            related_queries.append(query)

    appids = resolve_steam_appids(game, getattr(args, "steam_appid", None))

    log(f"Research Runner  game={game!r}  topic={topic!r}")
    log(f"Existing page    {existing_page}")
    if source_query:
        log(f"Source query     {source_query}")
    if appids:
        log(f"Steam appids     {appids}")
    else:
        log("Steam appids     (none — Steam source will be skipped)")
    reset_filter_log()

    if args.reuse:
        prev = json.loads(Path(args.reuse).read_text(encoding="utf-8"))
        evidence = filter_final_evidence(prev.get("evidence") or [], game)
        yt_counts = {
            "raw": prev.get("source_counts", {}).get("youtube", {}).get("raw_candidates", 0),
            "valid": prev.get("source_counts", {}).get("youtube", {}).get("valid_evidence", 0),
            "filtered": prev.get("source_counts", {}).get("youtube", {}).get("filtered") or {},
            "filtered_total": prev.get("source_counts", {}).get("youtube", {}).get("filtered_total"),
        }
        rd_counts = {
            "raw": prev.get("source_counts", {}).get("reddit", {}).get("raw_candidates", 0),
            "valid": prev.get("source_counts", {}).get("reddit", {}).get("valid_evidence", 0),
            "filtered": prev.get("source_counts", {}).get("reddit", {}).get("filtered") or {},
            "filtered_total": prev.get("source_counts", {}).get("reddit", {}).get("filtered_total"),
        }
        st_counts = {
            "raw": prev.get("source_counts", {}).get("steam", {}).get("raw_candidates", 0),
            "valid": prev.get("source_counts", {}).get("steam", {}).get("valid_evidence", 0),
            "filtered": prev.get("source_counts", {}).get("steam", {}).get("filtered") or {},
            "filtered_total": prev.get("source_counts", {}).get("steam", {}).get("filtered_total"),
        }
        log("Reusing stored evidence; skipping live collection")
    else:
        log("\n[1/4] YouTube")
        yt_items, yt_counts = collect_youtube(
            game, topic, source_query, related_queries
        )
        log(f"  raw={yt_counts['raw']} valid={yt_counts['valid']} filtered_total={yt_counts['filtered_total']}")

        log("\n[2/4] Reddit")
        rd_items, rd_counts = collect_reddit(
            game, topic, source_query, related_queries
        )
        log(f"  raw={rd_counts['raw']} valid={rd_counts['valid']} filtered_total={rd_counts['filtered_total']}")

        log("\n[3/4] Steam Discussions")
        st_items, st_counts = collect_steam(
            game, topic, appids, source_query, related_queries
        )
        log(f"  raw={st_counts['raw']} valid={st_counts['valid']} filtered_total={st_counts['filtered_total']}")
        evidence = dedupe_evidence(yt_items + rd_items + st_items)

    evidence = filter_final_evidence(evidence, game)
    candidate_evidence_n = len(evidence)
    if is_page_optimization_research(research_type):
        evidence = filter_final_evidence(
            evidence,
            game,
            topic,
            source_query,
            related_queries,
            research_type,
        )
    topic_relevant_evidence_n = len(evidence)

    log("\n[4/4] Cluster + recommend")
    allow_beta_fallback = is_legacy_beta_research(
        game, topic, source_query, related_queries, existing_page
    )
    page_text, page_fetch_status = fetch_existing_page(
        existing_page,
        game,
        research_type,
        allow_beta_fallback=allow_beta_fallback,
        return_status=True,
    )
    clusters = cluster_questions(evidence)
    freshness = collect_first_party_freshness(
        game,
        topic,
        freshness_signal,
        page_text,
        appids,
    ) if is_page_optimization_research(research_type) else None
    all_evidence = evidence + list((freshness or {}).get("evidence") or [])
    rec = recommend(
        clusters,
        page_text,
        len(evidence),
        game,
        research_type,
        topic_relevant_evidence_n=topic_relevant_evidence_n,
        freshness=freshness,
    )

    source_counts = {
        "youtube": pack_source_counts(yt_counts),
        "reddit": pack_source_counts(rd_counts),
        "steam": pack_source_counts(st_counts),
    }

    most_asked = [c["question"] for c in clusters[:8]]
    recommendation = {
        "action": rec["action"],
        "reason": rec["reason"],
    }
    result = {
        "input": {
            "game": game,
            "topic": topic,
            "existing_page": existing_page,
            "research_type": research_type,
            "context_queries": context_queries,
        },
        "run_at": now_iso(),
        "source_counts": source_counts,
        "evidence": all_evidence,
        "player_questions": clusters,
        "most_asked_questions": most_asked,
        "content_gaps": rec["content_gaps"],
        "recommendation": recommendation,
        "candidate_evidence_count": candidate_evidence_n,
        "community_evidence_count": len(evidence),
        "topic_relevant_evidence_count": topic_relevant_evidence_n,
        "page_fetch_status": page_fetch_status,
        "review_summary": make_review_summary(
            recommendation=recommendation,
            evidence=all_evidence,
            most_asked=most_asked,
        ),
        "filtered_examples": FILTER_EXAMPLES[:8],
    }
    if freshness is not None:
        result["input"]["freshness_signal"] = freshness_signal
        result["freshness"] = freshness
        result["evidence_summary"] = str(freshness.get("evidence_summary") or "")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="M1 Social Research Runner")
    parser.add_argument("--game", default="Mortal Shell II")
    parser.add_argument(
        "--topic",
        default="beta save / beta rewards / progress carry-over",
    )
    parser.add_argument(
        "--existing-page",
        default="/mortal-shell-ii/beta-progress-carry-over/",
    )
    parser.add_argument("--steam-appid", action="append", default=None)
    parser.add_argument(
        "--source-query",
        default="",
        help="Optional GSC source query for topic binding",
    )
    parser.add_argument(
        "--related-queries",
        default="",
        help="Optional comma-separated related queries",
    )
    parser.add_argument("--out", default=str(OUT_DEFAULT))
    parser.add_argument(
        "--reuse",
        default=None,
        help="Reuse evidence from an existing research_result.json and only re-cluster.",
    )
    args = parser.parse_args()

    result = run(args)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    counts = result["source_counts"]
    log("\n========== SUMMARY ==========")
    for name in ("youtube", "reddit", "steam"):
        block = counts[name]
        raw_n = block["raw_candidates"]
        valid_n = block["valid_evidence"]
        filtered_n = block.get("filtered_total", sum((block.get("filtered") or {}).values()))
        ok = "ok" if raw_n == valid_n + filtered_n else "MISMATCH"
        log(f"{name:7} raw={raw_n} valid={valid_n} filtered_total={filtered_n}  [{ok}]")
        reasons = block.get("filtered") or {}
        if reasons:
            log("        " + ", ".join(f"{k}={v}" for k, v in reasons.items()))
    log(f"Total   valid evidence : {len(result['evidence'])}")
    log("Player questions:")
    for q in result["player_questions"][:12]:
        log(f"  [{q['count']}] {q['question']}  ({q['discovered_topic']})")
    log(f"Recommendation: {result['recommendation']['action']}")
    log(f"Reason: {result['recommendation']['reason']}")
    log(f"Wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
