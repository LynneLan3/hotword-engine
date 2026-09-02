#!/usr/bin/env python3
"""M7C wrapper: run/reuse M7A, generate M7B, and callback Steam.

The callback is deliberately compact.  Raw social evidence, result URLs, and
full SERP rows stay in local artifacts and are never sent to the Steam Sheet.
"""

from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

import steam_candidate_recommendation as m7b
import steam_candidate_research_runner as m7a

ROOT = Path(__file__).resolve().parent
JOB_TYPE = "STEAM_CANDIDATE_RESEARCH"
EXEC_COMPLETED = "COMPLETED"
EXEC_FAILED = "FAILED"
API_URL_ENV = "STEAM_CANDIDATE_RESEARCH_API_URL"
CALLBACK_TOKEN_ENV = "STEAM_CANDIDATE_RESEARCH_CALLBACK_TOKEN"
CALLBACK_TIMEOUT_SEC = 45
ERROR_MAX_CHARS = 300

PostFn = Callable[[str, dict[str, Any]], Any]


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return payload


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _error_text(value: Any) -> str:
    text = _text(value) or "steam_candidate_research_failed"
    return text[:ERROR_MAX_CHARS]


def _relative_result_path(job_id: str) -> str:
    return f"jobs/{job_id}/steam_candidate_research_result.json"


def _relative_recommendation_path(job_id: str) -> str:
    return f"jobs/{job_id}/steam_candidate_recommendation.json"


def _valid_completed_result(result: Any, job: dict[str, Any]) -> bool:
    if not isinstance(result, dict):
        return False
    return (
        _text(result.get("job_id")) == _text(job.get("job_id"))
        and _text(result.get("job_type")).upper() == JOB_TYPE
        and _text(result.get("steam_app_id")) == _text(job.get("steam_app_id"))
        and _text(result.get("research_status")).upper() == EXEC_COMPLETED
    )


def _result_error(result: dict[str, Any] | None) -> str:
    if not isinstance(result, dict):
        return "steam_candidate_research_failed"
    explicit = _text(result.get("error"))
    if explicit:
        return _error_text(explicit)
    errors: list[str] = []
    for key in ("social", "serp"):
        section = result.get(key)
        if isinstance(section, dict) and _text(section.get("error")):
            errors.append(f"{key}:{_text(section['error'])}")
    return _error_text("; ".join(errors) if errors else "steam_candidate_research_failed")


def _top_topics(social: dict[str, Any]) -> list[str]:
    topics: list[str] = []
    clusters = social.get("top_clusters")
    if not isinstance(clusters, list):
        return topics
    for cluster in clusters:
        if not isinstance(cluster, dict):
            continue
        topic = _text(cluster.get("topic")) or _text(cluster.get("topic_key"))
        if topic.lower().startswith(("http://", "https://")):
            continue
        if topic and topic not in topics:
            topics.append(topic)
        if len(topics) >= 10:
            break
    return topics


def _serp_summary(
    result: dict[str, Any], recommendation: dict[str, Any]
) -> dict[str, Any]:
    serp = result.get("serp") if isinstance(result.get("serp"), dict) else {}
    snapshot = recommendation.get("evidence_snapshot")
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    return {
        "status": _text(serp.get("status")).upper(),
        "query": _text(serp.get("query")),
        "organic_count": serp.get("organic_count", 0),
        "guide_density": snapshot.get("serp_guide_density", "UNKNOWN"),
        "high_video_ugc": bool(snapshot.get("serp_high_video_ugc")),
        "contamination": bool(snapshot.get("serp_contamination")),
    }


