#!/usr/bin/env python3
"""Run preflight gate plus full machine research for MANUAL_REVIEW candidates."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable

import steam_candidate_machine_fields as machine_fields
import steam_candidate_preflight as preflight
import steam_candidate_preflight_executor as preflight_executor
import steam_candidate_recommendation as m7b
import steam_candidate_research_job_runner as m7c
import steam_candidate_research_runner as m7a

ROOT = Path(__file__).resolve().parent


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
    social_summary = dict(payload.get("social_summary") or {})
    social_summary["verdict"] = machine.get("social_verdict")
    social_summary["one_liner"] = machine.get("social_one_liner")
    payload["social_summary"] = social_summary
    return payload


def run_job(
    job_path: Path,
    *,
    root: Path = ROOT,
    preflight_fn: Callable[..., dict[str, Any]] = preflight.run_preflight,
    social_fn: Callable[[dict[str, Any]], dict[str, Any]] = m7a.run_candidate_social,
    research_fn: Callable[..., dict[str, Any]] | None = None,
    post_fn: Callable[[str, dict[str, Any]], Any] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Preflight first; full M7A+M7B callback only for MANUAL_REVIEW."""
    job = _load(job_path)
    job_id = _text(job.get("job_id"))
    if not job_id:
        raise ValueError("machine research job missing job_id")

    job_dir = root / "jobs" / job_id
    preflight_path = job_dir / "steam_candidate_preflight.json"
    research_path = job_dir / "steam_candidate_research_result.json"
    recommendation_path = job_dir / "steam_candidate_recommendation.json"

    reused_preflight = False
    preflight_result: dict[str, Any] | None = None
    if preflight_path.exists():
        try:
            existing = _load(preflight_path)
            if _text(existing.get("preflight_verdict")) in preflight.VERDICTS:
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
        result["social"] = social
        machine = machine_fields.build_machine_fields(social=social, preflight_result=preflight_result)
        result = _enrich_research_result(result, preflight_result=preflight_result, machine=machine)
        _write(research_path, result)
    else:
        machine = _as_machine_fields(result, social=social, preflight_result=preflight_result)

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

    callback_ok, callback_error = m7c._post_callback(callback_payload, post_fn=post_fn)
    m7c._update_status_json(root, job_id, callback_ok=callback_ok, callback_error=callback_error)
    return {
        "ok": callback_ok,
        "execution_status": m7c.EXEC_COMPLETED,
        "callback_ok": callback_ok,
        "callback_error": callback_error,
        "callback_payload": callback_payload,
        "preflight_verdict": verdict,
        "reused_preflight_artifact": reused_preflight,
        "reused_research_artifact": reused_research,
        "dry_run": False,
        "sent": True,
        "recommendation": recommendation.get("recommendation"),
    }


def _as_machine_fields(
    result: dict[str, Any],
    *,
    social: dict[str, Any],
    preflight_result: dict[str, Any],
) -> dict[str, Any]:
    existing = result.get("machine_fields")
    if isinstance(existing, dict) and existing.get("social_result"):
        return existing
    return machine_fields.build_machine_fields(social=social, preflight_result=preflight_result)
