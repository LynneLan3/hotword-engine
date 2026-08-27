"""Run the bounded G002A Steam deep qualification shadow."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path

from opportunity_discovery.games_popularity import GamesPopularityClient, provider_counts
from opportunity_discovery.steam_qualification import qualify_steam_deep_candidates
from opportunity_discovery.sources.steam import SteamShadowAdapter
from opportunity_discovery.shadow import write_shadow_artifact


DEFAULT_G002_ARTIFACT = Path("/tmp/g002-steam-shadow-20260826T082512Z.json")


def _load_reference(path: Path) -> dict[str, object]:
    if not path.exists():
        return {"status": "MISSING", "path": str(path)}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return {"status": "INVALID", "path": str(path)}
    if not isinstance(payload, dict):
        return {"status": "INVALID", "path": str(path)}
    return {
        "status": "AVAILABLE_SUMMARY_ONLY",
        "path": str(path),
        "run_id": payload.get("run_id"),
        "observed_at": payload.get("observed_at"),
        "incremental_unique_games": payload.get("deep", {}).get("incremental_unique_games")
        if isinstance(payload.get("deep"), dict)
        else None,
        "candidate_sample_count": len(payload.get("incremental_candidate_sample", []))
        if isinstance(payload.get("incremental_candidate_sample"), list)
        else 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Bounded Steam pages 1-5 deep qualification shadow")
    parser.add_argument("--page-start", type=int, default=1)
    parser.add_argument("--page-end", type=int, default=5)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--g002-artifact", type=Path, default=DEFAULT_G002_ARTIFACT)
    parser.add_argument("--observed-at")
    parser.add_argument("--timeout-seconds", type=float, default=20.0)
    parser.add_argument("--max-attempts", type=int, default=2)
    parser.add_argument("--max-workers", type=int, default=4)
    parser.add_argument("--request-interval-seconds", type=float, default=0.25)
    args = parser.parse_args()

    reference = _load_reference(args.g002_artifact)
    observed_at = args.observed_at or reference.get("observed_at") or datetime.now(timezone.utc).isoformat()
    run_id = f"steam-deep-qualification-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"

    adapter = SteamShadowAdapter(
        args.page_start,
        args.page_end,
        observed_at=str(observed_at),
        timeout_seconds=args.timeout_seconds,
        max_attempts=args.max_attempts,
        request_interval_seconds=1.8,
    )
    observations = adapter.collect()
    # The qualification module applies the cheap release/review gate first;
    # this preflight keeps page-1 and cheap-rejected deep candidates out of the
    # provider request set.
    from opportunity_discovery.steam_qualification import _candidate_map, _cheap_classification

    by_app, incremental_ids, _ = _candidate_map(adapter, observations)
    enrichment_ids = {
        app_id
        for app_id in incremental_ids
        if app_id in by_app and not _cheap_classification(by_app[app_id])[0]
    }
    api_key = os.environ.get("GAMES_POPULARITY_API_KEY", "").strip()
    provider_error = None
    if api_key:
        client = GamesPopularityClient(
            api_key,
            timeout_seconds=args.timeout_seconds,
            max_attempts=args.max_attempts,
            max_workers=args.max_workers,
            min_interval_seconds=args.request_interval_seconds,
        )
        popularity = client.fetch_many(enrichment_ids)
    else:
        popularity = {}
        provider_error = "GAMES_POPULARITY_API_KEY unavailable; no provider request was made"
    artifact = qualify_steam_deep_candidates(
        adapter,
        observations,
        popularity,
        provider_requested=len(enrichment_ids) if api_key else 0,
        run_id=run_id,
        provider_error=provider_error,
    )
    artifact["input_evidence"] = {
        "g002_artifact": reference,
        "replay": "bounded Steam pages 1-5 replayed because the G002 artifact stores a summary/sample, not all observations",
    }
    artifact["run_contract"] = {
        "run_id": run_id,
        "page_1_enriched": False,
        "search_api_calls": 0,
        "serp_calls": 0,
        "social_calls": 0,
        "trends_calls": 0,
        "production_mutations": 0,
        "provider_app_ids": len(enrichment_ids),
        "provider_endpoint_requests": len(enrichment_ids) * 2 if api_key else 0,
        "provider_credential_available": bool(api_key),
        "provider_counts": provider_counts(popularity),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    write_shadow_artifact(artifact, args.output)
    q = artifact["qualification"]
    print(f"artifact={args.output}")
    print(f"http_result={artifact['http_result']}")
    print(f"incremental_total={q['incremental_total']}")
    print(f"enrichment_attempted={q['enrichment_attempted']}")
    print(f"qualified_high_priority={q['qualified_high_priority']}")
    print(f"experiment_verdict={artifact['experiment_verdict']['verdict']}")
    return 0 if artifact["experiment_verdict"]["verdict"] != "INCONCLUSIVE" else 1


if __name__ == "__main__":
    raise SystemExit(main())
