#!/usr/bin/env python3
"""R2B DEMAND_DISCOVERY runner.

Consumes one R2A demand-discovery job contract and writes local artifacts:
  jobs/<job_id>/job.json
  jobs/<job_id>/demand_discovery_result.json
  jobs/<job_id>/status.json

No callback. No Apps Script writeback. No scheduler changes.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import research_runner as rr

ROOT = Path(__file__).resolve().parent

REQUIRED_FIELDS = (
    "job_id",
    "research_type",
    "site",
    "game",
    "radar_id",
    "trigger_type",
    "anchor_page",
    "source_signal_summary",
    "discovery_scope",
    "seed_terms",
    "source_families_requested",
    "discovery_cycle_date",
    "created_at",
)

PROVIDER_TO_FAMILY = {
    "reddit": "COMMUNITY",
    "steam": "COMMUNITY",
    "youtube": "VIDEO",
}

FAMILY_TO_PROVIDERS = {
    "COMMUNITY": ("reddit", "steam"),
    "VIDEO": ("youtube",),
}

DISCOVERY_STATUS_NO_SIGNAL = "NO_SIGNAL"
DISCOVERY_STATUS_DISCOVERED = "DISCOVERED"
DISCOVERY_STATUS_CROSS_VALIDATED = "CROSS_VALIDATED"

STOP_WORDS = {
    "the",
    "a",
    "an",
    "and",
    "or",
    "to",
    "of",
    "in",
    "on",
    "for",
    "is",
    "it",
    "do",
    "does",
    "did",
    "will",
    "would",
    "can",
    "could",
    "i",
    "we",
    "you",
    "my",
    "our",
    "your",
    "what",
    "how",
    "when",
    "where",
    "why",
    "which",
    "are",
    "be",
    "about",
    "game",
    "guide",
}


def now_iso() -> str:
    return rr.now_iso()


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def log(msg: str) -> None:
    rr.log(msg)


def normalize_text(text: str) -> str:
    return rr.norm(str(text or ""))


def normalize_url(url: str) -> str:
    u = str(url or "").strip()
    if not u:
        return ""
    u = u.split("#")[0]
    if "?" in u:
        base, q = u.split("?", 1)
        # Keep minimal deterministic query subset for video URLs.
        if "youtube.com/watch" in base and "v=" in q:
            v = re.search(r"(?:^|&)v=([^&]+)", q)
            if v:
                return base + "?v=" + v.group(1)
        u = base
    if u.endswith("/"):
        u = u[:-1]
    return u


def normalize_signal(text: str) -> str:
    n = normalize_text(text)
    n = re.sub(r"\s+", " ", n).strip()
    return n


def signal_tokens(text: str) -> set[str]:
    tokens = re.findall(r"[a-z0-9]+", normalize_signal(text))
    out: set[str] = set()
    for t in tokens:
        if len(t) < 2:
            continue
        if t in STOP_WORDS:
            continue
        out.add(t)
    return out


def jaccard_tokens(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def provider_to_family(provider: str) -> str:
    return PROVIDER_TO_FAMILY.get(str(provider or "").strip().lower(), "UNKNOWN")


# ---------------------------------------------------------------------------
# R2B.1  Anchor Relevance Gate helpers
# ---------------------------------------------------------------------------

_SIMPLE_PLURAL: list[tuple[str, str]] = [
    ("ies$", "y"),
    ("ves$", "f"),
    ("ses$", "s"),
    ("s$", ""),
]


def _singular(word: str) -> str:
    """Very lightweight singular form (English-only, no NLP)."""
    w = word.lower()
    for pattern, repl in _SIMPLE_PLURAL:
        candidate = re.sub(pattern, repl, w)
        if candidate != w and len(candidate) >= 2:
            return candidate
    return w


_ANCHOR_STOP = {"the", "a", "an", "and", "or", "to", "of", "in", "on", "for",
                "is", "it", "do", "does", "did", "are", "be", "about",
                "page", "index", "html", "htm"}


def anchor_discriminative_tokens(page_topic: str, game: str = "") -> set[str]:
    """Derive discriminative anchor tokens from *page_topic*.

    Removes game-name tokens and generic URL words.  Returns both the raw
    token and its singular form so that 'classes' yields {'classes', 'class'}.
    """
    game_tokens = set(re.findall(r"[a-z0-9]+", normalize_text(game)))
    raw_tokens = re.findall(r"[a-z0-9]+", normalize_text(page_topic))
    out: set[str] = set()
    for t in raw_tokens:
        if len(t) < 2:
            continue
        if t in _ANCHOR_STOP:
            continue
        if t in game_tokens:
            continue
        out.add(t)
        sing = _singular(t)
        if sing != t:
            out.add(sing)
    return out


def determine_seed_role(seed_term: str, game: str) -> str:
    """GAME_WIDE if seed equals normalised game name, else ANCHOR."""
    ns = normalize_text(seed_term)
    ng = normalize_text(game)
    if ns == ng:
        return "GAME_WIDE"
    return "ANCHOR"


def is_anchor_qualified(evidence: dict[str, Any], job: dict[str, Any]) -> bool:
    """Check whether evidence qualifies for anchor-specific clustering.

    Path A: at least one matched seed had role ANCHOR.
    Path B: signal_text / title / excerpt contains an anchor discriminative token.
    """
    trigger = str(job.get("trigger_type") or "")
    scope = job.get("discovery_scope") or {}
    page_topic = str(scope.get("page_topic") or "").strip()
    if not page_topic:
        return True  # GAME_WIDE scope – everything qualifies

    # Path A
    roles = evidence.get("matched_seed_roles") or []
    if "ANCHOR" in roles:
        return True

    # Path B – anchor discriminative token in text
    game = str(job.get("game") or "")
    disc_tokens = anchor_discriminative_tokens(page_topic, game)
    if not disc_tokens:
        return True  # no discriminative tokens derivable → pass through

    text = " ".join([
        str(evidence.get("signal_text") or ""),
        str(evidence.get("title") or ""),
        str(evidence.get("excerpt") or ""),
    ])
    text_tokens = set(re.findall(r"[a-z0-9]+", normalize_text(text)))
    return bool(disc_tokens & text_tokens)


def _validate_job(job: dict[str, Any]) -> dict[str, Any]:
    missing = []
    for k in REQUIRED_FIELDS:
        v = job.get(k)
        if isinstance(v, str):
            if not v.strip():
                missing.append(k)
        elif v is None:
            missing.append(k)
    if missing:
        raise ValueError("demand_discovery_job missing fields: " + ", ".join(missing))
    if str(job.get("research_type") or "").strip().upper() != "DEMAND_DISCOVERY":
        raise ValueError("research_type must be DEMAND_DISCOVERY")
    if not isinstance(job.get("discovery_scope"), dict):
        raise ValueError("discovery_scope must be object")
    if not isinstance(job.get("seed_terms"), list):
        raise ValueError("seed_terms must be array")
    if not isinstance(job.get("source_families_requested"), list):
        raise ValueError("source_families_requested must be array")
    return job


def load_job(path: Path) -> dict[str, Any]:
    job = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(job, dict):
        raise ValueError("job json must be object")
    return _validate_job(job)


def requested_providers(job: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for fam in job.get("source_families_requested") or []:
        family = str(fam or "").strip().upper()
        for provider in FAMILY_TO_PROVIDERS.get(family, ()):
            if provider not in out:
                out.append(provider)
    return out


def to_discovery_evidence_item(raw: dict[str, Any], seed_term: str, seed_role: str = "GAME_WIDE") -> dict[str, Any]:
    provider = str(raw.get("source") or "").strip().lower()
    title = str(raw.get("title") or "").strip()
    url = str(raw.get("url") or "").strip()
    excerpt = str(raw.get("excerpt") or raw.get("evidence") or "").strip()
    player_question = str(raw.get("player_question") or "").strip()
    signal_text = player_question or title or excerpt
    nsignal = normalize_signal(signal_text)
    st = str(seed_term or "").strip()
    return {
        "provider": provider,
        "source_family": provider_to_family(provider),
        "title": title,
        "url": url,
        "excerpt": excerpt,
        "player_question": player_question,
        "signal_text": signal_text,
        "seed_term": st,
        "seed_role": seed_role,
        "matched_seed_terms": [st],
        "matched_seed_roles": [seed_role],
        "relevance": float(raw.get("relevance") or 0.0),
        "normalized_signal": nsignal,
    }


def dedupe_discovery_evidence(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deduplicate evidence, merging seed provenance across duplicates."""
    key_to_idx: dict[tuple[str, str, str], int] = {}
    out: list[dict[str, Any]] = []
    for item in items:
        provider = str(item.get("provider") or "").strip().lower()
        nurl = normalize_url(str(item.get("url") or ""))
        nsig = normalize_signal(
            str(item.get("signal_text") or item.get("player_question") or item.get("excerpt") or "")
        )
        key = (provider, nurl, nsig[:240])
        if key in key_to_idx:
            existing = out[key_to_idx[key]]
            for st in (item.get("matched_seed_terms") or []):
                if st and st not in existing.get("matched_seed_terms", []):
                    existing.setdefault("matched_seed_terms", []).append(st)
            for sr in (item.get("matched_seed_roles") or []):
                if sr and sr not in existing.get("matched_seed_roles", []):
                    existing.setdefault("matched_seed_roles", []).append(sr)
            continue
        key_to_idx[key] = len(out)
        out.append(item)
    out.sort(key=lambda x: float(x.get("relevance") or 0.0), reverse=True)
    return out


