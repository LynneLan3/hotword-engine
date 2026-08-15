#!/usr/bin/env python3
"""Minimal Research Job wrapper around research_runner.py.

Takes one GSC content-opportunity job, runs the existing Research Runner,
leaves artifacts for human review, and POSTs REVIEW/FAILED back to the
Google Sheet Research Job callback endpoint.

No database, queue, API, dashboard, site edits, or articles.

Usage:
  RESEARCH_CALLBACK_URL=... RESEARCH_CALLBACK_TOKEN=... \\
    python3 research_job_runner.py input/research_job.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import research_runner as rr

ROOT = Path(__file__).resolve().parent
REQUIRED_FIELDS = (
    "job_id",
    "game",
    "topic",
    "existing_page",
    "opportunity_level",
    "recommended_action",
    "source_query",
    "created_at",
)
CALLBACK_TIMEOUT_SEC = 45


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_job(path: Path) -> dict[str, Any]:
    job = json.loads(path.read_text(encoding="utf-8"))
    missing = [k for k in REQUIRED_FIELDS if not str(job.get(k) or "").strip()]
    if missing:
        raise ValueError("research_job.json missing fields: " + ", ".join(missing))
    return job


def relative_result_path(job_id: str) -> str:
    return f"jobs/{job_id}/research_result.json"


def recommendation_action(recommendation: Any) -> str:
    if isinstance(recommendation, dict):
        return str(recommendation.get("action") or "").strip()
    return str(recommendation or "").strip()


def status_payload(
    job: dict[str, Any],
    *,
    status: str,
    started_at: str | None,
    finished_at: str | None = None,
    recommendation: dict[str, Any] | None = None,
    evidence_count: int | None = None,
    result_path: str | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "job_id": job["job_id"],
        "status": status,
        "started_at": started_at,
        "finished_at": finished_at,
        "recommendation": recommendation,
        "evidence_count": evidence_count,
        "result_path": result_path,
    }
    if error:
        out["error"] = error
    return out


def review_summary_from_result(result: dict[str, Any] | None) -> str:
    """Prefer research_result.review_summary; fall back to recommendation.reason."""
    if not isinstance(result, dict):
        return ""
    summary = str(result.get("review_summary") or "").strip()
    if summary:
        return summary
    rec = result.get("recommendation")
    if isinstance(rec, dict):
        return str(rec.get("reason") or "").strip()
    return ""


def evidence_from_result(result: dict[str, Any] | None) -> list[Any]:
    if not isinstance(result, dict):
        return []
    evidence = result.get("evidence")
    return list(evidence) if isinstance(evidence, list) else []


def build_callback_body(
    status: dict[str, Any],
    *,
    result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    job_id = str(status["job_id"]).strip()
    if status["status"] == "FAILED":
        return {
            "job_id": job_id,
            "status": "FAILED",
            "error": str(status.get("error") or "research_job_failed")[:300],
        }
    body: dict[str, Any] = {
        "job_id": job_id,
        "status": "REVIEW",
        "recommendation": recommendation_action(status.get("recommendation")),
        "evidence_count": int(status.get("evidence_count") or 0),
        "result_path": str(status.get("result_path") or relative_result_path(job_id)),
        "review_summary": review_summary_from_result(result),
        "evidence": evidence_from_result(result),
    }
    return body


def _http_exchange(
    url: str,
    *,
    method: str,
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    req = urllib.request.Request(
        url,
        data=data,
        headers=headers or {},
        method=method,
    )
    # Apps Script Web Apps return 302 to a one-shot ContentService URL.
    # Do not auto-follow: urllib would turn POST into GET and drop the body.
    opener = urllib.request.build_opener(urllib.request.HTTPHandler())
    try:
        with opener.open(req, timeout=CALLBACK_TIMEOUT_SEC) as resp:
            return resp.getcode() or 200, dict(resp.headers.items()), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers.items() if exc.headers else {}), exc.read() or b""


def post_research_callback(
    status: dict[str, Any],
    *,
    result: dict[str, Any] | None = None,
) -> bool:
    """POST REVIEW/FAILED to Google Sheet callback. Never mutates local artifacts."""
    url = str(os.environ.get("RESEARCH_CALLBACK_URL") or "").strip()
    token = str(os.environ.get("RESEARCH_CALLBACK_TOKEN") or "").strip()
    if not url:
        rr.log("Callback error    RESEARCH_CALLBACK_URL is not set")
        return False
    if not token:
        rr.log("Callback error    RESEARCH_CALLBACK_TOKEN is not set")
        return False

    body = build_callback_body(status, result=result)
    body["token"] = token
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "User-Agent": "hotword-engine-research-job-callback/1",
        "Accept": "application/json,text/plain,*/*",
    }

    try:
        code, resp_headers, raw = _http_exchange(
            url, method="POST", data=payload, headers=headers
        )
        # Follow redirect Location with GET (Apps Script ContentService pattern).
        location = ""
        for key, value in resp_headers.items():
            if key.lower() == "location" and value:
                location = value.strip()
                break
        if location and code in {301, 302, 303, 307, 308}:
            code, _, raw = _http_exchange(
                location,
                method="GET",
                headers={
                    "User-Agent": headers["User-Agent"],
                    "Accept": headers["Accept"],
                },
            )

        text = raw.decode("utf-8", errors="replace").strip()
        if code < 200 or code >= 300:
            rr.log(f"Callback error    HTTP {code}: {text[:300]}")
            return False
        try:
            parsed = json.loads(text) if text else {}
        except json.JSONDecodeError:
            rr.log(f"Callback error    non-JSON response: {text[:300]}")
            return False
        if not isinstance(parsed, dict) or not parsed.get("ok"):
            err = parsed.get("error") if isinstance(parsed, dict) else text
            rr.log(f"Callback error    {err}")
            return False
        rr.log(f"Callback          OK  HTTP {code}  status={body['status']}")
        return True
    except Exception as exc:
        rr.log(f"Callback error    {exc}")
        return False


def run_job(job_path: Path) -> dict[str, Any]:
    job = load_job(job_path)
    job_id = str(job["job_id"]).strip()
    job_dir = ROOT / "jobs" / job_id
    result_path = job_dir / "research_result.json"
    status_path = job_dir / "status.json"
    job_copy_path = job_dir / "job.json"
    rel_result = relative_result_path(job_id)

    write_json(job_copy_path, job)
    started_at = rr.now_iso()
    write_json(
        status_path,
        status_payload(
            job,
            status="RUNNING",
            started_at=started_at,
            result_path=None,
        ),
    )
    rr.log(f"Research Job     {job_id}")
    rr.log(f"Status           RUNNING")
    rr.log(f"Job dir          {job_dir}")

    try:
        args = argparse.Namespace(
            game=job["game"],
            topic=job["topic"],
            existing_page=job["existing_page"],
            steam_appid=None,
            out=str(result_path),
            reuse=None,
        )
        result = rr.run(args)
        write_json(result_path, result)
        finished = status_payload(
            job,
            status="REVIEW",
            started_at=started_at,
            finished_at=rr.now_iso(),
            recommendation=result.get("recommendation"),
            evidence_count=len(result.get("evidence") or []),
            result_path=rel_result,
        )
        write_json(status_path, finished)
        rr.log("Status           REVIEW  (waiting for human review)")
        rr.log(f"Wrote            {result_path}")
        post_research_callback(finished, result=result)
        return finished
    except Exception as exc:
        err = str(exc).strip() or exc.__class__.__name__
        rr.log(f"Research Job failed: {err}")
        traceback.print_exc()
        failed = status_payload(
            job,
            status="FAILED",
            started_at=started_at,
            finished_at=rr.now_iso(),
            result_path=rel_result if result_path.exists() else None,
            error=err[:300],
        )
        write_json(status_path, failed)
        post_research_callback(failed)
        return failed


def main() -> int:
    parser = argparse.ArgumentParser(description="M1 Research Job runner")
    parser.add_argument(
        "job_file",
        nargs="?",
        default=str(ROOT / "input" / "research_job.json"),
        help="Path to research_job.json",
    )
    args = parser.parse_args()
    job_path = Path(args.job_file)
    if not job_path.is_absolute():
        job_path = (Path.cwd() / job_path).resolve()
    if not job_path.exists():
        rr.log(f"Job file not found: {job_path}")
        return 2
    status = run_job(job_path)
    return 0 if status["status"] == "REVIEW" else 1


if __name__ == "__main__":
    sys.exit(main())
