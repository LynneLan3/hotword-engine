#!/usr/bin/env python3
"""Player search-alias discovery: evidence ranking and status resolution.

Ports the validated rules from steam_hotword_monitor/PlayerAliasDiscovery.gs.
Does not fetch the public web — collectors live in player_alias_discovery_runner.
"""

from __future__ import annotations

import re
from typing import Any

STATUS_FOUND = "FOUND"
STATUS_NO_EVIDENCE = "NO_ALIAS_EVIDENCE"
STATUS_RETRIEVAL_FAILED = "RETRIEVAL_FAILED"
MIN_SOURCE_HITS = 2

ROMAN_WORDS = {
    "II": "2",
    "III": "3",
    "IV": "4",
    "V": "5",
    "VI": "6",
    "VII": "7",
    "VIII": "8",
    "IX": "9",
    "X": "10",
}

_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001F9FF"
    "\U00002700-\U000027BF"
    "\U0001F000-\U0001F02F"
    "\uFE0F"
    "]+",
    flags=re.UNICODE,
)


def clean_name(text: Any) -> str:
    value = str(text or "")
    value = re.sub(r"[™®©]", "", value)
    value = _EMOJI_RE.sub("", value)
    value = value.replace("'", "").replace("\u2019", "")
    return re.sub(r"\s+", " ", value).strip()


