#!/usr/bin/env python3
"""R3B SEARCH_DEMAND runner.

Local only: Google/Bing Autocomplete → Anchor Relevance Gate → result artifact.

Does not:
  - POST callbacks
  - scrape Google PAA / Related SERP
  - create production Search Jobs
  - modify demand discovery (R2) runners
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Callable

import demand_discovery_runner as ddr
import search_demand_providers as sdp

ROOT = Path(__file__).resolve().parent

REQUIRED_FIELDS = (
    "job_id",
    "research_type",
    "site",
    "game",
    "radar_id",
    "trigger_type",
    "anchor_page",
    "discovery_scope",
    "seed_terms",
    "search_sources_requested",
    "source_signal_summary",
    "search_cycle_date",
    "created_at",
)

ENABLED_SOURCES = (
    sdp.SOURCE_GOOGLE_AUTOCOMPLETE,
    sdp.SOURCE_BING_AUTOCOMPLETE,
)
DISABLED_SOURCES = (
    sdp.SOURCE_GOOGLE_PAA,
    sdp.SOURCE_GOOGLE_RELATED,
)
SOURCE_FAMILY_SEARCH = "SEARCH"
DISABLED_REASON = "disabled_r3b_no_serp"

STATUS_CONFIRMED = "CONFIRMED"
STATUS_NO_SIGNAL = "NO_SIGNAL"
EXEC_COMPLETED = "COMPLETED"
EXEC_FAILED = "FAILED"
SCOPE_ANCHOR = "ANCHOR"
SCOPE_GAME_WIDE = "GAME_WIDE"

WHITELIST_ANCHOR_SOURCES = frozenset(ENABLED_SOURCES)

FetchFn = Callable[[str, dict[str, str] | None], dict[str, Any]]

_PROBE_BY_SOURCE = {
    sdp.SOURCE_GOOGLE_AUTOCOMPLETE: sdp.probe_google_autocomplete,
    sdp.SOURCE_BING_AUTOCOMPLETE: sdp.probe_bing_autocomplete,
}


def now_iso() -> str:
    return ddr.now_iso()


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def log(msg: str) -> None:
    ddr.log(msg)


def normalize_text(text: str) -> str:
    return ddr.normalize_text(text)


def determine_seed_role(seed_term: str, game: str) -> str:
    return ddr.determine_seed_role(seed_term, game)


def page_topic_of(job: dict[str, Any]) -> str:
    scope = job.get("discovery_scope") or {}
    if not isinstance(scope, dict):
        return ""
    return str(scope.get("page_topic") or "").strip()


def resolve_discovery_scope(job: dict[str, Any]) -> str:
    return SCOPE_ANCHOR if page_topic_of(job) else SCOPE_GAME_WIDE


def requested_sources(job: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for raw in job.get("search_sources_requested") or []:
        src = str(raw or "").strip().upper()
        if src and src not in out:
            out.append(src)
    return out


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
        raise ValueError("search_demand_job missing fields: " + ", ".join(missing))
    if str(job.get("research_type") or "").strip().upper() != "SEARCH_DEMAND":
        raise ValueError("research_type must be SEARCH_DEMAND")
    if not isinstance(job.get("discovery_scope"), dict):
        raise ValueError("discovery_scope must be object")
    if not isinstance(job.get("seed_terms"), list):
        raise ValueError("seed_terms must be array")
    if not isinstance(job.get("search_sources_requested"), list):
        raise ValueError("search_sources_requested must be array")
    return job


def load_job(path: Path) -> dict[str, Any]:
    job = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(job, dict):
        raise ValueError("job json must be object")
    return _validate_job(job)


def _suggestion_tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", normalize_text(text)))


def is_anchor_relevant_suggestion(suggestion: str, job: dict[str, Any]) -> bool:
    """Search-specific Anchor Relevance Gate. No LLM.

    ANCHOR=true iff:
      A. suggestion clearly matches an ANCHOR seed (equal to, or contains, that seed), or
      B. suggestion contains a page_topic discriminative token (class/classes).

    Coming from an ANCHOR seed query is not enough: generic Autocomplete
    continuations (walkthrough, review, ps5, xbox, romance) stay background.
    GAME_WIDE scope never yields anchor evidence.
    """
    topic = page_topic_of(job)
    if not topic:
        return False

    game = str(job.get("game") or "")
    sug_norm = normalize_text(suggestion)
    if not sug_norm:
        return False

    disc = ddr.anchor_discriminative_tokens(topic, game)
    sug_tokens = _suggestion_tokens(suggestion)
    if disc and (disc & sug_tokens):
        return True

    for seed in job.get("seed_terms") or []:
        seed_s = str(seed or "").strip()
        if not seed_s:
            continue
        if determine_seed_role(seed_s, game) != SCOPE_ANCHOR:
            continue
        ns = normalize_text(seed_s)
        if not ns:
            continue
        if sug_norm == ns or ns in sug_norm:
            return True
    return False


def _uniq_preserve(items: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        s = str(item or "").strip()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def _source_rank(source: str) -> int:
    order = {
        sdp.SOURCE_GOOGLE_AUTOCOMPLETE: 0,
        sdp.SOURCE_BING_AUTOCOMPLETE: 1,
        sdp.SOURCE_GOOGLE_PAA: 2,
        sdp.SOURCE_GOOGLE_RELATED: 3,
    }
    return order.get(source, 99)


def matched_seeds_for_suggestion(
    suggestion: str,
    query_seed: str,
    job: dict[str, Any],
) -> tuple[list[str], list[str]]:
    game = str(job.get("game") or "")
    sug_norm = normalize_text(suggestion)
    terms: list[str] = []
    roles: list[str] = []

    def add(seed: str) -> None:
        seed_s = str(seed or "").strip()
        if not seed_s or seed_s in terms:
            return
        terms.append(seed_s)
        roles.append(determine_seed_role(seed_s, game))

    add(query_seed)
    for seed in job.get("seed_terms") or []:
        seed_s = str(seed or "").strip()
        ns = normalize_text(seed_s)
        if seed_s and ns and ns in sug_norm:
            add(seed_s)
    return terms, roles


def make_evidence_item(
    *,
    source: str,
    suggestion: str,
    seed_term: str,
    job: dict[str, Any],
) -> dict[str, Any]:
    matched_terms, matched_roles = matched_seeds_for_suggestion(suggestion, seed_term, job)
    return {
        "source": source,
        "sources": [source],
        "source_family": SOURCE_FAMILY_SEARCH,
        "suggestion": suggestion,
        "seed_term": seed_term,
        "matched_seed_terms": matched_terms,
        "matched_seed_roles": matched_roles,
        "normalized_signal": normalize_text(suggestion),
        "anchor_relevant": is_anchor_relevant_suggestion(suggestion, job),
    }


def dedupe_search_evidence(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One unique Search expression per normalized suggestion.

    Google + Bing of the same suggestion merge into one row; sources provenance
    is kept. SEARCH remains a single source family.
    """
    key_to_idx: dict[str, int] = {}
    out: list[dict[str, Any]] = []
    for item in items:
        key = str(item.get("normalized_signal") or "").strip()
        if not key:
            continue
        if key not in key_to_idx:
            row = dict(item)
            row["sources"] = _uniq_preserve(list(item.get("sources") or [item.get("source")]))
            row["matched_seed_terms"] = _uniq_preserve(list(item.get("matched_seed_terms") or []))
            row["matched_seed_roles"] = _uniq_preserve(list(item.get("matched_seed_roles") or []))
            row["source_family"] = SOURCE_FAMILY_SEARCH
            out.append(row)
            key_to_idx[key] = len(out) - 1
            continue
        existing = out[key_to_idx[key]]
        existing["sources"] = _uniq_preserve(
            list(existing.get("sources") or []) + list(item.get("sources") or [item.get("source")])
        )
        existing["sources"] = sorted(existing["sources"], key=_source_rank)
        existing["source"] = existing["sources"][0] if existing["sources"] else existing.get("source")
        existing["matched_seed_terms"] = _uniq_preserve(
            list(existing.get("matched_seed_terms") or [])
            + list(item.get("matched_seed_terms") or [])
        )
        existing["matched_seed_roles"] = _uniq_preserve(
            list(existing.get("matched_seed_roles") or [])
            + list(item.get("matched_seed_roles") or [])
        )
        existing["anchor_relevant"] = bool(existing.get("anchor_relevant")) or bool(
            item.get("anchor_relevant")
        )
        existing["source_family"] = SOURCE_FAMILY_SEARCH
    return out


