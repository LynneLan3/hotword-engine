#!/usr/bin/env python3
"""Run preflight gate plus full machine research for MANUAL_REVIEW candidates."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable

import existing_site_exclusion as exclusion
import steam_candidate_machine_fields as machine_fields
import steam_candidate_preflight as preflight
import steam_candidate_preflight_executor as preflight_executor
import steam_candidate_recommendation as m7b
import steam_candidate_research_job_runner as m7c
import steam_candidate_research_runner as m7a
import search_demand_providers as providers

ROOT = Path(__file__).resolve().parent
DEFAULT_REGISTRY_SITES = ROOT.parent / "hotword-control-center" / "registry" / "sites.yaml"
DEFAULT_REGISTRY_GAMES = ROOT.parent / "hotword-control-center" / "registry" / "games.yaml"


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _load(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain an object")
    return payload


def _write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _enrich_research_result(
    result: dict[str, Any],
    *,
    preflight_result: dict[str, Any],
    machine: dict[str, Any],
) -> dict[str, Any]:
    enriched = dict(result)
    manual_signals = dict(enriched.get("manual_signals") or {})
    if machine.get("trends_result") not in {None, "", "未检查"}:
        manual_signals["trends_result"] = machine["trends_result"]
    if machine.get("keyword_opportunity") in {"有", "无"}:
        manual_signals["keyword_opportunity"] = machine["keyword_opportunity"]
    enriched["manual_signals"] = manual_signals
    enriched["preflight"] = preflight_result
    enriched["machine_fields"] = machine
    return enriched


def _extend_completed_callback(
    callback: dict[str, Any],
    *,
    preflight_result: dict[str, Any],
    machine: dict[str, Any],
) -> dict[str, Any]:
    payload = dict(callback)
    payload["preflight_verdict"] = preflight_result.get("preflight_verdict")
    payload["preflight_reason"] = (
        preflight_result.get("preflight_reason_text")
        or preflight_result.get("preflight_reason")
    )
    payload["preflight_checked_at"] = preflight_result.get("checked_at")
    payload["machine_fields"] = machine
    payload["machine_recommendation"] = machine_fields.normalize_machine_recommendation_display(
        payload.get("recommendation")
    )
    # Additive master-table contract: BUILD/WATCH/SKIP + existing confidence.
    payload["master_outcome_fields"] = machine_fields.build_master_outcome_machine_fields(
        machine=machine,
        recommendation=payload.get("recommendation"),
        confidence=payload.get("confidence"),
    )
    social_summary = dict(payload.get("social_summary") or {})
    social_summary["verdict"] = machine.get("social_verdict")
    social_summary["one_liner"] = machine.get("social_one_liner")
    payload["social_summary"] = social_summary
    return payload


def _paid_stage_errors(preflight_result: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    trends_status = _text(preflight_result.get("trends_status")).upper()
    if trends_status != providers.STATUS_SUPPORTED:
        trends_state = _text(preflight_result.get("trends_provider_state")).upper()
        errors.append(
            "trends:" + (trends_state + ":" if trends_state else "")
            + (_text(preflight_result.get("trends_error")) or "provider_unavailable")
        )
    serp = preflight_result.get("serp") if isinstance(preflight_result.get("serp"), dict) else {}
    queries = serp.get("queries") if isinstance(serp.get("queries"), list) else []
    if not any(
        _text(query.get("status")).upper() in {providers.STATUS_SUPPORTED, "AVAILABLE"}
        for query in queries
        if isinstance(query, dict)
    ):
        query_errors = [
            (
                _text(query.get("provider_state")).upper() + ":"
                if _text(query.get("provider_state"))
                else ""
            ) + _text(query.get("error"))
            for query in queries
            if isinstance(query, dict) and _text(query.get("error"))
        ]
        errors.append("serp:" + (" | ".join(query_errors) or "provider_unavailable"))
    return list(dict.fromkeys(errors))


def _as_machine_fields(
    result: dict[str, Any],
    *,
    social: dict[str, Any],
    preflight_result: dict[str, Any] | None,
) -> dict[str, Any]:
    existing = result.get("machine_fields")
    if isinstance(existing, dict) and existing:
        return existing
    return machine_fields.build_machine_fields(social=social, preflight_result=preflight_result)


def _attach_trends(
    result: dict[str, Any],
    *,
    job: dict[str, Any],
    root: Path,
    trends_fn: Callable[[str], dict[str, Any]] | None,
) -> dict[str, Any]:
    """Add one cached paid Trends result to a paid-stage preflight result."""
    query = _text(job.get("game_name"))
    raw, reused = preflight._cached_probe(
        cache_dir=root / "artifacts" / "steam-preflight",
        app_id=_text(job.get("steam_app_id")),
        query=query,
        run_date=m7a.now_iso()[:10].replace("-", ""),
        source=providers.SOURCE_SEARCHAPI_GOOGLE_TRENDS,
        probe=lambda: (trends_fn or providers.probe_searchapi_google_trends)(query),
    )
    raw = raw if isinstance(raw, dict) else {"status": providers.STATUS_UNAVAILABLE, "error": "invalid_trends_result"}
    item = raw.get("items", [{}])[0] if isinstance(raw.get("items"), list) and raw.get("items") else {}
    result = dict(result)
    result["trends"] = {**raw, "cache_reused": reused}
    result["trends_result"] = _text(item.get("strength")) or "未检查"
    result["trends_status"] = _text(raw.get("status")).upper() or providers.STATUS_UNAVAILABLE
    result["trends_error"] = _text(raw.get("error"))
    result["trends_provider_state"] = _text(raw.get("provider_state")).upper()
    result["trends_provider_reason"] = _text(raw.get("provider_reason"))
    result["paid_provider_usage"] = (raw.get("metadata") or {}).get("account")
    result["paid_provider_state"] = result["trends_provider_state"] or _text(
        result.get("paid_provider_state")
    ).upper()
    result["trends_cache_reused"] = reused
    if result["trends_status"] == providers.STATUS_UNAVAILABLE:
        result["provider_errors"] = list(result.get("provider_errors") or []) + [_text(raw.get("error")) or "trends_provider_unavailable"]
    return result


def _optional_existing_site_index() -> exclusion.ExistingSiteIndex | None:
    """Load live production index when explicitly enabled for job execution.

    Daily executor always passes its live index into ``run_job``. Standalone
    job runs opt in via ``HOTWORD_EXISTING_SITE_EXCLUSION=1`` (or snapshot envs).
    """
    enabled = os.environ.get("HOTWORD_EXISTING_SITE_EXCLUSION", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }
    gsc = os.environ.get("HOTWORD_GSC_SITE_SNAPSHOT")
    pool = os.environ.get("HOTWORD_STEAM_SITE_POOL_SNAPSHOT")
    sites_env = os.environ.get("HOTWORD_REGISTRY_SITES")
    if not (enabled or gsc or pool or sites_env):
        return None
    import existing_site_live_sources as live_sources

    index, _meta = live_sources.load_production_existing_site_index(
        allow_missing_registry=False,
    )
    return index



def _reconcile_recommendation_with_existing_site(
    job: dict[str, Any],
    recommendation: dict[str, Any],
    *,
    existing_site_index: exclusion.ExistingSiteIndex | None = None,
) -> dict[str, Any]:
    index = existing_site_index if existing_site_index is not None else _optional_existing_site_index()
    if index is None:
        return recommendation
    evaluation = exclusion.evaluate_existing_site(job, index)
    if not evaluation.get("existingSite"):
        return recommendation
    reconciled = dict(recommendation)
    reconciled["recommendation"] = exclusion.ALREADY_BUILT
    reconciled["existing_site"] = evaluation
    reconciled["eligibleForNewSite"] = False
    reconciled["reasons"] = list(reconciled.get("reasons") or []) + ["EXISTING_SITE"]
    reconciled["blocking_reasons"] = list(
        dict.fromkeys(list(reconciled.get("blocking_reasons") or []) + ["EXISTING_SITE"])
    )
    if evaluation.get("stateSyncGap"):
        reconciled["state_sync_codes"] = [exclusion.STATE_SYNC_GAP]
    reconciled["next_action"] = evaluation.get("next_action")
    return reconciled


def run_job(
    job_path: Path,
    *,
    root: Path = ROOT,
    preflight_fn: Callable[..., dict[str, Any]] = preflight.run_preflight,
    social_fn: Callable[[dict[str, Any]], dict[str, Any]] = m7a.run_candidate_social,
    research_fn: Callable[..., dict[str, Any]] | None = None,
    post_fn: Callable[[str, dict[str, Any]], Any] | None = None,
    dry_run: bool = False,
    existing_site_index: exclusion.ExistingSiteIndex | None = None,
    paid_serp_enabled: bool = True,
    force_paid_verification: bool = False,
    include_trends: bool = False,
    trends_fn: Callable[[str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Preflight first; full M7A+M7B callback only for MANUAL_REVIEW."""
    job = _load(job_path)
    job_id = _text(job.get("job_id"))
    if not job_id:
        raise ValueError("machine research job missing job_id")

    # Existing-site exclusion happens before research spend when an index is available.
    early_index = (
        existing_site_index
        if existing_site_index is not None
        else _optional_existing_site_index()
    )
    if early_index is not None:
        evaluation = exclusion.evaluate_existing_site(job, early_index)
        if evaluation.get("existingSite"):
            return {
                "ok": True,
                "execution_status": "SKIPPED_EXISTING_SITE",
                "callback_ok": None,
                "callback_payload": {
                    "recommendation": exclusion.ALREADY_BUILT,
                    "machine_recommendation": exclusion.ALREADY_BUILT,
                    "existing_site": evaluation,
                },
                "preflight_verdict": None,
                "existing_site": evaluation,
                "skipped_existing_site": True,
                "dry_run": dry_run,
                "sent": False,
            }

    job_dir = root / "jobs" / job_id
    preflight_path = job_dir / "steam_candidate_preflight.json"
    research_path = job_dir / "steam_candidate_research_result.json"
    recommendation_path = job_dir / "steam_candidate_recommendation.json"

    reused_preflight = False
    preflight_result: dict[str, Any] | None = None
    if preflight_path.exists():
        try:
            existing = _load(preflight_path)
            if _text(existing.get("preflight_verdict")) in preflight.VERDICTS and not (
                force_paid_verification and _text(existing.get("research_stage")).upper() == "FREE_FIRST"
            ):
                preflight_result = existing
                reused_preflight = True
        except (OSError, ValueError):
            reused_preflight = False

    social: dict[str, Any] = {"status": "REUSED", "evidence_count": 0, "top_clusters": []}
    if not reused_preflight:
        social = social_fn(job)
        if not isinstance(social, dict):
            social = {
                "status": "UNAVAILABLE",
                "error": "invalid_social_result",
                "evidence_count": 0,
                "top_clusters": [],
            }
        preflight_job = dict(job)
        preflight_job["social_supporting_evidence"] = {
            "status": _text(social.get("status")).upper(),
            "evidence_count": social.get("evidence_count", 0),
            "top_clusters": social.get("top_clusters") or [],
        }
        preflight_result = preflight_fn(
            preflight_job,
            cache_dir=root / "artifacts" / "steam-preflight",
            paid_serp_enabled=paid_serp_enabled,
        )
        if not isinstance(preflight_result, dict):
            preflight_result = {
                "preflight_verdict": preflight.PREFLIGHT_ERROR,
                "preflight_reason": "invalid_preflight_result",
            }
        if social.get("error") and preflight_result.get("preflight_verdict") == preflight.PREFLIGHT_ERROR:
            preflight_result = dict(preflight_result)
            preflight_result["provider_errors"] = list(
                preflight_result.get("provider_errors") or []
            ) + [f"social:{social['error']}"]
        _write(preflight_path, preflight_result)
    else:
        existing_research = job_dir / "steam_candidate_research_result.json"
        if existing_research.exists():
            try:
                artifact = _load(existing_research)
                social = artifact.get("social") if isinstance(artifact.get("social"), dict) else social
            except (OSError, ValueError):
                pass

    verdict = _text(preflight_result.get("preflight_verdict")).upper()
    if verdict != preflight.MANUAL_REVIEW:
        return preflight_executor.run_job(
            job_path,
            root=root,
            preflight_fn=lambda _job, **kwargs: preflight_result or {},
            social_fn=lambda _job: social,
            post_fn=post_fn,
            dry_run=dry_run,
        )

    if include_trends:
        preflight_result = _attach_trends(
            preflight_result,
            job=job,
            root=root,
            trends_fn=trends_fn,
        )
        _write(preflight_path, preflight_result)

    run_research = research_fn or m7a.run_candidate_research
    result: dict[str, Any] | None = None
    reused_research = False
    if research_path.exists():
        try:
            existing = _load(research_path)
            if m7c._valid_completed_result(existing, job):
                result = existing
                reused_research = True
        except (OSError, ValueError):
            reused_research = False

    if result is None:
        result = run_research(job)
        if not isinstance(result, dict):
            raise RuntimeError("M7A runner returned no research result")
    machine = machine_fields.build_machine_fields(social=social, preflight_result=preflight_result)
    result["social"] = social
    result = _enrich_research_result(result, preflight_result=preflight_result, machine=machine)
    _write(research_path, result)

    if include_trends:
        paid_errors = _paid_stage_errors(preflight_result)
        if paid_errors:
            result["provider_terminal_state"] = _text(
                preflight_result.get("trends_provider_state")
                or preflight_result.get("paid_provider_state")
            ).upper() or None
            result["provider_terminal_reason"] = _text(
                preflight_result.get("trends_provider_reason")
                or preflight_result.get("paid_provider_reason")
                or paid_errors[0]
            ) or None
            result["paid_provider_usage"] = preflight_result.get("paid_provider_usage")
            machine = machine_fields.build_machine_fields(
                social=social, preflight_result=preflight_result
            )
            result["machine_fields"] = machine
            result["research_status"] = m7c.EXEC_FAILED
            result["error"] = "paid_verification_failed: " + " | ".join(paid_errors)[:300]
            result["provider_errors"] = list(dict.fromkeys(
                list(result.get("provider_errors") or []) + paid_errors
            ))
            _write(research_path, result)
            callback_payload = m7c.build_steam_candidate_research_failed_callback(
                job=job,
                result=result,
                error=result["error"],
            )
            if dry_run:
                return {
                    "ok": True,
                    "execution_status": m7c.EXEC_FAILED,
                    "callback_ok": None,
                    "callback_payload": callback_payload,
                    "preflight_verdict": verdict,
                    "reused_preflight_artifact": reused_preflight,
                    "reused_research_artifact": reused_research,
                    "dry_run": True,
                    "sent": False,
                }
            callback_ok, callback_error, callback_diagnostics = m7c._post_callback(
                callback_payload, post_fn=post_fn, with_details=True
            )
            m7c._update_status_json(root, job_id, callback_ok=callback_ok, callback_error=callback_error)
            return {
                "ok": False,
                "execution_status": m7c.EXEC_FAILED,
                "callback_ok": callback_ok,
                "callback_error": callback_error,
                "callback_diagnostics": callback_diagnostics,
                "callback_payload": callback_payload,
                "preflight_verdict": verdict,
                "reused_preflight_artifact": reused_preflight,
                "reused_research_artifact": reused_research,
                "dry_run": False,
                "sent": True,
            }

    recommendation: dict[str, Any] | None = None
    if recommendation_path.exists():
        try:
            recommendation = _load(recommendation_path)
        except (OSError, ValueError):
            recommendation = None

    if recommendation is None or not _text(recommendation.get("recommendation")):
        recommendation = m7b.build_steam_candidate_recommendation(result)
        recommendation["generated_at"] = m7a.now_iso()
        _write(recommendation_path, recommendation)
    recommendation = _reconcile_recommendation_with_existing_site(
        job,
        recommendation,
        existing_site_index=early_index,
    )
    if recommendation.get("recommendation") == exclusion.ALREADY_BUILT:
        _write(recommendation_path, recommendation)

    callback_payload = m7c.build_steam_candidate_research_completed_callback(
        job=job,
        result=result,
        recommendation=recommendation,
        completed_at=m7a.now_iso(),
    )
    callback_payload = _extend_completed_callback(
        callback_payload,
        preflight_result=preflight_result,
        machine=machine,
    )
    if recommendation.get("existing_site"):
        callback_payload["existing_site"] = recommendation["existing_site"]
        callback_payload["machine_recommendation"] = exclusion.ALREADY_BUILT
        callback_payload["master_outcome_fields"] = machine_fields.build_master_outcome_machine_fields(
            machine=machine,
            recommendation=exclusion.ALREADY_BUILT,
            confidence=callback_payload.get("confidence"),
        )


    if dry_run:
        return {
            "ok": True,
            "execution_status": m7c.EXEC_COMPLETED,
            "callback_ok": None,
            "callback_payload": callback_payload,
            "preflight_verdict": verdict,
            "reused_preflight_artifact": reused_preflight,
            "reused_research_artifact": reused_research,
            "dry_run": True,
            "sent": False,
        }

    callback_ok, callback_error, callback_diagnostics = m7c._post_callback(
        callback_payload, post_fn=post_fn, with_details=True
    )
    m7c._update_status_json(root, job_id, callback_ok=callback_ok, callback_error=callback_error)
    return {
        "ok": callback_ok,
        "execution_status": m7c.EXEC_COMPLETED,
        "callback_ok": callback_ok,
        "callback_error": callback_error,
        "callback_diagnostics": callback_diagnostics,
        "callback_payload": callback_payload,
        "preflight_verdict": verdict,
        "reused_preflight_artifact": reused_preflight,
        "reused_research_artifact": reused_research,
        "dry_run": False,
        "sent": True,
        "recommendation": recommendation.get("recommendation"),
    }


