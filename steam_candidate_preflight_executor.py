#!/usr/bin/env python3
"""Run one Steam Candidate preflight and optionally send its callback."""

from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import steam_candidate_preflight as preflight
import steam_candidate_research_runner as research_runner

ROOT = Path(__file__).resolve().parent
JOB_TYPE = "STEAM_CANDIDATE_RESEARCH"
API_URL_ENV = "STEAM_CANDIDATE_RESEARCH_API_URL"
CALLBACK_TOKEN_ENV = "STEAM_CANDIDATE_RESEARCH_CALLBACK_TOKEN"
CALLBACK_TIMEOUT_SEC = 45


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


def _post(url: str, body: dict[str, Any]) -> dict[str, Any]:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    opener = urllib.request.build_opener(urllib.request.HTTPHandler())
    try:
        with opener.open(req, timeout=CALLBACK_TIMEOUT_SEC) as response:
            code = response.getcode() or 200
            raw = response.read()
            headers = dict(response.headers.items())
    except urllib.error.HTTPError as exc:
        code = exc.code
        raw = exc.read() or b""
        headers = dict(exc.headers.items() if exc.headers else {})
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
        except Exception as exc:
            return {"ok": False, "error": f"callback_redirect_failed: {exc}"}
    if code < 200 or code >= 300:
        return {"ok": False, "error": f"callback_http_{code}"}
    try:
        payload = json.loads(raw.decode("utf-8", errors="replace")) if raw else {}
    except json.JSONDecodeError:
        return {"ok": False, "error": "invalid_callback_response"}
    return payload if isinstance(payload, dict) else {"ok": False, "error": "invalid_callback_response"}


def _callback_url() -> str:
    base = _text(os.environ.get(API_URL_ENV))
    if not base:
        raise RuntimeError(f"{API_URL_ENV} is not set")
    return base


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _provider_error(result: dict[str, Any], social: dict[str, Any]) -> str:
    errors = [_text(item) for item in result.get("provider_errors") or [] if _text(item)]
    social_error = _text(social.get("error"))
    if social_error:
        errors.append(f"social:{social_error}")
    return " | ".join(dict.fromkeys(errors))[:300] or "preflight_providers_unavailable"