def build_steam_candidate_research_completed_callback(
    *,
    job: dict[str, Any],
    result: dict[str, Any],
    recommendation: dict[str, Any],
    completed_at: str,
) -> dict[str, Any]:
    recommendation_value = _text(recommendation.get("recommendation"))
    if recommendation_value not in m7b.RECOMMENDATIONS:
        raise ValueError("recommendation must be a M7B RECOMMEND_* value")
    social = result.get("social") if isinstance(result.get("social"), dict) else {}
    reasons = recommendation.get("reasons")
    missing = recommendation.get("missing_evidence")
    return {
        "job_id": _text(job.get("job_id")),
        "job_type": JOB_TYPE,
        "steam_app_id": _text(job.get("steam_app_id")),
        "game_name": _text(job.get("game_name")),
        "research_cycle_date": _text(job.get("research_cycle_date")),
        "execution_status": EXEC_COMPLETED,
        "recommendation": recommendation_value,
        "confidence": _text(recommendation.get("confidence")),
        "reasons": reasons if isinstance(reasons, list) else [],
        "blocking_reasons": (
            recommendation.get("blocking_reasons")
            if isinstance(recommendation.get("blocking_reasons"), list)
            else []
        ),
        "missing_evidence": missing if isinstance(missing, list) else [],
        "social_summary": {
            "status": _text(social.get("status")).upper(),
            "evidence_count": social.get("evidence_count", 0),
            "cluster_count": social.get("cluster_count", 0),
            "actionable_cluster_count": social.get("actionable_cluster_count", 0),
            "watch_cluster_count": social.get("watch_cluster_count", 0),
            "top_topics": _top_topics(social),
        },
        "serp_summary": _serp_summary(result, recommendation),
        "recalc_evidence": recommendation.get("evidence_snapshot")
        if isinstance(recommendation.get("evidence_snapshot"), dict)
        else {},
        "research_result_path": _relative_result_path(_text(job.get("job_id"))),
        "recommendation_result_path": _relative_recommendation_path(_text(job.get("job_id"))),
        "completed_at": completed_at,
    }


def build_steam_candidate_research_failed_callback(
    *,
    job: dict[str, Any],
    result: dict[str, Any] | None = None,
    error: Any = None,
) -> dict[str, Any]:
    return {
        "job_id": _text((result or {}).get("job_id") or job.get("job_id")),
        "job_type": JOB_TYPE,
        "steam_app_id": _text((result or {}).get("steam_app_id") or job.get("steam_app_id")),
        "game_name": _text((result or {}).get("game_name") or job.get("game_name")),
        "research_cycle_date": _text(job.get("research_cycle_date")),
        "execution_status": EXEC_FAILED,
        "error": _error_text(error if error is not None else _result_error(result)),
    }


def _update_status_json(
    root: Path,
    job_id: str,
    *,
    callback_ok: bool,
    callback_error: str | None = None,
) -> None:
    path = root / "jobs" / job_id / "status.json"
    if not path.exists():
        return
    try:
        status = _load_json(path)
    except Exception:
        return
    status["callback_ok"] = bool(callback_ok)
    if callback_error:
        status["callback_error"] = _error_text(callback_error)
    _write_json(path, status)


def _http_post(url: str, body: dict[str, Any]) -> bool:
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "hotword-engine-steam-candidate-research-callback/1",
            "Accept": "application/json,text/plain,*/*",
        },
        method="POST",
    )
    opener = urllib.request.build_opener(urllib.request.HTTPHandler())
    try:
        with opener.open(request, timeout=CALLBACK_TIMEOUT_SEC) as response:
            code = response.getcode() or 200
            raw = response.read()
            headers = dict(response.headers.items())
    except urllib.error.HTTPError as exc:
        code = exc.code
        raw = exc.read() or b""
        headers = dict(exc.headers.items() if exc.headers else {})
    except Exception:
        return False

    location = next(
        (value.strip() for key, value in headers.items() if key.lower() == "location" and value),
        "",
    )
    if location and code in {301, 302, 303, 307, 308}:
        try:
            redirect = urllib.request.Request(
                location,
                headers={"Accept": "application/json,text/plain,*/*"},
                method="GET",
            )
            with opener.open(redirect, timeout=CALLBACK_TIMEOUT_SEC) as response:
                code = response.getcode() or 200
                raw = response.read()
        except Exception:
            return False
    if code < 200 or code >= 300:
        return False
    try:
        parsed = json.loads(raw.decode("utf-8", errors="replace")) if raw else {}
    except json.JSONDecodeError:
        return False
    return isinstance(parsed, dict) and bool(parsed.get("ok"))


