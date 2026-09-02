"""Twitch Historical Raw Ledger V1 — append-only Run / Raw / Daily Canonical layers.

Mirrors the Steam Historical Raw Ledger V1 shape without touching production
候选主表, opportunity scoring, or Steam 1A/1B rules. Persistence targets are:

1. Local append-only JSON history (jobs artifacts).
2. Optional Google Sheet sync via the Steam Apps Script ledger callback.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping


TWITCH_LEDGER_SPREADSHEET_NAME = "Twitch Historical Raw Ledger V1"
TWITCH_LEDGER_SCHEMA_VERSION = "twitch_historical_raw_ledger_v1"
TWITCH_LEDGER_JOB_TYPE = "TWITCH_HISTORICAL_RAW_LEDGER_APPEND"

RAW_OBSERVATION_HEADERS = [
    "Observation ID",
    "Observed At",
    "Run ID",
    "Run Date",
    "Source",
    "Global Rank",
    "API Page",
    "Page Rank",
    "Twitch Game ID",
    "Name",
    "IGDB ID",
    "Box Art URL",
    "Raw Status",
    "Schema Version",
]

RUN_LEDGER_HEADERS = [
    "Run ID",
    "Run Date",
    "Started At",
    "Finished At",
    "Run Type",
    "Trigger Type",
    "Final Status",
    "Discovery Completeness",
    "Pages Completed",
    "Rows Returned",
    "Unique Twitch IDs",
    "Duplicates",
    "Missing IGDB Count",
    "Requested Limit",
    "Warning Summary",
    "Schema Version",
]

DAILY_CANONICAL_HEADERS = [
    "Run Date",
    "Canonical Run ID",
    "Final Status",
    "Run Type",
    "Trigger Type",
    "Discovery Completeness",
    "Rows Returned",
    "Finished At",
    "Selection Reason",
    "Schema Version",
]

PRODUCTION_RUN_TYPES = {"SCHEDULED_DAILY", "MANUAL_PRODUCTION"}
CANONICAL_SELECTION_REASON = "SUCCESS + COMPLETE + production + latest finished"


def observation_id(run_id: str, twitch_game_id: str, global_rank: int) -> str:
    return f"twitch-raw|{run_id}|{twitch_game_id}|{global_rank}"


def run_date_from_run_id(run_id: str) -> str:
    text = str(run_id or "").strip()
    return text[:8] if len(text) >= 8 and text[:8].isdigit() else text


def map_run_statuses(run_status: str) -> tuple[str, str]:
    """Return (final_status, discovery_completeness) for Run Ledger / canonical."""

    status = str(run_status or "").strip().upper()
    if status == "COMPLETE":
        return "SUCCESS", "COMPLETE"
    if status == "PARTIAL":
        return "PARTIAL", "PARTIAL"
    if status in {"AUTH_MISSING", "AUTH_FAILED", "HTTP_ERROR"}:
        return "FAILED", status
    return "FAILED", status or "UNKNOWN"


def build_raw_observation_rows(artifact: Mapping[str, Any]) -> list[list[Any]]:
    run_id = str(artifact.get("run_id") or "").strip()
    run_date = run_date_from_run_id(run_id)
    rows: list[list[Any]] = []
    for item in artifact.get("observations") or []:
        if not isinstance(item, Mapping):
            continue
        twitch_game_id = str(item.get("twitch_game_id") or "").strip()
        global_rank = int(item.get("global_rank") or 0)
        rows.append(
            [
                observation_id(run_id, twitch_game_id, global_rank),
                item.get("observed_at") or artifact.get("observed_at"),
                run_id,
                run_date,
                item.get("source") or artifact.get("source") or "TWITCH_HELIX_TOP_GAMES",
                global_rank,
                item.get("api_page"),
                item.get("page_rank"),
                twitch_game_id,
                item.get("name"),
                item.get("igdb_id"),
                item.get("box_art_url"),
                item.get("raw_status") or "OBSERVED",
                TWITCH_LEDGER_SCHEMA_VERSION,
            ]
        )
    return rows


def build_run_ledger_row(
    artifact: Mapping[str, Any],
    *,
    run_type: str = "SCHEDULED_DAILY",
    trigger_type: str = "SCHEDULED_TRIGGER",
    started_at: str | None = None,
    finished_at: str | None = None,
) -> list[Any]:
    metadata = artifact.get("run_metadata") if isinstance(artifact.get("run_metadata"), Mapping) else {}
    run_id = str(artifact.get("run_id") or "").strip()
    final_status, discovery = map_run_statuses(str(metadata.get("run_status") or ""))
    errors = metadata.get("source_errors") or []
    warning = ""
    if isinstance(errors, list) and errors:
        warning = "; ".join(
            f"{item.get('code')}:{item.get('api_page')}"
            for item in errors
            if isinstance(item, Mapping)
        )
    return [
        run_id,
        run_date_from_run_id(run_id),
        started_at or artifact.get("observed_at"),
        finished_at or datetime.now(timezone.utc).isoformat(),
        run_type,
        trigger_type,
        final_status,
        discovery,
        metadata.get("pages_completed") or 0,
        metadata.get("rows_returned") or 0,
        metadata.get("unique_twitch_ids") or 0,
        metadata.get("duplicates") or 0,
        metadata.get("missing_igdb_count") or 0,
        metadata.get("requested_limit") or 0,
        warning or None,
        TWITCH_LEDGER_SCHEMA_VERSION,
    ]


def select_daily_canonical_runs(run_ledger_rows: list[Mapping[str, Any]]) -> list[list[Any]]:
    """Pick one production canonical run per Run Date.

    Priority: SUCCESS + COMPLETE + production, then latest Finished At.
    TEST / BACKFILL rows are excluded.
    """

    grouped: dict[str, dict[str, Any]] = {}
    for row in run_ledger_rows:
        run_type = str(row.get("Run Type") or "").strip()
        if run_type in {"TEST", "BACKFILL"}:
            continue
        date = str(row.get("Run Date") or "").strip()
        if not date:
            continue
        final_status = str(row.get("Final Status") or "").strip()
        discovery = str(row.get("Discovery Completeness") or "").strip()
        complete = final_status == "SUCCESS" and discovery == "COMPLETE" and run_type in PRODUCTION_RUN_TYPES
        finished_raw = row.get("Finished At")
        try:
            finished = datetime.fromisoformat(str(finished_raw).replace("Z", "+00:00")).timestamp()
        except Exception:
            finished = 0.0
        candidate = {"row": row, "complete": complete, "finished": finished}
        current = grouped.get(date)
        if (
            current is None
            or (candidate["complete"] and not current["complete"])
            or (candidate["complete"] == current["complete"] and candidate["finished"] > current["finished"])
        ):
            grouped[date] = candidate

    out: list[list[Any]] = []
    for date in sorted(grouped):
        row = grouped[date]["row"]
        reason = (
            CANONICAL_SELECTION_REASON
            if grouped[date]["complete"]
            else "best available production run"
        )
        out.append(
            [
                date,
                row.get("Run ID"),
                row.get("Final Status"),
                row.get("Run Type"),
                row.get("Trigger Type"),
                row.get("Discovery Completeness"),
                row.get("Rows Returned"),
                row.get("Finished At"),
                reason,
                "twitch_daily_canonical_v1",
            ]
        )
    return out


def build_ledger_payload(
    artifact: Mapping[str, Any],
    *,
    run_type: str = "SCHEDULED_DAILY",
    trigger_type: str = "SCHEDULED_TRIGGER",
    started_at: str | None = None,
    finished_at: str | None = None,
) -> dict[str, Any]:
    run_row = build_run_ledger_row(
        artifact,
        run_type=run_type,
        trigger_type=trigger_type,
        started_at=started_at,
        finished_at=finished_at,
    )
    run_mapping = dict(zip(RUN_LEDGER_HEADERS, run_row))
    return {
        "job_type": TWITCH_LEDGER_JOB_TYPE,
        "spreadsheet_name": TWITCH_LEDGER_SPREADSHEET_NAME,
        "schema_version": TWITCH_LEDGER_SCHEMA_VERSION,
        "run_id": run_mapping["Run ID"],
        "run_date": run_mapping["Run Date"],
        "run_ledger_row": run_row,
        "raw_observation_rows": build_raw_observation_rows(artifact),
        "headers": {
            "run_ledger": RUN_LEDGER_HEADERS,
            "raw_observations": RAW_OBSERVATION_HEADERS,
            "daily_canonical": DAILY_CANONICAL_HEADERS,
        },
    }