def _empty_provider_status(requested: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for src in requested:
        if src in DISABLED_SOURCES:
            out[src] = {
                "status": sdp.STATUS_UNAVAILABLE,
                "error": DISABLED_REASON,
                "http_status": None,
            }
        elif src in ENABLED_SOURCES:
            out[src] = {
                "status": sdp.STATUS_UNAVAILABLE,
                "error": "not_executed",
                "http_status": None,
            }
        else:
            out[src] = {
                "status": sdp.STATUS_UNAVAILABLE,
                "error": "unknown_source",
                "http_status": None,
            }
    return out


def _collect_autocomplete(
    job: dict[str, Any],
    fetch_fn: FetchFn | None,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, bool]]:
    requested = requested_sources(job)
    provider_status = _empty_provider_status(requested)
    executed_ok: dict[str, bool] = {src: False for src in ENABLED_SOURCES if src in requested}
    evidence: list[dict[str, Any]] = []
    seeds = [str(s or "").strip() for s in (job.get("seed_terms") or []) if str(s or "").strip()]
    topic = page_topic_of(job) or "classes"

    for src in requested:
        if src in DISABLED_SOURCES:
            continue
        if src not in ENABLED_SOURCES:
            continue
        probe = _PROBE_BY_SOURCE[src]
        last_error = None
        last_http = None
        any_ok = False
        for seed in seeds:
            try:
                row = probe(
                    seed,
                    seed_terms=seeds,
                    anchor_topic=topic,
                    fetch_fn=fetch_fn,
                )
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                last_http = 0
                log(f"provider {src} seed={seed!r} isolated error: {last_error}")
                continue
            last_http = (row.get("metadata") or {}).get("http_status")
            if row.get("status") == sdp.STATUS_BEST_EFFORT:
                any_ok = True
                last_error = None
                for item in row.get("items") or []:
                    text = str(item.get("text") or "").strip()
                    if not text:
                        continue
                    evidence.append(
                        make_evidence_item(
                            source=src,
                            suggestion=text,
                            seed_term=seed,
                            job=job,
                        )
                    )
            else:
                last_error = str(row.get("error") or "unavailable")
        if any_ok:
            executed_ok[src] = True
            provider_status[src] = {
                "status": sdp.STATUS_BEST_EFFORT,
                "error": None,
                "http_status": last_http,
            }
        else:
            executed_ok[src] = False
            provider_status[src] = {
                "status": sdp.STATUS_UNAVAILABLE,
                "error": last_error or ("no_seed_terms" if not seeds else "unavailable"),
                "http_status": last_http,
            }
    return evidence, provider_status, executed_ok


