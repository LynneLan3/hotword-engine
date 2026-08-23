#!/usr/bin/env python3
"""Local RESEARCH_RECOMMENDATION artifact runner.

This runner integrates existing Search Demand and optional Social artifacts
with the pure M4 recommendation function. It does not scan jobs/, call a
provider, write callbacks, or emit Candidate Decisions.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import research_recommendation as recommendation

ROOT = Path(__file__).resolve().parent
JOB_TYPE = "RESEARCH_RECOMMENDATION"
EXEC_COMPLETED = "COMPLETED"
EXEC_FAILED = "FAILED"
EXEC_RUNNING = "RUNNING"

REQUIRED_FIELDS = (
    "job_id",
    "job_type",
    "site_key",
    "game_name",
    "search_result_path",
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _require_job_string(job: dict[str, Any], key: str) -> str:
    value = job.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"missing required field: {key}")
    return value.strip()


def _resolve_repo_path(raw_path: str, root: Path) -> Path:
    candidate = Path(raw_path)
    resolved = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(f"path outside repository: {raw_path}") from exc
    return resolved


def _load_json_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON object required: {path}")
    return payload


def validate_job(job: dict[str, Any], *, root: Path | None = None) -> dict[str, Any]:
    """Validate job shape and the required, explicit Search Demand source."""
    if not isinstance(job, dict):
        raise ValueError("recommendation job must be a JSON object")
    for key in REQUIRED_FIELDS:
        _require_job_string(job, key)
    if _require_job_string(job, "job_type") != JOB_TYPE:
        raise ValueError("job_type must be RESEARCH_RECOMMENDATION")

    base = (root or ROOT).resolve()
    search_path = _resolve_repo_path(_require_job_string(job, "search_result_path"), base)
    if not search_path.exists() or not search_path.is_file():
        raise ValueError(f"search result not found: {job['search_result_path']}")
    _load_json_object(search_path)
    return job


def _normalized_identity(value: Any) -> str:
    text = str(value or "").strip().lower()
    dotted = re.sub(r"(?<![a-z0-9])(?:[a-z]\.){2,}[a-z](?:\.)?(?![a-z0-9])", lambda m: re.sub(r"[^a-z0-9]", "", m.group(0)), text)
    return re.sub(r"[^a-z0-9]+", " ", dotted).strip()


def _artifact_game(result: dict[str, Any]) -> str:
    direct = result.get("game") or result.get("game_name")
    if direct:
        return str(direct)
    nested = result.get("input")
    if isinstance(nested, dict):
        return str(nested.get("game") or nested.get("game_name") or "")
    return ""


def _assert_game_consistency(job: dict[str, Any], *artifacts: dict[str, Any] | None) -> None:
    expected = _normalized_identity(job.get("game_name"))
    if not expected:
        return
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            continue
        actual = _normalized_identity(_artifact_game(artifact))
        if actual and actual != expected:
            raise ValueError("source artifact game does not match recommendation job")


def _optional_social_path(job: dict[str, Any], root: Path) -> tuple[dict[str, Any] | None, str | None]:
    raw = job.get("social_result_path")
    if not isinstance(raw, str) or not raw.strip():
        return None, None
    raw = raw.strip()
    try:
        path = _resolve_repo_path(raw, root)
    except ValueError:
        return None, raw
    if not path.exists() or not path.is_file():
        return None, raw
    try:
        return _load_json_object(path), raw
    except (OSError, ValueError, json.JSONDecodeError):
        return None, raw


def _failed_status(job_id: str, error: str) -> dict[str, Any]:
    return {
        "job_id": job_id,
        "job_type": JOB_TYPE,
        "status": EXEC_FAILED,
        "error": error[:300],
        "finished_at": now_iso(),
    }


def run_recommendation_job(job: dict[str, Any], *, root: Path | None = None) -> dict[str, Any]:
    """Run one explicitly configured recommendation job into local artifacts."""
    base = (root or ROOT).resolve()
    job_id = str(job.get("job_id") or "").strip() or "unknown-recommendation-job"
    job_dir = base / "jobs" / job_id
    result_rel = f"jobs/{job_id}/research_recommendation.json"
    result_path = job_dir / "research_recommendation.json"
    status_path = job_dir / "status.json"

    write_json(job_dir / "job.json", job)
    write_json(
        status_path,
        {
            "job_id": job_id,
            "job_type": JOB_TYPE,
            "status": EXEC_RUNNING,
            "started_at": now_iso(),
        },
    )
    try:
        validate_job(job, root=base)
        search_path = _resolve_repo_path(str(job["search_result_path"]), base)
        search_result = _load_json_object(search_path)
        social_result, social_path = _optional_social_path(job, base)
        _assert_game_consistency(job, search_result, social_result)

        built = recommendation.build_research_recommendation(search_result, social_result)
        output = {
            "job_id": job_id,
            "job_type": JOB_TYPE,
            "site_key": job["site_key"],
            "game_name": job["game_name"],
            "source_artifacts": {
                "search_result_path": job["search_result_path"],
                "social_result_path": social_path,
                "search_job_id": search_result.get("job_id"),
                "social_job_id": social_result.get("job_id") if social_result else None,
            },
            **built,
            "generated_at": now_iso(),
        }
        write_json(result_path, output)
        write_json(
            status_path,
            {
                "job_id": job_id,
                "job_type": JOB_TYPE,
                "status": EXEC_COMPLETED,
                "recommendation": output["recommendation"],
                "confidence": output["confidence"],
                "result_path": result_rel,
                "finished_at": now_iso(),
            },
        )
        return {"ok": True, "status": EXEC_COMPLETED, "job_id": job_id, "result": output}
    except Exception as exc:
        write_json(status_path, _failed_status(job_id, str(exc)))
        return {"ok": False, "status": EXEC_FAILED, "job_id": job_id, "error": str(exc)}


def run_job(job_path: Path, *, root: Path | None = None) -> dict[str, Any]:
    base = (root or ROOT).resolve()
    try:
        job = _load_json_object(job_path)
    except Exception as exc:
        return {"ok": False, "status": EXEC_FAILED, "job_id": "", "error": str(exc)}
    return run_recommendation_job(job, root=base)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a local RESEARCH_RECOMMENDATION job")
    parser.add_argument(
        "job_file",
        nargs="?",
        default=str(ROOT / "input" / "research_recommendation_job.json"),
    )
    args = parser.parse_args(argv)
    job_path = Path(args.job_file)
    if not job_path.is_absolute():
        job_path = (ROOT / job_path).resolve()
    if not job_path.exists():
        return 2
    outcome = run_job(job_path, root=ROOT)
    return 0 if outcome.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
