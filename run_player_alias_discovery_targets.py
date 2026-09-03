#!/usr/bin/env python3
"""Run alias discovery for explicit production validation targets."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import player_alias_discovery_job_runner as job_runner

ROOT = Path(__file__).resolve().parent

DEFAULT_TARGETS = [
    {
        "steam_app_id": "4075620",
        "game_name": "Combolands: Roguelike Citybuilder",
        "steam_url": "https://store.steampowered.com/app/4075620/",
    },
    {
        "steam_app_id": "4339280",
        "game_name": "ShipShaper: Falconeer Chronicles",
        "steam_url": "https://store.steampowered.com/app/4339280/",
    },
    {
        "steam_app_id": "2445260",
        "game_name": "Tyr",
        "steam_url": "https://store.steampowered.com/app/2445260/",
    },
]


def _load_dotenv() -> None:
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def build_jobs() -> list[dict[str, Any]]:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    cycle = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    created = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    jobs = []
    for target in DEFAULT_TARGETS:
        app_id = target["steam_app_id"]
        jobs.append(
            {
                "job_id": f"alias-discovery-{app_id}-{stamp}",
                "job_type": "PLAYER_ALIAS_DISCOVERY",
                "steam_app_id": app_id,
                "game_name": target["game_name"],
                "steam_url": target["steam_url"],
                "research_cycle_date": cycle,
                "created_at": created,
            }
        )
    return jobs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate player alias discovery on fixed targets")
    parser.add_argument("--dry-run", action="store_true", help="Discover without callback")
    parser.add_argument("--continue-on-callback-fail", action="store_true", default=True)
    args = parser.parse_args(argv)
    _load_dotenv()

    outcomes = []
    for job in build_jobs():
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            path = Path(handle.name)
            handle.write(json.dumps(job, ensure_ascii=False, indent=2))
        try:
            outcome = job_runner.run_job(path, dry_run=bool(args.dry_run), root=ROOT)
        finally:
            try:
                path.unlink()
            except OSError:
                pass
        result = outcome.get("result") or {}
        callback = outcome.get("callback") or {}
        outcomes.append(
            {
                "job_id": job["job_id"],
                "steam_app_id": job["steam_app_id"],
                "game_name": job["game_name"],
                "alias": result.get("alias") or callback.get("alias") or "",
                "status": result.get("status") or callback.get("status") or "",
                "confidence": result.get("confidence") or callback.get("confidence") or "",
                "source_count": result.get("source_count") or callback.get("source_count") or 0,
                "source_urls": (result.get("source_urls") or callback.get("source_urls") or [])[:8],
                "callback_ok": outcome.get("callback_ok"),
                "callback_error": outcome.get("callback_error"),
                "ranked": (result.get("ranked") or [])[:3],
                "source_diags": result.get("source_diags") or [],
            }
        )
        if (
            not args.dry_run
            and not outcome.get("callback_ok")
            and not args.continue_on_callback_fail
        ):
            break

    summary = {"ok": all(row.get("callback_ok") for row in outcomes) if outcomes else True, "results": outcomes}
    out_path = ROOT / "output" / "player_alias_discovery_targets.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