def derive_search_demand_status(
    *,
    discovery_scope: str,
    anchor_evidence: list[dict[str, Any]],
) -> str:
    if discovery_scope != SCOPE_ANCHOR:
        return STATUS_NO_SIGNAL
    if not anchor_evidence:
        return STATUS_NO_SIGNAL
    for item in anchor_evidence:
        sources = item.get("sources") or [item.get("source")]
        if any(str(s) in WHITELIST_ANCHOR_SOURCES for s in sources):
            return STATUS_CONFIRMED
    return STATUS_NO_SIGNAL


def callback_search_sources(anchor_evidence: list[dict[str, Any]]) -> list[str]:
    found: set[str] = set()
    for item in anchor_evidence:
        for src in item.get("sources") or [item.get("source")]:
            s = str(src or "").strip()
            if s in WHITELIST_ANCHOR_SOURCES:
                found.add(s)
    return [s for s in ENABLED_SOURCES if s in found]


def matched_queries_from(anchor_evidence: list[dict[str, Any]]) -> list[str]:
    return _uniq_preserve([str(e.get("suggestion") or "") for e in anchor_evidence])


def to_callback_fields(result: dict[str, Any]) -> dict[str, Any]:
    """Subset that maps directly onto the R3A SEARCH_DEMAND callback contract."""
    keys = (
        "job_id",
        "research_type",
        "radar_id",
        "search_cycle_date",
        "discovery_scope",
        "search_demand_status",
        "all_evidence_count",
        "anchor_evidence_count",
        "background_evidence_count",
        "search_evidence_count",
        "search_sources",
        "matched_queries",
        "top_questions",
        "provider_status",
        "result_path",
    )
    return {k: result.get(k) for k in keys}