def _research_artifact(job: dict[str, Any], social: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    return {
        "job_id": job.get("job_id"),
        "job_type": JOB_TYPE,
        "steam_app_id": job.get("steam_app_id"),
        "game_name": job.get("game_name"),
        "research_status": "COMPLETED" if result.get("preflight_verdict") != preflight.PREFLIGHT_ERROR else "FAILED",
        "social": social,
        "serp": result.get("serp") if isinstance(result.get("serp"), dict) else {},
        "preflight": result,
        "generated_at": result.get("checked_at") or _now_iso(),
    }


def _callback_payload(job: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    verdict = _text(result.get("preflight_verdict")).upper() or preflight.PREFLIGHT_ERROR
    if verdict not in preflight.VERDICTS:
        verdict = preflight.PREFLIGHT_ERROR
    checked_at = _text(result.get("checked_at")) or _now_iso()
    reason = _text(result.get("preflight_reason_text")) or _text(result.get("preflight_reason")) or "preflight_execution_error"
    error = " | ".join(result.get("provider_errors") or []) if isinstance(result.get("provider_errors"), list) else ""
    return {
        "job_type": JOB_TYPE,
        "job_id": job.get("job_id"),
        "steam_app_id": job.get("steam_app_id"),
        "game_name": job.get("game_name"),
        "research_cycle_date": job.get("research_cycle_date"),
        "execution_status": "COMPLETED" if verdict != preflight.PREFLIGHT_ERROR else "FAILED",
        "preflight_verdict": verdict,
        "preflight_reason": reason,
        "preflight_checked_at": checked_at,
        "next_review_date": result.get("next_review_date") if verdict == preflight.WATCH else None,
        "error": error or reason if verdict == preflight.PREFLIGHT_ERROR else "",
    }


def run_job(
    job_path: Path,
    *,
    root: Path = ROOT,
    preflight_fn: Callable[..., dict[str, Any]] = preflight.run_preflight,
    social_fn: Callable[[dict[str, Any]], dict[str, Any]] = research_runner.run_candidate_social,
    post_fn: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    job = _load(job_path)
    job_id = _text(job.get("job_id"))
    if not job_id:
        raise ValueError("preflight job missing job_id")
    job_dir = root / "jobs" / job_id
    result_path = job_dir / "steam_candidate_preflight.json"
    status_path = job_dir / "preflight_status.json"
    _write(job_dir / "job.json", job)

    reused = False
    if result_path.exists():
        try:
            result = _load(result_path)
            reused = _text(result.get("preflight_verdict")) in preflight.VERDICTS
        except (OSError, ValueError):
            reused = False
    social: dict[str, Any] = {"status": "REUSED", "evidence_count": 0, "top_clusters": []}
    if not reused:
        _write(status_path, {"job_id": job_id, "status": "RUNNING"})
        social = social_fn(job)
        if not isinstance(social, dict):
            social = {"status": "UNAVAILABLE", "error": "invalid_social_result", "evidence_count": 0, "top_clusters": []}
        preflight_job = dict(job)
        preflight_job["social_supporting_evidence"] = {
            "status": _text(social.get("status")).upper(),
            "evidence_count": social.get("evidence_count", 0),
            "top_clusters": social.get("top_clusters") or [],
        }
        result = preflight_fn(preflight_job, cache_dir=root / "artifacts" / "steam-preflight")
        if not isinstance(result, dict):
            result = {"preflight_verdict": preflight.PREFLIGHT_ERROR, "preflight_reason": "invalid_preflight_result"}
        if social.get("error") and result.get("preflight_verdict") == preflight.PREFLIGHT_ERROR:
            result = dict(result)
            result["provider_errors"] = list(result.get("provider_errors") or []) + [f"social:{social['error']}"]
        _write(result_path, result)
        _write(job_dir / "steam_candidate_research_result.json", _research_artifact(job, social, result))
        _write(status_path, {"job_id": job_id, "status": result.get("preflight_verdict"), "result_path": f"jobs/{job_id}/steam_candidate_preflight.json"})
    else:
        research_path = job_dir / "steam_candidate_research_result.json"
        if research_path.exists():
            try:
                existing_research = _load(research_path)
                social = existing_research.get("social") if isinstance(existing_research.get("social"), dict) else social
            except (OSError, ValueError):
                pass

    payload = _callback_payload(job, result)
    sent = False
    callback_ok: bool | None = None
    callback_error = None
    if not dry_run:
        try:
            url = _callback_url()
            payload_with_token = dict(payload)
            token = _text(os.environ.get(CALLBACK_TOKEN_ENV))
            if not token:
                raise RuntimeError(f"{CALLBACK_TOKEN_ENV} is not set; refusing callback")
            payload_with_token["token"] = token
            response = post_fn(url, payload_with_token) if post_fn else _post(url, payload_with_token)
            callback_ok = bool(response.get("ok", True)) if isinstance(response, dict) else False
            sent = callback_ok
            if not callback_ok:
                callback_error = _text(response.get("error")) if isinstance(response, dict) else "callback_failed"
        except (OSError, RuntimeError, urllib.error.URLError) as exc:
            callback_ok = False
            callback_error = str(exc)[:300]
    return {
        "ok": result.get("preflight_verdict") != preflight.PREFLIGHT_ERROR and (dry_run or callback_ok is True),
        "execution_status": "COMPLETED" if result.get("preflight_verdict") != preflight.PREFLIGHT_ERROR else "FAILED",
        "callback_ok": callback_ok,
        "callback_error": callback_error,
        "callback_payload": payload,
        "preflight_verdict": result.get("preflight_verdict"),
        "reused_preflight_artifact": reused,
        "sent": sent,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_file", nargs="?", default=str(ROOT / "input" / "steam_candidate_research_job.json"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    outcome = run_job(Path(args.job_file), dry_run=args.dry_run)
    print(json.dumps(outcome, ensure_ascii=False, indent=2))
    return 0 if outcome["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
