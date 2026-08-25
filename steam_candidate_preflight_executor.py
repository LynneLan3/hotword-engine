#!/usr/bin/env python3
"""Run one Steam Candidate preflight and optionally send its callback."""

from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

import steam_candidate_preflight as preflight

ROOT = Path(__file__).resolve().parent
JOB_TYPE = "STEAM_CANDIDATE_RESEARCH"
API_URL_ENV = "STEAM_CANDIDATE_RESEARCH_API_URL"
CALLBACK_TOKEN_ENV = "STEAM_CANDIDATE_RESEARCH_CALLBACK_TOKEN"


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
    with urllib.request.urlopen(req, timeout=45) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return payload if isinstance(payload, dict) else {"ok": False, "error": "invalid_callback_response"}


def _callback_url() -> str:
    base = _text(os.environ.get(API_URL_ENV))
    if not base:
        raise RuntimeError(f"{API_URL_ENV} is not set")
    return base


def _callback_payload(job: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    return {
        "job_type": JOB_TYPE,
        "job_id": job.get("job_id"),
        "steam_app_id": job.get("steam_app_id"),
        "game_name": job.get("game_name"),
        "research_cycle_date": job.get("research_cycle_date"),
        "execution_status": "COMPLETED" if result.get("preflight_verdict") != preflight.PREFLIGHT_ERROR else "FAILED",
        "preflight_verdict": result.get("preflight_verdict"),
        "preflight_reason": result.get("preflight_reason_text") or result.get("preflight_reason"),
        "preflight_reason_code": result.get("preflight_reason"),
        "preflight_checked_at": result.get("checked_at"),
        "next_review_date": result.get("next_review_date"),
        "preflight_result_path": f"jobs/{job.get('job_id')}/steam_candidate_preflight.json",
        "next_action": result.get("next_action"),
        "searchapi_queries_used": result.get("searchapi_queries_used", 0),
        "searchapi_queries_reused": result.get("searchapi_queries_reused", 0),
        "error": " | ".join(result.get("provider_errors") or []) if result.get("preflight_verdict") == preflight.PREFLIGHT_ERROR else "",
    }


def run_job(
    job_path: Path,
    *,
    root: Path = ROOT,
    preflight_fn: Callable[..., dict[str, Any]] = preflight.run_preflight,
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
    if not reused:
        _write(status_path, {"job_id": job_id, "status": "RUNNING"})
        result = preflight_fn(job, cache_dir=root / "artifacts" / "steam-preflight")
        _write(result_path, result)
        _write(status_path, {"job_id": job_id, "status": result.get("preflight_verdict"), "result_path": f"jobs/{job_id}/steam_candidate_preflight.json"})

    payload = _callback_payload(job, result)
    sent = False
    callback_ok: bool | None = None
    callback_error = None
    if not dry_run:
        try:
            url = _callback_url()
            payload_with_token = dict(payload)
            token = _text(os.environ.get(CALLBACK_TOKEN_ENV))
            if token:
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