def run_search_demand(
    job: dict[str, Any],
    fetch_fn: FetchFn | None = None,
    *,
    result_path: str = "",
) -> dict[str, Any]:
    job = _validate_job(dict(job))
    job_id = str(job["job_id"]).strip()
    seeds = [str(s or "").strip() for s in job.get("seed_terms") or [] if str(s or "").strip()]
    requested = requested_sources(job)
    scope_label = resolve_discovery_scope(job)

    log(f"Search Demand job={job_id} game={job['game']!r} scope={scope_label}")
    log(f"Requested sources: {requested}")
    log(f"Enabled sources: {[s for s in requested if s in ENABLED_SOURCES]}")
    log(f"Seed terms: {seeds}")

    raw_evidence, provider_status, executed_ok = _collect_autocomplete(job, fetch_fn)
    deduped = dedupe_search_evidence(raw_evidence)
    for item in deduped:
        item["anchor_relevant"] = is_anchor_relevant_suggestion(str(item.get("suggestion") or ""), job)

    if scope_label == SCOPE_ANCHOR:
        anchor_evidence = [e for e in deduped if e.get("anchor_relevant")]
        background_evidence = [e for e in deduped if not e.get("anchor_relevant")]
    else:
        anchor_evidence = []
        background_evidence = list(deduped)
        for e in background_evidence:
            e["anchor_relevant"] = False

    attempted = [s for s in requested if s in ENABLED_SOURCES]
    any_ok = any(executed_ok.get(s) for s in attempted)
    if attempted and not any_ok:
        execution_status = EXEC_FAILED
    elif not attempted:
        execution_status = EXEC_FAILED
    else:
        execution_status = EXEC_COMPLETED

    search_status = derive_search_demand_status(
        discovery_scope=scope_label,
        anchor_evidence=anchor_evidence,
    )
    if execution_status == EXEC_FAILED:
        search_status = STATUS_NO_SIGNAL

    search_sources = callback_search_sources(anchor_evidence) if execution_status == EXEC_COMPLETED else []
    rel_path = result_path or f"jobs/{job_id}/search_demand_result.json"

    result = {
        "job_id": job_id,
        "research_type": "SEARCH_DEMAND",
        "radar_id": job["radar_id"],
        "search_cycle_date": job["search_cycle_date"],
        "discovery_scope": scope_label,
        "search_demand_status": search_status,
        "execution_status": execution_status,
        "all_evidence_count": len(deduped),
        "anchor_evidence_count": len(anchor_evidence),
        "background_evidence_count": len(background_evidence),
        "search_evidence_count": len(anchor_evidence),
        "search_sources": search_sources,
        "matched_queries": matched_queries_from(anchor_evidence) if execution_status == EXEC_COMPLETED else [],
        "top_questions": [],
        "provider_status": provider_status,
        "result_path": rel_path,
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
            "search_sources_requested": requested,
            "search_cycle_date": job["search_cycle_date"],
            "created_at": job["created_at"],
        },
        "run_at": now_iso(),
        "all_evidence": deduped,
        "anchor_evidence": anchor_evidence,
        "background_evidence": background_evidence,
    }
    return result


def _status_payload(
    job_id: str,
    *,
    status: str,
    search_demand_status: str | None,
    result_path: str | None,
    error: str | None = None,
    started: bool = False,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "job_id": job_id,
        "research_type": "SEARCH_DEMAND",
        "status": status,
    }
    if started:
        row["started_at"] = now_iso()
        return row
    row["search_demand_status"] = search_demand_status
    row["result_path"] = result_path
    row["finished_at"] = now_iso()
    row["callback_ok"] = None
    if error:
        row["error"] = error
    return row


def run_job(
    job_path: Path,
    fetch_fn: FetchFn | None = None,
    *,
    root: Path | None = None,
) -> dict[str, Any]:
    base = root or ROOT
    job = load_job(job_path)
    job_id = str(job["job_id"]).strip()
    job_dir = base / "jobs" / job_id
    result_rel = f"jobs/{job_id}/search_demand_result.json"
    result_path = job_dir / "search_demand_result.json"
    status_path = job_dir / "status.json"
    job_copy_path = job_dir / "job.json"

    write_json(job_copy_path, job)
    write_json(status_path, _status_payload(job_id, status="RUNNING", search_demand_status=None, result_path=None, started=True))
    try:
        result = run_search_demand(job, fetch_fn=fetch_fn, result_path=result_rel)
        write_json(result_path, result)
        exec_status = str(result.get("execution_status") or EXEC_COMPLETED)
        write_json(
            status_path,
            _status_payload(
                job_id,
                status=exec_status,
                search_demand_status=str(result.get("search_demand_status") or STATUS_NO_SIGNAL),
                result_path=result_rel,
            ),
        )
        return {"ok": exec_status == EXEC_COMPLETED, "status": exec_status, "job_id": job_id, "result": result}
    except Exception as exc:
        write_json(
            status_path,
            _status_payload(
                job_id,
                status=EXEC_FAILED,
                search_demand_status=STATUS_NO_SIGNAL,
                result_path=None,
                error=str(exc)[:300],
            ),
        )
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description="R3B SEARCH_DEMAND Runner (local, no callback)")
    parser.add_argument(
        "job_file",
        nargs="?",
        default=str(ROOT / "input" / "search_demand_job.json"),
        help="Path to search_demand_job.json",
    )
    args = parser.parse_args()
    job_path = Path(args.job_file)
    if not job_path.is_absolute():
        job_path = (Path.cwd() / job_path).resolve()
    if not job_path.exists():
        log(f"Job file not found: {job_path}")
        return 2
    outcome = run_job(job_path)
    result = outcome["result"]
    log(
        "done status={status} search_demand_status={sds} all={alln} anchor={an} background={bn}".format(
            status=outcome["status"],
            sds=result.get("search_demand_status"),
            alln=result.get("all_evidence_count"),
            an=result.get("anchor_evidence_count"),
            bn=result.get("background_evidence_count"),
        )
    )
    return 0 if outcome["status"] != EXEC_FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