def _cluster_signature(tokens: set[str]) -> str:
    if not tokens:
        return "other"
    top = sorted(tokens)[:6]
    return "-".join(top)


def cluster_demand_signals(evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    clusters: list[dict[str, Any]] = []
    for item in evidence:
        signal_text = str(item.get("signal_text") or "")
        token_set = signal_tokens(signal_text)
        placed = False
        for cluster in clusters:
            sim = jaccard_tokens(token_set, cluster["_tokens"])
            phrase_same = (
                normalize_signal(signal_text) == normalize_signal(cluster["representative_signal"])
            )
            if phrase_same or sim >= 0.45:
                cluster["_items"].append(item)
                cluster["_tokens"] = cluster["_tokens"] | token_set
                # keep highest relevance signal as representative
                if float(item.get("relevance") or 0.0) > float(cluster["_max_rel"]):
                    cluster["_max_rel"] = float(item.get("relevance") or 0.0)
                    cluster["representative_signal"] = signal_text
                    if str(item.get("player_question") or "").strip():
                        cluster["representative_question"] = str(item.get("player_question") or "")
                placed = True
                break
        if not placed:
            clusters.append(
                {
                    "_items": [item],
                    "_tokens": token_set,
                    "_max_rel": float(item.get("relevance") or 0.0),
                    "representative_signal": signal_text,
                    "representative_question": str(item.get("player_question") or "").strip(),
                }
            )

    out: list[dict[str, Any]] = []
    for idx, cluster in enumerate(clusters, start=1):
        items = cluster["_items"]
        providers = sorted({str(x.get("provider") or "") for x in items if x.get("provider")})
        families = sorted(
            {str(x.get("source_family") or "") for x in items if str(x.get("source_family") or "") != "UNKNOWN"}
        )
        family_count = len(families)
        topic_terms = sorted(cluster["_tokens"])[:10]
        excerpts = [
            str(x.get("excerpt") or "")[:200]
            for x in items
            if str(x.get("excerpt") or "").strip()
        ][:5]
        urls = list(dict.fromkeys([normalize_url(str(x.get("url") or "")) for x in items if x.get("url")]))[:5]
        out.append(
            {
                "cluster_id": f"cluster-{idx:03d}-{_cluster_signature(cluster['_tokens'])}",
                "representative_signal": cluster["representative_signal"],
                "representative_question": cluster["representative_question"],
                "topic_terms": topic_terms,
                "evidence_count": len(items),
                "providers": providers,
                "source_families": families,
                "independent_source_family_count": family_count,
                "cross_validated": family_count >= 2,
                "example_urls": urls,
                "example_excerpts": excerpts,
            }
        )
    out.sort(key=lambda x: x["evidence_count"], reverse=True)
    return out


def derive_discovery_status(clusters: list[dict[str, Any]]) -> str:
    if not clusters:
        return DISCOVERY_STATUS_NO_SIGNAL
    cross_n = sum(1 for c in clusters if bool(c.get("cross_validated")))
    if cross_n > 0:
        return DISCOVERY_STATUS_CROSS_VALIDATED
    return DISCOVERY_STATUS_DISCOVERED


def _collect_for_seed(
    seed_term: str,
    job: dict[str, Any],
    providers: list[str],
) -> tuple[list[dict[str, Any]], dict[str, int], dict[str, str]]:
    game = str(job.get("game") or "")
    seed_role = determine_seed_role(seed_term, game)
    evidence: list[dict[str, Any]] = []
    source_counts: dict[str, int] = {"reddit": 0, "steam": 0, "youtube": 0}
    failures: dict[str, str] = {}

    appids = rr.resolve_steam_appids(game, None)

    if "youtube" in providers:
        try:
            yt_items, _ = rr.collect_youtube(game=game, topic=seed_term)
            source_counts["youtube"] += len(yt_items)
            evidence.extend([to_discovery_evidence_item(x, seed_term, seed_role) for x in yt_items])
        except Exception as exc:
            failures["youtube"] = str(exc)

    if "reddit" in providers:
        try:
            rd_items, _ = rr.collect_reddit(game=game, topic=seed_term)
            source_counts["reddit"] += len(rd_items)
            evidence.extend([to_discovery_evidence_item(x, seed_term, seed_role) for x in rd_items])
        except Exception as exc:
            failures["reddit"] = str(exc)

    if "steam" in providers:
        try:
            st_items, _ = rr.collect_steam(game=game, topic=seed_term, appids=appids)
            source_counts["steam"] += len(st_items)
            evidence.extend([to_discovery_evidence_item(x, seed_term, seed_role) for x in st_items])
        except Exception as exc:
            failures["steam"] = str(exc)

    return evidence, source_counts, failures


def _resolve_discovery_scope_label(job: dict[str, Any]) -> str:
    """Return 'ANCHOR' or 'GAME_WIDE' based on page_topic availability."""
    scope = job.get("discovery_scope") or {}
    page_topic = str(scope.get("page_topic") or "").strip()
    if page_topic:
        return "ANCHOR"
    return "GAME_WIDE"


def run_demand_discovery(job: dict[str, Any]) -> dict[str, Any]:
    providers = requested_providers(job)
    seeds = [str(s or "").strip() for s in job.get("seed_terms") or [] if str(s or "").strip()]
    all_evidence: list[dict[str, Any]] = []
    source_counts = Counter()
    source_failures: dict[str, str] = {}

    log(f"Demand Discovery job={job['job_id']} game={job['game']!r}")
    log(f"Providers: {providers}")
    log(f"Seed terms: {seeds}")

    for seed in seeds:
        ev, counts, failures = _collect_for_seed(seed, job, providers)
        all_evidence.extend(ev)
        source_counts.update(counts)
        for p, msg in failures.items():
            source_failures[p] = msg

    deduped = dedupe_discovery_evidence(all_evidence)

    scope_label = _resolve_discovery_scope_label(job)

    if scope_label == "ANCHOR":
        anchor_evidence = [e for e in deduped if is_anchor_qualified(e, job)]
        background_evidence = [e for e in deduped if not is_anchor_qualified(e, job)]
        cluster_input = anchor_evidence
    else:
        anchor_evidence = deduped
        background_evidence = []
        cluster_input = deduped

    clusters = cluster_demand_signals(cluster_input)
    cross_n = sum(1 for c in clusters if c.get("cross_validated"))
    discovery_status = derive_discovery_status(clusters)

    result = {
        "research_type": "DEMAND_DISCOVERY",
        "input": {
            "job_id": job["job_id"],
            "site": job["site"],
            "game": job["game"],
            "radar_id": job["radar_id"],
            "trigger_type": job["trigger_type"],
            "anchor_page": job["anchor_page"],
            "source_signal_summary": job["source_signal_summary"],
            "discovery_scope": job.get("discovery_scope") or {},
            "seed_terms": seeds,
            "source_families_requested": job.get("source_families_requested") or [],
            "discovery_cycle_date": job["discovery_cycle_date"],
            "created_at": job["created_at"],
        },
        "run_at": now_iso(),
        "discovery_scope_label": scope_label,
        "source_counts": dict(source_counts),
        "source_failures": source_failures,
        "all_evidence": deduped,
        "anchor_evidence": anchor_evidence,
        "anchor_evidence_count": len(anchor_evidence),
        "background_evidence": background_evidence,
        "background_evidence_count": len(background_evidence),
        "demand_clusters": clusters,
        "cross_validated_cluster_count": cross_n,
        "discovery_status": discovery_status,
    }
    return result


def run_job(job_path: Path) -> dict[str, Any]:
    job = load_job(job_path)
    job_id = str(job["job_id"]).strip()
    job_dir = ROOT / "jobs" / job_id
    result_path = job_dir / "demand_discovery_result.json"
    status_path = job_dir / "status.json"
    job_copy_path = job_dir / "job.json"

    write_json(job_copy_path, job)
    write_json(
        status_path,
        {"job_id": job_id, "research_type": "DEMAND_DISCOVERY", "status": "RUNNING", "started_at": now_iso()},
    )
    try:
        result = run_demand_discovery(job)
        write_json(result_path, result)
        write_json(
            status_path,
            {
                "job_id": job_id,
                "research_type": "DEMAND_DISCOVERY",
                "status": "COMPLETED",
                "finished_at": now_iso(),
                "result_path": f"jobs/{job_id}/demand_discovery_result.json",
                "discovery_status": result.get("discovery_status"),
                "cross_validated_cluster_count": result.get("cross_validated_cluster_count", 0),
            },
        )
        return {"ok": True, "status": "COMPLETED", "job_id": job_id, "result": result}
    except Exception as exc:
        write_json(
            status_path,
            {
                "job_id": job_id,
                "research_type": "DEMAND_DISCOVERY",
                "status": "FAILED",
                "finished_at": now_iso(),
                "error": str(exc)[:300],
            },
        )
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description="R2B DEMAND_DISCOVERY Runner")
    parser.add_argument(
        "job_file",
        nargs="?",
        default=str(ROOT / "input" / "demand_discovery_job.json"),
        help="Path to demand_discovery_job.json",
    )
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

