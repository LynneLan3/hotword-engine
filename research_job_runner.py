#!/usr/bin/env python3
"""Minimal Research Job wrapper around research_runner.py.

Takes one GSC content-opportunity job, runs the existing Research Runner,
leaves artifacts, and POSTs REVIEW / WATCH / FAILED back to the
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
import re
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
ACTION_RESEARCH_TYPES = {
    "NEW_INTENT_RESEARCH",
    "PAGE_OPTIMIZATION_RESEARCH",
    "CANNIBALIZATION_RESEARCH",
}


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


def resolve_finished_status(
    *,
    evidence_count: int,
    recommendation: Any,
) -> str:
    """Map a successful research result to REVIEW or WATCH.

    WATCH + 0 evidence → no human-review queue.
    Anything else successful → REVIEW (human review with evidence).
    """
    action = recommendation_action(recommendation).upper()
    if int(evidence_count or 0) == 0 and action == "WATCH":
        return "WATCH"
    return "REVIEW"


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
        "research_type": str(job.get("research_type") or "CONTENT_RESEARCH").strip().upper(),
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


def _as_list(value: Any) -> list[Any]:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return value
    return [value]


def _text_list(value: Any) -> list[str]:
    out: list[str] = []
    for item in _as_list(value):
        if isinstance(item, dict):
            item = item.get("query") or item.get("question") or item.get("title") or item.get("text")
        text = str(item or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def _context_queries(context: dict[str, Any], research_type: str) -> list[str]:
    queries = _text_list(context.get("clusterQueries"))
    for cluster in _as_list(context.get("clusters")):
        if isinstance(cluster, dict):
            for query in _text_list(cluster.get("queries")):
                if query not in queries:
                    queries.append(query)
    return queries


def build_content_decision(job: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """Turn the existing runner result into the M1 structured decision contract."""
    research_type = str(job.get("research_type") or "CONTENT_RESEARCH").strip().upper()
    context = job.get("action_context") if isinstance(job.get("action_context"), dict) else {}
    recommendation = result.get("recommendation") if isinstance(result.get("recommendation"), dict) else {}
    recommendation_action = str(recommendation.get("action") or "").strip().upper()
    evidence = list(result.get("evidence") or []) if isinstance(result.get("evidence"), list) else []
    evidence_count = len(evidence)
    topic_relevant_count = int(
        result.get("topic_relevant_evidence_count") or evidence_count
    )
    if research_type == "NEW_INTENT_RESEARCH":
        if recommendation_action == "NEW_CONTENT":
            primary = "CREATE_NEW_PAGE"
        elif recommendation_action == "EXPAND_EXISTING":
            primary = "EXPAND_EXISTING"
        else:
            primary = "WATCH"
    elif research_type == "PAGE_OPTIMIZATION_RESEARCH":
        primary = "WATCH" if recommendation_action == "WATCH" or evidence_count < 3 else "EXPAND_EXISTING"
    elif research_type == "CANNIBALIZATION_RESEARCH":
        primary = "WATCH" if evidence_count < 3 else "KEEP_BOTH"
    else:
        primary = "WATCH"

    secondary: list[str] = []
    gaps = result.get("content_gaps") if isinstance(result.get("content_gaps"), list) else []
    if research_type == "PAGE_OPTIMIZATION_RESEARCH" and gaps and primary != "WATCH":
        secondary.append("ADD_FAQ")
        if any(re.search(r"\b(how|where|find|use|summon|location|step)", str(gap.get("player_question") or "").lower()) for gap in gaps if isinstance(gap, dict)):
            secondary.append("ADD_STEPS")

    target_queries = _context_queries(context, research_type)
    if not target_queries:
        target_queries = _text_list(result.get("most_asked_questions"))
    sections = []
    for gap in gaps:
        if not isinstance(gap, dict):
            continue
        section = str(gap.get("discovered_topic") or gap.get("player_question") or "").strip()
        if section and section not in sections:
            sections.append(section)
    reason = str(recommendation.get("reason") or "").strip()
    if research_type == "CANNIBALIZATION_RESEARCH":
        pages = _as_list(context.get("competingPages"))
        reason = (reason + " " if reason else "") + f"Compared {len(pages)} competing page signals; content overlap requires page-level review.".strip()
    if research_type == "PAGE_OPTIMIZATION_RESEARCH":
        reason = (reason + " " if reason else "") + "Page metrics and all visible Query Cluster context were supplied to the research runner."
    if research_type == "PAGE_OPTIMIZATION_RESEARCH" and topic_relevant_count < 3:
        confidence = "LOW"
    else:
        confidence = "HIGH" if evidence_count >= 5 and primary != "WATCH" else "MEDIUM" if evidence_count >= 2 else "LOW"
    return {
        "research_type": research_type,
        "source_action": str(job.get("source_action") or context.get("sourceAction") or "").strip(),
        "primary_decision": primary,
        "secondary_actions": secondary,
        "decision_reason": reason,
        "evidence_summary": str(result.get("review_summary") or reason).strip(),
        "evidence_count": evidence_count,
        "target_queries": target_queries,
        "recommended_sections": sections,
        "recommended_title_change": "" if primary == "WATCH" else "Align title/snippet with the highest-impression target queries.",
        "recommended_internal_links": [],
        "confidence": confidence,
    }


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
    job: dict[str, Any] | None = None,
) -> dict[str, Any]:
    job_id = str(status["job_id"]).strip()
    status_enum = str(status.get("status") or "").strip().upper()
    research_type = str(status.get("research_type") or "").strip().upper()
    if status_enum == "FAILED":
        body = {
            "job_id": job_id,
            "status": "FAILED",
            "error": str(status.get("error") or "research_job_failed")[:300],
        }
        if research_type and research_type != "CONTENT_RESEARCH":
            body["research_type"] = research_type
        return body
    if status_enum == "WATCH":
        # No evidence payload — Apps Script must not write「研究审核」.
        body = {
            "job_id": job_id,
            "status": "WATCH",
            "recommendation": recommendation_action(status.get("recommendation"))
            or "WATCH",
            "evidence_count": int(status.get("evidence_count") or 0),
            "result_path": str(
                status.get("result_path") or relative_result_path(job_id)
            ),
            "review_summary": review_summary_from_result(result),
            "content_decision": build_content_decision(job, result or {}) if job else None,
        }
        if research_type and research_type != "CONTENT_RESEARCH":
            body["research_type"] = research_type
        return body
    body: dict[str, Any] = {
        "job_id": job_id,
        "status": "REVIEW",
        "recommendation": recommendation_action(status.get("recommendation")),
        "evidence_count": int(status.get("evidence_count") or 0),
        "result_path": str(status.get("result_path") or relative_result_path(job_id)),
        "review_summary": review_summary_from_result(result),
        "evidence": evidence_from_result(result),
    }
    if research_type and research_type != "CONTENT_RESEARCH":
        body["research_type"] = research_type
    if job:
        body["content_decision"] = build_content_decision(job, result or {})
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
    job: dict[str, Any] | None = None,
) -> bool:
    """POST REVIEW/WATCH/FAILED to Google Sheet callback. Never mutates local artifacts."""
    body = build_callback_body(status, result=result, job=job)
    return post_callback_body(body)


def post_callback_body(body: dict[str, Any]) -> bool:
    """POST JSON body to Apps Script callback endpoint.

    Reuses the exact HTTP behavior from post_research_callback:
    - Uses RESEARCH_CALLBACK_URL / RESEARCH_CALLBACK_TOKEN
    - Handles Apps Script 302 redirect Location with GET
    - Expects response JSON with { ok: true }
    """
    url = str(os.environ.get("RESEARCH_CALLBACK_URL") or "").strip()
    token = str(os.environ.get("RESEARCH_CALLBACK_TOKEN") or "").strip()
    if not url:
        rr.log("Callback error    RESEARCH_CALLBACK_URL is not set")
        return False
    if not token:
        rr.log("Callback error    RESEARCH_CALLBACK_TOKEN is not set")
        return False

    body = dict(body or {})
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
        rr.log(
            f"Callback          OK  HTTP {code}  status={str(body.get('status') or body.get('execution_status') or '')}"
        )
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
        research_type = str(job.get("research_type") or "").strip().upper()
        action_context = job.get("action_context") if isinstance(job.get("action_context"), dict) else {}
        context_queries = _context_queries(action_context, research_type)
        args = argparse.Namespace(
            game=job["game"],
            topic=job["topic"],
            existing_page=job["existing_page"],
            steam_appid=None,
            source_query=str(job.get("source_query") or "").strip(),
            related_queries=list(job.get("related_queries") or []),
            context_queries=context_queries,
            research_type=research_type,
            out=str(result_path),
            reuse=None,
        )
        result = rr.run(args)
        if str(job.get("research_type") or "").strip().upper() in ACTION_RESEARCH_TYPES:
            result["content_decision"] = build_content_decision(job, result)
        write_json(result_path, result)
        evidence_count = len(result.get("evidence") or [])
        finished_status = resolve_finished_status(
            evidence_count=evidence_count,
            recommendation=result.get("recommendation"),
        )
        finished = status_payload(
            job,
            status=finished_status,
            started_at=started_at,
            finished_at=rr.now_iso(),
            recommendation=result.get("recommendation"),
            evidence_count=evidence_count,
            result_path=rel_result,
        )
        write_json(status_path, finished)
        if finished_status == "WATCH":
            rr.log("Status           WATCH  (insufficient evidence — no human review)")
        else:
            rr.log("Status           REVIEW  (waiting for human review)")
        rr.log(f"Wrote            {result_path}")
        callback_ok = post_research_callback(finished, result=result, job=job)
        finished_out = dict(finished)
        finished_out["callback_ok"] = callback_ok
        return finished_out
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
        callback_ok = post_research_callback(failed)
        failed_out = dict(failed)
        failed_out["callback_ok"] = callback_ok
        return failed_out


def exit_code_from_run(status: dict[str, Any]) -> int:
    """REVIEW/WATCH succeed only when Sheet callback succeeds.

    FAILED is always non-zero. Local artifacts are not deleted on callback failure.
    """
    if not isinstance(status, dict):
        return 1
    if status.get("callback_ok") is False:
        return 1
    st = str(status.get("status") or "").strip().upper()
    return 0 if st in {"REVIEW", "WATCH"} else 1


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
    return exit_code_from_run(status)


if __name__ == "__main__":
    sys.exit(main())
