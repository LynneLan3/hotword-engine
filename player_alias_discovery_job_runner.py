#!/usr/bin/env python3
"""Run one PLAYER_ALIAS_DISCOVERY job and callback Steam Apps Script.

Reuses the Steam Candidate Research callback transport
(STEAM_CANDIDATE_RESEARCH_API_URL + STEAM_CANDIDATE_RESEARCH_CALLBACK_TOKEN).
"""

from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

import player_alias_discovery_runner as runner

ROOT = Path(__file__).resolve().parent
JOB_TYPE = "PLAYER_ALIAS_DISCOVERY"
EXEC_COMPLETED = "COMPLETED"
EXEC_FAILED = "FAILED"
API_URL_ENV = "STEAM_CANDIDATE_RESEARCH_API_URL"
CALLBACK_TOKEN_ENV = "STEAM_CANDIDATE_RESEARCH_CALLBACK_TOKEN"
CALLBACK_TIMEOUT_SEC = 60
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
    text = _text(value) or "player_alias_discovery_failed"
    return text[:ERROR_MAX_CHARS]


def build_completed_callback(*, job: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    evidence = result.get("evidence") if isinstance(result.get("evidence"), list) else []
    compact_evidence = []
    for item in evidence[:20]:
        if not isinstance(item, dict):
            continue
        compact_evidence.append(
            {
                "source": _text(item.get("source")),
                "title": _text(item.get("title"))[:160],
                "snippet": _text(item.get("snippet"))[:200],
                "url": _text(item.get("url")),
            }
        )
    source_urls = result.get("source_urls") if isinstance(result.get("source_urls"), list) else []
    return {
        "job_id": _text(job.get("job_id")),
        "job_type": JOB_TYPE,
        "steam_app_id": _text(job.get("steam_app_id")),
        "game_name": _text(job.get("game_name")),
        "research_cycle_date": _text(job.get("research_cycle_date")),
        "execution_status": EXEC_COMPLETED,
        "alias": _text(result.get("alias")),
        "status": _text(result.get("status")),
        "confidence": _text(result.get("confidence")),
        "source_count": int(result.get("source_count") or 0),
        "source_urls": [str(url) for url in source_urls if str(url).strip()][:20],
        "evidence": compact_evidence,
        "patterns": result.get("patterns") if isinstance(result.get("patterns"), list) else [],
        "ranked": result.get("ranked") if isinstance(result.get("ranked"), list) else [],
        "source_diags": result.get("source_diags")
        if isinstance(result.get("source_diags"), list)
        else [],
        "research_result_path": f"jobs/{_text(job.get('job_id'))}/player_alias_discovery_result.json",
        "completed_at": _text(result.get("completed_at")),
    }


def build_failed_callback(*, job: dict[str, Any], error: Any = None) -> dict[str, Any]:
    return {
        "job_id": _text(job.get("job_id")),
        "job_type": JOB_TYPE,
        "steam_app_id": _text(job.get("steam_app_id")),
        "game_name": _text(job.get("game_name")),
        "research_cycle_date": _text(job.get("research_cycle_date")),
        "execution_status": EXEC_FAILED,
        "alias": "",
        "status": "RETRIEVAL_FAILED",
        "confidence": "UNKNOWN",
        "source_count": 0,
        "source_urls": [],
        "evidence": [],
        "error": _error_text(error),
    }


def _http_post(url: str, body: dict[str, Any]) -> bool:
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "hotword-engine-player-alias-discovery-callback/1",
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


def post_callback(
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
    collect_fn: Any = None,
    post_fn: PostFn | None = None,
    root: Path = ROOT,
    dry_run: bool = False,
) -> dict[str, Any]:
    job = runner.validate_job(_load_json(job_path))
    job_id = job["job_id"]
    job_dir = root / "jobs" / job_id
    result_path = job_dir / "player_alias_discovery_result.json"
    status_path = job_dir / "status.json"

    result: dict[str, Any] | None = None
    run_error: Any = None
    try:
        result = runner.run_discovery(job, collect_fn=collect_fn)
        _write_json(result_path, result)
    except Exception as exc:
        run_error = exc

    if result is not None:
        callback_payload = build_completed_callback(job=job, result=result)
    else:
        callback_payload = build_failed_callback(job=job, error=run_error)
        _write_json(
            result_path,
            {
                **callback_payload,
                "research_status": EXEC_FAILED,
            },
        )

    callback_ok = True
    callback_error = None
    if dry_run:
        callback_ok = True
        callback_error = None
    else:
        callback_ok, callback_error = post_callback(callback_payload, post_fn=post_fn)

    status = {
        "job_id": job_id,
        "job_type": JOB_TYPE,
        "execution_status": callback_payload.get("execution_status"),
        "alias_status": callback_payload.get("status"),
        "alias": callback_payload.get("alias"),
        "callback_ok": callback_ok,
        "callback_error": callback_error,
        "dry_run": dry_run,
    }
    _write_json(status_path, status)
    return {
        "job": job,
        "result": result,
        "callback": callback_payload,
        "callback_ok": callback_ok,
        "callback_error": callback_error,
        "status": status,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one player alias discovery job")
    parser.add_argument(
        "--job",
        default=str(ROOT / "input" / "player_alias_discovery_job.json"),
        help="Path to job JSON",
    )
    parser.add_argument("--dry-run", action="store_true", help="Skip callback POST")
    args = parser.parse_args()
    outcome = run_job(Path(args.job), dry_run=args.dry_run)
    print(json.dumps({
        "job_id": outcome["job"]["job_id"],
        "alias": (outcome.get("result") or {}).get("alias"),
        "status": (outcome.get("result") or outcome.get("callback") or {}).get("status"),
        "callback_ok": outcome["callback_ok"],
        "callback_error": outcome["callback_error"],
    }, ensure_ascii=False))
    if not outcome["callback_ok"] and not args.dry_run:
        return 2
    if outcome.get("result") is None:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
