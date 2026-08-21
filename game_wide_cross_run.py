"""Cross-run accumulation and intent-family normalization for GAME_WIDE."""

from __future__ import annotations

import urllib.parse
from pathlib import Path
from typing import Any

from game_wide_social_runner import intent_family as _infer_intent_family


def _normalize_url(url: str) -> str:
    """Return a stable URL identity without losing YouTube video identity."""
    raw = str(url or "").strip()
    parsed = urllib.parse.urlsplit(raw)
    host = parsed.netloc.lower().split("@")[-1].split(":")[0]

    if host in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
        video_id = urllib.parse.parse_qs(parsed.query).get("v", [""])[0]
        if video_id:
            return f"https://www.youtube.com/watch?v={video_id}"
    elif host in {"youtu.be", "www.youtu.be"}:
        video_id = parsed.path.strip("/").split("/", 1)[0]
        if video_id:
            return f"https://www.youtube.com/watch?v={video_id}"

    path = parsed.path.rstrip("/")
    return f"{parsed.scheme.lower()}://{host}{path}" if parsed.scheme else path


def _evidence_key(provider: str, url: str) -> str:
    """Evidence identity: provider plus canonical URL only."""
    return f"{str(provider or '').strip()}|{_normalize_url(url)}"


def _family_for_cluster(cluster: dict[str, Any], topic_key: str, topic: str) -> str:
    family = str(cluster.get("intent_family") or "").strip()
    if family and family != "OTHER":
        return family
    key_tuple = tuple(topic_key.split("-")) if topic_key else ()
    return _infer_intent_family(key_tuple, topic)


def accumulate_runs(artifacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Aggregate topic clusters across runs and dedupe their evidence."""
    clusters: dict[str, dict[str, Any]] = {}

    for run_idx, artifact in enumerate(artifacts, 1):
        for cluster in artifact.get("clusters", []):
            topic_key = str(cluster.get("topic_key") or cluster.get("topic") or "")
            topic = str(cluster.get("topic") or "")
            if topic_key not in clusters:
                clusters[topic_key] = {
                    "topic_key": topic_key,
                    "topic": topic,
                    "intent_family": _family_for_cluster(cluster, topic_key, topic),
                    "intent": cluster.get("intent", ""),
                    "runs": 0,
                    "raw_occurrences": 0,
                    "unique_evidence_count": 0,
                    "unique_urls": set(),
                    "unique_providers": set(),
                    "unique_source_families": set(),
                    "unique_authors": set(),
                    "seen_evidence_keys": set(),
                    "decisions": [],
                    "gsc_status": "",
                    "page_status": "",
                    "evidence_details": [],
                }

            accumulated = clusters[topic_key]
            accumulated["runs"] += 1
            accumulated["decisions"].append(cluster.get("decision", "?"))
            accumulated["gsc_status"] = (cluster.get("gsc_match") or {}).get("status", "NONE")
            accumulated["page_status"] = (cluster.get("existing_page_match") or {}).get("status", "NONE")

            for evidence in cluster.get("evidence", []):
                accumulated["raw_occurrences"] += 1
                provider = str(evidence.get("provider") or evidence.get("source") or "").strip()
                url = str(evidence.get("url") or "").strip()
                evidence_key = _evidence_key(provider, url)
                if evidence_key in accumulated["seen_evidence_keys"]:
                    continue

                accumulated["seen_evidence_keys"].add(evidence_key)
                accumulated["unique_evidence_count"] += 1
                canonical_url = _normalize_url(url)
                if canonical_url:
                    accumulated["unique_urls"].add(canonical_url)
                if provider:
                    accumulated["unique_providers"].add(provider)
                source_family = str(evidence.get("source_family") or "").strip()
                author = str(evidence.get("author") or "").strip()
                if source_family:
                    accumulated["unique_source_families"].add(source_family)
                if author:
                    accumulated["unique_authors"].add(author)
                accumulated["evidence_details"].append({
                    "provider": provider,
                    "source_family": source_family,
                    "author": author,
                    "url": url,
                    "canonical_url": canonical_url,
                    "title": (evidence.get("title") or evidence.get("text") or "")[:120],
                    "first_seen_run": run_idx,
                })

    result: list[dict[str, Any]] = []
    for accumulated in clusters.values():
        accumulated["unique_urls_count"] = len(accumulated.pop("unique_urls"))
        accumulated["unique_providers_count"] = len(accumulated.pop("unique_providers"))
        accumulated["unique_source_families_count"] = len(accumulated.pop("unique_source_families"))
        accumulated["unique_authors_count"] = len(accumulated.pop("unique_authors"))
        accumulated.pop("seen_evidence_keys")
        result.append(accumulated)

    result.sort(key=lambda item: (-item["runs"], -item["unique_evidence_count"], item["topic_key"]))
    return result


def aggregate_intent_families(accumulated_clusters: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Aggregate already-deduped clusters by ``intent_family``."""
    families: dict[str, dict[str, Any]] = {}
    for cluster in accumulated_clusters:
        family = str(cluster.get("intent_family") or "OTHER")
        aggregate = families.setdefault(family, {
            "intent_family": family,
            "child_topics": set(),
            "seen_evidence_keys": set(),
            "unique_urls": set(),
            "unique_authors": set(),
            "unique_providers": set(),
            "unique_source_families": set(),
        })
        aggregate["child_topics"].add(cluster.get("topic_key") or cluster.get("topic") or "")
        for evidence in cluster.get("evidence_details", []):
            provider = str(evidence.get("provider") or "").strip()
            url = str(evidence.get("url") or "").strip()
            evidence_key = _evidence_key(provider, url)
            if evidence_key in aggregate["seen_evidence_keys"]:
                continue
            aggregate["seen_evidence_keys"].add(evidence_key)
            canonical_url = _normalize_url(url)
            if canonical_url:
                aggregate["unique_urls"].add(canonical_url)
            if provider:
                aggregate["unique_providers"].add(provider)
            author = str(evidence.get("author") or "").strip()
            source_family = str(evidence.get("source_family") or "").strip()
            if author:
                aggregate["unique_authors"].add(author)
            if source_family:
                aggregate["unique_source_families"].add(source_family)

    result = []
    for aggregate in families.values():
        result.append({
            "intent_family": aggregate["intent_family"],
            "child_topics": sorted(aggregate["child_topics"]),
            "unique_evidence_count": len(aggregate["seen_evidence_keys"]),
            "unique_urls_count": len(aggregate["unique_urls"]),
            "unique_authors_count": len(aggregate["unique_authors"]),
            "unique_providers": sorted(aggregate["unique_providers"]),
            "unique_source_families": sorted(aggregate["unique_source_families"]),
        })
    result.sort(key=lambda item: (-item["unique_evidence_count"], item["intent_family"]))
    return result


def load_artifacts(*paths: str | Path) -> list[dict[str, Any]]:
    """Load existing artifact JSON files from paths."""
    import json

    artifacts = []
    for path in paths:
        artifact_path = Path(path)
        if artifact_path.exists():
            with artifact_path.open(encoding="utf-8") as handle:
                artifacts.append(json.load(handle))
    return artifacts