def run_free_first_job(
    job_path: Path,
    *,
    root: Path = ROOT,
    preflight_fn: Callable[..., dict[str, Any]] = preflight.run_preflight,
    social_fn: Callable[[dict[str, Any]], dict[str, Any]] = m7a.run_candidate_social,
    existing_site_index: exclusion.ExistingSiteIndex | None = None,
) -> dict[str, Any]:
    """Persist free/existing/cache-first signals without paid or callback work."""
    job = _load(job_path)
    job_id = _text(job.get("job_id"))
    if not job_id:
        raise ValueError("machine research job missing job_id")
    index = existing_site_index if existing_site_index is not None else _optional_existing_site_index()
    if index is not None:
        evaluation = exclusion.evaluate_existing_site(job, index)
        if evaluation.get("existingSite"):
            return {
                "ok": True,
                "execution_status": "SKIPPED_EXISTING_SITE",
                "callback_ok": None,
                "existing_site": evaluation,
                "paid_serp_enabled": False,
                "paid_verification_status": "NOT_APPLICABLE",
                "sent": False,
            }

    job_dir = root / "jobs" / job_id
    preflight_path = job_dir / "steam_candidate_preflight.json"
    research_path = job_dir / "steam_candidate_research_result.json"
    social = social_fn(job)
    if not isinstance(social, dict):
        social = {"status": "UNAVAILABLE", "error": "invalid_social_result", "evidence_count": 0, "top_clusters": []}
    preflight_job = dict(job)
    preflight_job["social_supporting_evidence"] = {
        "status": _text(social.get("status")).upper(),
        "evidence_count": social.get("evidence_count", 0),
        "top_clusters": social.get("top_clusters") or [],
    }
    result = preflight_fn(
        preflight_job,
        cache_dir=root / "artifacts" / "steam-preflight",
        paid_serp_enabled=False,
    )
    if not isinstance(result, dict):
        result = {"preflight_verdict": preflight.PREFLIGHT_ERROR, "preflight_reason": "invalid_preflight_result"}
    result = dict(result)
    result.update({
        "research_stage": "FREE_FIRST",
        "paid_serp_enabled": False,
        "paid_verification_status": "DEFERRED",
        "paid_provider_calls": 0,
    })
    if social.get("error") and result.get("preflight_verdict") == preflight.PREFLIGHT_ERROR:
        result["provider_errors"] = list(result.get("provider_errors") or []) + [f"social:{social['error']}"]
    machine = machine_fields.build_machine_fields(social=social, preflight_result=result)
    artifact = {
        "job_id": job.get("job_id"),
        "job_type": job.get("job_type"),
        "steam_app_id": job.get("steam_app_id"),
        "game_name": job.get("game_name"),
        "research_status": "FREE_FIRST_COMPLETE" if result.get("preflight_verdict") != preflight.PREFLIGHT_ERROR else "FAILED",
        "research_stage": "FREE_FIRST",
        "social": social,
        "preflight": result,
        "machine_fields": machine,
        "generated_at": result.get("checked_at") or m7a.now_iso(),
    }
    _write(preflight_path, result)
    _write(research_path, artifact)
    return {
        "ok": result.get("preflight_verdict") != preflight.PREFLIGHT_ERROR,
        "execution_status": "FREE_RESEARCH_COMPLETED" if result.get("preflight_verdict") != preflight.PREFLIGHT_ERROR else "FAILED",
        "callback_ok": None,
        "callback_payload": {"machine_fields": machine, "preflight_verdict": result.get("preflight_verdict")},
        "preflight_verdict": result.get("preflight_verdict"),
        "paid_serp_enabled": False,
        "paid_verification_status": "DEFERRED",
        "paid_provider_calls": 0,
        "sent": False,
    }


if __name__ == "__main__":
    raise SystemExit("Use steam_candidate_daily_executor or import run_job()")