def _post_callback(
    payload: dict[str, Any],
    *,
    post_fn: PostFn | None = None,
) -> tuple[bool, str | None]:
    url = _text(os.environ.get(API_URL_ENV))
    token = _text(os.environ.get(CALLBACK_TOKEN_ENV))
    if not url:
        return False, f"{API_URL_ENV} is not set; refusing callback"
    if not token:
        return False, f"{CALLBACK_TOKEN_ENV} is not set; refusing callback"
    request_body = dict(payload)
    request_body["token"] = token
    try:
        if post_fn is not None:
            response = post_fn(url, request_body)
            if isinstance(response, dict):
                return bool(response.get("ok")), None if response.get("ok") else "callback_rejected"
            return bool(response), None if response else "callback_rejected"
        ok = _http_post(url, request_body)
        return ok, None if ok else "callback_request_failed"
    except Exception as exc:
        return False, _error_text(exc)


def run_job(
    job_path: Path,
    *,
    fetch_fn: Any = None,
    post_fn: PostFn | None = None,
    root: Path = ROOT,
    dry_run: bool = False,
) -> dict[str, Any]:
    job = m7a.validate_job(_load_json(job_path))
    job_id = _text(job["job_id"])
    job_dir = root / "jobs" / job_id
    research_path = job_dir / "steam_candidate_research_result.json"
    recommendation_path = job_dir / "steam_candidate_recommendation.json"
    reused = False
    result: dict[str, Any] | None = None
    run_error: Any = None

    if research_path.exists():
        try:
            existing = _load_json(research_path)
        except Exception:
            existing = None
        if _valid_completed_result(existing, job):
            result = existing
            reused = True

    if result is None:
        try:
            outcome = m7a.run_job(job_path, fetch_fn=fetch_fn, root=root)
            result = outcome.get("result") if isinstance(outcome, dict) else None
            if not isinstance(result, dict):
                raise RuntimeError("M7A runner returned no research result")
        except Exception as exc:
            run_error = exc

    if result is not None and _text(result.get("research_status")).upper() == EXEC_COMPLETED:
        recommendation = m7b.build_steam_candidate_recommendation(result)
        recommendation["generated_at"] = m7a.now_iso()
        _write_json(recommendation_path, recommendation)
        callback_payload = build_steam_candidate_research_completed_callback(
            job=job,
            result=result,
            recommendation=recommendation,
            completed_at=m7a.now_iso(),
        )
        execution_status = EXEC_COMPLETED
    else:
        recommendation = None
        callback_payload = build_steam_candidate_research_failed_callback(
            job=job,
            result=result,
            error=run_error or _result_error(result),
        )
        execution_status = EXEC_FAILED

    if dry_run:
        return {
            "ok": True,
            "job_id": job_id,
            "execution_status": execution_status,
            "callback_ok": None,
            "callback_error": None,
            "dry_run": True,
            "sent": False,
            "reused_research_artifact": reused,
            "research_result_path": _relative_result_path(job_id),
            "recommendation_result_path": (
                _relative_recommendation_path(job_id) if recommendation is not None else None
            ),
            "callback_payload": callback_payload,
        }

    callback_ok, callback_error = _post_callback(callback_payload, post_fn=post_fn)
    _update_status_json(root, job_id, callback_ok=callback_ok, callback_error=callback_error)
    return {
        "ok": execution_status == EXEC_COMPLETED and callback_ok,
        "job_id": job_id,
        "execution_status": execution_status,
        "callback_ok": callback_ok,
        "callback_error": callback_error,
        "dry_run": False,
        "sent": True,
        "reused_research_artifact": reused,
        "research_result_path": _relative_result_path(job_id),
        "recommendation_result_path": (
            _relative_recommendation_path(job_id) if recommendation is not None else None
        ),
        "callback_payload": callback_payload,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="M7C Steam Candidate Research callback runner")
    parser.add_argument(
        "job_file",
        nargs="?",
        default=str(ROOT / "input" / "steam_candidate_research_job.json"),
    )
    parser.add_argument("--dry-run", action="store_true", help="build artifacts and payload without POST")
    args = parser.parse_args(argv)
    job_path = Path(args.job_file)
    if not job_path.is_absolute():
        job_path = (Path.cwd() / job_path).resolve()
    if not job_path.exists():
        print(f"Job file not found: {job_path}")
        return 2
    outcome = run_job(job_path, dry_run=bool(args.dry_run))
    if args.dry_run:
        print(json.dumps(outcome["callback_payload"], ensure_ascii=False, indent=2))
        return 0
    return 0 if outcome["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