def normalize_key(text: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", clean_name(text).lower())


def core_tokens(name: Any) -> list[str]:
    cleaned = clean_name(name).lower()
    cleaned = re.sub(r"[^a-z0-9\s]", " ", cleaned)
    stop = {"the", "and", "for", "game", "steam"}
    return [tok for tok in cleaned.split() if len(tok) > 2 and tok not in stop]


def remove_subtitle(name: str) -> str:
    text = clean_name(name)
    idx = text.find(":")
    if idx <= 0:
        return text
    main = clean_name(text[:idx])
    sub = clean_name(text[idx + 1 :])
    return f"{main} {sub}".strip() if sub else main


def roman_variants(name: str) -> list[str]:
    out: set[str] = set()
    text = clean_name(name)
    for roman, digit in ROMAN_WORDS.items():
        if re.search(rf"\b{roman}\b", text, flags=re.I):
            out.add(re.sub(rf"\b{roman}\b", digit, text, flags=re.I))
        if re.search(rf"\b{digit}\b", text):
            out.add(re.sub(rf"\b{digit}\b", roman, text))
    return [re.sub(r"\s+", " ", item).strip() for item in out if item]


def generate_evidence_patterns(official_name: str) -> list[str]:
    patterns: set[str] = set()
    cleaned = clean_name(official_name)
    if cleaned:
        patterns.add(cleaned)
    no_sub = remove_subtitle(cleaned)
    if no_sub and no_sub != cleaned:
        patterns.add(no_sub)
    patterns.update(roman_variants(cleaned))
    colon_idx = cleaned.find(":")
    if colon_idx > 0:
        main = clean_name(cleaned[:colon_idx])
        sub = clean_name(cleaned[colon_idx + 1 :])
        if main and sub:
            joined = f"{main} {sub}"
            patterns.add(joined)
            patterns.update(roman_variants(joined))
            if len(main) >= 4:
                patterns.add(main)
            if len(sub) >= 8:
                patterns.add(sub)
    return [item for item in patterns if item and len(item) >= 3]


def extract_sequel_indicator(name: str) -> str:
    text = clean_name(name)
    roman = re.search(r"\b(I{1,3}|IV|VI{0,3}|IX|X|XI|XII)\b", text, flags=re.I)
    if roman:
        return roman.group(1).upper()
    arabic = re.search(r"\b(\d{1,2})\b", text)
    return arabic.group(1) if arabic else ""


def strip_sequel_indicator(name: str) -> str:
    text = clean_name(name)
    text = re.sub(r"\b(I{1,3}|IV|VI{0,3}|IX|X|XI|XII)\b", "", text, flags=re.I)
    text = re.sub(r"\b\d{1,2}\b", "", text)
    return re.sub(r"\s+", " ", text).strip()


def is_series_only_alias(alias: str, official_name: str) -> bool:
    if not extract_sequel_indicator(official_name):
        return False
    if extract_sequel_indicator(alias):
        return False
    return normalize_key(alias) == normalize_key(strip_sequel_indicator(official_name))


def contains_roman_numeral(text: str) -> bool:
    return bool(re.search(r"\b(I{1,3}|IV|VI{0,3}|IX|X|XI|XII)\b", clean_name(text), flags=re.I))


def contains_arabic_sequel(text: str) -> bool:
    return bool(re.search(r"\b\d{1,2}\b", clean_name(text)))


def normalize_numerals(text: str) -> str:
    out = clean_name(text)
    for roman, digit in ROMAN_WORDS.items():
        out = re.sub(rf"\b{roman}\b", digit, out, flags=re.I)
    return normalize_key(out)


def is_numeral_variant_only(alias: str, official_name: str) -> bool:
    if normalize_numerals(alias) != normalize_numerals(official_name):
        return False
    official_roman = contains_roman_numeral(official_name)
    alias_roman = contains_roman_numeral(alias)
    official_arabic = contains_arabic_sequel(official_name)
    alias_arabic = contains_arabic_sequel(alias)
    if official_roman and alias_arabic and not alias_roman:
        return False
    if official_arabic and alias_roman and not alias_arabic:
        return True
    return True


def is_weak_subtitle_only_alias(alias: str, official_name: str) -> bool:
    cleaned = clean_name(official_name)
    idx = cleaned.find(":")
    if idx <= 0:
        return False
    main = clean_name(cleaned[:idx])
    sub = clean_name(cleaned[idx + 1 :])
    if normalize_key(alias) != normalize_key(sub):
        return False
    main_words = [w for w in main.split() if w]
    return bool(main_words) and len(main_words) <= 2 and len(main) <= 20


def text_refers_to_same_game(alias: str, official_name: str, item: dict[str, Any]) -> bool:
    alias_key = normalize_key(alias)
    official_key = normalize_key(official_name)
    if not alias_key or alias_key == official_key:
        return False
    text_key = normalize_key(f"{item.get('title', '')} {item.get('snippet', '')}")
    if alias_key not in text_key:
        return False
    url = str(item.get("url") or "")
    steam_markers = ["steam", "appid", "game"]
    if "steampowered" in url or "steamcommunity" in url:
        steam_markers.append("steamstore")
    official_tokens = core_tokens(official_name)
    matched = [tok for tok in official_tokens if len(tok) > 2 and tok in text_key]
    if len(matched) >= min(2, len(official_tokens) or 1):
        return True
    return any(marker in text_key for marker in steam_markers if marker) and len(matched) >= 1


def shares_core_identity(alias: str, official_name: str) -> bool:
    alias_tokens = core_tokens(alias)
    official_tokens = core_tokens(official_name)
    if not alias_tokens or not official_tokens:
        return False
    shared = [tok for tok in alias_tokens if tok in official_tokens]
    return len(shared) >= min(2, len(official_tokens))


def extract_alternate_names(item: dict[str, Any], official_name: str) -> list[str]:
    text = clean_name(f"{item.get('title', '')} {item.get('snippet', '')}")
    if not text:
        return []
    out: set[str] = set()
    for match in re.finditer(r'["“]([^"”]{3,80})["”]', text):
        inner = clean_name(match.group(1))
        if inner:
            out.add(inner)
    out.update(roman_variants(clean_name(official_name)))
    out.update(generate_evidence_patterns(official_name))
    return list(out)


def rank_candidates(
    official_name: str,
    patterns: list[str],
    snippets: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    scores: dict[str, dict[str, Any]] = {}
    official_key = normalize_key(official_name)

    for pattern in patterns:
        if not pattern or normalize_key(pattern) == official_key:
            continue
        if is_series_only_alias(pattern, official_name):
            continue
        key = normalize_key(pattern)
        scores[key] = {
            "text": pattern,
            "hits": 0,
            "sources": set(),
            "examples": [],
        }

    for item in snippets:
        haystack = normalize_key(f"{item.get('title', '')} {item.get('snippet', '')}")
        if not haystack:
            continue
        for entry in list(scores.values()):
            needle = normalize_key(entry["text"])
            if not needle or len(needle) < 3:
                continue
            if needle not in haystack:
                continue
            if not text_refers_to_same_game(entry["text"], official_name, item):
                continue
            entry["hits"] += 1
            entry["sources"].add(str(item.get("source") or "unknown"))
            if len(entry["examples"]) < 3:
                entry["examples"].append(item)
        for name in extract_alternate_names(item, official_name):
            if is_series_only_alias(name, official_name):
                continue
            key = normalize_key(name)
            if not key or key == official_key:
                continue
            if not shares_core_identity(name, official_name):
                continue
            existing = scores.get(key)
            if existing:
                existing["hits"] += 1
                existing["sources"].add(str(item.get("source") or "unknown"))
            else:
                scores[key] = {
                    "text": name,
                    "hits": 1,
                    "sources": {str(item.get("source") or "unknown")},
                    "examples": [item],
                }

    ranked: list[dict[str, Any]] = []
    for entry in scores.values():
        sources = sorted(entry["sources"])
        if entry["hits"] <= 0 or not sources:
            continue
        ranked.append(
            {
                "text": entry["text"],
                "hits": entry["hits"],
                "sources": sources,
                "examples": entry["examples"],
            }
        )
    ranked.sort(key=lambda row: (-row["hits"], -len(row["sources"]), len(row["text"])))
    return ranked


def resolve_discovery_status(alias: str, source_diags: list[dict[str, Any]]) -> str:
    if alias:
        return STATUS_FOUND
    if not source_diags:
        return STATUS_RETRIEVAL_FAILED
    if not any(item.get("ok") for item in source_diags if isinstance(item, dict)):
        return STATUS_RETRIEVAL_FAILED
    return STATUS_NO_EVIDENCE


def confidence_for(status: str, alias: str) -> str:
    if alias and status == STATUS_FOUND:
        return "HIGH"
    if status == STATUS_RETRIEVAL_FAILED:
        return "UNKNOWN"
    return "LOW"


def discover_from_snippets(
    official_name: str,
    snippets: list[dict[str, Any]],
    source_diags: list[dict[str, Any]],
) -> dict[str, Any]:
    name = clean_name(official_name)
    if not name:
        return {
            "alias": "",
            "status": "EMPTY",
            "evidence": [],
            "patterns": [],
            "ranked": [],
            "source_diags": source_diags,
            "source_urls": [],
            "source_count": 0,
            "confidence": "UNKNOWN",
        }

    patterns = generate_evidence_patterns(name)
    ranked = [
        entry
        for entry in rank_candidates(name, patterns, snippets)
        if not is_numeral_variant_only(entry["text"], name)
        and not is_weak_subtitle_only_alias(entry["text"], name)
    ]
    best = ranked[0] if ranked else None
    alias = best["text"] if best and best["hits"] >= MIN_SOURCE_HITS else ""
    status = resolve_discovery_status(alias, source_diags)
    evidence = snippets[:40]
    source_urls = []
    seen_urls: set[str] = set()
    for item in evidence:
        url = str(item.get("url") or "").strip()
        if url and url not in seen_urls:
            seen_urls.add(url)
            source_urls.append(url)
    if alias and best:
        for item in best.get("examples") or []:
            url = str(item.get("url") or "").strip()
            if url and url not in seen_urls:
                seen_urls.add(url)
                source_urls.append(url)

    ok_sources = {
        str(item.get("source") or "")
        for item in source_diags
        if isinstance(item, dict) and item.get("ok") and str(item.get("source") or "").strip()
    }
    return {
        "alias": alias,
        "status": status,
        "evidence": evidence,
        "patterns": patterns,
        "ranked": ranked[:5],
        "source_diags": source_diags,
        "source_urls": source_urls[:20],
        "source_count": len(ok_sources),
        "confidence": confidence_for(status, alias),
    }
