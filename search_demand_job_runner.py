#!/usr/bin/env python3
"""R3C SEARCH_DEMAND callback sender.

Loads one local SEARCH_DEMAND job (or an existing R3B result), builds a
compact SEARCH_DEMAND callback payload, and POSTs it to the Apps Script
receiver using the same HTTP/token mechanism as research_job_runner.py.

No scheduler changes, no SERP Gap, no production job creation.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import research_job_runner as rjr
import search_demand_providers as sdp
import search_demand_runner as sdr

ROOT = Path(__file__).resolve().parent

CALLBACK_ERROR_MAX_CHARS = 300

ALLOWED_SEARCH_SOURCES = frozenset(
    {
        sdp.SOURCE_GOOGLE_AUTOCOMPLETE,
        sdp.SOURCE_GOOGLE_PAA,
        sdp.SOURCE_GOOGLE_RELATED,
        sdp.SOURCE_BING_AUTOCOMPLETE,
    }
)
SEARCH_SOURCE_ORDER = (
    sdp.SOURCE_GOOGLE_AUTOCOMPLETE,
    sdp.SOURCE_GOOGLE_PAA,
    sdp.SOURCE_GOOGLE_RELATED,
    sdp.SOURCE_BING_AUTOCOMPLETE,
)
ALLOWED_SCOPES = frozenset({sdr.SCOPE_ANCHOR, sdr.SCOPE_GAME_WIDE})
ALLOWED_SEARCH_STATUSES = frozenset({sdr.STATUS_CONFIRMED, sdr.STATUS_NO_SIGNAL})


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _truncate_error(err: Any) -> str:
    s = str(err).strip()
    if not s:
        s = "search_demand_failed"
    return s[:CALLBACK_ERROR_MAX_CHARS]


def _nonneg_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be a non-negative integer")
    if value < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return value


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        s = str(item or "").strip()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def _filter_search_sources(raw: Any) -> list[str]:
    found: set[str] = set()
    if not isinstance(raw, list):
        raw = []
    for item in raw:
        s = str(item or "").strip().upper()
        if s in ALLOWED_SEARCH_SOURCES:
            found.add(s)
    return [s for s in SEARCH_SOURCE_ORDER if s in found]


def _sources_from_anchor_evidence(result: dict[str, Any]) -> list[str]:
    evidence = result.get("anchor_evidence") or []
    if not isinstance(evidence, list):
        evidence = []
    raw: list[str] = []
    for item in evidence:
        if not isinstance(item, dict):
            continue
        sources = item.get("sources") or [item.get("source")]
        if not isinstance(sources, list):
            sources = [sources]
        for src in sources:
            raw.append(str(src or "").strip().upper())
    return _filter_search_sources(raw)


def callback_search_sources_from_result(result: dict[str, Any]) -> list[str]:
    """Whitelist sources that actually produced Anchor evidence.

    Background / generic evidence never enters search_sources.
    """
    from_field = _filter_search_sources(result.get("search_sources"))
    from_anchor = _sources_from_anchor_evidence(result)
    if from_anchor:
        return [s for s in from_field if s in set(from_anchor)] or from_anchor
    if result.get("anchor_evidence") == [] or int(result.get("anchor_evidence_count") or 0) == 0:
        return []
    return from_field


def _failed_error_from_result(result: dict[str, Any] | None) -> str:
    if isinstance(result, dict):
        explicit = str(result.get("error") or "").strip()
        if explicit:
            return _truncate_error(explicit)
        bits: list[str] = []
        provider_status = result.get("provider_status") or {}
        if isinstance(provider_status, dict):
            for src, row in provider_status.items():
                if not isinstance(row, dict):
                    continue
                if str(row.get("status") or "").strip().upper() == sdp.STATUS_BEST_EFFORT:
                    continue
                err = str(row.get("error") or "").strip()
                if err:
                    bits.append(f"{src}:{err}")
        if bits:
            return _truncate_error("; ".join(bits))
    return "search_demand_failed"


def build_search_demand_completed_callback_body(result: dict[str, Any]) -> dict[str, Any]:
    """Success-style SEARCH_DEMAND callback. Strictly mapped from an R3B result."""
    search_evidence_count = _nonneg_int(
        int(result.get("anchor_evidence_count") or 0),
        field="search_evidence_count",
    )
    body: dict[str, Any] = {
        "job_id": str(result.get("job_id") or "").strip(),
        "research_type": str(result.get("research_type") or "").strip().upper(),
        "radar_id": str(result.get("radar_id") or "").strip(),
        "search_cycle_date": str(result.get("search_cycle_date") or "").strip(),
        "execution_status": sdr.EXEC_COMPLETED,
        "discovery_scope": str(result.get("discovery_scope") or "").strip().upper(),
        "search_demand_status": str(result.get("search_demand_status") or "").strip().upper(),
        "search_evidence_count": search_evidence_count,
        "search_sources": callback_search_sources_from_result(result),
        "matched_queries": _string_list(result.get("matched_queries")),
        "top_questions": _string_list(result.get("top_questions")),
        "result_path": str(result.get("result_path") or "").strip()
        or f"jobs/{str(result.get('job_id') or '').strip()}/search_demand_result.json",
    }
    return body


def build_search_demand_failed_callback_body(
    *,
    job: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
    error: Any = None,
) -> dict[str, Any]:
    """R3A FAILED callback. Not a CONFIRMED/NO_SIGNAL success payload."""
    src = result if isinstance(result, dict) else {}
    job = job if isinstance(job, dict) else {}
    job_id = str(src.get("job_id") or job.get("job_id") or "").strip()
    radar_id = str(src.get("radar_id") or job.get("radar_id") or "").strip()
    cycle = str(src.get("search_cycle_date") or job.get("search_cycle_date") or "").strip()
    err = error if error is not None else _failed_error_from_result(result)
    return {
        "job_id": job_id,
        "research_type": "SEARCH_DEMAND",
        "radar_id": radar_id,
        "search_cycle_date": cycle,
        "execution_status": sdr.EXEC_FAILED,
        "error": _truncate_error(err),
    }


def validate_search_demand_callback_body(body: dict[str, Any]) -> dict[str, Any]:
    """Local pre-send validation. Raises ValueError; never sends on failure."""
    if not isinstance(body, dict):
        raise ValueError("callback body must be an object")
    if str(body.get("research_type") or "").strip().upper() != "SEARCH_DEMAND":
        raise ValueError("research_type must be SEARCH_DEMAND")
    if not str(body.get("job_id") or "").strip():
        raise ValueError("job_id is required")
    if not str(body.get("radar_id") or "").strip():
        raise ValueError("radar_id is required")
    if not str(body.get("search_cycle_date") or "").strip():
        raise ValueError("search_cycle_date is required")

    execution = str(body.get("execution_status") or "").strip().upper()
    if execution == sdr.EXEC_FAILED:
        return body
    if execution != sdr.EXEC_COMPLETED:
        raise ValueError("execution_status must be COMPLETED or FAILED")

    status = str(body.get("search_demand_status") or "").strip().upper()
    if status not in ALLOWED_SEARCH_STATUSES:
        raise ValueError("search_demand_status must be CONFIRMED or NO_SIGNAL")
    scope = str(body.get("discovery_scope") or "").strip().upper()
    if scope not in ALLOWED_SCOPES:
        raise ValueError("discovery_scope must be ANCHOR or GAME_WIDE")
    count = _nonneg_int(body.get("search_evidence_count"), field="search_evidence_count")
    sources = _filter_search_sources(body.get("search_sources"))

    if status == sdr.STATUS_CONFIRMED:
        if scope != sdr.SCOPE_ANCHOR:
            raise ValueError("CONFIRMED requires discovery_scope=ANCHOR")
        if count <= 0:
            raise ValueError("CONFIRMED requires search_evidence_count > 0")
        if not sources:
            raise ValueError("CONFIRMED requires at least one allowed search_sources value")
    return body


def _update_status_json_with_callback(
    job_id: str,
    *,
    callback_ok: bool,
    error: str | None = None,
) -> None:
    status_path = ROOT / "jobs" / job_id / "status.json"
    if not status_path.exists():
        return
    try:
        current = json.loads(status_path.read_text(encoding="utf-8"))
    except Exception:
        return
    if not isinstance(current, dict):
        return
    current["callback_ok"] = bool(callback_ok)
    if error:
        current["error"] = _truncate_error(error)
    _write_json(status_path, current)


def _load_result(path: Path) -> dict[str, Any]:
    result = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(result, dict):
        raise ValueError("search_demand_result.json must be an object")
    return result


def _payload_for_outcome(
    *,
    job: dict[str, Any] | None,
    result: dict[str, Any] | None,
    execution_status: str,
    error: Any = None,
) -> tuple[dict[str, Any], bool]:
    """Return (payload, is_success_style)."""
    if execution_status == sdr.EXEC_FAILED:
        return (
            build_search_demand_failed_callback_body(job=job, result=result, error=error),
            False,
        )
    if execution_status != sdr.EXEC_COMPLETED or not isinstance(result, dict):
        raise ValueError("search demand result is not COMPLETED")
    if str(result.get("execution_status") or "").strip().upper() == sdr.EXEC_FAILED:
        return (
            build_search_demand_failed_callback_body(
                job=job,
                result=result,
                error=error or _failed_error_from_result(result),
            ),
            False,
        )
    body = build_search_demand_completed_callback_body(result)
    validate_search_demand_callback_body(body)
    return body, True


def run_job(
    job_path: Path | None = None,
    *,
    from_result: Path | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    job: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    execution_status = sdr.EXEC_FAILED
    run_error: Any = None

    if from_result is not None:
        result = _load_result(from_result)
        if job_path is not None:
            job = sdr.load_job(job_path)
        execution_status = str(result.get("execution_status") or "").strip().upper() or sdr.EXEC_FAILED
    else:
        if job_path is None:
            raise ValueError("job_file is required unless --from-result is set")
        job = sdr.load_job(job_path)
        try:
            outcome = sdr.run_job(job_path, root=ROOT)
            result = outcome.get("result") if isinstance(outcome, dict) else None
            if not isinstance(result, dict):
                raise RuntimeError("search_demand_runner returned no result")
            execution_status = str(outcome.get("status") or result.get("execution_status") or "").strip().upper()
        except Exception as exc:
            execution_status = sdr.EXEC_FAILED
            run_error = exc
            result = None

    job_id = str(
        (result or {}).get("job_id")
        or (job or {}).get("job_id")
        or ""
    ).strip()

    try:
        payload, is_success = _payload_for_outcome(
            job=job,
            result=result,
            execution_status=execution_status,
            error=run_error,
        )
        validate_search_demand_callback_body(payload)
    except Exception as exc:
        is_success = False
        payload = build_search_demand_failed_callback_body(
            job=job,
            result=result,
            error=run_error or exc,
        )
        if execution_status == sdr.EXEC_COMPLETED and run_error is None:
            # Invalid success payload: do not send a success-style callback,
            # and do not disguise validation failure as a provider FAILED callback.
            if not dry_run and job_id:
                _update_status_json_with_callback(
                    job_id,
                    callback_ok=False,
                    error=f"callback_validation_failed: {_truncate_error(exc)}",
                )
            return {
                "ok": False,
                "job_id": job_id,
                "execution_status": execution_status,
                "callback_ok": False,
                "dry_run": dry_run,
                "sent": False,
                "callback_payload": None,
                "error": _truncate_error(exc),
            }

    if dry_run:
        return {
            "ok": True,
            "job_id": job_id,
            "execution_status": execution_status,
            "callback_ok": None,
            "dry_run": True,
            "sent": False,
            "callback_payload": payload,
            "success_style": is_success,
        }

    callback_ok = rjr.post_callback_body(payload)
    if job_id:
        _update_status_json_with_callback(job_id, callback_ok=callback_ok)
    return {
        "ok": True,
        "job_id": job_id,
        "execution_status": execution_status,
        "callback_ok": callback_ok,
        "dry_run": False,
        "sent": True,
        "callback_payload": payload,
        "success_style": is_success,
    }


def exit_code_from_run(execution_status: str, callback_ok: bool | None, *, dry_run: bool = False) -> int:
    if execution_status == sdr.EXEC_FAILED:
        return 1
    if dry_run:
        return 0
    return 0 if callback_ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="R3C SEARCH_DEMAND callback sender")
    parser.add_argument(
        "job_file",
        nargs="?",
        default="",
        help="Path to search_demand_job.json",
    )
    parser.add_argument(
        "--from-result",
        default="",
        help="Use an existing search_demand_result.json instead of running providers",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build callback payload only; do not POST",
    )
    args = parser.parse_args()

    job_path: Path | None = None
    if str(args.job_file or "").strip():
        job_path = Path(args.job_file)
        if not job_path.is_absolute():
            job_path = (Path.cwd() / job_path).resolve()
        if not job_path.exists():
            sdr.log(f"Job file not found: {job_path}")
            return 2
    elif not str(args.from_result or "").strip():
        job_path = ROOT / "input" / "search_demand_job.json"
        if not job_path.exists():
            sdr.log(f"Job file not found: {job_path}")
            return 2

    from_result: Path | None = None
    if str(args.from_result or "").strip():
        from_result = Path(args.from_result)
        if not from_result.is_absolute():
            from_result = (Path.cwd() / from_result).resolve()
        if not from_result.exists():
            sdr.log(f"Result file not found: {from_result}")
            return 2

    run = run_job(job_path, from_result=from_result, dry_run=bool(args.dry_run))
    payload = run.get("callback_payload")
    if args.dry_run and isinstance(payload, dict):
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        sdr.log(
            "SEARCH_DEMAND dry-run job_id={job} execution={ex} "
            "search_demand_status={sds} search_evidence_count={sec} "
            "search_sources={src} sent=false".format(
                job=run.get("job_id"),
                ex=run.get("execution_status"),
                sds=payload.get("search_demand_status"),
                sec=payload.get("search_evidence_count"),
                src=payload.get("search_sources"),
            )
        )
    else:
        sdr.log(
            "SEARCH_DEMAND callback job_id={job} execution={ex} "
            "callback_ok={ok} sent={sent}".format(
                job=run.get("job_id"),
                ex=run.get("execution_status"),
                ok=run.get("callback_ok"),
                sent=run.get("sent"),
            )
        )
    return exit_code_from_run(
        str(run.get("execution_status") or ""),
        run.get("callback_ok"),  # type: ignore[arg-type]
        dry_run=bool(run.get("dry_run")),
    )


if __name__ == "__main__":
    sys.exit(main())
